# Runtime Health Telemetry (WS_RELIABILITY_001) — measurement contract (companion)

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md), [`docs/lip-governance.md`](lip-governance.md), [`docs/lip-execution-validation.md`](lip-execution-validation.md), [`docs/lip-burst-detection.md`](lip-burst-detection.md), [`docs/lip-metric-registry.md`](lip-metric-registry.md).

**Status: IMPLEMENTED (2026-05-29, WS_RELIABILITY_001).** Diagnostic-only observability layer. Code: [`shared/kazus_logic/liquidity/realtime/health.py`](../shared/kazus_logic/liquidity/realtime/health.py) (pure classifier + constants), `engine._health_loop` / `engine._write_health` (probes + heartbeat), table [`LiquidityRuntimeHealth`](../shared/kazus_db/models.py).

**Boundary statement (load-bearing).** This layer measures **where, within the instrumented runtime pipeline, forward progress stopped** — nothing else. It does **not** measure market state, metric correctness, or any business quantity. It asserts **only failure boundaries it can prove through instrumentation**; uninstrumented failure localization is prohibited (lip-governance §11). When the recorded signals are not conclusive it must emit `DOWNSTREAM_OF_INGEST_SUCCESS` (i.e. "unknown internal runtime failure boundary") rather than a precise but unprovable diagnosis.

## 1. Why this exists

A recurring pattern (`WS_RELIABILITY_001`, OPEN) was observed: Credible Depth, Exec Impact and Exec Validation stall **simultaneously**, the runtime correctly emits `DROPPED`, no fabrication occurs. The only defensible split with prior instrumentation was: *sequence gap → feed/network/ingest* vs *no gap + metrics stop → `DOWNSTREAM_OF_INGEST_SUCCESS`*. This layer adds the **minimal** telemetry to localize the downstream case further — without claiming more than the instruments prove.

This concerns **observability reliability, not metric correctness**. It re-opens nothing (Phase 2 / 3A / 3B remain closed).

## 2. Runtime topology being instrumented

`RealtimeEngine.run()` runs two coroutines on **one event loop**: `ws-reader` (`async for msg in ws.messages(): _on_frame(msg)`) and `ws-ticker` (reconcile / sample / status / **flush**). Two facts shape what is provable:

1. `_flush` is a **synchronous DB write inside the loop** — a slow commit blocks the whole loop (reader included). So a persistence stall and a total freeze are the same physical event unless separated by timing probes.
2. There is **no application-level queue** — `ws.messages()` yields straight from the socket; buffering lives inside the `websockets` library / OS and is **not app-observable**.

## 3. Stage probes (A) — additive, in-memory

Cheap marks set at existing stages (no new work):

| Probe | Set at | Proves |
|---|---|---|
| `frames_total` | end of `_on_frame` | reader is draining the socket |
| `last_ws_message_ms` | `_on_frame` (existing `_last_message_at`) | a frame crossed the instrumentation boundary (ingest success) |
| `last_sample_ms`, `samples_total` | end of `_sample_all` | consumer/sampler progressing |
| `flush_started_ms` / `flush_completed_ms` / `flush_duration_ms` / `flush_rows_total` | around the `_flush` DB write | persistence stage + its latency / in-flight state |

## 4. Heartbeat (B) — `_health_loop`

