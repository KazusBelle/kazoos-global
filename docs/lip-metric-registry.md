# LIP Metric Registry

**Canonical source for every published metric in the Liquidity Intelligence Platform.**

Extracted from `docs/2026-05-23-architecture-freeze.md` on 2026-05-26 (Option-A documentation decomposition pass). The freeze document remains the snapshot at freeze time; this registry is the living per-metric reference from this point forward.

Cross-doc back-references:
- **Foundational contracts** (data_quality semantics, refusal-first ordering, append-only persistence, UNKNOWN as first-class verdict): `docs/2026-05-23-architecture-freeze.md` §8 invariants.
- **Failure modes / blind spots** per metric: `docs/2026-05-23-architecture-freeze.md` §10.
- **Validation / calibration status** per metric: `docs/lip-validation-and-calibration.md`.
- **Epistemic ceiling** (what each metric is licensed to claim): `docs/lip-epistemic-boundaries.md`.
- **Operator surfacing** of metrics: `docs/2026-05-23-architecture-freeze.md` §6.

---

## Part A — Realtime tier (1 Hz against WS book + tape)

Per-symbol metrics computed in [`shared/kazus_logic/liquidity/realtime/`](../shared/kazus_logic/liquidity/realtime/) and written to `liquidity_samples` under the metric names below. Each entry uses the same 7-field contract: Purpose · Inputs · Formula · Threshold · Failure conditions · Replay behavior · Validation constraints.

### A.1 Credible Depth (`credible_depth`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/metrics.py:56`](../shared/kazus_logic/liquidity/realtime/metrics.py#L56) `credible_depth_usd()` |
| Purpose | USD value of resting orderbook liquidity around mid that has demonstrated **persistence** — defeats spoof flicker by ignoring quotes younger than a minimum age |
| Inputs | `state.bids` / `state.asks` (top-20 WS book), each level keyed by price with `(qty, first_ts)` first-appearance timestamp; `state.mid_price()` |
| Formula | `band = mid × (1 ± CREDIBLE_BAND_PCT)` with `CREDIBLE_BAND_PCT = 0.005` (±0.5%). For each side, sum `price × qty` over levels where `price ∈ band` **AND** `(now_ms − first_ts) ≥ CREDIBLE_MIN_AGE_MS = 400`. Quotes younger than 400 ms contribute **zero** |
| Threshold | Reported as raw USD. Higher = more genuine resting liquidity at the touch |
| Failure conditions | `mid_price()` returns None → metric returns None (no fabricated value). Empty book → None. Per-symbol thresholds for "low credible depth" are not centralized — read via the per-symbol percentile context the operator pulls from `/metrics/{symbol}` |
| Replay behavior | **Not reconstructible from history**: the metric depends on per-level `first_ts` which is only held in memory in `SymbolState`. Historical samples carry the computed value, not the inputs. Replay tier uses the persisted `liquidity_samples` row as authoritative |
| Validation constraints | The 400 ms persistence floor is the anti-spoof primitive. Lowering it weakens the metric's core property; raising it makes the metric blind to near-touch state that lived shorter than the floor. Any change must be paired with a recalibration against persistence-labelled samples — currently not measured (see [validation-and-calibration](lip-validation-and-calibration.md)) |

