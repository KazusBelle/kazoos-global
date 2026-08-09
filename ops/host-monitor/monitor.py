#!/usr/bin/env python3
"""Host resource + container + LIQ watchdog (v2) for kazus-global.

Runs on the HOST (systemd timer), independent of Docker, so it keeps alerting
even when the stack is degraded. One invocation = one check cycle (the systemd
timer re-invokes every 60s). Born from the 2026-06-23/24 freezes: research
aggregations pinned the DB and the box hard-froze with no warning.

Design:
- stdlib only (urllib + /proc + subprocess to docker/psql). No pip deps.
- Cheap: targeted `docker stats` for the db container only; `docker inspect`
  for the four key containers; ONE bounded psql query over system views only
  (never app tables / liquidity_samples). All subprocess calls are timeout-
  bounded. The monitor must never become a load source.
- Stateful: ~/.kazus-monitor-state.json holds sustained-breach timers, per-key
  cooldowns, recovery timers, consecutive-HTTP-failure streaks, restart counts,
  and the previous /proc/stat sample (for CPU/iowait deltas).
- Alerts to the existing Telegram bot/chat (creds from the project .env).

Flags: --dry-run | --once | --force-warn | --force-critical (default = --once).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ── Configuration ────────────────────────────────────────────────────────────
PROJECT_DIR = os.environ.get("KAZUS_PROJECT_DIR", "/home/deploy/workspace/kazus-global")
ENV_FILE = os.environ.get("KAZUS_ENV_FILE", f"{PROJECT_DIR}/.env")
STATE_FILE = os.environ.get("KAZUS_MONITOR_STATE", os.path.expanduser("~/.kazus-monitor-state.json"))
# Own log sink, independent of journald. Under the systemd timer this script's
# stdout goes to the journal only — and the journal is the FIRST thing purged
# when the disk fills, either by journald's own SystemKeepFree or by the
# operator's emergency vacuum. That is exactly the failure class this monitor
# exists to catch, so its record of the event disappears with the event: the
# 2026-07-31 → 08-08 collection outage left a single surviving journal line.
# Capped and rotated by hand — a monitor must never be the thing that fills
# the disk it is watching.
LOG_FILE = os.environ.get("KAZUS_MONITOR_LOG", os.path.expanduser("~/.kazus-monitor.log"))
LOG_MAX_BYTES = int(os.environ.get("KAZUS_MONITOR_LOG_MAX_BYTES", 5 * 1024 * 1024))
BACKEND_BASE = os.environ.get("KAZUS_BACKEND_BASE", "http://localhost:8000")
DB_CONTAINER = os.environ.get("KAZUS_DB_CONTAINER", "kazus-global-db-1")
CONTAINERS = {
    "frontend": "kazus-global-frontend-1",
    "backend": "kazus-global-backend-1",
    "worker": "kazus-global-worker-1",
    "db": "kazus-global-db-1",
}
HTTP_TIMEOUT = 8
SUBP_TIMEOUT = 15
NCPU = os.cpu_count() or 4
HOSTNAME = socket.gethostname()

RECOVERY_STABLE_S = 5 * 60
ALERT_RETRY_S = 10 * 60  # retry only a delivery that never succeeded

OK, WARNING, CRITICAL = "OK", "WARNING", "CRITICAL"
_RANK = {OK: 0, WARNING: 1, CRITICAL: 2}

# Thresholds (4-core / 8 GB box). Sustained windows in seconds.
TH = {
    "mem_avail_warn_pct": 12.0, "mem_avail_crit_pct": 7.0,
    # Static swap occupancy is historical and non-actionable. Memory-pressure
    # alerts require sustained PSI, or active swap I/O together with low-ish
    # MemAvailable. Any swap I/O is enough for the latter gate; the 20% here is
    # RAM available, not swap used.
    "swap_pressure_mem_avail_pct": 20.0,
    "swap_pressure_sustain_s": 180,
    "mem_psi_warn_avg10": 10.0, "mem_psi_warn_sustain_s": 180,
    "mem_psi_crit_full_avg10": 10.0, "mem_psi_crit_sustain_s": 120,
    "disk_warn_pct": 88.0, "disk_crit_pct": 93.0,
    # Absolute free-space floor (matches df "Avail", i.e. space usable by
    # non-root processes incl. the DB). Gates independently of used% so a nearly
    # full disk pages BEFORE the fs hits 0 — the % check alone under-reports
    # because it counts the ~5% root-reserved blocks as "not used".
    "disk_avail_warn_gb": 5.0, "disk_avail_crit_gb": 2.5,
    "inode_warn_pct": 80.0, "inode_crit_pct": 90.0,
    "load_warn": 6.0, "load_crit": 8.0,
    "iowait_warn_pct": 30.0, "iowait_warn_sustain_s": 180,
    "iowait_crit_pct": 50.0, "iowait_crit_sustain_s": 120,
    "cpu_crit_pct": 90.0, "cpu_crit_sustain_s": 300,
    "db_cpu_warn_pct": 300.0, "db_cpu_warn_sustain_s": 180,
    "db_cpu_crit_pct": 350.0, "db_cpu_crit_sustain_s": 300,
    "liq_sustain_crit_s": 180,           # value_path/subs degraded this long → CRIT
    "heavy_research_warn_sustain_s": 120,
    "heavy_research_crit_sustain_s": 300,
    "http_fail_streak_crit": 3,
    "ws_stale_age_s": 30,
}


# ── small helpers ────────────────────────────────────────────────────────────
def now_ts() -> float:
    return time.time()


def iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def emit(line: str) -> None:
    """Print to stdout (journald) AND append to LOG_FILE.

    Never raises: a monitor that dies because it cannot write its own log is
    worse than a monitor with no log. Rotation is a single generation — enough
    to survive a journal vacuum, bounded enough to never threaten the disk.
    """
    print(line)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) >= LOG_MAX_BYTES:
            os.replace(LOG_FILE, f"{LOG_FILE}.1")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def read_env_value(key: str) -> str | None:
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def run(cmd: list[str], timeout: int = SUBP_TIMEOUT) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"ERR:{exc.__class__.__name__}"


def http_get(path: str, token: str | None = None) -> tuple[int, str]:
    url = path if path.startswith("http") else f"{BACKEND_BASE}{path}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # noqa
        return e.code, ""
    except Exception:  # noqa: BLE001
        return 0, ""


def get_token() -> str | None:
    user = read_env_value("ADMIN_USERNAME") or "kazus"
    pw = read_env_value("ADMIN_PASSWORD") or "globalocal"
    data = json.dumps({"username": user, "password": pw}).encode()
    req = urllib.request.Request(
        f"{BACKEND_BASE}/api/auth/login-json", data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read()).get("access_token")
    except Exception:  # noqa: BLE001
        return None


# ── Finding model ────────────────────────────────────────────────────────────
class Finding:
    __slots__ = ("key", "level", "value", "threshold", "service", "msg")

    def __init__(self, key, level, value="", threshold="", service="", msg=""):
        self.key, self.level = key, level
        self.value, self.threshold = value, threshold
        self.service, self.msg = service, msg

    def line(self) -> str:
        thr = f" (thr {self.threshold})" if self.threshold else ""
        svc = f"[{self.service}] " if self.service else ""
        return f"{svc}{self.msg or self.key}: {self.value}{thr}"


# ── sustained-breach helper ──────────────────────────────────────────────────
def sustained(state: dict, key: str, breaching: bool, need_s: int, now: float) -> bool:
    """True iff `breaching` has held continuously for >= need_s. Tracks the
    first-breach timestamp in state['raw_breach']; clears it when not breaching."""
    rb = state.setdefault("raw_breach", {})
    if not breaching:
        rb.pop(key, None)
        return False
    since = rb.get(key)
    if since is None:
        rb[key] = now
        return need_s <= 0
    return (now - since) >= need_s


def _read_meminfo() -> dict[str, int]:
    mi: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                p = ln.split()
                if len(p) >= 2:
                    mi[p[0].rstrip(":")] = int(p[1])
    except OSError:
        pass
    return mi


def _read_vmstat() -> dict[str, int]:
    wanted = {"pswpin", "pswpout"}
    values: dict[str, int] = {}
    try:
        with open("/proc/vmstat") as fh:
            for ln in fh:
                p = ln.split()
                if len(p) == 2 and p[0] in wanted:
                    values[p[0]] = int(p[1])
    except OSError:
        pass
    return values


def _read_memory_psi() -> dict[str, float]:
    values: dict[str, float] = {}
    try:
        with open("/proc/pressure/memory") as fh:
            for ln in fh:
                p = ln.split()
                if p and p[0] in ("some", "full"):
                    avg10 = next((x for x in p[1:] if x.startswith("avg10=")), "")
                    if avg10:
                        values[p[0]] = float(avg10.partition("=")[2])
    except (OSError, ValueError):
        pass
    return values


def _memory_findings(state: dict, now: float, mi: dict[str, int],
                     vm: dict[str, int], psi: dict[str, float]) -> list[Finding]:
    """Classify actionable memory pressure without paging on stale swap use."""
    f: list[Finding] = []
    mem_total = mi.get("MemTotal", 0)
    mem_avail = mi.get("MemAvailable", 0)
    avail_pct = (mem_avail / mem_total * 100) if mem_total else 100.0
    if avail_pct < TH["mem_avail_crit_pct"]:
        f.append(Finding("mem", CRITICAL, f"avail {avail_pct:.1f}%", f"<{TH['mem_avail_crit_pct']}%", "host", "RAM available"))
    elif avail_pct < TH["mem_avail_warn_pct"]:
        f.append(Finding("mem", WARNING, f"avail {avail_pct:.1f}%", f"<{TH['mem_avail_warn_pct']}%", "host", "RAM available"))

    swap_total = mi.get("SwapTotal", 0)
    swap_used_pct = ((swap_total - mi.get("SwapFree", 0)) / swap_total * 100) if swap_total else 0.0
    prev_vm = state.get("vmstat") if isinstance(state.get("vmstat"), dict) else {}
    state["vmstat"] = vm
    swap_io_pages = 0
    if prev_vm and vm:
        swap_io_pages = sum(max(0, vm.get(k, 0) - int(prev_vm.get(k, 0)))
                            for k in ("pswpin", "pswpout"))

    psi_some = psi.get("some", 0.0)
    psi_full = psi.get("full", 0.0)
    state.setdefault("diagnostics", {})["memory"] = {
        "mem_available_pct": round(avail_pct, 2),
        "swap_used_pct": round(swap_used_pct, 2),
        "swap_io_pages_delta": swap_io_pages,
        "psi_some_avg10": psi_some,
        "psi_full_avg10": psi_full,
    }

    psi_critical = sustained(
        state, "memory_psi_critical",
        psi_full >= TH["mem_psi_crit_full_avg10"],
        TH["mem_psi_crit_sustain_s"], now,
    )
    psi_warning = sustained(
        state, "memory_psi_warning",
        psi_some >= TH["mem_psi_warn_avg10"],
        TH["mem_psi_warn_sustain_s"], now,
    )
    swap_pressure = sustained(
        state, "memory_swap_io",
        swap_io_pages > 0 and avail_pct < TH["swap_pressure_mem_avail_pct"],
        TH["swap_pressure_sustain_s"], now,
    )
    if psi_critical:
        f.append(Finding("memory_pressure", CRITICAL,
                         f"PSI full avg10={psi_full:.1f}%",
                         f">={TH['mem_psi_crit_full_avg10']}% {TH['mem_psi_crit_sustain_s']//60}m",
                         "host", "memory pressure"))
    elif psi_warning:
        f.append(Finding("memory_pressure", WARNING,
                         f"PSI some avg10={psi_some:.1f}%",
                         f">={TH['mem_psi_warn_avg10']}% {TH['mem_psi_warn_sustain_s']//60}m",
                         "host", "memory pressure"))
    elif swap_pressure:
        f.append(Finding("memory_pressure", WARNING,
                         f"active swap I/O; RAM avail {avail_pct:.1f}%",
                         f"RAM <{TH['swap_pressure_mem_avail_pct']}% {TH['swap_pressure_sustain_s']//60}m",
                         "host", "memory pressure"))
    return f


def check_recent_kernel_oom(state: dict, now: float) -> list[Finding]:
    """Report newly observed kernel OOM evidence; unreadable journal is silent."""
    previous = state.get("oom_scan_since")
    try:
        since = max(float(previous), now - 3600) if previous is not None else now - 120
    except (TypeError, ValueError):
        since = now - 120
    state["oom_scan_since"] = now
    rc, out = run(["journalctl", "-k", f"--since=@{int(since)}", "--no-pager", "-o", "cat"])
    if rc != 0:
        return []
    low = out.lower()
    if any(marker in low for marker in ("oom-kill:", "out of memory: killed process", "oom_reaper:")):
        return [Finding("host_oom", CRITICAL, "kernel OOM event detected", "", "host", "memory")]
    return []


# ── checks ───────────────────────────────────────────────────────────────────
def check_host(state: dict, now: float) -> list[Finding]:
    f: list[Finding] = []
    f += _memory_findings(state, now, _read_meminfo(), _read_vmstat(), _read_memory_psi())

    du = shutil.disk_usage("/")
    disk_pct = du.used / du.total * 100
    avail_gb = du.free / 1e9  # matches df "Avail" (non-root usable space)
    if disk_pct > TH["disk_crit_pct"] or avail_gb < TH["disk_avail_crit_gb"]:
        f.append(Finding("disk", CRITICAL, f"{disk_pct:.1f}% ({avail_gb:.1f}G free)",
                         f">{TH['disk_crit_pct']}% or <{TH['disk_avail_crit_gb']}G", "host", "disk /"))
    elif disk_pct > TH["disk_warn_pct"] or avail_gb < TH["disk_avail_warn_gb"]:
        f.append(Finding("disk", WARNING, f"{disk_pct:.1f}% ({avail_gb:.1f}G free)",
                         f">{TH['disk_warn_pct']}% or <{TH['disk_avail_warn_gb']}G", "host", "disk /"))

    try:
        st = os.statvfs("/")
        inode_pct = (st.f_files - st.f_ffree) / st.f_files * 100 if st.f_files else 0.0
        if inode_pct > TH["inode_crit_pct"]:
            f.append(Finding("inode", CRITICAL, f"{inode_pct:.1f}%", f">{TH['inode_crit_pct']}%", "host", "inodes /"))
        elif inode_pct > TH["inode_warn_pct"]:
            f.append(Finding("inode", WARNING, f"{inode_pct:.1f}%", f">{TH['inode_warn_pct']}%", "host", "inodes /"))
    except OSError:
        pass

    try:
        with open("/proc/loadavg") as fh:
            load1 = float(fh.read().split()[0])
        if load1 > TH["load_crit"]:
            f.append(Finding("load", CRITICAL, f"{load1:.2f}", f">{TH['load_crit']} ({NCPU}c)", "host", "load1"))
        elif load1 > TH["load_warn"]:
            f.append(Finding("load", WARNING, f"{load1:.2f}", f">{TH['load_warn']} ({NCPU}c)", "host", "load1"))
    except OSError:
        pass

    # CPU/iowait via /proc/stat delta vs previous cycle (stored in state).
    cur = _cpu_stat()
    prev = state.get("cpu_stat")
    state["cpu_stat"] = cur
    if prev and cur and cur["total"] > prev["total"]:
        dt = cur["total"] - prev["total"]
        cpu_pct = (cur["nonidle"] - prev["nonidle"]) / dt * 100
        iowait_pct = (cur["iowait"] - prev["iowait"]) / dt * 100
        if sustained(state, "cpu", cpu_pct > TH["cpu_crit_pct"], TH["cpu_crit_sustain_s"], now):
            f.append(Finding("cpu", CRITICAL, f"{cpu_pct:.0f}%", f">{TH['cpu_crit_pct']}% {TH['cpu_crit_sustain_s']//60}m", "host", "CPU busy"))
        if sustained(state, "iowait_c", iowait_pct > TH["iowait_crit_pct"], TH["iowait_crit_sustain_s"], now):
            f.append(Finding("iowait", CRITICAL, f"{iowait_pct:.0f}%", f">{TH['iowait_crit_pct']}% {TH['iowait_crit_sustain_s']//60}m", "host", "iowait"))
        elif sustained(state, "iowait_w", iowait_pct > TH["iowait_warn_pct"], TH["iowait_warn_sustain_s"], now):
            f.append(Finding("iowait", WARNING, f"{iowait_pct:.0f}%", f">{TH['iowait_warn_pct']}% {TH['iowait_warn_sustain_s']//60}m", "host", "iowait"))
    return f


def _cpu_stat() -> dict | None:
    try:
        with open("/proc/stat") as fh:
            parts = fh.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        total = sum(vals)
        return {"total": total, "idle": idle, "nonidle": total - idle, "iowait": vals[4] if len(vals) > 4 else 0}
    except Exception:  # noqa: BLE001
        return None


def check_docker(state: dict, now: float) -> list[Finding]:
    f: list[Finding] = []
    rc, _ = run(["docker", "ps", "-q"], timeout=SUBP_TIMEOUT)
    if rc != 0:
        f.append(Finding("docker_daemon", CRITICAL, "unreachable", "", "docker", "Docker daemon"))
        return f  # nothing else is meaningful without the daemon

    prev_rc = state.setdefault("restart_counts", {})
    for svc, name in CONTAINERS.items():
        rc, out = run(["docker", "inspect", "-f",
                       "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}|{{.State.OOMKilled}}",
                       name])
        if rc != 0 or "|" not in out:
            f.append(Finding(f"container_{svc}", CRITICAL, "MISSING", "", svc, "container"))
            continue
        status, health, rcount, oom_killed = (out.split("|") + ["", "", "0", "false"])[:4]
        if status != "running":
            f.append(Finding(f"container_{svc}", CRITICAL, status, "running", svc, "container down"))
        elif health == "unhealthy":
            f.append(Finding(f"container_{svc}", CRITICAL, "unhealthy", "healthy", svc, "container"))
        if oom_killed.lower() == "true":
            f.append(Finding(f"oom_{svc}", CRITICAL, "OOMKilled=true", "", svc, "container memory"))
        try:
            rcount_i = int(rcount)
            if svc in prev_rc and rcount_i > prev_rc[svc]:
                f.append(Finding(f"restart_{svc}", CRITICAL, f"{prev_rc[svc]}→{rcount_i}", "", svc, "restart count rose"))
            prev_rc[svc] = rcount_i
        except ValueError:
            pass

    # db container CPU (targeted; cheap) with sustained thresholds.
    rc, out = run(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", DB_CONTAINER], timeout=SUBP_TIMEOUT)
    if rc == 0 and out.endswith("%"):
        try:
            db_cpu = float(out.rstrip("%"))
            if sustained(state, "db_cpu_c", db_cpu > TH["db_cpu_crit_pct"], TH["db_cpu_crit_sustain_s"], now):
                f.append(Finding("db_cpu", CRITICAL, f"{db_cpu:.0f}%", f">{TH['db_cpu_crit_pct']}% {TH['db_cpu_crit_sustain_s']//60}m", "db", "db CPU"))
            elif sustained(state, "db_cpu_w", db_cpu > TH["db_cpu_warn_pct"], TH["db_cpu_warn_sustain_s"], now):
                f.append(Finding("db_cpu", WARNING, f"{db_cpu:.0f}%", f">{TH['db_cpu_warn_pct']}% {TH['db_cpu_warn_sustain_s']//60}m", "db", "db CPU"))
        except ValueError:
            pass
    return f


def check_postgres(state: dict, now: float) -> list[Finding]:
    f: list[Finding] = []
    q = (
        "SELECT 'conns', count(*)::text FROM pg_stat_activity "
        "UNION ALL SELECT 'maxc', current_setting('max_connections') "
        "UNION ALL SELECT 'idletx', count(*)::text FROM pg_stat_activity WHERE state='idle in transaction' "
        "UNION ALL SELECT 'idletx_age', coalesce(round(max(extract(epoch from now()-state_change)))::text,'0') "
        "FROM pg_stat_activity WHERE state='idle in transaction' "
        "UNION ALL SELECT 'blocked', count(*)::text FROM pg_stat_activity WHERE wait_event_type='Lock' "
        "UNION ALL SELECT 'longest', coalesce(round(max(extract(epoch from now()-query_start)))::text,'0') "
        "FROM pg_stat_activity WHERE state='active' AND query NOT ILIKE '%pg_stat_activity%' "
        "UNION ALL SELECT 'heavy', count(*)::text FROM pg_stat_activity "
        "WHERE state='active' AND (query LIKE '%AVG(value)%' OR query LIKE '%percentile_disc%') "
        "AND query NOT LIKE '%pg_stat_activity%'"
    )
    rc, out = run(["docker", "exec", "-i", DB_CONTAINER, "psql", "-U", "kazus", "-d", "kazus", "-tA", "-F|", "-c", q])
    if rc != 0:
        return f  # db check already covers container down; avoid double-paging
    m: dict[str, str] = {}
    for ln in out.splitlines():
        if "|" in ln:
            k, _, v = ln.partition("|")
            m[k.strip()] = v.strip()
    try:
        conns, maxc = int(m.get("conns", 0)), int(m.get("maxc", 100))
        if maxc and conns > 0.8 * maxc:
            f.append(Finding("pg_conns", WARNING, f"{conns}/{maxc}", ">80%", "db", "PG connections"))
    except ValueError:
        pass
    try:
        longest = int(m.get("longest", 0))
        if longest > 900:
            f.append(Finding("pg_longest", CRITICAL, f"{longest}s", ">900s", "db", "longest query"))
        elif longest > 300:
            f.append(Finding("pg_longest", WARNING, f"{longest}s", ">300s", "db", "longest query"))
    except ValueError:
        pass
    try:
        # A single short idle-in-transaction is normal (a backend request mid-txn).
        # Only warn on the zombie-accumulation pattern from the incident: several
        # sessions, or one aged past the 90s reaper window.
        idletx, idletx_age = int(m.get("idletx", 0)), int(m.get("idletx_age", 0))
        if idletx >= 3 or idletx_age > 120:
            f.append(Finding("pg_idletx", WARNING, f"{idletx} (oldest {idletx_age}s)", ">=3 or >120s", "db", "idle-in-transaction"))
    except ValueError:
        pass
    try:
        if sustained(state, "pg_blocked", int(m.get("blocked", 0)) > 0, 180, now):
            f.append(Finding("pg_blocked", WARNING, m.get("blocked"), ">0 3m", "db", "blocked queries"))
    except ValueError:
        pass
    try:
        heavy = int(m.get("heavy", 0))
        if sustained(state, "heavy_c", heavy > 0, TH["heavy_research_crit_sustain_s"], now):
            f.append(Finding("heavy_research", CRITICAL, str(heavy), f">0 {TH['heavy_research_crit_sustain_s']//60}m", "db", "heavy research queries"))
        elif sustained(state, "heavy_w", heavy > 0, TH["heavy_research_warn_sustain_s"], now):
            f.append(Finding("heavy_research", WARNING, str(heavy), f">0 {TH['heavy_research_warn_sustain_s']//60}m", "db", "heavy research queries"))
    except ValueError:
        pass
    return f


def check_http_and_liq(state: dict, now: float) -> list[Finding]:
    f: list[Finding] = []
    streaks = state.setdefault("http_fail_streak", {})

    def probe(key: str, path: str, token=None) -> tuple[bool, str]:
        code, body = http_get(path, token)
        ok = 200 <= code < 300
        streaks[key] = 0 if ok else streaks.get(key, 0) + 1
        if not ok and streaks[key] >= TH["http_fail_streak_crit"]:
            f.append(Finding(f"http_{key}", CRITICAL, f"{streaks[key]}x fail (code {code})", "3x", "api", f"{path}"))
        return ok, body

    hz_ok, _ = probe("healthz", "/healthz")
    token = get_token()
    rs_ok, rs_body = probe("runtime_state", "/api/liquidity/runtime-state", token)
    ws_ok, ws_body = probe("ws_status", "/api/liquidity/ws/status", token)
    probe("top", "/api/liquidity/top?limit=100", token)

    # LIQ semantics from runtime-state (when available).
    if rs_ok and rs_body:
        try:
            d = json.loads(rs_body)
            vp = bool(d.get("value_path", {}).get("ok"))
            subs = d.get("subscribed_count")
            baseline = len(d.get("baseline") or [])
            derived = d.get("derived_status")
            fb = d.get("failure_boundary", "")
            if not vp:
                lvl = CRITICAL if sustained(state, "vp", True, TH["liq_sustain_crit_s"], now) else WARNING
                f.append(Finding("value_path", lvl, "ok=false", "", "liq", "value_path"))
            else:
                sustained(state, "vp", False, TH["liq_sustain_crit_s"], now)
            if subs is not None and baseline and subs < baseline:
                lvl = CRITICAL if sustained(state, "subs", True, TH["liq_sustain_crit_s"], now) else WARNING
                f.append(Finding("subs", lvl, f"{subs}/{baseline}", f"<{baseline}", "liq", "subscribed_count"))
            else:
                sustained(state, "subs", False, TH["liq_sustain_crit_s"], now)
            if derived == "RED":
                f.append(Finding("derived", WARNING, "RED", "", "liq", "derived_status"))
            # SCHEDULER_STARVATION is chronic on this box and is already covered
            # by the db_cpu / load / value_path checks — paging on it alone would
            # be noise (friction-bar guidance). Only NOVEL failure_boundary states
            # warn here.
            if fb and fb not in ("HEALTHY", "SCHEDULER_STARVATION"):
                f.append(Finding("failure_boundary", WARNING, fb, "HEALTHY", "liq", "failure_boundary"))
        except Exception:  # noqa: BLE001
            pass

    # runtime-state unavailable + ws_status stale, sustained → critical.
    ws_stale = False
    if ws_ok and ws_body:
        try:
            ws = json.loads(ws_body)
            ua = ws.get("updated_at")
            ws_stale = (ua is None) or (now - float(ua) > TH["ws_stale_age_s"])
        except Exception:  # noqa: BLE001
            ws_stale = False
    combined = (not rs_ok) and (ws_stale or not ws_ok)
    if sustained(state, "rs_ws_dark", combined, TH["liq_sustain_crit_s"], now):
        f.append(Finding("liq_dark", CRITICAL, "runtime-state down + ws stale", "", "liq", "health gate dark"))
    return f


# ── alert orchestration ──────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    token = read_env_value("TELEGRAM_BOT_TOKEN")
    chat = read_env_value("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=HTTP_TIMEOUT) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        pass
    rc, out = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(HTTP_TIMEOUT),
                   "-d", f"chat_id={chat}", "--data-urlencode", f"text={text}", "-d", "parse_mode=HTML", url],
                  timeout=HTTP_TIMEOUT + 5)
    return out.strip() == "200"


def load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = ""
    try:
        parent = os.path.dirname(STATE_FILE) or "."
        fd, tmp = tempfile.mkstemp(prefix=".kazus-monitor-state.", dir=parent)
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, STATE_FILE)
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def evaluate(state: dict, now: float) -> list[Finding]:
    findings: list[Finding] = []
    findings += check_host(state, now)
    findings += check_recent_kernel_oom(state, now)
    findings += check_docker(state, now)
    findings += check_postgres(state, now)
    findings += check_http_and_liq(state, now)
    return findings


def orchestrate_alerts(state: dict, findings: list[Finding], now: float) -> list[str]:
    """State-transition alerts + recovery. No periodic reminder spam."""
    active = state.setdefault("active", {})   # key -> {level,last_sent,since}
    by_key = {f.key: f for f in findings if f.level != OK}
    logs: list[str] = []

    # 1) due alerts: first entry, or a material severity escalation only.
    due: list[Finding] = []
    for key, fnd in by_key.items():
        a = active.get(key)
        previous_level = (a or {}).get("level", OK)
        entered = a is None
        escalated = a is not None and _RANK[fnd.level] > _RANK.get(previous_level, 0)
        if entered:
            a = {"level": fnd.level, "since": now, "notified": False, "last_attempt": 0.0}
            active[key] = a
        elif escalated:
            a["level"] = fnd.level
            a["notified"] = False
        else:
            # A downgrade stays active but does not page. If it later escalates,
            # the rank increase is notified exactly once.
            a["level"] = fnd.level
            a["since"] = a.get("since", now)
        retry_due = (not a.get("notified", True)
                     and now - float(a.get("last_attempt", 0.0)) >= ALERT_RETRY_S)
        if entered or escalated or retry_due:
            due.append(fnd)
            a["last_attempt"] = now
        a["ok_since"] = None
    if due:
        sev = CRITICAL if any(d.level == CRITICAL for d in due) else WARNING
        emoji = "🔴" if sev == CRITICAL else "🟠"
        body = "\n".join(f"• {d.line()}" for d in sorted(due, key=lambda x: -_RANK[x.level]))
        msg = f"{emoji} <b>KAZUS {sev}</b> {HOSTNAME} {iso()}\n{body}"
        if send_telegram(msg):
            for d in due:
                active[d.key]["notified"] = True
                active[d.key]["last_sent"] = now
            logs.append(f"sent {sev}: {[d.key for d in due]}")
        else:
            logs.append(f"SEND-FAILED {sev}: {[d.key for d in due]}")

    # 2) recoveries: keys previously active, now OK for >= RECOVERY_STABLE_S
    recovered: list[str] = []
    for key in list(active.keys()):
        if key in by_key:
            continue
        a = active[key]
        if a.get("ok_since") is None:
            a["ok_since"] = now
        last_recovery_attempt = a.get("recovery_last_attempt")
        retry_due = (last_recovery_attempt is None
                     or now - float(last_recovery_attempt) >= ALERT_RETRY_S)
        if now - a["ok_since"] >= RECOVERY_STABLE_S and retry_due:
            recovered.append(key)
    if recovered:
        for key in recovered:
            active[key]["recovery_last_attempt"] = now
        msg = (f"✅ <b>KAZUS RECOVERED</b> {HOSTNAME} {iso()}\n"
               + "\n".join(f"• {k}" for k in recovered))
        if send_telegram(msg):
            logs.append(f"sent RECOVERED: {recovered}")
            for k in recovered:
                active.pop(k, None)
        else:
            logs.append(f"SEND-FAILED RECOVERED: {recovered}")
    return logs


# ── entrypoints ──────────────────────────────────────────────────────────────
def overall_level(findings: list[Finding]) -> str:
    lvl = OK
    for f in findings:
        if _RANK[f.level] > _RANK[lvl]:
            lvl = f.level
    return lvl


def cmd_dry_run() -> int:
    state = load_state()  # read-only: we do NOT save
    findings = evaluate(dict(state), now_ts())  # copy so saves/mutations are throwaway
    lvl = overall_level(findings)
    print(f"[{iso()}] DRY-RUN overall={lvl} host={HOSTNAME} ncpu={NCPU}")
    if not findings:
        print("  all checks OK")
    for f in sorted(findings, key=lambda x: -_RANK[x.level]):
        print(f"  {f.level:8} {f.line()}")
    print("(dry-run: no Telegram sent, no state written)")
    return 0


def cmd_once() -> int:
    state = load_state()
    now = now_ts()
    findings = evaluate(state, now)
    lvl = overall_level(findings)
    logs = orchestrate_alerts(state, findings, now)
    state["last_level"] = lvl
    state["last_run"] = iso()
    save_state(state)
    summary = "; ".join(f.line() for f in findings if f.level != OK) or "all OK"
    emit(f"[{iso()}] once overall={lvl} | {summary} | {logs}")
    return 0


def cmd_force(level: str) -> int:
    emoji = "🔴" if level == CRITICAL else "🟠"
    msg = (f"{emoji} <b>KAZUS {level} (TEST)</b> {HOSTNAME} {iso()}\n"
           f"• synthetic {level.lower()} — channel test, ignore.")
    ok = send_telegram(msg)
    print(f"[{iso()}] force-{level.lower()} telegram_sent={ok}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="kazus host-monitor v2")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="evaluate + print, no Telegram, no state write")
    g.add_argument("--once", action="store_true", help="one real check cycle (default)")
    g.add_argument("--force-warn", action="store_true", help="send one synthetic WARNING test")
    g.add_argument("--force-critical", action="store_true", help="send one synthetic CRITICAL test")
    args = ap.parse_args()
    if args.dry_run:
        return cmd_dry_run()
    if args.force_warn:
        return cmd_force(WARNING)
    if args.force_critical:
        return cmd_force(CRITICAL)
    return cmd_once()  # default and --once


if __name__ == "__main__":
    sys.exit(main())
