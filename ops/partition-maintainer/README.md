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

- **Creates** missing daily partitions for **today+1 .. today+`RUNWAY_DAYS`** (14).
- **Enforces retention** (`--enforce-retention`): DROPs dated partitions whose
  upper bound is `<= today_utc_midnight − RETENTION_DAYS` (14), oldest first, one
  autocommit txn each, bounded locks, **never `CASCADE`**. The UTC-day-floored
  cutoff matches `poller.prune_old`, so the two agree and the dropper only ever
  removes WHOLE expired days.
- Idempotent — existing partitions are skipped, expired ones dropped once.
- Every DDL session sets `lock_timeout` + `statement_timeout` so it can never
  block the collection insert path for long, nor spill onto a full disk.
- **Never** touches `liquidity_samples_default`, future partitions, the current
  straddling partition, or any non-`liquidity_samples_pYYYYMMDD` table — a name
  regex is the hard guard. It never moves/copies/vacuums rows. Draining `default`
  is a separate, human-approved operation.

## Modes

| Command | Action |
|---|---|
| _(no flag)_ | create missing future partitions |
| `--enforce-retention` | DROP fully-expired dated partitions |
| `--check` | report health; exit 2 if unhealthy |
| `--daily` | create → enforce-retention → check (the timer entrypoint) |

Modifiers: `--dry-run` (create/enforce/daily — prints actions, touches nothing),
`--alert` (check/daily — Telegram on problems).

### The default-partition trap

Creating a partition while a DEFAULT exists makes Postgres scan the default to
prove no existing row belongs in the new range; if it does, the CREATE fails.
The maintainer only targets ranges from the next UTC-day boundary forward (which
`default` is no longer accumulating into). Should a CREATE still hit the
overlap error it is logged `SKIPPED_OCCUPIED` and is **not** a hard failure.

## Usage

```bash
# Preview the whole daily pass — touches nothing:
python3 ops/partition-maintainer/partition_maintainer.py --daily --dry-run

# What the timer runs (create + enforce-retention + check + alert):
python3 ops/partition-maintainer/partition_maintainer.py --daily --alert

# Individual modes:
python3 ops/partition-maintainer/partition_maintainer.py                      # create only
python3 ops/partition-maintainer/partition_maintainer.py --enforce-retention --dry-run
python3 ops/partition-maintainer/partition_maintainer.py --check              # health, exit 2 if bad
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

`--check` alerts (catalog-only — never scans a table) on:
- future runway `< MIN_RUNWAY_DAYS` (7);
- **post-boundary** DEFAULT growth or a **missing current-day partition** — i.e.
  new writes leaking into DEFAULT (routing regression);
- fully-expired dated partitions still present (retention not being enforced);
- disk free below `DISK_WARN_GB` / `DISK_CRIT_GB`.

It is **boundary-aware**: before `WRITES_ROUTED_BOUNDARY_MS` (2026-07-21 00:00Z),
and for the legacy July 14–20 rows aging out of DEFAULT through ~2026-08-04, it
logs an INFO note and **never pages** — only genuine post-boundary regressions
alert. Previous DEFAULT row count is stored in `~/.kazus-partition-state.json`.

## Configuration (env overrides)

| Var | Default | Meaning |
|-----|---------|---------|
| `KAZUS_PARTITION_RUNWAY_DAYS` | `14` | future days to keep provisioned |
| `KAZUS_PARTITION_MIN_RUNWAY_DAYS` | `7` | `--check` alert floor |
| `KAZUS_DEFAULT_GROWTH_ALERT_ROWS` | `50000` | `--check` default-growth alert threshold |
| `KAZUS_PARTITION_LOCK_TIMEOUT` | `15s` | psql `lock_timeout` for CREATE |
| `KAZUS_PARTITION_STATEMENT_TIMEOUT` | `180s` | psql `statement_timeout` for CREATE |
| `KAZUS_DB_CONTAINER` | `kazus-global-db-1` | db container name |
