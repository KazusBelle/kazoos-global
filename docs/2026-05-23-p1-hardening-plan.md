# P1 Hardening — Plan + Scaling Estimates

Companion to [`2026-05-23-production-hardening-audit.md`](2026-05-23-production-hardening-audit.md). The earlier doc identified P0 (synthesis pool starvation, sanity recompute, pool defaults) and they're already merged. This one covers the remaining work needed for the system to live for months under realistic growth — most items are designs with effort estimates, not full implementations, because each is large enough to deserve its own review pass.

What landed in this branch (concrete):
- Retention pruning for `liquidity_alert_history` (90d), `liquidity_intelligence_history` (90d), `liquidity_anomaly_memory` + `liquidity_anomaly_edges` (180d), `liquidity_crossex_history` (90d). Wired into the worker's hourly prune cycle.
- Operational safety guards: propagation overload cap (50K alerts max), per-request `statement_timeout = 45s`, `overloaded` flag on propagation response.
- Runtime observability extension: heartbeat-by-proxy for 4 worker tasks, top-8 tables by size, overall health state on `/admin/runtime-health`.

What this doc covers (design-only):
1. Aggregation tier for `liquidity_samples`
2. Propagation scalability beyond ~50K alerts/day
3. Replay / memory survivability audit
4. Long-horizon scaling estimates (30d / 90d / 1y)
5. `pg_stat_statements` enablement
6. Operational-safety items not yet implemented

---

## 1 — Aggregation tier for `liquidity_samples`

The samples table writes ~1M rows/day. At 35-day retention that's ~35M rows / ~6 GB steady-state. That's manageable but every research endpoint that reads multi-day windows pays a fixed scan cost proportional to row count. Once symbols 2× → 70M rows; the index probably still fits in RAM, but seq-scan paths get linearly slower.

**Proposed tiers:**

| tier | granularity | retention | storage @ steady |
|---|---|---|---|
| **L0 raw** | 1 Hz / poll cadence | 7 days | 7M rows × ~190B = ~1.3 GB |
| **L1 minute** | 60s rollup (avg/min/max/last per metric per symbol) | 30 days | 30 × 24 × 60 × 104 × 14 = ~63M rows × 80B = ~5 GB |
| **L2 hour** | 1h rollup | 1 year+ | 365 × 24 × 104 × 14 = ~13M rows × 80B = ~1 GB |

L1 is *larger* than L0 because of granularity choice — the rollup gives us 60× compaction per metric but stores 4 statistics per row (avg/min/max/last) instead of 1, and at 30d × 4× longer than L0 the net is bigger. To make L1 actually save space, only store rows where the metric is "active" (sampled at all) and drop the 4-stat tuple to just 2 (avg, max). Then L1 ≈ 1.5 GB.

**Implementation sketch:**

```
worker/app/aggregation.py
  rollup_minute_samples(db_factory, retention_l0=7d) async
    - Find min(ts) where rolled_up_at IS NULL OR ts < now - 5min
    - For each (symbol, metric, minute_bucket) compute avg/max
    - INSERT into liquidity_samples_minute ON CONFLICT DO UPDATE
    - DELETE from liquidity_samples WHERE ts < now - 7d

  rollup_hour_samples(db_factory, retention_l1=30d) — same pattern from L1 → L2
```

Tables:
```sql
CREATE TABLE liquidity_samples_minute (
  symbol VARCHAR(32),
  metric VARCHAR(32),
  bucket_ts BIGINT,  -- minute boundary
  avg_value DOUBLE PRECISION,
  max_value DOUBLE PRECISION,
  sample_count INT,
  PRIMARY KEY (metric, bucket_ts, symbol)  -- leading metric for pattern_discovery
);
CREATE INDEX ix_lsm_symbol_metric_ts ON liquidity_samples_minute (symbol, metric, bucket_ts);
```

**Query rewrite cost:** every research function that reads `liquidity_samples` needs to choose tier by window:
- ts ≥ now − 7d: read L0
- ts ≥ now − 30d: read L1, fall through to L0 for the last 7d
- ts ≥ now − 1y: read L2, fall through to L1 then L0

This is the heaviest part of the implementation — affects ~10 functions in `research.py`. A wrapper `sample_tier(window_days)` returning the right table+columns would centralize it.

**Effort:** 1.5–2 days. Migration includes a backfill pass from current L0 → L1 (one-time, ~10 min for 1M rows). Rollout: shadow-write L1/L2 for a week before switching readers.

**Decision criterion:** when sample table crosses ~10 GB (about when 2× symbols or 3× retention).

---

## 2 — Propagation scalability

Current pairing in `propagation_graph` is O(N²) per kind: for each alert event, walk forward in time until window expires. Today: ~700 alerts/day × 3 kinds = ~50K pairs evaluated per call. Fine.