A third coroutine, fixed `HEALTH_INTERVAL_S = 5`. Each tick it measures `loop_lag_ms = max(0, actual_elapsed − interval)` (the only direct scheduler-starvation signal — if a blocking call hogs the loop, the heartbeat's own wake is delayed and the lag is recorded on the next tick), snapshots the probes, classifies, and **appends one row**. It is wrapped so a diagnostic failure can never disrupt ingestion (the measured system must not be endangered by its own telemetry). It is **not** awaited in the engine's `FIRST_COMPLETED` set — only cancelled on shutdown.

## 5. Persistence (C) — `liquidity_runtime_health`

Append-only, forward-only time-series (created via `create_all`). Columns: `ts, loop_lag_ms, last_ws_message_ms, frames_total, last_sample_ms, samples_total, flush_started_ms, flush_completed_ms, flush_duration_ms, flush_rows_total, conn_id, subscribed_count, failure_boundary, created_at`. The **persisted row is authoritative**: replay reads it, never recomputes the live runtime.

## 6. Classifier — pure, deterministic, replay-stable

`failure_boundary` is a **pure deterministic function of persisted numeric fields only** (`classify_failure_boundary` in `health.py`): given `(ts, subscribed_count, last_ws_message_ms, last_sample_ms, flush_started_ms, flush_completed_ms, flush_duration_ms, loop_lag_ms)` it returns exactly one label. Re-running it over a stored row yields the same label — that is the replay-determinism guarantee (the row carries every input).

Evaluation order (first match wins):

1. `subscribed_count ≤ 0` → **HEALTHY** (idle by design — demand-driven engine, nothing should be flowing).
2. `loop_lag_ms ≥ LOOP_LAG_HIGH_MS` (event loop starved): attribute to **PERSISTENCE_BOTTLENECK** *only if* flush activity explains the **majority** of the lag — `flush_contribution_ms ≥ PERSISTENCE_LAG_FRACTION × loop_lag_ms`, where `flush_contribution_ms` = in-flight elapsed (`ts − flush_started_ms`) if a flush is in-flight, else the last flush's `flush_duration_ms` when it completed within the blocked window (`ts − flush_completed_ms ≤ loop_lag_ms + HEALTH_INTERVAL_MS`), else 0. **Flush occurrence alone is insufficient.** Otherwise → **SCHEDULER_STARVATION** ("the loop was starved" — *not* "we know what starved it"; no attribution to CPU/parser/locks/DB/network).
3. frames silent (`ts − last_ws_message_ms ≥ MESSAGE_SILENCE_MS`), loop not starved → **FEED_NETWORK_SILENCE** (cannot sub-attribute Binance vs network vs socket vs ingest-read).
4. frames fresh, sampler stale (`ts − last_sample_ms ≥ SAMPLE_STALE_MS`), loop not starved → **CONSUMER_STALL**.
5. frames fresh, sampler fresh, loop not starved, no flush in-flight → **HEALTHY**.
6. otherwise → **DOWNSTREAM_OF_INGEST_SUCCESS** (ingest succeeded; finer boundary not provable — preferred over an incorrect precise diagnosis).

Interim thresholds (Class C, uncalibrated, diagnostic-only): `LOOP_LAG_HIGH_MS = 1000`, `MESSAGE_SILENCE_MS = 5000`, `SAMPLE_STALE_MS = 5000`, `PERSISTENCE_LAG_FRACTION = 0.5`.

## 7. Frozen enum

`FAILURE_BOUNDARIES = { HEALTHY, FEED_NETWORK_SILENCE, CONSUMER_STALL, PERSISTENCE_BOTTLENECK, SCHEDULER_STARVATION, DOWNSTREAM_OF_INGEST_SUCCESS }`. **Frozen.** Any finer label (e.g. distinguishing the blocking call, sub-attributing silence, or a queue-backlog state) requires a **separate governance review**.

## 8. Explicit non-claims (the governance core)

- **Queue backlog is NOT instrumentable** in this design (no app queue; `websockets`/OS buffering is invisible). It is deliberately left unmeasured and folds into `SCHEDULER_STARVATION` or `DOWNSTREAM_OF_INGEST_SUCCESS`. Making it observable would require inserting an explicit bounded queue — an architectural / behavior change, out of scope.
- **`FEED_NETWORK_SILENCE` does not sub-attribute** Binance vs network vs ingest-read.
- **`SCHEDULER_STARVATION` does not identify the blocking call.**
- No corrective action. This phase is **diagnostic-only**.

## 9. Governance

Class **B + E**, additive / append-only, **no behaviour dependence**: it only *reads* existing progress and *appends* a separate table. Zero change to metric computation, state taxonomies, or Phase 3A / 3B / `liquidity_samples` outputs. Permitted Observation-Period category: *operational tooling — telemetry for measuring frictions*. Forward-only, append-only, replay-deterministic (persisted row authoritative; classifier pure). Audit: [lip-governance §14](lip-governance.md) `2026-05-29-04`.
