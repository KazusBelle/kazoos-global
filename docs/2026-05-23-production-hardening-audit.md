# Production Hardening Audit — 2026-05-23

Snapshot of the liquidity discovery layer under current data (~28h of accumulated history). Numbers are real measurements, not estimates. The audit produced one applied fix (an index that addresses the worst seq scan) and a prioritized backlog for the rest. No major refactors landed in this pass.

## TL;DR

| risk | what | impact |
|---|---|---|
| 🔴 high | `synthesis` endpoint runs **30s** per call, polled every 30s | one stuck Coordination page can saturate the connection pool |
| 🔴 high | `liquidity_samples` writes **1.0M rows/day**, no aggregation tier | 35d retention ≈ **35M rows / 6 GB** steady-state, growing 1:1 with symbol count |
| 🟠 med | `sanity_audit` now calls 4 heavy functions per check (5s end-to-end), polled every 60s | wasteful — most checks recompute the same data |
| 🟠 med | DB pool uses SQLAlchemy defaults (`pool_size=5`, `max_overflow=10`) | a single slow endpoint can starve everything else |
| 🟠 med | `liquidity_anomaly_memory`, `liquidity_anomaly_edges`, `liquidity_intelligence_history` have **no retention** | unbounded growth (slow but unbounded) |
| 🟡 low | `liquidity_alert_history` indexes don't cover pure time-range scans | seq scans today are fast (700 rows) but slope to ~250K rows/year |
| ✅ fixed | pattern_discovery seq-scanned 131K rows + disk-spilled sort | added `ix_liq_samples_metric_ts`, **629ms → 376ms** |

## 1 — DB growth & sizes

Largest tables right now:

```
table                          total_size  rows         24h growth
liquidity_samples              209 MB      1,224,267    1,014,165 / day  ← dominates
liquidity_alert_history        424 kB      691            683 / day
server_metrics                 296 kB      926          (cadence unknown)
liquidity_intelligence_history 104 kB      131            131 / day
liquidity_anomaly_edges         72 kB       27               27 lifetime
liquidity_anomaly_memory        64 kB       10               10 / day
liquidity_crossex_history       40 kB       11               11 / day
```

`liquidity_samples` is the only table whose growth matters at meaningful horizons. The rest combined add < 1 MB/day. Projections:

| horizon | samples rows | size |
|---|---|---|
| 30 d | 30 M | ~6 GB |
| 90 d | 90 M | ~18 GB (capped by 35d retention) |
| 1 y | 35d retention → steady 35 M / 6 GB | growth limit only changes if symbol count grows |

**Implicit scaling factor: symbol count.** Current dataset has 104 symbols polled. Sample rate is 1 Hz × ~14 metrics × symbols. Doubling symbols doubles rows, doubles index size, doubles seq-scan cost on every research endpoint.

## 2 — Index audit

Existing on `liquidity_samples` (post-fix):

```
ix_liq_samples_symbol_metric_ts   93 MB   (symbol, metric, ts)   — chart/replay reads
ix_liq_samples_metric_ts          NEW     (metric, ts)            — pattern_discovery, cross-symbol agg
liquidity_samples_pkey            26 MB   (id)
```

Total index size will be ~1.3× heap once `(metric, ts)` is fully populated. Still acceptable.

Existing on `liquidity_alert_history`:
- `(kind, started_at_ms)`, `(symbol, started_at_ms)`, unique `(alert_id)`, pk `(id)`
- **Missing: `(started_at_ms)`** for pure time-range scans. Today it doesn't matter (seq scan over 691 rows is 1.4 ms). At 250K/y it will start to matter — add when row count crosses ~50K.

Existing on `liquidity_intelligence_history`: `ix_liq_intel_history_ts` already exists. Forecast / hidden_regimes queries use it correctly.

## 3 — Endpoint latencies (median of 3 runs)

```
endpoint                        latency
synthesis                       31.4 s   ← 🔴 critical
multi-horizon                    5.5 s   ← 🟠
sanity-audit                     5.3 s   ← 🟠
adaptation-recommendations       4.5 s   ← 🟠
evolutionary-behavior            1.1 s   ← 🟡
pattern-discovery (post-fix)     0.6 s   (was 0.8s, 0.6s after index)
propagation                     70 ms    ✅
hidden-regimes                  25 ms    ✅
intelligence-forecast           18 ms    ✅
intelligence-history            22 ms    ✅
```

### synthesis = 30s

[`intelligence_synthesis`](shared/kazus_logic/liquidity/research.py) calls six heavy functions sequentially: `risk_state`, `regime_shift_warning`, `structural_breaks`, `meta_confidence`, `meta_intelligence_health`, `strategic_state`. Each does its own DB reads on `liquidity_samples` and computes its own per-symbol aggregates. There is no shared cache across calls.

Frontend `Coordination.tsx` polls `getSynthesis` every 30 s. **Each call holds a DB session for 30s.** With pool_size=5 + max_overflow=10, four parked Coordination tabs already exhaust the pool.

### sanity-audit = 5s

[`sanity_audit`](shared/kazus_logic/liquidity/research.py) now invokes (within one request):
- `propagation_graph(lookback=7d)`
- `intelligence_evolution_forecast(horizon=7d)` (possibly twice)
- `discover_patterns(lookback=14d, min_support=8)`
- `hidden_regimes(lookback=14d, max_clusters=8)`
- `adaptation_recommendations()`

That's the entire discovery layer in one HTTP call. Frontend polls every 60s.

## 4 — Worst plan: pattern_discovery before fix