At 10× alerts (one cascade or 5× symbols): 250K events × ~100 forward-scan steps = 25M comparisons per call → multi-second hot loop in Python.

**Three options, increasing scope:**

### Option A: hard cap (already landed)

```python
OVERLOAD_HARD_CAP = 50_000
if len(rows) > cap:
    rows = rows[-cap:]
```

Pros: zero risk, ships immediately. Cons: silently truncates the historical view during high-alert periods, which is exactly when propagation analysis is most valuable.

### Option B: bucketed pairing (recommended)

Group events into `(kind, minute_bucket)` first. For each event, only pair with events in the same or next ~30 minute_buckets. O(N) in events, O(B²) in buckets per kind where B = lookback_minutes / 30.

```python
by_minute_bucket = defaultdict(list)
for ts, sym in events:
    by_minute_bucket[(kind, ts // 60_000)].append((ts, sym))

for (kind, mb), evts in by_minute_bucket.items():
    for ts_a, sym_a in evts:
        for offset in range(30):  # 30 × 1-min buckets = lead_window
            for ts_b, sym_b in by_minute_bucket.get((kind, mb + offset), ()):
                if min_lead_ms <= ts_b - ts_a <= lead_window_ms:
                    edge_step(...)
```

Pros: O(N × W) where W is the lookback in minutes. Constant memory. Cons: ~half day of careful refactoring + test cases for boundary events.

### Option C: precomputed propagation table

Worker maintains `liquidity_propagation_edges_hourly` table, incrementally updated as alerts come in. Read endpoint is a SELECT.

Pros: O(1) read latency. Cons: 1-2 days. Adds write path + reconciliation logic + bug surface. Probably overkill until symbols ≥ 200.

**Decision criterion:** when `total_alerts > 5000/day` (currently ~700). Adopt B then.

---

## 3 — Replay / memory survivability audit

Read-only audit of frontend pages that could deteriorate with growth:

### Memory page ([Memory.tsx](frontend/src/components/Memory.tsx))

Renders anomaly lineage trees as inline SVG. Depth-3 BFS = up to ~50 nodes for the current 10-record memory, scales linearly with edges. At 1y / 180d retention × ~10/day rate ≈ 1800 nodes. SVG at 1800 nodes will:
- Render in ~50–100ms (fine)
- But occupy ~100KB DOM per frame, causing GC pressure if polled at 60s
- Be unreadable without zoom/pan

**Recommendations:**
- Cap depth at 3 (current) and node count at 200 client-side; show "+ N more" indicator
- Add a maxNodes prop to the lineage endpoint with default 200; server already returns small graphs but the contract isn't explicit
- Lazy-load: render only on user interaction, not on polling tick

### Memory graph ([Memory.tsx#L362](frontend/src/components/Memory.tsx#L362), [#L548](frontend/src/components/Memory.tsx#L548), [#L639](frontend/src/components/Memory.tsx#L639))

Three SVG canvases (different layouts). Each redraws on REFRESH_MS=60s tick. At 1000+ nodes this becomes a measurable per-tick cost.

**Recommendation:** memoize SVG render with `useMemo` on the data array. Already React-functional, so add deps and an early-return when data hash unchanged.

### Discovery panels

Discovery page polls 4-6 endpoints per 60s tick. All now ≤ 50ms warm after P0 caching. No survivability issue.

### Liquidity page

WS status poll every 4s, snapshot poll every 5s. At months of uptime this is 13M HTTP requests/year per open tab. Each is small (sub-50ms), so no bottleneck — but worth a `visibilitychange` listener to pause when tab hidden. ~15 min effort.

**Effort to fix all of the above:** ~3 hours.

---

## 4 — Long-horizon scaling estimates

Based on current measured rates + steady-state retention from `poller.py`:

| | now (2 days) | 30 days | 90 days | 1 year |
|---|---|---|---|---|
| **liquidity_samples** | 1.2M rows / 234 MB | 30M / **6 GB** (retention caps at 35d) | 35M / **6.6 GB** (steady) | same (steady) |
| **liquidity_alert_history** | 691 rows / 424 KB | 21K / ~12 MB | 63K / ~36 MB (steady at 90d) | same |
| **liquidity_intelligence_history** | 137 rows / 104 KB | 8.6K / ~7 MB | 26K / ~22 MB (steady at 90d) | same |
| **liquidity_anomaly_memory** | 10 rows / 64 KB | 300 / 200 KB | 900 / 600 KB | 1.8K / 1.2 MB (steady at 180d) |
| **liquidity_anomaly_edges** | 27 rows | scales with memory rows × ~3 edges/node | ~2.7K | ~5.4K (steady) |
| **liquidity_crossex_history** | 11 rows / 40 KB | 330 / 1.2 MB | 990 / 3.6 MB (steady at 90d) | same |