### A.1a Credible Depth — per-side decomposition (`credible_bid_depth` · `credible_ask_depth` · `credible_depth_delta`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/metrics.py`](../shared/kazus_logic/liquidity/realtime/metrics.py) `credible_depth_sides()` (single survivorship walk), with `credible_bid_depth_usd()` / `credible_ask_depth_usd()` / `credible_depth_delta_usd()` thin wrappers |
| Purpose | Decomposition of [A.1 Credible Depth](#a1-credible-depth-credible_depth) into the per-side persistent visible liquidity (`credible_bid_depth`, `credible_ask_depth`) and the **observable imbalance** between them (`credible_depth_delta = bid − ask`). No new instrumentation, data source, or filter — same ±0.5% band and same 400 ms survivorship floor, reported per side instead of summed |
| Inputs | Identical to A.1: `state.bids` / `state.asks` `(qty, first_ts)` levels and `state.mid_price()`. All three outputs and the combined `credible_depth` come from one `credible_depth_sides()` walk per tick, so the four cannot drift apart |
| Formula | `(bid_usd, ask_usd)` = per-side sums of `price × qty` over in-band levels with `(now_ms − first_ts) ≥ 400`. `credible_depth_delta = bid_usd − ask_usd` (USD; sign convention: **positive → persistent visible liquidity leans to the bid side**). `credible_depth = bid_usd + ask_usd` (unchanged) |
| Threshold | Raw USD. `credible_depth_delta` is a descriptive observable imbalance only — **not** a directional signal, a structural-irregularity verdict, or an executable-liquidity estimate |
| Failure conditions | `mid_price()` is None → all three outputs are **None** (UNKNOWN), propagated uniformly with `credible_depth`. Mid known but a side has no surviving level → that side is `0.0` — an *observed* absence of persistent visible liquidity, which is **distinct from UNKNOWN** and must not be conflated downstream |
| Replay behavior | Same as A.1: not reconstructible from history (depends on in-memory `first_ts`); the persisted `liquidity_samples` rows are authoritative. Emitted as **dense** rows every tick (None when UNKNOWN); no interpolation, append-only |
| Validation constraints | Inherits A.1's 400 ms / ±0.5% calibration debt — these outputs add no new threshold. `credible_depth_delta` magnitude is uncalibrated raw USD with no per-symbol baseline; comparison across symbols is not meaningful without one |

### A.1b Persistence Quality (`persistence_quality`)

Measurement-quality self-assessment for Credible Depth. Full design contract: [`lip-credible-depth-persistence.md`](lip-credible-depth-persistence.md).

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/metrics.py`](../shared/kazus_logic/liquidity/realtime/metrics.py) `persistence_quality()` |
| Purpose | Grades **how well Credible Depth could be measured at this tick** from the snapshot sequence — completeness, gaps, freshness of the depth20 frames. It describes the *measurement*, never the market: no direction, no event probability, and **no manipulation / spoof / fake-liquidity / executable-liquidity verdict** |
| Inputs | `state.book_history` (frozen depth20 frame timestamps, pushed every frame in `apply_depth20`) and `state.mid_price()`. No new data source — purely existing ingestion state |
| Formula | Over the last `PQ_WINDOW_MS = 5_000` ms of in-window frames: `freshness = ramp(now − latest_ts, 100, PQ_STALE_MS=1_000)`; `coverage = min(frames_in_window / (PQ_WINDOW_MS / PQ_FRAME_INTERVAL_MS=100), 1)`; `continuity = ramp(max_inter_frame_gap, 100, PQ_MAX_GAP_MS=1_000)` where `ramp(x, good, bad)` is 1 below `good`, 0 above `bad`, linear between. Output = `freshness · continuity · coverage ∈ [0, 1]`. Multiplicative: any axis collapsing to 0 (stale book / a ≥ 1 s gap) zeroes the quality — the gate under-claims rather than over-claims |
| Threshold | Raw [0, 1] score; higher = better-measured. Grade bands are interim and uncalibrated. Read relative to a per-symbol baseline, not as an absolute |
| Failure conditions | `mid_price()` is None → **None (UNKNOWN)** — quality of an unmeasurable Credible Depth is itself UNKNOWN. `< PQ_MIN_FRAMES = 10` frames in window → **None (INSUFFICIENT)**. A *measured* degradation returns a low float (e.g. `0.0`), which is **distinct from None**: `0.0` = "measured, and bad"; `None` = "could not measure / not enough to judge". At this tier both UNKNOWN and INSUFFICIENT are represented as `None` (no score); the distinguishing reason is not separately persisted, consistent with `resiliency_score`'s no-events → None |
| Replay behavior | Like A.1: depends on in-memory frame timestamps; the persisted `liquidity_samples` row is authoritative for replay. Pure function of `(state, now_ms)` — deterministic, no interpolation, no hidden fallback. Emitted as a **dense** row every tick (None when UNKNOWN/INSUFFICIENT); additive and append-only |
| Validation constraints | `PQ_FRAME_INTERVAL_MS = 100` (the `@depth20@100ms` cadence) is the load-bearing assumption — if real arrival cadence differs, `coverage` mis-reads. It and the four other interim constants (`PQ_WINDOW_MS`, `PQ_MIN_FRAMES`, `PQ_STALE_MS`, `PQ_MAX_GAP_MS`) are uncalibrated and constitute a Class C item against observed inter-arrival distributions (see [validation-and-calibration](lip-validation-and-calibration.md)). Governance: authorized additive Class B+E change, [lip-governance §14](lip-governance.md) entry 2026-05-29-01 |

### A.2 Resiliency Score (`resiliency_score`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/intelligence.py:194`](../shared/kazus_logic/liquidity/realtime/intelligence.py#L194) `resiliency_score()` |
| Purpose | 0..100 measure of how the book recovers after a stress event. Higher = faster + more complete refill |
| Inputs | `state.events` list of `RecoveryEvent` records produced by `_detect_events`: kinds ∈ {`liq_spike`, `spread_explosion`, `depth_collapse`, `obi_flip`}, each with `pre_depth`, `started_ts`, `recovered_ts`, `recovery_ms`, `refill_velocity`. Event detection is debounced by `EVENT_DEBOUNCE_MS = 10_000` |
| Formula | For each completed event (i.e. `recovery_ms is not None`): `time_part = 100 × exp(−recovery_ms / 30_000)`; `velo_part = 50 × tanh(refill_velocity / 50_000) + 50`; event-score = `0.6·time_part + 0.4·velo_part`. Aggregate: exp-weighted by event age with **5-min half-life** (`weight = exp(−age_s / 300)`). Output clipped to [0, 100] |
| Threshold | Recovery defined as depth climbing back to `RECOVERY_FRACTION × pre_depth = 0.80 × pre`. Events past `RECOVERY_MAX_AGE_MS = 90_000` are marked "did not recover" (recovery_ms = 90s, refill_velocity = 0) — not silently dropped |
| Failure conditions | No completed events → returns **None**, not a fabricated 100. Per-event `pre_depth ≤ 0` → event skipped from recovery advancement. Operator-visible column shows "—" when None |
| Replay behavior | Reconstructible only from `liquidity_samples` (the persisted score); the in-memory `RecoveryEvent` ring is not on disk |
| Validation constraints | Three event-detection thresholds are load-bearing: `DEPTH_COLLAPSE_DROP = 0.40` (40% drop in 10s), `SPREAD_EXPLOSION_BPS = 8.0`, `LIQ_SPIKE_USD = 50_000`. All three are absolute (not per-symbol percentiles) and are interim values that should be recalibrated per-symbol — currently not measured |

### A.3 Impact Score (`impact_score`) — Kyle λ sigmoid

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/intelligence.py:270`](../shared/kazus_logic/liquidity/realtime/intelligence.py#L270) `impact_score()` |
| Purpose | 0..100 measure of price sensitivity to signed flow — the **Kyle Lambda** classical microstructure quantity, sigmoid-normalized for display |
| Inputs | `state.trades` over the last `KYLE_WINDOW_MS = 60_000` ms |
| Formula | Bucket trades into `KYLE_BUCKET_MS = 1_000` ms windows. For each bucket: `signed_usd = signed_qty × first_price`; skip if `|signed_usd| < KYLE_MIN_VOLUME_USD = 100`; `ret = (last_p − first_p) / first_p`; `λ = |ret| / |signed_usd| × 1e9`. Take **median** over buckets for robustness. Output: `100 / (1 + exp(−(λ − 1)))` — sigmoid centred at λ = 1 |
| Threshold | λ = 1 ⇒ score ≈ 50 ("typical mid-cap perp under normal flow") per code comment. No hard verdict thresholds — the metric is published raw |
| Failure conditions | `< KYLE_MIN_BUCKETS = 8` filled buckets → returns **None**. Zero or negative prices → bucket skipped. Negligible flow → bucket skipped |
| Replay behavior | Persisted to `liquidity_samples`; the underlying tape is not retained at 1-second granularity past the 60s window |
| Validation constraints | The "λ = 1 → 50" anchor is a documentation claim, not a calibration result — it has not been measured against a labelled corpus of "normal" vs "stressed" flow. Re-anchoring requires a per-symbol baseline |

### A.4 Fragility Score (`fragility_score`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/intelligence.py:279`](../shared/kazus_logic/liquidity/realtime/intelligence.py#L279) `fragility_score()` |
| Purpose | 0..100 measure of **price-impact instability** — high variance of bucket λs means the relationship between flow and price is unstable, regardless of its central level |
| Inputs | Same bucket λs as Impact Score |
| Formula | `mean = Σλ / N`; `var = Σ(λ − mean)² / N`; `std = √var`; `cv = std / mean if mean > 1e-12 else 0`; output = `clip(cv × 50, 0, 100)`. CV ≥ 2 ⇒ score = 100 ("very fragile") |
| Threshold | No hard verdict — published raw. CV in [0, 2] maps linearly to [0, 100] |
| Failure conditions | `< KYLE_MIN_BUCKETS = 8` filled buckets → **None**. Mean λ near zero → CV forced to 0 (rather than div-by-zero) |
| Replay behavior | Persisted to `liquidity_samples` |
| Validation constraints | The `cv × 50` scaling and the "CV ≥ 2 = fragile" anchor are interim. Calibration requires labelling regimes of known fragility, not yet collected |

### A.5 Realized vs Predicted Impact (`exec_impact`)

> **Burst Detection (PHASE 3A) — `liquidity_bursts` table, not `liquidity_samples`.** The burst (a temporally clustered same-side `@trade` run, gap ≤ 250 ms, sliding window) is the unit this layer measures; PHASE 3A emits each settled burst as a standalone append-only record (`burst_start_ts · burst_end_ts · burst_duration_ms · burst_trade_count · burst_notional · burst_side`) plus explicit refusal markers (UNKNOWN/INSUFFICIENT/DROPPED). Burst boundaries are the **single shared definition** (`burst.iter_settled_bursts`) consumed by both burst records and `exec_impact`. Full contract → [`docs/lip-burst-detection.md`](lip-burst-detection.md). Not a liquidity_samples scalar — lives in its own table; replay-deterministic, forward-only.

> **Per-burst Execution Validation records (PHASE 3B) — `liquidity_exec_validation` table.** In addition to the rolling medians below, each settled burst now persists one append-only row: `execution_validation_state ∈ {MEASURED·EXHAUSTED·INSUFFICIENT·DROPPED·UNKNOWN}` (frozen set), `expected_impact_bps · realized_impact_bps · divergence_bps · divergence_label (POSITIVE/NEGATIVE_DIVERGENCE, sign only) · exhaustion_state`, over the **shared** burst boundaries (no second grouping). Reuses the single `evaluate_burst` measurement core. Full contract → [`docs/lip-execution-validation.md`](lip-execution-validation.md) §4.

**Full execution-validation contract → [`docs/lip-execution-validation.md`](lip-execution-validation.md).** The table below is the registry-tier summary; semantics, blind-spot inventory, vocabulary discipline, precedence ordering, and the per-burst outcome enum live in the companion. This row deliberately does not duplicate them.

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/exec_impact.py`](../shared/kazus_logic/liquidity/realtime/exec_impact.py) |
| Purpose | **Forward-only measurement** of how trade bursts actually move the market vs. what book-walk on the visible top-20 predicted. Per memory `project_exec_impact_layer`: pure observation mode, downstream not calibrated |
| Inputs | Consecutive same-side taker prints with gap ≤ `BURST_GAP_MS = 250` (one burst); pre-burst book snapshot from `state.book_history` ring; post-settle mid `SETTLE_MS = 500` ms after the burst's last print |
| Formula | Per burst: `expected_bps` = book-walk impact computed over pre-burst top-20 in the taker's direction; `realized_bps` = signed mid move (pre → post-settle); `divergence_bps = realized_bps − expected_bps`; `ratio = realized_bps / expected_bps` published **only when `|expected_bps| ≥ EXPECTED_FLOOR_BPS = 0.5`**, otherwise None. Bursts bucketed by notional: S < `BUCKET_M_USD = 50_000` ≤ M < `BUCKET_L_USD = 500_000` ≤ L |
| Threshold | No verdict — four numbers per ExecEvent + a `book_exhausted` flag. When `book_exhausted = True` (burst notional exceeded visible top-20), `expected_bps / divergence / ratio` are **None**, but the burst still counts under `exec_book_exhausted` |
| Failure conditions | Burst < `NOTIONAL_FLOOR_USD = 5_000` → skipped. Missing pre or post book snapshot → event **dropped** (not approximated). `expected_bps` below noise floor → ratio = None |
| Replay behavior | **Strictly forward-only**. L2 book state is not persisted to disk, so historical bursts before the layer activated are structurally unmeasurable. Published per-(side, bucket) rolling medians over `EVENT_WINDOW_MS = 5 × 60 × 1000` ms |
| Validation constraints | This layer is the only direct empirical test of [A.1 Credible Depth](#a1-credible-depth-credible_depth)'s anti-spoof claim — if `realized_bps ≫ expected_bps` while `book_exhausted = False`, the book promised liquidity that did not materialize. Aggregating that relationship over time is in the [validation backlog](lip-validation-and-calibration.md) |

### A.6 Liquidation stress (`liq_stress`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/metrics.py:80`](../shared/kazus_logic/liquidity/realtime/metrics.py#L80) `liquidation_stress_usd()` |
| Purpose | Rolling USD value of forced liquidations — cascade indicator |
| Inputs | `state.liquidations` (WS `@forceOrder` feed — note: switched from dead `forceOrder` stream in commit 5c4acbc) |
| Formula | Sum of `price × qty` over liquidations within `LIQ_WINDOW_MS = 60_000` ms |
| Threshold | Raw USD, no verdict |
| Failure conditions | No liquidations in window → 0.0 (not None — absence of stress is a valid measurement) |
| Replay behavior | Persisted to `liquidity_samples` |
| Validation constraints | Threshold for "cascade conditions" is symbol-dependent; not centrally calibrated |

### A.7 Cross-venue divergence (`crossex`)

| | |
|---|---|
| Code | [`backend/app/api/liquidity.py:465`](../backend/app/api/liquidity.py#L465) `CrossExDivergence` |
| Purpose | Pairwise divergence vs the reference exchange (Binance). Sustained price separation across major venues is rare and usually indicates a venue-side issue |
| Inputs | Per-exchange snapshot from `exchanges.REGISTRY` (currently Binance + Bybit): `funding_rate`, `open_interest_usd`, `spread_fraction`, `mid_price` |
| Formula | For each non-reference exchange: `funding_diff = this.funding − reference.funding` (absolute); `oi_diff_pct = (this.oi − reference.oi) / reference.oi`; `spread_diff_pct = same shape`; `mid_price_diff_pct = same shape` |
| Threshold | No hard verdict — published raw with the reference labelled. Mid-price divergence is the canonical signal (per code comment) |
| Failure conditions | Exchange fetch raises / returns None → snapshot dropped silently. Per `liquidity.py:498-508` errors are caught individually; the response carries only successful venues |
| Replay behavior | Snapshots persisted to `liquidity_crossex_history` (90 d retention per `poller.py:199`) |
| Validation constraints | "Sustained" is not currently formalized — there is no `divergence_persistence` threshold below which a one-tick divergence is suppressed |

### A.8 OBI (`obi_rt`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/metrics.py:43`](../shared/kazus_logic/liquidity/realtime/metrics.py#L43) `obi_rt()` |
| Purpose | Top-20 order-book imbalance — instantaneous bid-vs-ask quantity skew |
| Inputs | `state.bids` / `state.asks` (top-20 WS book) |
| Formula | `(Σbid_qty − Σask_qty) / (Σbid_qty + Σask_qty)`. Range: [-1, +1]. No normalization, no smoothing |
| Threshold | Raw value; no verdict. Sign is the load-bearing semantic |
| Failure conditions | Empty book on either side → returns None. `total ≤ 0` → returns None |
| Replay behavior | Persisted to `liquidity_samples` at 1 Hz; not reconstructible from history (depends on in-memory book state) |
| Validation constraints | The 10s OBI-flip event detector in `intelligence.py:117-121` is a stub — OBI history is not retained in `DepthSample` for cost reasons; `obi_flip` events listed in [A.2 Resiliency](#a2-resiliency-score-resiliency_score) do not currently fire from the realtime path |

### A.9 Recovery time (`recovery_time_ms`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/intelligence.py:180`](../shared/kazus_logic/liquidity/realtime/intelligence.py#L180) `recovery_time_ms()` |
| Purpose | Per-event time from stress trigger to depth-recovery threshold crossing |
| Inputs | Last completed `RecoveryEvent` in `state.events` |
| Formula | `recovered_ts − started_ts` (ms). Recovered defined as depth crossing `RECOVERY_FRACTION × pre_depth = 0.80 × pre`. Events past `RECOVERY_MAX_AGE_MS = 90_000` are stamped with `recovery_ms = 90_000` and `refill_velocity = 0` rather than left open |
| Threshold | Raw ms; consumed by [A.2 Resiliency](#a2-resiliency-score-resiliency_score) as the `time_part` component |
| Failure conditions | No completed event yet → returns None (column shows "—") |
| Replay behavior | Persisted as a `liquidity_samples` metric; underlying `RecoveryEvent` ring is in-memory only |
| Validation constraints | The 80% recovery floor and 30s exp-decay anchor in `resiliency_score` are interim constants; not calibrated against labelled regimes |

### A.10 Refill velocity (`refill_velocity`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/intelligence.py:187`](../shared/kazus_logic/liquidity/realtime/intelligence.py#L187) `refill_velocity_usd_per_s()` |
| Purpose | Per-event rate at which depth refilled toward the pre-event baseline |
| Inputs | Last completed `RecoveryEvent` |
| Formula | `delta_depth / elapsed_s`, where `delta_depth = max(0, current_depth − pre_depth × (1 − DEPTH_COLLAPSE_DROP)) = max(0, current_depth − pre_depth × 0.60)`. `elapsed_s = max(0.5, recovery_ms / 1000)`. Units: USD per second |
| Threshold | Raw value; consumed by [A.2 Resiliency](#a2-resiliency-score-resiliency_score) as the `velo_part` component (`50 × tanh(refill_velocity / 50_000) + 50`) |
| Failure conditions | No completed event yet → None |
| Replay behavior | Persisted to `liquidity_samples`; in-memory event ring not retained |
| Validation constraints | The 50,000 USD/s scaling anchor (tanh saturation point) is an interim constant; per-symbol calibration pending |

---

## Part B — Research aggregator formulas

Aggregator-tier formulas computed in [`shared/kazus_logic/liquidity/research.py`](../shared/kazus_logic/liquidity/research.py) over persisted history tables. All formulas are deterministic, multiplicative, and explainable; every score is bounded [0, 100] or [0, 1] and every factor is exposed in the API response.

### B.1 Sanity audit

```
severity_score = clip( (value − info_threshold) / (critical − info) × 100, 0, 100 )
overall_state  = CRITICAL if any critical, else WARN if any warn, else INFO if any, else CLEAN
overall_score  = max(severity_score across findings)
```

10 checks: `validation_collapse` · `anomaly_inflation` · `propagation_loop` · `propagation_instability` · `forecast_overshoot` · `pattern_explosion` · `confidence_collapse` · `regime_fragmentation_spike` · `unstable_clustering` · `adaptation_oscillation`. Each with explicit info/warn/critical thresholds.

### B.2 Data quality (scarcity)

```
_discovery_quality(samples, low, medium, high) →
  "HIGH"         if samples ≥ high
  "MEDIUM"       if samples ≥ medium
  "LOW"          if samples ≥ low
  "INSUFFICIENT" otherwise

SCARCITY_FACTOR = {INSUFFICIENT: 0.15, LOW: 0.40, MEDIUM: 0.75, HIGH: 1.00}
```

Thresholds chosen per-endpoint (e.g. pattern_discovery `low=20, medium=100, high=500` buckets; forecast `low=24, medium=72, high=288` snapshots).

### B.3 Pattern discovery

```
stability_score   = base × (penalty_per_flag^k) × half_balance_factor
                    where base = 1.0; penalties:
                      SINGLE_WINDOW       ×0.30
                      LOW_RECURRENCE      ×0.35
                      HIGH_LIFT_LOW_SUPPORT ×0.50
                      REGIME_FRAGILE      ×0.65
                      BUCKET_SENSITIVE    ×0.65
                      LOW_SUPPORT         ×0.75
                    half_balance_factor = 0.6 + 0.4 × (2 × minority_share)
effective_lift    = raw_lift × stability_score
pattern_confidence = 100 × stability_score × scarcity_factor
```

Output range: `pattern_confidence` ∈ [0, 100]. Sort key is `effective_lift` (not raw lift).

### B.4 Propagation graph

**Canonical companion:** [`lip-causal-propagation.md`](lip-causal-propagation.md) — semantic decomposition of this stack into seven primitives, allowed/forbidden verdict interpretations, banned vocabulary, anti-overclaim invariant. The formulas below are the measurement contract; the canonical companion is the epistemic contract.

**Sampling-resolution guard.** Pairs with `lead < min_lead_ms = 5_000` ms are **dropped at ingestion** before any score is computed. `lead_window_ms = 30 × 60_000` (30 min) upper bound similarly drops pairs separated by so much time that recurrence cannot be distinguished from background co-incidence. See [epistemic-boundaries.md §3](lip-epistemic-boundaries.md) for the simultaneity rationale.

Per-edge (computed only on pairs that survived the simultaneity + window guards):

```
volume_strength       = 1 − exp(−count / 15)
lead_clarity          = clip( (avg_lead − min_lead) / 60s, 0, 1 )
lead_consistency      = clip( 1 − std_lead/avg_lead, 0, 1 )
temporal_consistency  = days_with_events / lookback_days
recurrence_stability  = 1 − (max_day_count − 1) / (count − 1)
symmetry_penalty      = (min/max reverse_count)²    (0 if no reverse)

base_confidence       = 0.30·volume + 0.20·lead_clarity + 0.15·lead_consist
                      + 0.20·temporal + 0.15·recurrence

leader_stability(s)   = Σ(edge.base_conf × edge.count) / Σ(edge.count)
                        across outgoing edges of node s
leader_pull           = 0.5 + 0.5 × leader_stability(from_node)

confidence_score      = base × (1 − sym_penalty) × leader_pull

confidence label      = HIGH ≥ 0.70 · MEDIUM ≥ 0.45 · LOW otherwise
```

Graph-level:

```
integrity_score = 100 × ( 0.45·avg_confidence + 0.25·(1−sym_share)
                        + 0.20·(1−weak_share) + 0.10·coverage )
```

### B.5 Causal propagation (Phase 15 #1)

```
volume_factor          = 1 − exp(−count/15)
asymmetry              = (count − reverse_count) / (count + reverse_count)
asymmetry_factor       = max(0, asymmetry)
evidence_factor        = evidence_count / n_windows           (sub-window survival)
common_driver_factor   = 0.35 if common_driver else 1.0
symmetry_factor        = 1 − sym_penalty
scarcity_factor        = SCARCITY[data_quality]

causal_confidence      = volume × asymmetry × evidence × cd × sym × scarcity   ∈ [0, 1]

verdict (priority order — refusal verdicts FIRST so a clean DIRECTIONAL
is only emitted when every refusal path was rejected):
  COINCIDENCE         sym_penalty ≥ 0.70          (effectively bidirectional)
  EXPLORATORY         data_quality ∈ {INSUFFICIENT, LOW}
  COMMON_DRIVEN       common-driver candidate found
  UNDER_EVIDENCED     evidence_count ≤ 1
  AMBIGUOUS           asymmetry < 0.40
  DIRECTIONAL         else
```

### B.6 Influence hierarchy (Phase 15 #5)

**Note on legacy enum vocabulary.** The role labels `{ISOLATED, INSTABILITY_HUB, LEADER, FOLLOWER, AMPLIFIER}` below are **legacy code identifiers**. They name observable properties of out-ratio and average confidence — **not** market roles. See [`lip-causal-propagation.md §8.3`](lip-causal-propagation.md) for the operative reading discipline (e.g., `LEADER` ≡ "out-dense node in this window", not "asset led the market"). UI surfaces using these labels MUST carry the inline disclosure required by that section.


```
stability             = directional_edge_count / total_edges
out_ratio             = out_count / (out + in)
avg_out_confidence    = mean(causal_confidence of outgoing edges)
avg_in_confidence     = mean(causal_confidence of incoming edges)

role classification:
  ISOLATED          total < 3
  INSTABILITY_HUB   stability < 0.30 AND ≥2 low-quality edges
  LEADER            out_ratio > 0.70 AND avg_out_conf ≥ 0.20
  FOLLOWER          out_ratio < 0.30 AND avg_in_conf ≥ 0.20
  AMPLIFIER         0.30 ≤ out_ratio ≤ 0.70 AND (avg_out OR avg_in ≥ 0.20)
  ISOLATED          else
```

### B.7 Market state transitions (Phase 15 #3)

```
persistence           = count of consecutive snapshots in to_state
was_reverted          = bounced to from_state within REVERSAL_WINDOW=3 snapshots
acceleration          = post_stress_slope − pre_stress_slope (6 snapshots each)
meta_conf_at          = meta_confidence_score at transition moment

persistence_factor    = min(1, persistence / 12)
meta_conf_factor      = meta_conf_at / 100
reversal_factor       = 0.25 if was_reverted else 1.0
scarcity_factor       = SCARCITY[data_quality]
confidence            = persistence_factor × max(0.2, meta_conf_factor)
                        × reversal_factor × scarcity_factor

verdict:
  REVERSED            bounced back within REVERSAL_WINDOW
  FLICKER             persistence < PERSISTENCE_THRESHOLD=3
  ACCELERATING        |acceleration| ≥ 5
  PERSISTENT          else
```

### B.8 Distributed Stress Detection (Phase 15 #4)

Engine code identifier: `crisis_genesis()` (retained for stability). Composite verdict over 7 independent probes:

```
contributing_probes  = count of probes whose data_quality ≠ INSUFFICIENT
hot_count            = count of probes whose individual score ≥ 75
genesis_score        = mean(probe.score for contributing probes)        ∈ [0, 100]
confidence           = contributing_probes / 7

verdict (priority order, applied AFTER scarcity cap):
  INSUFFICIENT       contributing_probes == 0
  PRE_CASCADE        genesis_score ≥ 75  AND  hot_count ≥ 3
  ELEVATED_RISK      genesis_score ≥ 50
  EARLY_DISTORTION   genesis_score ≥ 25
  CALM               genesis_score <  25

scarcity cap:
  if > 3 probes INSUFFICIENT → verdict floored at EARLY_DISTORTION
```

7 probes (each emits an independent [0,100] score with its own scarcity gate): `fragmentation_growth` · `resiliency_decay` · `propagation_widening` · `dependency_concentration` · `anomaly_synchronization` · `transition_instability` · `stress_acceleration`. See `docs/2026-05-23-architecture-freeze.md` §14 for the per-probe state-machine specification.

Operator-visible decomposition is mandatory: every published verdict carries the probe list, per-probe score, per-probe data_quality, and the contributing-probes count.

### B.9 Forecast hardening

```
slope_capped              = raw_slope clipped to ±SLOPE_CAP=25/day
extrapolation_capped      = forecast_value left [0, 100] band
slope_consistency         = agreement between first/last half slope fits
horizon_decay             = data_span_days / (data_span_days + horizon_days)
rmse_factor               = max(0, 1 − rmse/50)
cap_factor                = 0.5 if (slope_capped OR extrapolation_capped) else 1.0

confidence                = 100 × rmse × horizon_decay × consistency × cap_factor

trajectory only labeled when data_quality MEDIUM+ AND confidence ≥ 30
```

### B.10 Adaptation modifiers (Phase 16)

```
narrative_confidence_modifier   = 0.50 + (max(0, nc − 0.30) / 0.40) × 0.50  when nc < 0.70
alert_sensitivity_modifier      = 1.00 + min(1, max(0, gs − 30)/50) × 0.50 × max(0.3, gc)
causal_strictness_modifier      = 1.00 + min(0.30, max(0, flicker − 0.25)) + (0.20 if osc else 0)
discovery_suppression_modifier  = {CRITICAL:0.50, WARN:0.70, INFO:0.90, CLEAN:1.00}[sanity_overall]
global_trust_modifier           = product(meta_conf factor × structural_break factor)

all clipped to ADAPTATION_BOUNDS[name]
```

### B.11 Operator priorities (Phase 17)

```
priority_score        = severity_raw × confidence × recency × source_weight   ∈ [0, 100]

source_weights:
  sanity      1.50
  genesis     1.30
  transitions 1.00
  structural  0.80
  causal      0.70
  adaptation  0.90

escalation:
  NORMAL    < 25
  WATCH     25 — 50
  IMPORTANT 50 — 75
  CRITICAL  ≥ 75

lifecycle (DB-backed):
  NEW          age < 5 min OR first appearance after RESOLVED
  WORSENING    Δ ≥ +8 since last call
  STABILIZING  Δ ≤ −8
  PERSISTENT   within ±8
  RESOLVED     disappeared from current run
```

---

## Part C — Measurement contract reference

One row per published metric / aggregator; one column per measurement-contract field. Scan-able comparison across the realtime tier and the research tier on units · cadence · normalization · aggregation · stale behavior · replay reconstructibility · calibration status. Cross-references point at Parts A and B above; this table does not duplicate formulas.

| metric | units | cadence | normalization | aggregation | stale behavior | replay | calibration |
|---|---|---|---|---|---|---|---|
| [Credible Depth](#a1-credible-depth-credible_depth) | USD | 1 Hz | none (raw USD); per-symbol baselines via `/metrics/{symbol}` | per-tick scalar | empty book → None | persisted value only; not recomputable | uncalibrated (±0.5% band, 400 ms floor interim) |
| [Credible Depth per-side / delta](#a1a-credible-depth--per-side-decomposition-credible_bid_depth--credible_ask_depth--credible_depth_delta) | USD (delta signed: + = bid-leaning) | 1 Hz | none (raw USD); no per-symbol baseline for delta | per-tick scalars from one survivorship walk | mid None → all None (UNKNOWN); side with no survivor → 0.0 (observed, ≠ UNKNOWN) | persisted value only; not recomputable | inherits A.1 ±0.5% / 400 ms debt; delta magnitude uncalibrated |
| [Persistence Quality](#a1b-persistence-quality-persistence_quality) | [0, 1] (measurement quality, not market) | 1 Hz | multiplicative freshness·coverage·continuity; no per-symbol baseline | per-tick scalar from book_history frame timestamps | mid None → None (UNKNOWN); < 10 frames → None (INSUFFICIENT); measured-bad → 0.0 (≠ None) | persisted value only; not recomputable | uncalibrated; `PQ_FRAME_INTERVAL_MS=100` load-bearing (Class C) |
| [Resiliency Score](#a2-resiliency-score-resiliency_score) | [0, 100] | 1 Hz | sigmoid + tanh + exp-decay; no per-symbol baseline | exp-weighted (5-min half-life) blend of completed events; weights 0.6 / 0.4 on `time_part` / `velo_part` | no completed events → None | persisted only | uncalibrated (30 s exp anchor, 50 k USD/s tanh anchor) |
| [Impact Score (Kyle λ)](#a3-impact-score-impact_score--kyle-λ-sigmoid) | [0, 100] | 1 Hz | sigmoid centred at λ = 1 | median over ≥ 8 buckets in 60 s | < 8 filled buckets → None | persisted only | uncalibrated (λ = 1 → 50 anchor stated, not measured) |
| [Fragility Score](#a4-fragility-score-fragility_score) | [0, 100] | 1 Hz | CV × 50, clipped | std / mean of bucket λs | < 8 buckets → None | persisted only | uncalibrated (CV ≥ 2 → 100 interim) |
| [Realized vs Predicted Impact](#a5-realized-vs-predicted-impact-exec_impact) | bps + USD + ratio | per-burst (event-driven) | none on `expected_bps` / `realized_bps`; ratio gated by `EXPECTED_FLOOR_BPS = 0.5` | per-event, no aggregation; 5-min rolling medians per (side, bucket) published separately | missing pre or post book → event dropped | forward-only; unavailable before activation | observation mode; not calibrated downstream |
| [Liquidation stress](#a6-liquidation-stress-liq_stress) | USD | 1 Hz | none | sum over 60 s window | empty window → 0.0 (absence is a valid measurement) | persisted only | uncalibrated |
| [Cross-venue divergence](#a7-cross-venue-divergence-crossex) | dimensionless / % | on-demand (per-request) | pairwise `(this − reference) / reference` | per-venue scalar set, no consolidated aggregate | venue fetch fail → venue omitted silently | snapshots persisted to `liquidity_crossex_history` 90 d | "sustained" not formalized; no `divergence_persistence` gate |
| [OBI (`obi_rt`)](#a8-obi-obi_rt) | [-1, +1] | 1 Hz | none | per-tick scalar | empty side → None | persisted only; in-memory dependent | uncalibrated; OBI-flip event detector currently stubbed |
| [Recovery time](#a9-recovery-time-recovery_time_ms) | ms | event-driven (latest completed `RecoveryEvent`) | none | latest-event scalar | no completed event → None | persisted only | 80 % recovery floor + 30 s decay anchor uncalibrated |
| [Refill velocity](#a10-refill-velocity-refill_velocity) | USD/s | event-driven | none | latest-event scalar | no completed event → None | persisted only | 50 k USD/s tanh saturation uncalibrated |
| [Propagation per-edge confidence](#b4-propagation-graph) | [0, 1] + label HIGH/MEDIUM/LOW | per `causal_propagation` call (300 s TTL) | `confidence_score = base × (1 − sym_penalty) × leader_pull`; ratio-of-counts, no z-score | per-edge composite of 5 factors with weights 0.30 / 0.20 / 0.15 / 0.20 / 0.15 | pair drop if `lead < min_lead_ms = 5_000` ms; `data_quality = INSUFFICIENT/LOW` → verdict floored at EXPLORATORY | recomputed on demand from `liquidity_alert_history` (90 d); deterministic | not measured (edge lifetime, lag stability, HIGH-confidence degradation) |
| [Causal verdict](#b5-causal-propagation-phase-15-1) | enum {DIRECTIONAL · AMBIGUOUS · UNDER_EVIDENCED · COMMON_DRIVEN · COINCIDENCE · EXPLORATORY} | per `causal_propagation` call | n/a (enum) | refusal-first 6-step ladder | scarcity-gated to EXPLORATORY | recomputed on demand from `liquidity_alert_history` | DIRECTIONAL false-positive rate not measured |
| [Distributed Stress verdict](#b8-distributed-stress-detection-phase-15-4) | enum {CALM · EARLY_DISTORTION · ELEVATED_RISK · PRE_CASCADE · INSUFFICIENT} + `genesis_score ∈ [0, 100]` | per `crisis_genesis` call (120 s TTL) | per-probe score mapped to [0,100] via probe-specific transforms; composite is unweighted mean of contributing | refusal-first 6-step ladder; scarcity cap floors at EARLY_DISTORTION when > 3 probes INSUFFICIENT | per-probe insufficient → contributes nothing; composite verdict still emits | recomputed on demand; stateless between calls (no persistence/hysteresis) | uncalibrated; PRE_CASCADE false-positive rate not measured |
| [Data-quality gate (`_discovery_quality`)](#b2-data-quality-scarcity) | enum {HIGH · MEDIUM · LOW · INSUFFICIENT} → `SCARCITY_FACTOR ∈ {0.15, 0.40, 0.75, 1.00}` | per-call (each layer that uses it) | per-layer (sample-count thresholds chosen per endpoint) | scalar lookup, applied multiplicatively at the consumer | INSUFFICIENT is a first-class verdict; never silently substituted | deterministic from sample counts | per-layer thresholds configurable but currently hard-coded; calibration pending |

**Reading rule.** If a row says "uncalibrated" or "not measured" or "interim", the metric is still usable for diagnosis but its absolute level should not be cited as a market judgment. The verdict columns (Causal / Distributed Stress) carry their own refusal verdicts that make this constraint operational; the realtime-tier numeric columns rely on the operator reading them as relative-to-symbol-baseline rather than absolute.
