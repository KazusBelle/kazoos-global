# liquidity_samples partition maintainer

Keeps a rolling runway of **empty future daily partitions** for the
RANGE-partitioned `liquidity_samples` table so live writes always have a dated
home and never fall back into `liquidity_samples_default`.

## Background

`liquidity_samples` is partitioned by `ts` (bigint epoch-ms), one partition per
UTC day: `liquidity_samples_pYYYYMMDD`, bounds `FROM (day_midnight_ms) TO
(+86400000)`. There was **no automation** — a manual repartition on 2026-07-05
created dailies only through `p20260713`, so from **2026-07-14** every new row
fell into `liquidity_samples_default`, which grew to ~9 GB and threatened the
disk. This maintainer prevents a recurrence.

## What it does (and never does)

- Creates missing daily partitions for **today+1 .. today+`RUNWAY_DAYS`** (14).
- Idempotent — existing partitions are skipped, never recreated.
- Every DDL session sets `lock_timeout` + `statement_timeout` so it can never
  block the collection insert path for long, nor spill onto a full disk.
- **Never** moves, deletes, copies, detaches, drops, vacuums, or alters any row
  or existing partition — `default` included. Draining `default` is a separate,
  human-approved operation.

### The default-partition trap

Creating a partition while a DEFAULT exists makes Postgres scan the default to
prove no existing row belongs in the new range; if it does, the CREATE fails.
The maintainer only targets ranges from the next UTC-day boundary forward (which
`default` is no longer accumulating into). Should a CREATE still hit the
overlap error it is logged `SKIPPED_OCCUPIED` and is **not** a hard failure.

## Usage

```bash
# Preview — touches nothing:
python3 ops/partition-maintainer/partition_maintainer.py --dry-run

# Real pass (what the timer runs):
python3 ops/partition-maintainer/partition_maintainer.py

# Health check — exit 2 if runway < 7d or default is still growing:
python3 ops/partition-maintainer/partition_maintainer.py --check          # print only
python3 ops/partition-maintainer/partition_maintainer.py --check --alert   # + Telegram
```

## Install (systemd timer, daily at 12:00 UTC)

```bash
sudo bash ops/partition-maintainer/install-systemd.sh           # install unit files only
sudo bash ops/partition-maintainer/install-systemd.sh --enable  # install + enable + start
```

Files-only by default so install is side-effect free. Verify:

```bash
systemctl list-timers kazus-partition-maintainer.timer --no-pager
sudo systemctl start kazus-partition-maintainer.service   # run one pass now
journalctl -u kazus-partition-maintainer.service -n 20 --no-pager
```

## Monitoring

The maintainer's `--check` mode is the durable guard (Phase 3 requirement #8):
it alerts if the contiguous future runway drops below `MIN_RUNWAY_DAYS` (7) or
if `liquidity_samples_default`'s estimated row count grows materially between
checks (a sign routing regressed). It uses `pg_class.reltuples` — a catalog
estimate, no table scan — and stores the previous count in
`~/.kazus-partition-state.json`.

You can wire `--check --alert` onto the existing 60s host-monitor cadence, or
add a second daily timer, or fold an equivalent `check_partitions()` into
`ops/host-monitor/monitor.py` (which already owns Telegram cooldowns).

## Configuration (env overrides)

| Var | Default | Meaning |
|-----|---------|---------|
| `KAZUS_PARTITION_RUNWAY_DAYS` | `14` | future days to keep provisioned |
| `KAZUS_PARTITION_MIN_RUNWAY_DAYS` | `7` | `--check` alert floor |
| `KAZUS_DEFAULT_GROWTH_ALERT_ROWS` | `50000` | `--check` default-growth alert threshold |
| `KAZUS_PARTITION_LOCK_TIMEOUT` | `15s` | psql `lock_timeout` for CREATE |
| `KAZUS_PARTITION_STATEMENT_TIMEOUT` | `180s` | psql `statement_timeout` for CREATE |
| `KAZUS_DB_CONTAINER` | `kazus-global-db-1` | db container name |
