#!/usr/bin/env python3
"""Daily partition maintainer for liquidity_samples (kazus-global).

Why this exists
---------------
liquidity_samples is RANGE-partitioned by `ts` (bigint epoch-ms), one partition
per UTC day (`liquidity_samples_pYYYYMMDD`). There was no automation: the July 5
manual repartition only created dailies through p20260713, so from 2026-07-14
every new row fell into liquidity_samples_default, which ballooned to ~9 GB and
threatened the disk. This script keeps a rolling runway of EMPTY future daily
partitions so live writes always have a dated home and never fall back to
default again.

Design (matches ops/host-monitor/monitor.py conventions)
--------------------------------------------------------
- stdlib only (subprocess -> `docker exec ... psql`). No pip deps, no app venv.
- One invocation = one maintenance pass (systemd timer re-invokes daily).
- Idempotent: existing partitions are skipped, never recreated.
- SAFE by construction. It ONLY ever runs `CREATE TABLE ... PARTITION OF` for
  day ranges strictly in the FUTURE. It NEVER moves, deletes, copies, detaches,
  drops, vacuums or alters any row or existing partition (default included).
- Bounded: every DDL session sets lock_timeout + statement_timeout so it can
  never block the collection insert path for long, nor spill to a full disk.

The default-partition trap
--------------------------
Creating a partition while a DEFAULT partition exists makes Postgres scan the
default to prove no existing row belongs in the new range. If default HAS rows
in that range the CREATE fails ("would be violated by some row in the default
partition"). We therefore only target day ranges whose lower bound is at or
after the next UTC-day boundary (today+1 00:00), i.e. ranges default is no
longer accumulating into. If a CREATE still hits the default-overlap error it
is logged as SKIPPED_OCCUPIED and is NOT treated as a hard failure (draining
default is a separate, human-approved operation).

It also OWNS the other end of the partition lifecycle: --enforce-retention
DROPs dated partitions that are fully older than RETENTION_DAYS (UTC-day floored
cutoff, matching poller.prune_old), oldest first, one autocommit txn each, never
CASCADE, never touching DEFAULT or future/straddling partitions. --daily does
create + enforce-retention + check in one pass (the systemd timer entrypoint).

Exit codes
----------
- create / --enforce-retention : 0 on success (incl. idempotent no-op);
                                 non-zero on hard error.
- --check : 0 healthy; 2 if runway < MIN_RUNWAY_DAYS, expired partitions remain,
            post-boundary DEFAULT growth, or disk below floors.
- --daily : non-zero only on a create/retention execution failure (a --check
            ALERT is surfaced via --alert/Telegram, not by failing the unit).

Flags: (default = create) | --enforce-retention | --check | --daily
       modifiers: --dry-run (create/enforce/daily), --alert (check/daily)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# ── Configuration ────────────────────────────────────────────────────────────
PROJECT_DIR = os.environ.get("KAZUS_PROJECT_DIR", "/home/deploy/workspace/kazus-global")
ENV_FILE = os.environ.get("KAZUS_ENV_FILE", f"{PROJECT_DIR}/.env")
DB_CONTAINER = os.environ.get("KAZUS_DB_CONTAINER", "kazus-global-db-1")
DB_USER = os.environ.get("KAZUS_DB_USER", "kazus")
DB_NAME = os.environ.get("KAZUS_DB_NAME", "kazus")
STATE_FILE = os.environ.get(
    "KAZUS_PARTITION_STATE", os.path.expanduser("~/.kazus-partition-state.json")
)

PARENT = "liquidity_samples"
PREFIX = "liquidity_samples_p"          # + YYYYMMDD
DAY_MS = 86_400_000                     # one UTC day in epoch-milliseconds

# Keep at least this many EMPTY future daily partitions ahead of today.
RUNWAY_DAYS = int(os.environ.get("KAZUS_PARTITION_RUNWAY_DAYS", "14"))
# --check alerts if the contiguous future runway drops below this.
MIN_RUNWAY_DAYS = int(os.environ.get("KAZUS_PARTITION_MIN_RUNWAY_DAYS", "7"))
# --check alerts if default's estimated row count grows by more than this many
# rows between two consecutive checks (material growth => routing regressed).
DEFAULT_GROWTH_ALERT_ROWS = int(os.environ.get("KAZUS_DEFAULT_GROWTH_ALERT_ROWS", "50000"))

# DDL session guards.
LOCK_TIMEOUT = os.environ.get("KAZUS_PARTITION_LOCK_TIMEOUT", "15s")
STATEMENT_TIMEOUT = os.environ.get("KAZUS_PARTITION_STATEMENT_TIMEOUT", "180s")
PSQL_TIMEOUT_S = 200  # subprocess wall cap (> statement_timeout)

DEFAULT_OVERLAP_MARKER = "default partition"  # substring of PG's overlap error
DEFAULT_NAME = f"{PARENT}_default"

# ── Retention ────────────────────────────────────────────────────────────────
# Drop dated partitions fully older than this many UTC days. Mirrors
# shared/kazus_logic/liquidity/poller.py RETENTION_DAYS with the same UTC-day
# floored cutoff, so the row-level prune and this partition dropper agree.
RETENTION_DAYS = int(os.environ.get("KAZUS_PARTITION_RETENTION_DAYS", "14"))
# The ONLY names ever eligible for a retention DROP. This regex is the primary
# guard that liquidity_samples_default (and any other table) can never be
# selected — it matches only liquidity_samples_pYYYYMMDD.
DATED_RE = re.compile(r"^liquidity_samples_p\d{8}$")

# UTC instant after which live writes route to dated partitions (the manual
# repartition created p20260721+). BEFORE this, DEFAULT growth is EXPECTED and
# never paged; AFTER it, DEFAULT growth or a missing current-day partition is a
# routing regression worth alerting on.
WRITES_ROUTED_BOUNDARY_MS = int(
    os.environ.get("KAZUS_WRITES_ROUTED_BOUNDARY_MS", "1784592000000")  # 2026-07-21 00:00Z
)
# --check disk floors (GB free), aligned with the host-monitor thresholds.
DISK_WARN_GB = float(os.environ.get("KAZUS_DISK_WARN_GB", "5.0"))
DISK_CRIT_GB = float(os.environ.get("KAZUS_DISK_CRIT_GB", "2.5"))


# ── helpers ──────────────────────────────────────────────────────────────────
def iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def log(level: str, msg: str) -> None:
    print(f"[{iso()}] {level:8} {msg}", flush=True)


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


def psql(sql: str, timeout: int = PSQL_TIMEOUT_S) -> tuple[int, str]:
    """Run one SQL string via `docker exec ... psql -tA`. Returns (rc, out/err)."""
    cmd = [
        "docker", "exec", "-i", DB_CONTAINER,
        "psql", "-U", DB_USER, "-d", DB_NAME, "-v", "ON_ERROR_STOP=1",
        "-tA", "-F", "|", "-c", sql,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, "ERR:timeout"
    except Exception as exc:  # noqa: BLE001
        return 1, f"ERR:{exc.__class__.__name__}:{exc}"


def utc_midnight_ms(days_from_today: int = 0) -> int:
    """Epoch-ms of 00:00:00Z for today + `days_from_today`."""
    d = datetime.now(timezone.utc).date() + timedelta(days=days_from_today)
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def part_name_for(lo_ms: int) -> str:
    d = datetime.fromtimestamp(lo_ms / 1000, tz=timezone.utc)
    return f"{PREFIX}{d.strftime('%Y%m%d')}"


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def ms_to_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def human_bytes(b: int) -> str:
    v = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(v) < 1024:
            return f"{v:.0f}{unit}" if unit == "B" else f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}PB"


def disk_free_gb() -> float:
    return shutil.disk_usage("/").free / 1e9


def existing_partitions() -> set[str]:
    rc, out = psql(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_inherits i ON i.inhrelid=c.oid "
        "JOIN pg_class p ON p.oid=i.inhparent "
        f"WHERE p.relname='{PARENT}'"
    )
    if rc != 0:
        raise RuntimeError(f"could not list partitions: {out}")
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def default_estimate() -> tuple[int, int]:
    """(estimated_rows, total_bytes) for the default partition, from catalog
    stats only — no scan of the table itself."""
    rc, out = psql(
        "SELECT reltuples::bigint, "
        "pg_total_relation_size(oid) "
        f"FROM pg_class WHERE relname='{PARENT}_default'"
    )
    if rc != 0 or "|" not in out:
        return (-1, -1)
    a, _, b = out.partition("|")
    try:
        return (int(float(a)), int(b))
    except ValueError:
        return (-1, -1)


def load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh)
    except OSError:
        pass


# ── create mode ──────────────────────────────────────────────────────────────
def desired_days() -> list[tuple[int, int, str]]:
    """Target ranges: [today+1 .. today+RUNWAY_DAYS]. We start at today+1 so we
    never touch the day range default may still be actively writing into."""
    out = []
    for k in range(1, RUNWAY_DAYS + 1):
        lo = utc_midnight_ms(k)
        out.append((lo, lo + DAY_MS, part_name_for(lo)))
    return out


def create_partition(name: str, lo: int, hi: int, dry_run: bool) -> str:
    """Returns one of: CREATED | EXISTS | SKIPPED_OCCUPIED | ERROR."""
    if dry_run:
        log("DRYRUN", f"would CREATE {name} FROM {lo} TO {hi}")
        return "CREATED"
    sql = (
        f"SET lock_timeout='{LOCK_TIMEOUT}'; "
        f"SET statement_timeout='{STATEMENT_TIMEOUT}'; "
        f"CREATE TABLE {name} PARTITION OF {PARENT} "
        f"FOR VALUES FROM ({lo}) TO ({hi});"
    )
    rc, out = psql(sql)
    if rc == 0:
        log("CREATED", f"{name} FROM {lo} TO {hi}")
        return "CREATED"
    if DEFAULT_OVERLAP_MARKER in out.lower():
        log("SKIP", f"{name}: default holds rows in range, not creating "
                    f"(drain is a separate approved op) -- {out.splitlines()[-1] if out else ''}")
        return "SKIPPED_OCCUPIED"
    log("ERROR", f"{name}: {out}")
    return "ERROR"


def cmd_create(dry_run: bool) -> int:
    try:
        have = existing_partitions()
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 1

    created = existed = skipped = errored = 0
    for lo, hi, name in desired_days():
        if name in have:
            existed += 1
            continue
        outcome = create_partition(name, lo, hi, dry_run)
        if outcome == "CREATED":
            created += 1
        elif outcome == "SKIPPED_OCCUPIED":
            skipped += 1
        else:
            errored += 1

    # Runway = contiguous future dated partitions from tomorrow forward. Re-read
    # the catalog after a real pass so freshly created partitions are counted;
    # in dry-run nothing was created, so reflect the intended names instead.
    if dry_run:
        final = have | {n for _, _, n in desired_days()}
    else:
        try:
            final = existing_partitions()
        except RuntimeError:
            final = have
    runway = future_runway_days(final)
    log("INFO", f"summary created={created} existed={existed} "
                f"skipped_occupied={skipped} errors={errored} runway_days={runway}")
    if errored:
        return 1
    if runway < MIN_RUNWAY_DAYS:
        log("WARN", f"runway {runway}d < target min {MIN_RUNWAY_DAYS}d "
                    f"(occupied ranges blocked backfill?)")
    return 0


def future_runway_days(part_names: set[str]) -> int:
    """Count consecutive FUTURE dated partitions starting at tomorrow
    (today+1, today+2, ...).

    We start at tomorrow, not today, on purpose: 'today' is the day currently
    being written, and during the one-time transition off DEFAULT it may have no
    dated partition yet (DEFAULT still owns today's range and a partition over it
    cannot be created without a drain). Runway is about whether *upcoming* days
    have a home, so counting from tomorrow both avoids that artifact and is the
    metric we actually care about."""
    n = 0
    k = 1
    while True:
        name = part_name_for(utc_midnight_ms(k))
        if name in part_names:
            n += 1
            k += 1
        else:
            break
    return n


# ── retention mode ───────────────────────────────────────────────────────────
def retention_cutoff_ms() -> int:
    """UTC-midnight-floored cutoff. A dated partition whose UPPER bound is <=
    this is fully older than RETENTION_DAYS and safe to drop. Day-floored so it
    matches poller.prune_old exactly and only ever expires WHOLE UTC days."""
    return utc_midnight_ms(0) - RETENTION_DAYS * DAY_MS


def dated_partitions() -> list[dict]:
    """Every dated child partition with bounds/size/est-rows. DEFAULT and any
    child not named liquidity_samples_pYYYYMMDD are dropped by the name regex,
    so they can never appear in the result (and thus never be dropped)."""
    rc, out = psql(
        "SELECT c.relname, "
        "substring(pg_get_expr(c.relpartbound,c.oid) from 'FROM \\(''(\\d+)''\\)'), "
        "substring(pg_get_expr(c.relpartbound,c.oid) from 'TO \\(''(\\d+)''\\)'), "
        "pg_total_relation_size(c.oid), c.reltuples::bigint "
        "FROM pg_class c JOIN pg_inherits i ON i.inhrelid=c.oid "
        "JOIN pg_class p ON p.oid=i.inhparent "
        f"WHERE p.relname='{PARENT}'"
    )
    if rc != 0:
        raise RuntimeError(f"could not list dated partitions: {out}")
    parts: list[dict] = []
    for ln in out.splitlines():
        f = ln.split("|")
        if len(f) < 5:
            continue
        name = f[0].strip()
        if not DATED_RE.match(name) or name == DEFAULT_NAME:
            continue  # excludes DEFAULT + any unexpected/non-dated child
        try:
            parts.append({"name": name, "lo": int(f[1]), "hi": int(f[2]),
                          "bytes": int(f[3]), "rows": int(f[4])})
        except ValueError:
            continue
    return sorted(parts, key=lambda p: p["name"])  # oldest first


def expired_partitions(cutoff_ms: int) -> list[dict]:
    """Dated partitions fully older than the cutoff (upper bound <= cutoff).
    `hi <= cutoff` inherently excludes future partitions AND the current
    straddling partition (both have hi > cutoff)."""
    return [p for p in dated_partitions() if p["hi"] <= cutoff_ms]


def drop_partition(p: dict, dry_run: bool) -> str:
    """DROP one expired dated partition. Returns DROPPED | ERROR."""
    name = p["name"]
    # Defense-in-depth: never drop DEFAULT or a non-dated table, even if a caller
    # somehow passed one. The DROP itself is plain (NEVER CASCADE).
    if not DATED_RE.match(name) or name == DEFAULT_NAME:
        log("ERROR", f"refusing to drop non-dated/default table {name!r}")
        return "ERROR"
    sz = human_bytes(p["bytes"])
    if dry_run:
        log("DRYRUN", f"would DROP {name} (upper={p['hi']}={ms_to_utc(p['hi'])}) "
                      f"size={sz} est_rows={p['rows']}")
        return "DROPPED"
    rc, out = psql(
        f"SET lock_timeout='{LOCK_TIMEOUT}'; "
        f"SET statement_timeout='{STATEMENT_TIMEOUT}'; "
        f"DROP TABLE {name};"  # no CASCADE, ever
    )
    if rc != 0:
        log("ERROR", f"{name}: {out}")
        return "ERROR"
    rc2, out2 = psql(f"SELECT count(*) FROM pg_class WHERE relname='{name}'")
    if rc2 == 0 and out2.strip() not in ("0", ""):
        log("ERROR", f"{name}: DROP returned success but table still present")
        return "ERROR"
    log("DROPPED", f"{name} size={sz} est_rows={p['rows']}")
    return "DROPPED"


def cmd_enforce_retention(dry_run: bool) -> int:
    cutoff = retention_cutoff_ms()
    log("INFO", f"retention: RETENTION_DAYS={RETENTION_DAYS} "
                f"cutoff={cutoff} ({ms_to_utc(cutoff)}); dropping dated partitions "
                f"with upper bound <= cutoff, oldest first")
    try:
        exp = expired_partitions(cutoff)
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 1
    if not exp:
        log("INFO", "retention: no expired dated partitions to drop")
        return 0
    dropped = errored = 0
    for p in exp:  # oldest first
        outcome = drop_partition(p, dry_run)
        if outcome == "DROPPED":
            dropped += 1
        else:
            errored += 1
            log("ERROR", f"retention: stopping on unexpected failure at {p['name']}")
            break
    log("INFO", f"retention summary dropped={dropped} errors={errored} "
                f"candidates={len(exp)}")
    return 1 if errored else 0


# ── daily combined pass (timer entrypoint) ───────────────────────────────────
def cmd_daily(dry_run: bool, alert: bool) -> int:
    log("INFO", "daily: create future partitions -> enforce retention -> health check")
    rc_create = cmd_create(dry_run)
    rc_ret = cmd_enforce_retention(dry_run)
    rc_check = cmd_check(alert)  # 0 healthy / 2 unhealthy: alerts, doesn't fail unit
    log("INFO", f"daily summary create_rc={rc_create} retention_rc={rc_ret} "
                f"check_rc={rc_check}")
    # Fail the unit only on an execution failure (create/retention); a check
    # ALERT is surfaced via --alert/Telegram, not by failing the timer.
    return max(rc_create, rc_ret)


# ── check mode ───────────────────────────────────────────────────────────────
def cmd_check(alert: bool) -> int:
    try:
        have = existing_partitions()
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 2

    runway = future_runway_days(have)
    rows, size = default_estimate()          # catalog estimate — no table scan
    cutoff = retention_cutoff_ms()
    try:
        expired_present = [p["name"] for p in expired_partitions(cutoff)]
    except RuntimeError:
        expired_present = []
    today_part = part_name_for(utc_midnight_ms(0))
    today_exists = today_part in have
    post_boundary = utc_now_ms() >= WRITES_ROUTED_BOUNDARY_MS
    free_gb = disk_free_gb()

    state = load_state()
    prev_rows = state.get("default_rows")
    growth = (rows - prev_rows) if (prev_rows is not None and rows >= 0) else None
    state["default_rows"] = rows
    state["default_bytes"] = size
    state["last_check"] = iso()
    save_state(state)

    problems: list[str] = []   # ALERT-worthy (real regressions / failures)
    notes: list[str] = []      # INFO only (expected conditions — never paged)

    # 1) future runway
    if runway < MIN_RUNWAY_DAYS:
        problems.append(f"future runway {runway}d < {MIN_RUNWAY_DAYS}d")
    # 2) new writes entering DEFAULT: after the boundary, today's dated partition
    #    MUST exist, else live writes fall back into DEFAULT (routing regression).
    if post_boundary and not today_exists:
        problems.append(
            f"current-day partition {today_part} MISSING -> new writes entering DEFAULT")
    # 3) DEFAULT reltuples still increasing AFTER the boundary = new rows leaking
    #    into DEFAULT. Pre-boundary growth is expected and never paged.
    if post_boundary and growth is not None and growth > DEFAULT_GROWTH_ALERT_ROWS:
        problems.append(
            f"DEFAULT grew ~{growth} rows post-boundary (routing regression)")
    # 4) expired dated partitions still present => retention not being enforced.
    if expired_present:
        problems.append(
            f"{len(expired_present)} expired dated partition(s) not dropped: "
            f"{expired_present[:5]}")
    # 6) disk floors
    if free_gb < DISK_CRIT_GB:
        problems.append(f"disk free {free_gb:.1f}GB < {DISK_CRIT_GB}GB (CRITICAL)")
    elif free_gb < DISK_WARN_GB:
        problems.append(f"disk free {free_gb:.1f}GB < {DISK_WARN_GB}GB")

    # Expected legacy DEFAULT contents are INFORMATIONAL, never a page: the
    # July 14-20 rows sit in DEFAULT and age out via the row-level prune between
    # ~2026-07-29 and ~2026-08-04. Distinguish them from post-boundary leakage.
    if not post_boundary:
        notes.append("pre-boundary: DEFAULT still receiving today's rows (expected)")
    elif rows and rows > 0:
        notes.append(
            f"DEFAULT holds ~{rows} legacy rows aging out via retention "
            f"(expected until ~2026-08-04) — not a fault")

    size_h = human_bytes(size) if size >= 0 else "?"
    log("INFO", f"runway={runway}d default_rows~{rows} default_size={size_h} "
                f"growth={growth} today_part={'present' if today_exists else 'MISSING'} "
                f"expired_present={len(expired_present)} free={free_gb:.1f}GB "
                f"post_boundary={post_boundary}")
    for n in notes:
        log("INFO", n)
    if problems:
        log("ALERT", "; ".join(problems))
        if alert:
            send_telegram(f"\U0001f7e0 KAZUS partition-maintainer {iso()}\n" +
                          "\n".join(f"• {p}" for p in problems))
        return 2
    log("OK", "runway healthy, retention enforced, no post-boundary DEFAULT growth")
    return 0


def send_telegram(text: str) -> bool:
    import urllib.parse
    import urllib.request
    token = read_env_value("TELEGRAM_BOT_TOKEN")
    chat = read_env_value("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=8) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


# ── entrypoint ───────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="kazus liquidity_samples partition maintainer")
    # Modes (mutually exclusive). Default (no mode) = create future partitions,
    # preserving the original behaviour the installed unit relied on.
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--enforce-retention", action="store_true",
                      help="DROP fully-expired dated partitions (oldest first)")
    mode.add_argument("--check", action="store_true",
                      help="report health; exit 2 if unhealthy")
    mode.add_argument("--daily", action="store_true",
                      help="create + enforce-retention + check (timer entrypoint)")
    # Modifiers.
    ap.add_argument("--dry-run", action="store_true",
                    help="print actions; touch nothing (create/enforce/daily)")
    ap.add_argument("--alert", action="store_true",
                    help="with --check/--daily: send a Telegram alert on problems")
    args = ap.parse_args()
    if args.check:
        return cmd_check(args.alert)
    if args.enforce_retention:
        return cmd_enforce_retention(args.dry_run)
    if args.daily:
        return cmd_daily(args.dry_run, args.alert)
    return cmd_create(args.dry_run)  # default = create future partitions


if __name__ == "__main__":
    sys.exit(main())
