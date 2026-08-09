# Host monitor (v2) — kazus-global

Host-level watchdog that alerts to Telegram on **critical load, container
outages, DB overload, API failures, and LIQ degradation**. Runs on the host via
a systemd timer (every 60s), independent of Docker, so it keeps alerting even
when the stack is degraded. Born from the 2026-06-23/24 freezes (research
aggregations pinned the DB → host hard-froze with no warning).

Stdlib-only (`urllib` + `/proc` + `docker`/`psql` subprocess). No pip deps. It
never queries application tables or `liquidity_samples`; PostgreSQL checks use
system views only. All external calls are timeout-bounded so the monitor can
never become a load source.

## What it checks (one cycle / 60s)

**Host:** RAM (by `MemAvailable`, not cache-inflated "used%"), actionable
memory pressure (PSI and sustained active swap I/O), kernel OOM evidence, disk
`/`, inodes `/`, load (1-min), CPU busy % and iowait % (from `/proc/stat`
deltas, sustained windows). Static swap occupancy remains diagnostic state and
does not alert by itself.

**Docker/containers:** daemon reachable; `frontend/backend/worker/db` status +
health + restart-count; db container CPU (targeted `docker stats`, sustained).
Any key container down/unhealthy, a restart-count increase, or `OOMKilled=true`
is CRITICAL.

**HTTP/API:** `/healthz`, `/api/liquidity/runtime-state`, `/ws/status`,
`/top?limit=100`. 3 consecutive failures on a probe = CRITICAL (single blips are
soft, no storm).

**PostgreSQL (system views only):** active connections vs max, longest running
query, idle-in-transaction, blocked queries, and active **heavy research** query
count (`AVG(value)`/`percentile_disc`, by query text — never reads the table).

**LIQ:** `value_path.ok`, `subscribed_count` vs baseline, `derived_status`,
`failure_boundary`; plus runtime-state-unavailable **combined with** ws_status
stale (sustained) = CRITICAL.

## Thresholds (4-core / 8 GB)

| Signal | WARNING | CRITICAL |
|---|---|---|
| RAM available | <12% | <7% |
| Memory PSI avg10 | `some` >=10% sust 3m | `full` >=10% sust 2m |
| Active swap I/O | RAM available <20% sust 3m | — |
| Disk `/` | >88% | >93% |
| Inodes `/` | >80% | >90% |
| Load (1m) | >6 | >8 |
| iowait | >30% sust 3m | >50% sust 2m |
| CPU busy | — | >90% sust 5m |
| db CPU | >300% sust 3m | >350% sust 5m |
| heavy research | >0 sust 2m | >0 sust 5m |
| PG longest query | >300s | >900s |
| PG conns | >80% max | — |
| idle-in-txn / blocked | >0 (blocked sust 3m) | — |
| HTTP probe | (soft) | 3 consecutive fails |
| value_path / subs | degraded | degraded sust 3m |
| derived RED / SCHEDULER_STARVATION | warning | — |
| container down/unhealthy / restart++ | — | critical |

Tune in the `TH` dict at the top of `monitor.py` (or override paths via
`KAZUS_*` env vars). Alerts are transition-based: once when a key first enters
WARNING/CRITICAL, once on a material severity escalation, and no reminder while
the same state persists. RECOVERY is sent once after a key is OK for 5 min.
An alert or recovery whose Telegram delivery actually fails is retried at most
once per 10 minutes until one delivery succeeds.
State is atomically replaced at `~/.kazus-monitor-state.json`, so a full disk
cannot truncate the last good deduplication state.

## Run it

```bash
# Evaluate everything, print structured results. No Telegram, no state write.
python3 ops/host-monitor/monitor.py --dry-run

# One production-like cycle (reads/writes state, sends on state transitions).
python3 ops/host-monitor/monitor.py --once          # (also the default with no flag)

# Channel tests (ignore cooldowns, no heavy checks):
python3 ops/host-monitor/monitor.py --force-warn
python3 ops/host-monitor/monitor.py --force-critical
```

## Install later (needs root — not done yet)

```bash
sudo bash ops/host-monitor/install-systemd.sh           # install unit files only
sudo bash ops/host-monitor/install-systemd.sh --enable  # install + enable + start (removes cron bridge)
# or, after a files-only install:
sudo systemctl enable --now kazus-monitor.timer
```

No-sudo fallback (host cron, survives reboot): `bash ops/host-monitor/install-cron.sh`.

## Logs

```bash
journalctl -u kazus-monitor.service -n 50 --no-pager   # systemd
journalctl -u kazus-monitor.service -f                 # follow
tail -f ~/.kazus-monitor.log                           # cron-bridge log
```

## Troubleshooting

- **Alert storm:** inspect `~/.kazus-monitor-state.json` and recent service logs;
  repeated identical Telegram alerts indicate state is not being preserved.
  Static swap occupancy and known `SCHEDULER_STARVATION` do not page by
  themselves. As a blunt stop, `sudo systemctl stop kazus-monitor.timer`.
- **Monitor silent (no alerts when expected):** run `--dry-run` to see evaluated
  state; check `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are set in `.env`
  (`--force-warn` tests the channel); check the timer is active
  (`systemctl status kazus-monitor.timer`) and recent runs
  (`journalctl -u kazus-monitor.service`). A stale `~/.kazus-monitor-state.json`
  can hold cooldowns — delete it to reset.

## Uninstall / rollback

```bash
sudo systemctl stop kazus-monitor.timer
sudo systemctl disable kazus-monitor.timer
sudo rm -f /etc/systemd/system/kazus-monitor.service /etc/systemd/system/kazus-monitor.timer
sudo systemctl daemon-reload
# optional: remove the cron bridge + state
crontab -l | grep -v KAZUS-HOST-MONITOR | crontab -
rm -f ~/.kazus-monitor-state.json ~/.kazus-monitor.log
```

## Scope / non-goals

Read-only **observation + alerting**. It never restarts containers, changes
limits, or touches the app/DB/worker. It warns; a human decides. Preventing the
root cause (research analytics) is handled separately by the backend/worker
`RESEARCH_ENABLED` / `WORKER_RESEARCH_ANALYTICS_ENABLED` kill-switches.