Before:
```
Parallel Seq Scan on liquidity_samples  131,711 rows (per worker × 3)
Sort Method: external merge  Disk: 5,944 kB ← work_mem spillover
Execution Time: 629 ms
```

After adding `ix_liq_samples_metric_ts`:
```
Bitmap Index Scan on ix_liq_samples_metric_ts  396,868 rows in 18 ms
HashAggregate (in-memory, 3 MB)
Execution Time: 376 ms
```

40% faster, no disk spill. The remaining 376ms is the bucket grouping + AVG — that's irreducible at this row count.

## 5 — Worker / runtime

[`worker/app/runner.py:407`](worker/app/runner.py) — main loop is an M5-boundary scheduler. Four background tasks:
- liquidity_poller (60s)
- liquidity_realtime engine (1Hz sampler, 5s reconcile, 5s flush, 3s status)
- anomaly_recorder (5 min)
- intel_snapshot (5 min)

WS reconnect at [`shared/kazus_logic/liquidity/realtime/ws_client.py:55`](shared/kazus_logic/liquidity/realtime/ws_client.py#L55): exponential backoff 1s→30s with jitter, ping/pong 30s/10s. Robust.

**Retention currently implemented:**
- `liquidity_samples`: 35-day TTL via `poller.prune_old()` hourly
- `alert_events`: keep top 100 rows (worker)

**Retention currently missing:**
- `liquidity_alert_history`
- `liquidity_anomaly_memory` + `liquidity_anomaly_edges`
- `liquidity_intelligence_history`
- `liquidity_crossex_history`
- `server_metrics`

All grow slowly but unbounded. At their current rates, none becomes a storage problem within 1y, but `liquidity_intelligence_history` × 12 metric columns × 1y = ~48K rows × ~600 bytes = ~30 MB — fine.

## 6 — Frontend polling pressure

Pages and their polling cadences (from `Discovery.tsx`, `Coordination.tsx`, `Strategy.tsx`, etc.):

```
Liquidity        ws_status 4s, snapshot 5s          ← 2 reqs every ~5s
Dashboard        15s
Coordination     30s × {synthesis, conflicts, ...}  ← multiple per tick
Strategy / Operations / Meta   30s
Discovery        60s × {sanity, prop, patterns, ...} 
ServerHealth / Memory          60s
```

A single open Coordination tab calls `synthesis` every 30s → 30s of DB work every 30s → **a single tab is permanently saturating one DB connection.** This is the worst current production behavior.

## 7 — Discovery scaling under load

Order-of-magnitude reasoning (no synthetic load test run):

| factor | propagation | pattern_discovery | hidden_regimes |
|---|---|---|---|
| 10× symbols | O(N²) edge scan → ~100× | linear in samples → 10× | linear in snapshots → unchanged |
| 10× alerts/day | O(N²) edge scan → 100× | unchanged | unchanged |
| 10× intel snapshots | unchanged | unchanged | linear in snapshots → 10× |

Propagation is the only O(N²) hot spot today. Currently bounded by alert count (~700/day → tractable). At 10× alerts the pairwise scan inside `propagation_graph` would take ~minutes. Mitigation: bucket by `(kind, day)` and pair within buckets only.

## 8 — Applied fix

Single fix in this pass: [`shared/kazus_db/models.py`](shared/kazus_db/models.py) — add `ix_liq_samples_metric_ts(metric, ts)`. Materialized via `CREATE INDEX CONCURRENTLY` so it doesn't block the 1Hz sampler.

## Prioritized backlog

### P0 (do before next reasoning phase)

1. **Cache `synthesis` for 30s in-memory.** It already polls at 30s — even a 25s cache per-process eliminates pool starvation. Effort: ~1h.
2. **Memoize sanity_audit sub-computations.** A 60s LRU on `propagation_graph(lookback=7d)`, `intelligence_evolution_forecast(horizon=7d)`, `discover_patterns(14d)` would drop sanity-audit from 5s → ~50ms when nothing changed. Effort: ~2h.
3. **Set explicit DB pool**: `pool_size=20, max_overflow=10, pool_timeout=10`. Defaults are dangerously low for an engine that has 30s endpoints. Effort: ~15min.

### P1 (next sprint)

4. **Add retention for slow-growth tables**: `liquidity_alert_history` (90d), `liquidity_intelligence_history` (90d), `liquidity_anomaly_memory/edges` (180d). Wire into worker's hourly prune. Effort: ~2h.
5. **Aggregation tier for `liquidity_samples`**: roll up to per-minute / per-15min beyond 7d, drop raw beyond 7d. Cuts steady-state size from 6 GB to ~1 GB. Effort: ~1d.
6. **Operational visibility endpoint**: `/api/admin/runtime-health` with slow queries (pg_stat_statements), pool stats, worker timings, anomaly throughput. Effort: ~half day.

### P2 (when symbol/alert count grows 5×)

7. Propagation bucketing: replace flat O(N²) pair-finder with `(kind, day_bucket)` grouped scan.
8. Anomaly memory pruning policy + index on `occurred_at_ms`.
9. `(started_at_ms)` index on `liquidity_alert_history` (after row count crosses ~50K).

## Operational visibility — what to instrument next

Minimum metrics to graph before adding any new intelligence layer:

- `synthesis` p50/p95/p99 latency
- DB pool checkouts / queue depth
- `liquidity_samples` row count + index size
- worker task heartbeats (last successful tick per background task)
- WS reconnect rate
- Slow queries from `pg_stat_statements` (top-5 by total_time)

A simple admin-only endpoint reading these from `pg_stat_*` would unblock most ops questions without per-endpoint timing decoration.
