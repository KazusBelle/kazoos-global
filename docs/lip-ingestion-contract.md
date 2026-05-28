# Ingestion Contract — audit companion

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md) §1 (Layer 1 LIQ Scanner, Layer 2 Realtime WS Engine), [`docs/lip-execution-validation.md`](lip-execution-validation.md) §8 / §9, [`docs/lip-governance.md`](lip-governance.md) §4 (replay stability), [`docs/lip-epistemic-boundaries.md`](lip-epistemic-boundaries.md).

**Status: Class A audit companion.** No code changes. Documents that the existing ingestion layer satisfies the Phase 1 contract (timestamping · append-only · explicit degradation · no silent interpolation · no synthetic reconstruction · no inferred liquidity) and enumerates the limitations that are honestly disclosed rather than silently present.

**Cross-cutting ontology invariant** (per [`lip-ontology-boundaries.md`](lip-ontology-boundaries.md)): the ingestion layer emits bounded observational measurements under current instrumentation constraints. It records the market state as seen by the instrumentation surface — not the market itself.

---

## 1. Boundary statement

The ingestion layer subscribes to a named, finite set of upstream sources, records every frame with a wall-clock timestamp, persists samples append-only with bounded retention, and exposes health state to downstream consumers. It does not interpolate, it does not synthesize, it does not infer absent liquidity. When upstream data is absent, stale, partial, or unknown, the layer makes that absence first-class.

---

## 2. Scope of ingestion

| Tier | Surface |
|---|---|
| **WebSocket consumers** | Binance Futures combined-stream over `wss://fstream.binance.com/stream`; `depth20`, `aggTrade` |
| **REST pollers** | Binance funding, mark/index, open interest, exchange info; Bybit v5 tickers |
| **Normalization** | Per-symbol state assembly into `SymbolState`; book snapshot freezing into `BookSnapshot`; trade tape into `state.trades`; book history ring (≤ 60 snapshots) |
| **Persistence** | Append-only history tables (`liquidity_samples`, `liquidity_alert_history`, `liquidity_intelligence_history`, `liquidity_crossex_history`, etc.) with bounded retention |
| **Health tracking** | `LiquidityWsStatus` single-row table updated on every reconcile |

Layers above the ingestion tier (metrics computation, alert engine, synthesis, propagation, regime, replay) consume the persisted output but do not modify it.

---

## 3. Source inventory

### 3.1 Binance Futures

