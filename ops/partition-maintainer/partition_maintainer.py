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

Exit codes
----------
- create mode : 0 on success (incl. idempotent no-op); non-zero on hard error.
- --check mode : 0 healthy; 2 if runway < MIN_RUNWAY_DAYS or default looks like
                 it is still receiving live writes (routing broken).

Flags: (default = create) | --dry-run | --check [--alert]
"""

from __future__ import annotations

import argparse
import json
import os
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


# ── check mode ───────────────────────────────────────────────────────────────
def cmd_check(alert: bool) -> int:
    try:
        have = existing_partitions()
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 2

    runway = future_runway_days(have)
    rows, size = default_estimate()

    state = load_state()
    prev_rows = state.get("default_rows")
    growth = (rows - prev_rows) if (prev_rows is not None and rows >= 0) else None
    state["default_rows"] = rows
    state["default_bytes"] = size
    state["last_check"] = iso()
    save_state(state)

    problems: list[str] = []
    if runway < MIN_RUNWAY_DAYS:
        problems.append(f"partition runway {runway}d < {MIN_RUNWAY_DAYS}d")
    if growth is not None and growth > DEFAULT_GROWTH_ALERT_ROWS:
        problems.append(
            f"default grew ~{growth} rows since last check "
            f"(routing may be broken; expected frozen)"
        )

    size_h = f"{size/1e9:.2f}GB" if size >= 0 else "?"
    log("INFO", f"runway_days={runway} default_rows~{rows} default_size={size_h} "
                f"growth_since_last={growth}")
    if problems:
        msg = "; ".join(problems)
        log("ALERT", msg)
        if alert:
            send_telegram(f"\U0001f7e0 KAZUS partition-maintainer {iso()}\n" +
                          "\n".join(f"• {p}" for p in problems))
        return 2
    log("OK", "partition runway healthy, default not growing")
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
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="print the CREATEs that would run; touch nothing")
    g.add_argument("--check", action="store_true",
                   help="report runway + default growth; exit 2 if unhealthy")
    ap.add_argument("--alert", action="store_true",
                    help="with --check: send a Telegram alert on problems")
    args = ap.parse_args()
    if args.check:
        return cmd_check(args.alert)
    return cmd_create(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