**Total DB size projection at 1y steady-state:** ~7 GB. All retention-bounded. The single growth driver is `liquidity_samples`. The aggregation tier from §1 caps that at ~7 GB indefinitely while extending visible history to 1y.

**Endpoint latency projection:**
- Cached endpoints (after P0): unchanged. The cache key is the (lookback, args) tuple, not the row count.
- Cold misses scale with the function. Heaviest case is `intelligence_synthesis` because it pulls multiple per-symbol aggregates. At 2× symbols, cold synthesis goes from ~30s → ~60s. P0 cache absorbs this in normal operation; the only operator-visible effect is a slower cold-start after deploy.

**Worker pressure at 2× symbols:**
- liquidity_samples write rate: 2× → 2M rows/day. 35d retention → 70M / 12 GB samples. Index size grows 1.3× heap → ~16 GB total. **Adopt aggregation tier here.**
- WS subscriptions: 2× → bandwidth doubles. Connection count unchanged (multiplexed).
- Realtime engine flush: every 5s, ~2× the rows per flush. Bulk insert handles ~10K rows/sec comfortably.

---

## 5 — `pg_stat_statements`

Currently absent (only `plpgsql` extension installed). Enabling unlocks:
- Top-N slowest queries by `total_time`
- Per-query call count + mean time + I/O stats
- Visibility into queries not on the critical path (worker writes, prune, replay)

**Enablement:**
```
# postgresql.conf — requires restart
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.track = all

# After restart:
CREATE EXTENSION pg_stat_statements;
```

This is a 5-minute change but the DB restart is operator-coordinated. Don't enable casually. Once enabled, `/admin/runtime-health` can surface top-5 queries by total_time.

**Effort:** 30 min including the runtime-health hook.

---

## 6 — Operational safety items not yet implemented

In priority order:

### Worker WS reconnect counter (P1)

[`shared/kazus_logic/liquidity/realtime/ws_client.py:55`](shared/kazus_logic/liquidity/realtime/ws_client.py#L55) already has exp-backoff. Add a process-local counter `_reconnect_count` per connection, expose via the same channel as worker heartbeats (a small row in `system_status` or a dedicated `worker_state` table). Lets ops detect WS instability before it shows up as data gaps.

**Effort:** 1 hour. Storage: per-process counter persisted to DB every minute.

### Anomaly recorder rate-limit circuit breaker (P1)

If `anomaly_inflation` sanity finding fires `CRITICAL`, the auto-recorder should temporarily back off its cooldowns (raise threshold for 30 min). Prevents runaway recording from poisoning the memory graph.

**Effort:** ~2 hours. Adds a feedback loop sanity → recorder; needs careful design so it can't get stuck.

### Replay safety limits (P1)

`/research/replay` already accepts since/until — but no cap on the spread. A 1y replay request would scan a third of the samples table. Cap at 30 days or paginate.

**Effort:** 30 min. Per-endpoint cap with HTTP 400 if exceeded.

### Stale-data indicators in UI (P2)

The TTL cache means responses can be up to 5 min stale. UI doesn't currently show this. A `cached_at_ms` field on cached endpoints + a "data freshness X min ago" chip in panel toolbars when > 60s.

**Effort:** 1 hour backend (modify `_ttl_cached` to record fetch timestamp) + 1 hour frontend (chip).

---

## Updated backlog priority

| | task | landed | effort |
|---|---|---|---|
| ✅ P0 | synthesis caching, pool config, /admin/runtime-health | yes | done |
| ✅ P1.A | retention prune for slow-growth tables | yes (this branch) | done |
| ✅ P1.A | propagation overload cap | yes (this branch) | done |
| ✅ P1.A | statement_timeout per session | yes (this branch) | done |
| ✅ P1.A | runtime-health: heartbeats + tables + overall | yes (this branch) | done |
| 🟠 P1.B | aggregation tier (samples → minute → hour) | design only | 1.5–2 days |
| 🟠 P1.B | propagation bucketed pairing | design only | ~half day |
| 🟠 P1.B | pg_stat_statements + top-N in runtime-health | design only | 30 min + DB restart |
| 🟡 P1.C | worker WS reconnect counter | not landed | 1 hour |
| 🟡 P1.C | anomaly recorder circuit breaker | not landed | 2 hours |
| 🟡 P1.C | replay safety limits | not landed | 30 min |
| 🟡 P1.C | stale-data indicators in UI | not landed | 2 hours |
| 🟢 P1.D | Memory page SVG memoization + node cap | not landed | 3 hours |
| 🟢 P1.D | Liquidity tab visibility-pause | not landed | 15 min |

P1.B items wait until we cross the trigger thresholds (samples > 10 GB, alerts > 5K/day, or operator asks for query-level slow-query view). P1.C is small enough to land in one half-day push if needed. P1.D is UI polish — wait for user feedback that it actually matters.