| Source | Path | Cadence | Status |
|---|---|---|---|
| `depth20` (top-of-book / N price levels) | WebSocket combined-stream | sub-second (server-driven) | **Implemented**. Frames pushed into `SymbolState.bids/asks` + frozen into `BookSnapshot`; ring retention `_BOOK_HISTORY_MAX = 60` |
| `aggTrade` (trade tape) | WebSocket combined-stream | per-print | **Implemented**. Appended to `state.trades`; consumed forward-only by exec_impact via `state.exec_cursor_ts` |
| Funding rate | REST `/fapi/v1/premiumIndex` | `POLL_INTERVAL_S = 60 s` | **Implemented** in `metrics/funding.py` + `poller.py` |
| Mark / index | REST `/fapi/v1/premiumIndex` (same call) | 60 s | **Implemented** |
| Open interest | REST `/fapi/v1/openInterest` | 60 s | **Implemented** in `metrics/open_interest.py` |
| Exchange info / universe | REST `/fapi/v1/exchangeInfo` | startup + periodic | **Implemented** in `universe.py` |
| `@forceOrder` (liquidation stream) | WebSocket (per-symbol or aggregated) | server-driven | **Upstream-unavailable on this network.** Documented in [`liquidity/__init__.py:54-58`](../shared/kazus_logic/liquidity/__init__.py#L54-L58) and [`realtime/engine.py:15-18`](../shared/kazus_logic/liquidity/realtime/engine.py#L15-L18): Binance accepts SUBSCRIBE then delivers zero frames. Honest absence — see §7 |

### 3.2 Bybit

| Source | Path | Cadence | Status |
|---|---|---|---|
| V5 tickers (best bid/ask, funding, OI in one call) | REST `https://api.bybit.com/v5/market/tickers` | on-demand (per `/crossex/{symbol}` request) | **Implemented** in [`exchanges/bybit.py`](../shared/kazus_logic/liquidity/exchanges/bybit.py). Single HTTP request per symbol per call |

Bybit is **not deeply observed** — no WebSocket subscription, no per-frame book history. It exists as a cross-venue reference for `crossex` divergence per [lip-metric-registry §A.7](lip-metric-registry.md). Coverage asymmetry honestly documented per [lip-venue-quality §3](lip-venue-quality.md).

---

## 4. Phase 1 criteria — point-by-point evaluation

### 4.1 Every event timestamped

**PASS.**

| Surface | Timestamp field | Type |
|---|---|---|
| `liquidity_samples` | `ts` | `BigInteger NOT NULL` ([models.py:164](../shared/kazus_db/models.py#L164)) |
| `liquidity_alert_history` | `started_at_ms`, `last_seen_at_ms` | `BigInteger NOT NULL` ([models.py:267-268](../shared/kazus_db/models.py#L267)) |
| `liquidity_annotations` | `ts_ms` | `BigInteger NOT NULL` ([models.py:293](../shared/kazus_db/models.py#L293)) |
| `LiquidityWsStatus` | `last_message_at`, `updated_at` | `DateTime` ([models.py:229-232](../shared/kazus_db/models.py#L229)) |
| `BookSnapshot` (in-memory ring) | `ts` | wall-clock ms at frame ingest |
| `Trade` (tape) | `ts` | exchange-provided event time |
| `investigation_*` rows | `created_at_ms`, `ts_ms`, `captured_at_ms` | `BigInteger NOT NULL` |

Two timestamp domains coexist:

- **Exchange event time** — what the venue sent, used for ordering inside event streams.
- **Local receive time** — `time.time()` at the worker, used for staleness / window evaluation.

The platform does not assume these are identical. The "Cross-venue timestamp equality is structurally unavailable" invariant from [lip-venue-quality §17](lip-venue-quality.md) holds at the ingestion tier too.

### 4.2 Append-only

**PASS.**

| Table | Append-only mechanism |
|---|---|
| `liquidity_samples` | Insert-only; pruned by age via `prune_old(retention_days=35)` ([poller.py:172](../shared/kazus_logic/liquidity/poller.py#L172)) — bulk delete by age, never per-row update |
| `liquidity_alert_history` | Insert-only; 90 d retention |
| `liquidity_intelligence_history` | Insert-only; 90 d retention |
| `liquidity_crossex_history` | Insert-only; 90 d retention |
| `liquidity_anomaly_memory` | Insert-only; 180 d retention |
| `investigation_notes` | Insert-only; **NEVER edited or deleted** ([models.py:651-657](../shared/kazus_db/models.py#L651)) — corrections are follow-up notes |
| `investigation_events` | Insert-only ([models.py:733-757](../shared/kazus_db/models.py#L733)) |
| `investigation_replay_snapshots` | Revision-based append-only with `is_active` pointer ([models.py:707-714](../shared/kazus_db/models.py#L707)) |
| `LiquidityWsStatus` | Single-row UPSERT — convenience reflection of current state, not authoritative history (history lives in `last_message_at` over time-windowed reads of downstream tables) |

The only mutation paths in the ingestion → persistence flow are:

1. **Insert** new rows.
2. **Bulk delete by age** during retention prune.
3. **UPSERT** on the single-row `LiquidityWsStatus` health table (convenience field; not a history record).

No per-row updates exist on the history tables.

### 4.3 Missing data → explicit degradation

**PASS.**

The platform uses **named degradation states** at every layer where data may be absent or thin. Mapping to the four states requested by the Phase 1 contract:

| Phase 1 state | Implemented as |
|---|---|
| **STALE** | `LiquidityWsStatus.last_message_at` compared against `now()` by frontend; staleness threshold operator-tier. Per-symbol metrics fall to UNKNOWN when no recent frame |
| **GAP** | `book_history` ring eviction (≤ 60 snapshots) means events older than the ring are unreachable — exec_impact returns `DROPPED` on `pre_snapshot is None`. Frame loss during reconnect creates an effective gap; per [freeze §13 line 1064](2026-05-23-architecture-freeze.md), there is no diff-vs-rest reconciliation loop, so partial drift is silent for that window but recovered on next reconnect |
| **PARTIAL** | `EXHAUSTED` outcome in exec_impact when burst notional exceeds visible top-20 ([exec_impact.py:234-241](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L234)); `is_pruned=True` flag when upstream evidence has been retention-pruned but a link snapshot survives ([freeze line 233](2026-05-23-architecture-freeze.md)); `exploratory=True` flag for classifications under low data quality |
| **UNKNOWN** | `data_quality ∈ {INSUFFICIENT, LOW, MEDIUM, HIGH}` enum via `_discovery_quality()` ([research.py:5962](../shared/kazus_logic/liquidity/research.py#L5962)) — INSUFFICIENT/LOW propagate to `exploratory=True` and dampen all confidence; `UNKNOWN` is the documented initial state per [`lib/liquidityIntelligence.ts:26`](../frontend/src/lib/liquidityIntelligence.ts#L26) |

All four states are first-class outputs, never overloaded into a fabricated numeric value.

### 4.4 No silent interpolation

**PASS.** The codebase contains explicit anti-interpolation discipline:

| Location | Anti-interpolation statement |
|---|---|
| [exec_impact.py:29](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L29) | "if either is missing the event is **dropped honestly** rather than approximated" |
| [exec_impact.py:145](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L145) | "caller should wait, **not approximate**" |
| [exec_impact.py:279](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L279) | "Empty buckets are simply not reported — the layer **never writes fabricated zeros**" |
| [derived.py:59](../shared/kazus_logic/liquidity/metrics/derived.py#L59) | "target_ts AND within tol_ms — otherwise None (**don't fabricate a delta**)" |
| [engine.py:23](../shared/kazus_logic/liquidity/realtime/engine.py#L23) | "operator surfaces **never paint a fabricated value**" |
| [__init__.py:57](../shared/kazus_logic/liquidity/__init__.py#L57) | "instead of a **fabricated constant 0.0**" |

Two cases that look like smoothing but are not silent interpolation:

- **Wilder's smoothed ATR** ([metrics/atr_liquidity.py:27](../shared/kazus_logic/liquidity/metrics/atr_liquidity.py#L27)) — named technical method, documented, deterministic, audit-traceable.
- **DB-load smoothing** ([runner.py:392](../worker/app/runner.py#L392), [runner.py:506](../worker/app/runner.py#L506)) — refers to **polling cadence** distribution across symbols, not to data-value interpolation. The polling stagger smooths database write load; the values written are raw.

### 4.5 No synthetic reconstruction

**PASS.** Documented across multiple companions:

| Invariant | Source |
|---|---|
| Forward-only cursor advancement | [exec_impact.py:201](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L201) — cursor advances on emit OR drop; same burst never re-evaluated |
| Pre-activation events structurally unreconstructable | [lip-execution-validation §9](lip-execution-validation.md) — L2 depth not persisted to disk |
| Replay reconstructs persisted emit, not market reality | [lip-ontology-boundaries §6](lip-ontology-boundaries.md), [lip-regime-engine §23](lip-regime-engine.md), [lip-causal-propagation §5](lip-causal-propagation.md) |
| `book_history` ring eviction not synthesized | [orderbook.py:155-156](../shared/kazus_logic/liquidity/realtime/orderbook.py#L155) — `popleft()`, no shadow buffer |
| No backfill of any kind | Multiple — [lip-execution-validation §9 (6 NEVER-rules)](lip-execution-validation.md) |

### 4.6 No inferred liquidity

**PASS.**

- `book_exhausted` flag honestly reports "burst notional did not fit visible top-20" ([exec_impact.py:234-241](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L234)). When `_walk` exhausts visible levels, returns `(None, True)` — does not invent depth beyond the snapshot.
- Hidden / iceberg / RPI / OTC / queue priority explicitly enumerated as blind spots — [lip-execution-validation §10](lip-execution-validation.md), [lip-epistemic-boundaries §2 / §5](lip-epistemic-boundaries.md).
- "Spoof saturation regimes" documented per [freeze line 1076](2026-05-23-architecture-freeze.md): when > 50% of displayed depth is sub-400-ms quote flicker, Credible Depth correctly reports near-zero — the metric *measures the depletion*, not an inferred deeper book.

### 4.7 Replay determinism at the ingestion tier

**PASS.**

The ingestion tier holds the property that **same persisted inputs at the same code + same configuration produce the same downstream output**. This is the load-bearing condition for everything in [lip-governance §4 Replay Stability Contract](lip-governance.md); below is its ingestion-side manifestation.

| Property | Mechanism |
|---|---|
| Forward-only cursor advancement | `state.exec_cursor_ts` is monotonic ([exec_impact.py:201](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L201)). Once advanced past a burst (emit or drop), the burst is never re-evaluated. Replay over the same trade tape from the same cursor position produces the same `ExecEvent` set |
| Append-only persistence | §4.2 above. Re-reading `liquidity_samples` / `liquidity_alert_history` / `liquidity_intelligence_history` for a window with `ts ≤ as_of` returns the same rows on every read until retention prune crosses the window |
| Deterministic walk arithmetic | `_walk` over price-sorted pre-snapshot levels is deterministic for a fixed `target_qty` ([exec_impact.py:104-127](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L104)) |
| Deterministic snapshot lookup | `_find_pre_snapshot` / `_find_post_snapshot` are pure functions of `(book_history, target_ts)` — no randomness, no wall-clock dependency at decision time |
| No hidden writer-side mutation | Once a sample is written, the row is not modified. Subsequent reads at any `as_of ≥ ts_ms` see the same value |

**Determinism boundaries (not violations — explicit bounds):**

- **Past the `book_history` ring** (≤ 60 snapshots): per-burst `expected_bps` becomes unavailable, not non-deterministic. Replay-availability state = `INSUFFICIENT_PRE_EVENT_STATE` per [lip-execution-validation §21](lip-execution-validation.md).
- **Past retention** (35–90 d depending on table): rows are absent, not different. Replay-availability state = `REPLAY_NOT_PERSISTED`.
- **Across calibration changes when `calibration_version` is unstamped** (platform-wide gap per [lip-governance §8](lip-governance.md)): replay across the boundary uses current thresholds. This is **acknowledged governance debt**, not silent non-determinism — the boundary is undetected because version stamping is NOT IMPLEMENTED, not because behavior is hidden.

**Invariant.** Determinism holds **within** the retained, version-stable window. Outside that window, the system returns absence (UNKNOWN / REPLAY_NOT_PERSISTED / INSUFFICIENT_PRE_EVENT_STATE), not a different answer.

### 4.8 No hidden fallback logic

**PASS.**

When an input is missing, the corresponding output is **missing** — not a default, not a best-effort estimate, not a stale substitute, not a nearest-neighbor approximation.

| Surface | Behavior on missing input | Anti-fallback evidence |
|---|---|---|
| `_walk` exhausts visible levels | Returns `(None, True)` ([exec_impact.py:126](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L126)). No partial vwap estimate, no extrapolation from the deepest level reached | Explicit `return None, True` |
| `_find_pre_snapshot` finds none | Returns `None` ([exec_impact.py:140](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L140)). No nearest-neighbor approximation, no "closest snapshot wins" | Returns `None` not a candidate snapshot |
| `_find_post_snapshot` not yet produced | Returns `None` ([exec_impact.py:149](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L149)). Comment: "caller should wait, **not approximate**" | Docstring is explicit |
| `_measure` sees `pre is None or post is None` | Returns `None` immediately ([exec_impact.py:220](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L220)). No fallback to "use whichever snapshot we have" | Short-circuit guard |
| `_measure` sees `pre.mid ≤ 0 or post.mid ≤ 0` | Returns `None` ([exec_impact.py:222](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L222)). No "treat as small positive" |  Short-circuit guard |
| `rolling_exec_metrics` empty bucket | Bucket is **simply not reported** ([exec_impact.py:278-279](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L278)). Comment: "the layer **never writes fabricated zeros**" | Sparse emit |
| `_derived` delta computation | Returns `None` when target is missing or stale beyond tolerance ([metrics/derived.py:59](../shared/kazus_logic/liquidity/metrics/derived.py#L59)). Comment: "**don't fabricate a delta**" | Returns `None` |
| Bybit fetch failure in `crossex` | Returns `None`; response carries only successful venues per [freeze line 1057](2026-05-23-architecture-freeze.md). Failed venues drop out **silently** — see §7 limitation #4 for explicit-flag gap | `try/except` → `return None`, no substitution |
| Trade tape with no new prints | `detect_and_measure_bursts` returns `[]` ([exec_impact.py:166](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L166)). No synthesized "no-burst" record |  Empty list, not a placeholder |
| `synthesize_intelligence` upstream score NULL | Defaults to `0.0` in slope arithmetic ([research.py:6010](../shared/kazus_logic/liquidity/research.py#L6010)) but `meta_conf_factor` is then floored at `0.2` in confidence formula. This is **bounded substitution**, not fallback: the floor is documented in [lip-regime-engine §11](lip-regime-engine.md), the resulting confidence is dampened, the input absence is reflected in the output |
| Polling cycle skipped (REST timeout) | Sample is not written for that cycle. Next cycle writes its own sample. **No back-fill of the skipped cycle** |

**The one bounded substitution** (NULL → 0.0 for `synthesized_stress` slope) is explicitly documented, dampens confidence proportionally, and surfaces in the emit. It is not a hidden path.

**Invariant.** Absence propagates. The ingestion tier does not have a hidden code path that converts missing input into a fabricated output. Every substitution that exists is named, documented, and surfaces in the downstream confidence calculation.

---

## 5. Health-state tracking

**PASS.**

| Surface | Field | Behavior |
|---|---|---|
| Connection generation | `FuturesWsClient.conn_id` ([ws_client.py:46](../shared/kazus_logic/liquidity/realtime/ws_client.py#L46)) | Monotonic counter; bumps on every successful reconnect. Consumers compare against remembered value to know when to re-issue SUBSCRIBE |
| Persisted health | `LiquidityWsStatus` ([models.py:214-232](../shared/kazus_db/models.py#L214)) | Single-row UPSERT each reconcile: `conn_id`, `connected`, `subscribed_json`, `last_message_at`, `updated_at` |
| Reconnect policy | `ws_client.py` constants | `PING_INTERVAL_S = 30`, `RECONNECT_MIN_S = 1.0`, `RECONNECT_MAX_S = 30.0` — exponential backoff with jitter |
| Subscription state authority | `SubscriptionManager` in-memory desired-set | After reconnect, manager re-issues SUBSCRIBE for currently-desired streams. Client deliberately does **not** track its own subscription state across reconnects — single source of truth |

**Frontend / operator visibility:** the worker writes `last_message_at` on every reconcile; the frontend compares against `now()` to render live / stale / reconnect badges per [models.py:215-220](../shared/kazus_db/models.py#L215).

---

## 6. Reconnect discipline

| Property | Implementation |
|---|---|
| Automatic reconnect | Yes, in `FuturesWsClient` |
| Exponential backoff | `RECONNECT_MIN_S = 1.0`, `RECONNECT_MAX_S = 30.0` ([ws_client.py:33-34](../shared/kazus_logic/liquidity/realtime/ws_client.py#L33)) |
| Jitter | Yes — `random` import in module ([ws_client.py:23](../shared/kazus_logic/liquidity/realtime/ws_client.py#L23)) |
| Connection counter | `conn_id` monotonic; bumps on every successful reconnect |
| Subscription replay after reconnect | Yes — `SubscriptionManager` re-issues SUBSCRIBE for desired-set; client does not remember subscriptions across reconnects (single source of truth) |
| Health visibility during reconnect | `LiquidityWsStatus.connected = False` until first frame after reconnect succeeds |

---

## 7. Known limitations — honestly disclosed

The platform's discipline is to **document** what it cannot do, not to hide it. The following limitations exist at the ingestion tier and are surfaced at the documentation tier:

| Limitation | Source | Operational consequence |
|---|---|---|
| **Binance `@forceOrder` unavailable on this network** | [__init__.py:54-58](../shared/kazus_logic/liquidity/__init__.py#L54), [engine.py:15-18](../shared/kazus_logic/liquidity/realtime/engine.py#L15) | Liquidation stream subscribed but delivers zero frames. Platform omits the liquidation-stream-derived signals **honestly**. Re-enable when upstream feed becomes available |
| **WS desync (server-side `lastUpdateId` skip)** | [freeze §13 line 1064](2026-05-23-architecture-freeze.md) | No diff-vs-rest reconciliation loop. `SymbolState.bids/asks` may drift from venue state until reconnect. Documented; no auto-mitigation today |
| **Timestamp drift (local clock vs venue clock)** | [freeze §13 line 1065](2026-05-23-architecture-freeze.md) | All age calculations use `time.time()` locally. A drifting host clock biases Credible Depth's 400 ms persistence floor. No NTP enforcement at the app layer |
| **Venue outage > 30 s** | [freeze §13 line 1066](2026-05-23-architecture-freeze.md) | WS reconnect keeps trying; if Bybit is down, `crossex` simply omits it without flagging which venues are missing in the divergence response |
| **L2 depth not persisted to disk** | [lip-execution-validation §8](lip-execution-validation.md), [exec_impact.py docstring](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L34) | Per-burst replay is structurally unavailable past the in-memory ring (≤ 60 snapshots). Only per-event median samples persist to `liquidity_samples` |
| **`calibration_version` not stamped on persisted rows** | [lip-governance §8](lip-governance.md), [lip-validation-and-calibration §5](lip-validation-and-calibration.md) | Platform-wide governance debt. Threshold changes create undetectable boundaries in historical interpretation until version stamping is implemented (Class B + Class E, NOT AUTHORIZED during Observation Period) |
| **Bybit deep observation absent** | [lip-venue-quality §3](lip-venue-quality.md) | Only REST tickers (single call per symbol). No WebSocket subscription, no per-frame book history. Structural coverage asymmetry |
| **Cross-venue clock alignment** | [lip-venue-quality §17](lip-venue-quality.md) | Structurally unavailable. Binance and Bybit do not share a clock. Cross-venue temporal claims are approximate observational comparisons |

Each limitation is **named, located, and documented**. None is silently present.

---

## 8. Compliance verdict

| Phase 1 criterion | Status |
|---|---|
| Every event timestamped | **PASS** |
| Append-only data | **PASS** |
| Missing data → STALE / GAP / PARTIAL / UNKNOWN | **PASS** (mapped to implemented enum set) |
| No silent interpolation | **PASS** (explicit anti-interpolation discipline in code comments) |
| No synthetic reconstruction | **PASS** (forward-only invariant; no backfill; ring eviction not synthesized) |
| No inferred liquidity | **PASS** (`book_exhausted` honest flag; blind-spot inventory explicit) |
| Replay determinism at ingestion tier | **PASS** (forward-only cursor + append-only persistence + pure-function snapshot lookup; bounded by ring eviction / retention / version-stamping gap) |
| No hidden fallback logic | **PASS** (absence propagates; one bounded NULL→0.0 substitution documented and reflected in dampened confidence) |
| WebSocket consumers | **IMPLEMENTED** (`FuturesWsClient` + combined-stream) |
| REST pollers | **IMPLEMENTED** (`poller.py` 60-s cadence; per-source modules) |
| Normalization layer | **IMPLEMENTED** (`SymbolState`, `BookSnapshot`, book history ring) |
| Timestamp discipline | **IMPLEMENTED** (dual domain: exchange event time + local receive time; both first-class) |
| Persistence pipeline | **IMPLEMENTED** (append-only tables; bounded retention; UPSERT only on convenience reflection rows) |
| Health-state tracking | **IMPLEMENTED** (`LiquidityWsStatus` + `conn_id` monotonic counter) |

**Overall: the ingestion layer satisfies the Phase 1 contract.** Limitations are documented, not hidden. No code changes are required; this companion records the audit.

---

## 9. Patch-list (for operator review, NOT executing)

The criteria above are met. The following are **observed gaps in disclosure or instrumentation**, not violations of the contract. They are listed for operator awareness and prioritization, not for immediate implementation. All would be Class B (measurement / emit) or Class B+E (persistence) per [lip-governance §2](lip-governance.md), **NOT AUTHORIZED during Operational Observation Period**.

| # | Gap | Class if implemented | Authorization required |
|---|---|---|---|
| 1 | Per-row `calibration_version` / `schema_version` stamping on persisted samples | Class B + Class E (platform-wide) | Observation Period exit + governance event with audit-trail per [lip-governance §10](lip-governance.md) |
| 2 | Diff-vs-REST reconciliation loop for WS desync detection (`lastUpdateId` skip) | Class B | Same |
| 3 | Timestamp-drift detector (host clock vs venue clock divergence threshold) | Class B | Same |
| 4 | Explicit `venue_outage` flag on `crossex` response (vs current silent venue-omit) | Class B (surface change) | Same |
| 5 | `@forceOrder` re-enable check (worker probes upstream periodically) | Class B (passive — no behavior change today since stream delivers 0 frames anyway) | Same |
| 6 | Persisted `LiquidityWsStatus` history (vs current single-row UPSERT) — would enable longitudinal connection-health analysis | Class B + Class E | Same |

None of the above is **required** for Phase 1 compliance. Each is a future-tier hardening item, ranked by operator preference, not by audit-failure pressure.

---

## 10. Governance classification

| Aspect | Status |
|---|---|
| **This document** | Class A (audit-only documentation) per [lip-governance §2](lip-governance.md). Authorized during Observation Period |
| **Implementation of any patch-list item** | Class B / B+E. NOT AUTHORIZED today |
| **Renaming any ingestion-tier emit / field** | Class B (semantic relabeling). Would require synchronous companion update |
| **Maturity stage of ingestion layer** | **Operational**. Has run continuously for 11+ days at audit time. Promotion to Validated-operational per [lip-governance §9](lip-governance.md) gated on calibration-version stamping (gap #1 above) |

---

## 11. What this document is not

- Not a code change.
- Not a new ingestion layer specification.
- Not authorization to refactor existing ingestion.
- Not a recommendation to enable `@forceOrder` (upstream availability gates that).
- Not a remediation plan for the patch-list items.
- Not an exhaustive code review.
- Not a performance audit.
- Not a security audit.

It is an audit companion that records, in the language of the Phase 1 contract, that the existing ingestion layer satisfies the contract today and enumerates the limitations that are honestly disclosed at the documentation tier rather than silently present in the runtime.
