# Regime Engine / Market State Transition Layer — hardening contract (companion)

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md), [`docs/lip-metric-registry.md`](lip-metric-registry.md) §B.7, [`docs/lip-epistemic-boundaries.md`](lip-epistemic-boundaries.md), [`docs/lip-validation-and-calibration.md`](lip-validation-and-calibration.md), [`docs/lip-governance.md`](lip-governance.md).

**Status: Class A documentation hardening pass** of an **already-implemented** observational transition layer. This document does not add code, does not propose new states, does not invent thresholds. It formalizes the existing behavior, marks gaps honestly, and constrains the layer's future evolution under [lip-governance §2](lip-governance.md).

**Boundary statement (load-bearing).** The Regime Engine in this platform is an **observational transition classifier** operating on a persisted state stream (`LiquidityIntelligenceHistory.coordinated_state`). It does **not** compute regimes from microstructure; that computation happens upstream in `synthesize_intelligence`. What this layer does is classify *changes* in the upstream state series into a small verdict enum, compute aggregate stability metrics, and flag oscillation windows — under explicit data-quality gating.

**Cross-cutting ontology invariant** (per [`lip-ontology-boundaries.md`](lip-ontology-boundaries.md)): the layer emits bounded observational classifications under current instrumentation constraints. It does not establish authoritative market ontology.

The Regime Engine does not predict, does not narrate, does not attribute cause, and does not recommend action. Phrases like "bullish regime", "smart-money accumulation", "crisis incoming", "macro regime shift", "risk-on / risk-off" are out-of-vocabulary for this layer (§9, §18).

---

## 1. Definition

**Regime (as observable state)** = the value of `coordinated_state` for the system or `dominant_regime` for a per-symbol record, both computed by upstream layers and persisted to `liquidity_intelligence_history` / `liquidity_alert_history` respectively. The Regime Engine reads these as inputs.

**Transition** = an event where `coordinated_state[i] ≠ coordinated_state[i−1]` across consecutive persisted snapshots within a `lookback_days` window.

**Verdict** = a 4-valued classification of each transition: `REVERSED` / `FLICKER` / `ACCELERATING` / `PERSISTENT`. Verdict precedence is deterministic ([research.py:6012-6020](../shared/kazus_logic/liquidity/research.py#L6012-L6020)).

**Regime is NOT:**

- A market narrative.
- A prediction of future direction.
- A trend label (no "bullish" / "bearish" / "trending" semantics).
- A macro explanation.
- A causal attribution of an upstream event.
- An execution recommendation.

A `coordinated_state = ACTIVE_CASCADE_PROPAGATION` means *the synthesis layer's threshold ladder placed the current `synthesized_stress` above its highest cutoff* — it does **not** mean "the market is in a cascade", let alone "trade defensively".

---

## 2. Current implementation audit

### 2.1 State streams (upstream — inputs to this layer)

| Stream | Field | Type | Producer | Enum values (observed in code) |
|---|---|---|---|---|
| **System-level** | `LiquidityIntelligenceHistory.coordinated_state` | String(32) | [`synthesize_intelligence`, research.py:2815-2827](../shared/kazus_logic/liquidity/research.py#L2815-L2827) | `STABLE_COORDINATED_MARKET` · `EARLY_STRUCTURAL_STRESS` · `STRUCTURAL_MARKET_DETERIORATION` · `FRAGMENTING_LIQUIDITY_ENVIRONMENT` · `ESCALATING_SYSTEMIC_INSTABILITY` · `ACTIVE_CASCADE_PROPAGATION` |
| **Per-symbol** | `LiquidityAlertHistory.regime` (and `LiquidityIntelligenceHistory.dominant_regime`) | String(32) | Upstream alert classifier (read by `regime_shift_warning`, `regime_outcomes`, `transition_forecast` at [research.py:1600, 3445](../shared/kazus_logic/liquidity/research.py#L1600), default `"HEALTHY_TREND"` on missing) | `HEALTHY_TREND` · `THIN_LIQUIDITY` · `CROWDED_LONGS` · `CROWDED_SHORTS` · `SPOOF_PRONE` · `UNSTABLE_MARKET` · `LIQUIDATION_CASCADE` (rank table at [research.py:988-996](../shared/kazus_logic/liquidity/research.py#L988-L996)) |

**The Regime Engine being hardened by this document operates on stream 1** (`coordinated_state`). Stream 2 (`dominant_regime`) is consumed by adjacent functions (`regime_outcomes`, `transition_forecast`, `regime_shift_warning`); those are out of scope here and bound by their own contracts in the metric registry.

### 2.2 Transition layer (in-scope)

| Component | Location | Status |
|---|---|---|
| Transition detection | [`market_state_transitions`, research.py:5918-6145](../shared/kazus_logic/liquidity/research.py#L5918) | Implemented |
| Verdict enum (REVERSED/FLICKER/ACCELERATING/PERSISTENT) | research.py:6012-6020 | Implemented; deterministic precedence |
| Confidence formula | research.py:6022-6026 | Implemented; documented in [lip-metric-registry §B.7](lip-metric-registry.md) |
| Aggregate `flicker_ratio` | research.py:6106 | Implemented |
| Aggregate `transition_rate_per_day` | research.py:6108 | Implemented |
| Stability label (stable/noisy/unstable) | research.py:6117 | Implemented; cutoffs `0.25`, `0.5` |
| Oscillation period detection | research.py:6084-6101 | Implemented; ≥ 3 transitions in 1 h window |
| Current-state duration tracking | research.py:6071-6082 | Implemented |
| Data-quality gating (`_discovery_quality`) | research.py:5962-5965 | Implemented; cutoffs `n<24 / 72 / 288` |
| `exploratory` flag (low-data short-circuit) | research.py:5963 | Implemented |

### 2.3 Implementation-vs-design matrix

| Feature | Status | Notes |
|---|---|---|
| Per-transition verdict | **Implemented** | REVERSED/FLICKER/ACCELERATING/PERSISTENT, priority-ordered |
| Stability aggregate | **Implemented** | flicker_ratio + stable/noisy/unstable label |
| Oscillation detection | **Implemented** | sliding 1 h window, threshold 3 |
| Data-quality gating | **Implemented** | scarcity factor in confidence; exploratory flag |
| Confidence multiplicative blend | **Implemented** | persistence × meta_conf × reversal × scarcity |
| Replay (reads persisted history) | **Implemented** | reads `LiquidityIntelligenceHistory` directly |
| **Named hysteresis primitive** | **NOT IMPLEMENTED** | REVERSED verdict + persistence threshold approximate the same outcome (see §5) |
| **Cooldown / minimum hold time** | **NOT IMPLEMENTED** as a named gate; persistence threshold serves a related role |
| **Common-shock contamination flag** | **NOT IMPLEMENTED** | No `common_shock_unresolved` field; common BTC-beta moves can appear as regime transitions (see §8) |
| **Calibration-version stamping on emit** | **NOT IMPLEMENTED** | Per [lip-validation-and-calibration §5](lip-validation-and-calibration.md), platform-wide gap |
| **Cross-venue confirmation on transitions** | **NOT IMPLEMENTED** | Layer is structurally Binance-centric (see [freeze §14.8](2026-05-23-architecture-freeze.md), [lip-venue-quality §3](lip-venue-quality.md)) |
| **Per-transition replay reproducibility test** | **PENDING VALIDATION** | Determinism is structural (sorted query, deterministic verdict) but not test-covered |
| **Operator-usefulness rate of verdicts** | **PENDING MEASUREMENT** | No labelling exists |
| **State-duration distribution per state** | **Proxy available** | `current_state_duration_*` emitted; distribution not aggregated |

---

## 3. State taxonomy — exact code-enum mapping

### 3.1 `coordinated_state` (upstream, consumed by transition layer)

| Code enum | Doc label | Operational meaning | Allowed claim | Forbidden claim |
|---|---|---|---|---|
| `STABLE_COORDINATED_MARKET` | "stable observed state" | Synthesized stress < 25 AND strategic_state ≠ any escalation rung | "the synthesis layer's stress dial is below the lowest threshold" | "the market is calm", "buy the dip", "no risk" |
| `EARLY_STRUCTURAL_STRESS` | "early structural stress observed" | Stress 25..40 OR structural break ≥ 50 with degrading meta-health, no higher rung hit | "the lowest stress rung is active" | "the market is turning", "regime change imminent" |
| `STRUCTURAL_MARKET_DETERIORATION` | "structural deterioration observed" | structural_break_score ≥ 50 AND meta_intelligence_health in `{DEGRADING, CRITICAL}` (no synthesized-stress condition met above 40) | "structural-break + meta-health condition triggered" | "the market is breaking down", "fundamental shift" |
| `FRAGMENTING_LIQUIDITY_ENVIRONMENT` | "fragmenting liquidity observed" | Stress 40..55 OR strategic_state = `FRAGILE_SPECULATIVE_MARKET` | "mid-rung threshold crossed" | "liquidity crisis", "evacuation regime" |
| `ESCALATING_SYSTEMIC_INSTABILITY` | "escalating instability observed" | Stress 55..70 OR strategic_state ∈ {`TRANSITIONAL_UNSTABLE`, `LIQUIDITY_DETERIORATION_PHASE`} | "second-highest rung threshold crossed" | "panic regime", "cascade incoming" |
| `ACTIVE_CASCADE_PROPAGATION` | "active cascade conditions observed" | Stress ≥ 70 OR strategic_state = `CASCADE_RISK_ENVIRONMENT` | "highest rung threshold crossed" | "cascade in progress", "exit everything" |

**Important.** These six labels are produced by a fixed threshold ladder on a synthesized score that is itself a weighted mean of five other scores. Each upstream score has its own uncalibrated thresholds. The label is the *residue of those thresholds*, not an independent measurement.

### 3.2 `dominant_regime` (out of scope, listed for completeness)

| Code enum | Used at | Notes |
|---|---|---|
| `HEALTHY_TREND` | Default for rank-table calculations | Rank 0 |
| `THIN_LIQUIDITY` | Per [research.py:988](../shared/kazus_logic/liquidity/research.py#L988) | Rank 30 |
| `CROWDED_LONGS`, `CROWDED_SHORTS` | Per [research.py:990-991](../shared/kazus_logic/liquidity/research.py#L990-L991) | Rank 40 |
| `SPOOF_PRONE` | Per [research.py:992](../shared/kazus_logic/liquidity/research.py#L992) | Rank 50 |
| `UNSTABLE_MARKET` | Per [research.py:994](../shared/kazus_logic/liquidity/research.py#L994) | Rank 70; member of `CASCADE_REGIMES` |
| `LIQUIDATION_CASCADE` | Per [research.py:995](../shared/kazus_logic/liquidity/research.py#L995) | Rank 90; member of `CASCADE_REGIMES` |

`CASCADE_REGIMES = {"LIQUIDATION_CASCADE", "UNSTABLE_MARKET"}` (research.py:397).

Per-symbol `dominant_regime` is consumed by `regime_outcomes`, `transition_forecast`, `regime_shift_warning`. Those functions inherit the same boundary contract (observe-only, no prediction beyond explicit OLS extrapolation with capped slope per [freeze line 1115](2026-05-23-architecture-freeze.md)).

### 3.3 Transition verdicts (this layer's output)

| Verdict | Entry condition (code) | Operational meaning | Allowed claim | Forbidden claim |
|---|---|---|---|---|
| `REVERSED` | `was_reverted = True` (returned to from_state within `REVERSAL_WINDOW = 3` snapshots) | The transition bounced back; not a sustained change | "the change did not hold for ≥ 3 snapshots" | "the market faked a transition", "spoof regime" |
| `FLICKER` | Not reversed, but `persistence < PERSISTENCE_THRESHOLD = 3` snapshots | The to_state held briefly but flipped before the threshold | "transition was brief by the configured threshold" | "regime is unstable" (without qualifier — see §6) |
| `ACCELERATING` | Not reversed/flicker, and `|acceleration| ≥ ACCELERATION_THRESHOLD = 5.0` stress-points/tick | The post-transition stress slope diverged from the pre-transition slope by ≥ 5 | "the synthesized-stress rate of change shifted at the transition" | "the market is accelerating", "trend forming" |
| `PERSISTENT` | None of the above | Transition held ≥ 3 snapshots without significant slope change | "the new state has held for ≥ 3 snapshots at the configured threshold" | "regime change confirmed", "new market regime" |

**Verdict precedence (load-bearing, [research.py:6012-6020](../shared/kazus_logic/liquidity/research.py#L6012)):** `REVERSED > FLICKER > ACCELERATING > PERSISTENT`. The first matching condition wins; the precedence is not order-dependent on the input — it is deterministic on the per-transition feature tuple.

### 3.4 Stability label

| Label | Condition (code) |
|---|---|
| `stable` | `flicker_ratio < 0.25` |
| `noisy` | `0.25 ≤ flicker_ratio < 0.5` |
| `unstable` | `flicker_ratio ≥ 0.5` |

Cutoffs `0.25`, `0.5` are L0 implementation anchors per [lip-governance §5](lip-governance.md).

---

## 4. Input contract

| Input | Source | Cadence | Window | Normalization | Threshold | Stale behavior | Replay | Calibration | Validation |
|---|---|---|---|---|---|---|---|---|---|
| `coordinated_state` | `LiquidityIntelligenceHistory.coordinated_state` (NOT NULL filter applied) | Whatever the upstream `synthesize_intelligence` cadence is — historically ≈ 5 min per `lookback_days` calculation comment | `lookback_days × 86_400_000` ms ending at current time | None (string label) | N/A (label) | Rows with NULL state filtered out; gap creates effective transition boundary | Read from DB, reproducible if `intelligence_history` not pruned/rewritten | N/A — upstream-determined | Upstream's thresholds (synthesized_stress cutoffs `25/40/55/70`) are L0 |
| `synthesized_stress` | Same table | Same | Same | 0..100 | `ACCELERATION_THRESHOLD = 5.0` per-tick slope diff | NULLs default to None in slope calc; absent values reduce sample size | Same | L0 | L0 |
| `meta_confidence_score` | Same table | Same | Same | 0..100 | None directly; enters confidence as `/100` | NULL → `0.0` per [research.py:6010](../shared/kazus_logic/liquidity/research.py#L6010) | Same | L0 | L0 |
| `dominant_regime` | Same table | Same | Same | string | None | Not used in transition detection; only carried through for downstream context | Same | L0 (rank table) | L0 |

**No microstructure inputs** are consumed directly by the transition layer. All inputs are post-synthesis. This is by design — the layer is *the transition observer*, not the regime computer.

---

## 5. Transition contract

For each detected transition `A → B` at `ts_ms = snapshots[i].ts_ms`:

| Computed field | Definition (code-grounded) |
|---|---|
| **Entry** | Any change `snapshots[i].state ≠ snapshots[i−1].state`. No minimum-hold-time gate at entry; entries can be later reclassified as REVERSED or FLICKER |
| **Persistence** | Count of consecutive subsequent snapshots remaining in `B` ([research.py:5982-5988](../shared/kazus_logic/liquidity/research.py#L5982)). Bounded by window end |
| **Reversal** | `True` iff state returns to `A` within `REVERSAL_WINDOW = 3` snapshots after `ts_ms` ([research.py:5990-5994](../shared/kazus_logic/liquidity/research.py#L5990)) |
| **Pre-slope** | Mean step difference of `synthesized_stress` over `PRE_WINDOW = 6` snapshots before transition |
| **Post-slope** | Same over `POST_WINDOW = 6` snapshots starting at transition |
| **Acceleration** | `post_slope − pre_slope`. `None` if either is None |
| **meta_confidence_at** | `meta_confidence_score` of the post-transition snapshot |
| **Confidence** | `persistence_factor × max(0.2, meta_conf_factor) × reversal_factor × scarcity_factor` (formula at [research.py:6022-6026](../shared/kazus_logic/liquidity/research.py#L6022); also documented in [lip-metric-registry §B.7](lip-metric-registry.md)) |

**Hysteresis: NOT IMPLEMENTED as a named primitive.** The closest analogue is the **REVERSED verdict** (penalizes flips back to `A` within 3 snapshots via `reversal_factor = 0.25`) combined with **PERSISTENCE_THRESHOLD** (subthreshold persistence demotes verdict to FLICKER). Together these reduce confidence on unstable transitions but do not prevent the transition row from being emitted. **Transitions may flicker under unstable inputs; existing mitigation is verdict + confidence demotion, not suppression.**

**Cooldown: NOT IMPLEMENTED.** Repeated transitions A→B→A→B within `OSCILLATION_WINDOW_S = 3600` s with count ≥ 3 are flagged as an `oscillation_period` (research.py:6084-6101), but each transition is still emitted and verdicted individually. There is no cooldown gate that suppresses subsequent transitions.

**UNKNOWN fallback:** When `data_quality ∈ {INSUFFICIENT, LOW}`, the `exploratory` flag is set and the summary string explicitly tags classifications as exploratory ([research.py:5962-5965, 6111-6115](../shared/kazus_logic/liquidity/research.py#L5962)). Transitions are still emitted, but their interpretation is gated by the flag.

---

## 6. Flicker & jitter discipline

**`flicker_ratio` (implemented, [research.py:6106](../shared/kazus_logic/liquidity/research.py#L6106)):** `(count of REVERSED + count of FLICKER) / total transitions` over the window. Defined only when `total > 0`; else `0.0`.

**Stability label thresholds (implemented):** `< 0.25` → `stable`; `< 0.5` → `noisy`; else `unstable`.

**`transition_rate_per_day` (implemented, [research.py:6108](../shared/kazus_logic/liquidity/research.py#L6108)):** total transitions divided by span in days; exists alongside `flicker_ratio` but neither is automatically translated into a higher-confidence verdict on any single transition.

**Per-transition confidence demotion under flicker:** the `reversal_factor = 0.25` (research.py:6025) reduces confidence by 75% on REVERSED transitions; FLICKER verdicts retain higher persistence factor but their `persistence_factor = min(1, persistence / 12)` caps at `< 0.25` for persistence = 1, 2.

**Regime instability does not silently promote to high confidence.** Across the implemented chain: REVERSED → confidence × 0.25; FLICKER → low persistence_factor; `noisy` / `unstable` aggregate stability → exposed as a top-level summary field that operators see alongside individual transitions.

**Note on freeze line 1386** ([architecture freeze](2026-05-23-architecture-freeze.md)): `transition_instability` (a sanity-audit derivative) uses `flicker_ratio` as instability evidence, not as transition magnitude. It tends to rise on real regime jitter and stay flat on synchronized clean moves — a deliberate property, documented at the audit layer.

---

## 7. Refusal-first invariant

The layer applies refusal-equivalents at multiple gates **before** classification:

| Gate | Condition | Effect |
|---|---|---|
| **No data** | `n == 0` snapshots in window | Returns `current_state = None`, `transitions = []`, `summary` includes `0 transitions`; nothing fabricated |
| **NULL state filter** | `coordinated_state IS NOT NULL` SQL filter ([research.py:5946](../shared/kazus_logic/liquidity/research.py#L5946)) | Snapshots with missing state are excluded before transition detection |
| **Low data quality** | `_discovery_quality(n) ∈ {INSUFFICIENT, LOW}` (n < 24 or < 72) | `exploratory = True`; `scarcity_factor` ∈ `{0.15, 0.40}` damps every confidence; summary explicitly tags classifications as exploratory |
| **Insufficient slope data** | Fewer than 2 stress values in pre or post window | `acceleration = None`; transition cannot earn ACCELERATING verdict (falls through to PERSISTENT) |
| **Missing meta_confidence** | NULL | `meta_conf = 0.0`; `meta_conf_factor = 0`; `max(0.2, meta_conf_factor) = 0.2` ([research.py:6026](../shared/kazus_logic/liquidity/research.py#L6026)) caps confidence at 20% of the rest |
| **No transitions in window** | `total = 0` | `flicker_ratio = 0.0` by definition; no `stable/noisy/unstable` claim is made (summary uses `0 transitions` framing) |

**Invariant (load-bearing).** A clean transition verdict is the **residue** after the refusal-equivalent gates above are evaluated. It is not the default interpretation of available rows. In particular, under `exploratory = True`, all verdicts are tagged exploratory and the operator surface MUST preserve that tag through any rendering.

---

## 8. Common-shock / global-move boundary

**Status: NOT IMPLEMENTED.** The Regime Engine has no `common_shock_unresolved` flag, no BTC-beta filter, no cross-asset correlation gate. A coordinated move driven by an external common shock (BTC move spilling to alts; macro release) will appear as a regime transition with the same verdict shape as a venue-internal microstructure event.

**Allowed framing for transitions under common-shock conditions:**

- "multi-symbol deterioration"
- "synchronized spread expansion"
- "liquidation pressure elevated"
- "depth deterioration"
- "propagation activation"

**Forbidden framing (under any conditions, but especially under unresolved common-shock):**

- "crisis cause"
- "systemic breakdown"
- "smart-money event"
- "macro regime shift"
- "structural turn"

**Operator-tier mitigation:** Investigation workflows (Phase 18, [freeze §1 Layer 11](2026-05-23-architecture-freeze.md)) can cross-reference per-symbol `dominant_regime`, distributed-stress probes, and `crossex` to disambiguate venue-local vs common-driver effects. This is operator work; the Regime Engine layer does not infer cause.

**Documentation discipline.** Until a `common_shock_unresolved` primitive exists, every operator-visible verdict surface MUST either:

1. Include the multi-symbol context (e.g., the share of active symbols transitioning concurrently), OR
2. Mark common-shock contamination as a known blind spot in tooltip / accordion.

Currently neither is enforced runtime-side; this is a documentation-only requirement carried by this companion.

---

## 9. Banned directional vocabulary

The Regime Engine **never** emits or surfaces:

- bullish / bearish
- accumulation / distribution
- risk-on / risk-off
- pump regime / dump regime
- smart-money regime / dumb-money regime
- reversal regime / breakout regime
- trend / counter-trend / continuation
- regime "change of character" / "structural turn"

These phrases are out-of-vocabulary for the layer regardless of the value of any input. They would constitute [lip-governance §3](lip-governance.md) row 9 (inferred intent scoring) and row 11 (semantic relabeling) violations simultaneously.

**Approved labels (consistent with implemented enums):**

- `liquidity_stable` ↔ `STABLE_COORDINATED_MARKET`
- `liquidity_degraded` ↔ `FRAGMENTING_LIQUIDITY_ENVIRONMENT` or `STRUCTURAL_MARKET_DETERIORATION`
- `volatility_elevated` (qualifier; not a state of this layer — would come from per-symbol metrics)
- `spread_expanded` / `depth_evaporated` (per-symbol observations, not regime states)
- `liquidation_stressed` ↔ adjacency to `LIQUIDATION_CASCADE` regime in per-symbol stream
- `transition_unstable` ↔ stability label `unstable` (`flicker_ratio ≥ 0.5`)
- `insufficient_evidence` ↔ `exploratory = True` / `data_quality ∈ {INSUFFICIENT, LOW}`

---

## 10. Decomposition (current emit, exhaustively)

What `market_state_transitions` actually returns ([research.py:6127-6145](../shared/kazus_logic/liquidity/research.py#L6127)):

| Field | Source | Implementation status |
|---|---|---|
| `since_ms`, `lookback_days` | Input echo | Implemented |
| `data_quality` | `_discovery_quality(n)` ∈ `{INSUFFICIENT, LOW, MEDIUM, HIGH}` | Implemented |
| `exploratory` | `data_quality ∈ {INSUFFICIENT, LOW}` | Implemented |
| `snapshot_count` | `n` | Implemented |
| `state_vocabulary` | Sorted unique states observed in window | Implemented |
| `state_counts` | Per-state count | Implemented |
| `current_state` | Most recent snapshot's state | Implemented |
| `current_state_duration_snapshots`, `current_state_duration_seconds` | Consecutive snapshots in current state, latency | Implemented |
| `transition_count`, `flicker_count`, `flicker_ratio` | Aggregates | Implemented |
| `transition_rate_per_day` | total / span | Implemented |
| `transitions[]` | List of per-transition records (top 30 by recency) | Implemented |
| Per-transition: `ts_ms`, `from_state`, `to_state`, `persistence_snapshots`, `persistence_seconds`, `was_reverted`, `pre_stress_slope`, `post_stress_slope`, `acceleration`, `meta_confidence_at`, `verdict`, `confidence`, `rationale` | Per-transition record | Implemented |
| `oscillation_periods[]` | Sliding-window detected oscillation runs | Implemented |
| `summary` | Human-readable one-liner | Implemented |
| **`calibration_version`, `schema_version`** | — | **NOT IMPLEMENTED** (platform-wide per [lip-validation-and-calibration §5](lip-validation-and-calibration.md), [lip-governance §8](lip-governance.md)) |
| **`refusal_reason` (explicit field)** | — | **NOT IMPLEMENTED**; refusal-equivalence carried by `exploratory` flag + `data_quality` enum |
| **`common_shock_unresolved`** | — | **NOT IMPLEMENTED** (§8) |
| **`transition_history` for replay reconstruction across windows** | — | **NOT IMPLEMENTED**; transitions are recomputed from `intelligence_history` each call |
| **Cross-venue confirmation flag** | — | **NOT IMPLEMENTED** (§17) |

Any consumer documentation MUST list only the implemented fields. Adding any of the NOT IMPLEMENTED fields requires Class B + Class C + Class E changes per [lip-governance §2](lip-governance.md).

---

## 11. Composite / score governance

**There is no standalone "regime score".** The layer emits:

- A scalar `confidence` per transition (multiplicative blend, not a regime score).
- A scalar `flicker_ratio` aggregate (count of REVERSED+FLICKER / total transitions).
- A scalar `transition_rate_per_day`.

**Confidence formula (already documented in [lip-metric-registry §B.7](lip-metric-registry.md)):**

```
confidence = persistence_factor
           × max(0.2, meta_conf_factor)
           × reversal_factor
           × scarcity_factor

where:
  persistence_factor = min(1, persistence / 12)           # 1h @ 5-min cadence = full credit
  meta_conf_factor   = meta_conf_at / 100
  reversal_factor    = 0.25 if was_reverted else 1.0
  scarcity_factor    = SCARCITY[data_quality]             # 0.15 / 0.40 / 0.75 / 1.0
```

**Per [lip-governance §7 Composite Creation Contract](lip-governance.md):**

- **Upstream metrics:** `persistence`, `meta_confidence_at`, `was_reverted`, `data_quality` — all derivable from `intelligence_history` rows.
- **Weights:** named constants in the formula above. No `w_*` named separately; the structure itself is the composition. **No learned weights ever.**
- **Thresholds within the composite:** `0.2` floor on meta_conf_factor; `12` divisor on persistence; `0.25` reversal penalty; SCARCITY map.
- **Normalization:** result is in `[0, 1]` (all factors ≤ 1).
- **Stale behavior:** missing `meta_conf` → `0` → factor `0.2` (floor). Missing slope data → ACCELERATING unreachable → falls through to PERSISTENT. Missing `data_quality` (impossible by current code path; `_discovery_quality` always returns a tier) → would default to `0.15` via `SCARCITY.get(..., 0.15)`.
- **Replay behavior:** deterministic given the same `intelligence_history` rows. Not version-stamped.
- **Failure behavior:** zero transitions ⇒ no per-transition confidence emitted; aggregate `flicker_ratio = 0.0`. Empty window ⇒ all aggregates trivial; no claim made.
- **Calibration status:** every constant in the formula is L0 ([lip-governance §5](lip-governance.md), [lip-execution-validation §23](lip-execution-validation.md)).
- **Blind spots:** inherits §16.
- **Validation state:** PENDING for every measurement in §15.

**No hidden confidence modifiers.** The four factors above are the entire confidence pipeline for this layer.

---

## 12. Precedence rules

Deterministic precedence applied per `market_state_transitions` call:

1. **Window-level: no data** → empty result set; no other rule fires.
2. **Window-level: data-quality gate** → `exploratory` flag set if INSUFFICIENT/LOW; does NOT short-circuit but tags everything downstream.
3. **Per-row: NULL state** → row excluded by SQL filter; cannot enter transition detection.
4. **Per-transition: REVERSED** → wins over all other verdicts (research.py:6013).
5. **Per-transition: FLICKER** → wins over ACCELERATING/PERSISTENT when persistence < threshold (research.py:6015).
6. **Per-transition: ACCELERATING** → wins over PERSISTENT when |acceleration| ≥ 5.0 (research.py:6017).
7. **Per-transition: PERSISTENT** → default residue (research.py:6019).
8. **Confidence demotion** applied uniformly via multiplicative blend after verdict assigned — does not change verdict.
9. **Oscillation flag** computed post-hoc on transition timestamps; does not alter individual verdicts.

This precedence is the operative precedence today. It is encoded in the structural order of `if/elif/else` at [research.py:6012-6020](../shared/kazus_logic/liquidity/research.py#L6012). Reordering it is a Class B change requiring [lip-governance §2](lip-governance.md) audit.

---

## 13. Replay semantics

**What is reconstructable:** The layer's output for a window is deterministic given the same `LiquidityIntelligenceHistory` rows in `[since_ms, now]`. Two invocations within the same workflow produce identical results.

**What is not reconstructable:**

- The exact upstream `synthesized_stress` and `coordinated_state` values for windows where `intelligence_history` has been pruned (retention policy applies to the upstream table, not to this layer's output).
- Per-transition verdicts under different `PERSISTENCE_THRESHOLD`, `REVERSAL_WINDOW`, `ACCELERATION_THRESHOLD`, or `OSCILLATION_*` values — these constants live in the function body. Any threshold change creates a calibration-version discontinuity that this layer does **not** mark on its emit ([lip-validation-and-calibration §5](lip-validation-and-calibration.md); [lip-governance §8](lip-governance.md) gap).

**Stale historical input behavior:** The layer reads the snapshot as it was persisted. If the upstream `synthesize_intelligence` was changed between the write of historical rows and the replay, the rows reflect the old computation; the layer's output reflects the old labels. Mixing windows that straddle an upstream change is currently silent.

**Pre-activation unavailability:** Bursts of `coordinated_state` before activation of the upstream `synthesize_intelligence` writer are not in the table; the window simply starts later. No backfill, no synthesis.

**Per-transition transition_history persistence:** **NOT IMPLEMENTED.** Each call recomputes from `intelligence_history`. There is no `liquidity_transitions` table.

**Replay must not silently reinterpret old market states under new thresholds.** This is currently enforced **only by the operator**; without `calibration_version` stamping on each transition record, the replay engine cannot detect a threshold-boundary crossing inside a single response. Tracked as governance debt per [lip-governance §8](lip-governance.md).

---

## 14. Calibration status (per anchor)

All anchors are **Implementation constant (L0)** per [lip-governance §5](lip-governance.md). None have progressed past L0.

| Anchor | Value | Class |
|---|---|---|
| `PERSISTENCE_THRESHOLD` | 3 snapshots | L0 |
| `REVERSAL_WINDOW` | 3 snapshots | L0 |
| `PRE_WINDOW` | 6 snapshots | L0 |
| `POST_WINDOW` | 6 snapshots | L0 |
| `OSCILLATION_MIN_TRANSITIONS` | 3 | L0 |
| `OSCILLATION_WINDOW_S` | 3600 s | L0 |
| `ACCELERATION_THRESHOLD` | 5.0 stress-pts/tick | L0 |
| Stability cutoff #1 (`stable`) | `flicker_ratio < 0.25` | L0 |
| Stability cutoff #2 (`noisy`) | `flicker_ratio < 0.5` | L0 |
| Scarcity map | `INSUFFICIENT=0.15, LOW=0.40, MEDIUM=0.75, HIGH=1.0` | L0 |
| Data-quality cutoffs | `_discovery_quality(n, low=24, medium=72, high=288)` | L0 |
| Confidence floor (`max(0.2, meta_conf_factor)`) | 0.2 | L0 |
| Confidence persistence divisor | 12 (= 1 h at 5-min cadence) | L0 |
| Reversal factor | 0.25 | L0 |

**No anchor is empirically calibrated.** Changing any of them is a Class C governance change requiring [lip-execution-validation §22](lip-execution-validation.md)-style acceptance contract.

---

## 15. Validation backlog (all PENDING)

| Validation question | Status |
|---|---|
| Per-state regime transition stability across volatility regimes | PENDING |
| `flicker_ratio` distribution per (lookback_days, regime context) | PENDING |
| False ELEVATED-rung promotion rate (transitions to `ESCALATING_*` or `ACTIVE_*` that REVERSE within window) | PENDING — derivable from emit, not aggregated |
| State duration distribution per `coordinated_state` value | PENDING |
| Transition persistence vs verdict accuracy (do ACCELERATING transitions actually accelerate?) | PENDING — would require labelled future window |
| Replay reproducibility (bit-exact on golden vectors) | PENDING; determinism is structural but no golden vectors exist |
| Confidence formula correctness (does multiplicative blend rank transitions usefully?) | PENDING — operator-usefulness study not run |
| Operator-usefulness rate of verdicts (do operators act on PERSISTENT vs FLICKER consistently?) | PENDING |
| Common-shock false-regime rate (transitions during synchronized macro moves) | PENDING; gated on §8 primitive being added |
| Regime-state agreement across volatility buckets (do labels cluster sensibly?) | PENDING |
| Oscillation-period precision (do flagged periods contain actionable structure or are they noise?) | PENDING |

None of the above is run. The layer is **Observational** per [lip-governance §9](lip-governance.md) — promotion to Operator-visible (formally) requires these to clear; informally the layer is already surfaced in research endpoints (see §17).

---

## 16. Failure modes & blind spots

| Failure mode | Mechanism | Mitigation in layer |
|---|---|---|
| **Regime over-classification on jitter** | Rapid alternation A↔B↔A produces many transition rows, all with low confidence | Confidence demotion via `reversal_factor = 0.25` + persistence_factor cap; aggregate `flicker_ratio` exposes the pattern |
| **Stale-driven false state** | Upstream pause leaves `coordinated_state` static; on resume, jump appears as transition | Not detected; window-level `exploratory` flag if sample count drops, otherwise transition emits normally |
| **Liquidation burst misclassified as sustained regime** | If liq burst persists ≥ 3 snapshots, becomes PERSISTENT with high confidence | Not mitigated at this layer; per-symbol `dominant_regime = LIQUIDATION_CASCADE` available cross-tab via `regime_outcomes` |
| **Common BTC-beta move misread as regime transition** | §8 — common-shock contamination unresolved | Operator-tier disambiguation; layer-side: NONE |
| **Missing cross-venue confirmation** | Layer is Binance-centric (see [lip-venue-quality §3](lip-venue-quality.md), [freeze §14.8](2026-05-23-architecture-freeze.md)) | NONE in this layer; cross-venue confirmation enum proposed in lip-venue-quality §7 but NOT IMPLEMENTED there either |
| **Timestamp drift** | Per [freeze §13 line 1065](2026-05-23-architecture-freeze.md), host-clock NTP not enforced; `intelligence_history.ts_ms` reflects local clock; transition windowing skewed | NONE |
| **Insufficient symbol coverage** | `coordinated_state` is system-level; computed across whatever symbols `synthesize_intelligence` observed. Sparse coverage upstream propagates here invisibly | NONE in this layer |
| **Sparse altcoin books** | Per [lip-venue-quality §3 coverage asymmetry](lip-venue-quality.md): some symbols have shallow observation; upstream synthesis may be biased | NONE; per-symbol `dominant_regime` partially exposes this via `THIN_LIQUIDITY` value |
| **Hidden liquidity / iceberg / RPI** | Standard blind spot per [lip-execution-validation §10](lip-execution-validation.md), [lip-epistemic-boundaries §2](lip-epistemic-boundaries.md) | NONE; structural epistemic ceiling |
| **Feed outage** | Window contains gap; transitions across gap may have unrealistic acceleration | NONE; `acceleration = None` if pre/post windows underflow, demoting to PERSISTENT |
| **Uncalibrated thresholds** | Every L0 anchor in §14 could be wrong for any given regime context | NONE; documented honestly |

**Inherited blind spots (pointer, not duplication):** [lip-execution-validation §10 / §24](lip-execution-validation.md), [lip-epistemic-boundaries §2 / §5](lip-epistemic-boundaries.md).

---

## 17. Relationship to existing layers

| Layer | This layer's relation |
|---|---|
| **`synthesize_intelligence`** (upstream) | Direct input source. This layer's `coordinated_state` is whatever the upstream writes. The transition layer does not validate the synthesis — it observes the stream |
| **Distributed Stress (Phase 15 #4, `crisis_genesis`)** | Adjacent. Crisis Genesis composes seven precursor probes into `genesis_score`; the transition layer composes state changes into verdicts. **No merge.** Per [freeze §14](2026-05-23-architecture-freeze.md) and [lip-epistemic-boundaries §4](lip-epistemic-boundaries.md), the two are independent epistemic surfaces |
| **Propagation (Phase 15 #1)** | Operates on alert lineage, not on `coordinated_state`. Transitions and propagation events may co-occur but the transition layer does not consume propagation graph |
| **Credible Depth / Resiliency / Fragility** | Per-symbol microstructure layers; their values flow into the synthesis upstream, not directly into transition detection. Transition layer is post-synthesis |
| **`exec_impact`** ([lip-execution-validation.md](lip-execution-validation.md)) | Independent. Execution mismatch is per-burst per-symbol; regime transitions are system-level per-window. No data flow between them |
| **Venue Quality** ([lip-venue-quality.md](lip-venue-quality.md) — DESIGN CANDIDATE, not implemented) | Would be adjacent if built; transitions could be downgraded under venue-quality DEGRADED windows. Currently no link |
| **Sanity Audit** | Consumes this layer's `flicker_ratio` as evidence for `transition_instability` ([freeze §3, line 1386](2026-05-23-architecture-freeze.md)). One-way relation: audit reads; this layer does not consume audit output |
| **Governance** | This document and [lip-governance.md](lip-governance.md). Every constant in §14 is a governance-controlled anchor |

**Allowed for this layer:**

- Consume `intelligence_history` rows.
- Publish per-transition verdicts + aggregates + oscillation periods.
- Downgrade confidence per the multiplicative formula.
- Mark `exploratory = True` when data is thin.
- Support operator investigation by surfacing transitions with rationale.

**Not allowed for this layer:**

- Produce trade advice or recommendations.
- Override execution validation verdicts on individual symbols.
- Infer cause for a transition (synchronized vs venue-local vs spec-driven — unattributable from current inputs).
- Infer venue trust or quality.
- Infer participant intent.
- Silently promote a `noisy` or `unstable` window into a "high alert" without operator decision.
- Merge with prediction surfaces beyond explicit OLS-with-cap extrapolation (per [freeze line 1115](2026-05-23-architecture-freeze.md)).

---

## 18. UI language discipline

| BANNED label | Approved replacement |
|---|---|
| Market regime shift | "transition observed (verdict: X)" |
| Crisis incoming | (rejected — no predictive surface) |
| Bull regime / bear regime | (rejected — §9 directional vocabulary ban) |
| Smart money active | (rejected — intent inference) |
| Breakout regime / selloff regime | (rejected — directional + predictive) |
| Market panic | (rejected — narrative) |
| Market changed character | "current_state = X (held N snapshots)" |
| Structural market turn | (rejected — directional implication) |
| Regime collapse | (rejected — causal implication) |
| Top regime / safest regime | (rejected — ranking; see [lip-venue-quality §18](lip-venue-quality.md) analogous invariant) |

| Approved status phrase | When applicable |
|---|---|
| "regime evidence insufficient" | `exploratory = True` or `data_quality ∈ {INSUFFICIENT, LOW}` |
| "liquidity conditions degraded" | current_state ∈ {`FRAGMENTING_*`, `STRUCTURAL_*`} |
| "transition unstable" | stability label = `unstable` (`flicker_ratio ≥ 0.5`) |
| "transition noisy" | stability label = `noisy` (0.25 ≤ flicker_ratio < 0.5) |
| "transition stable" | stability label = `stable` |
| "verdict: REVERSED — transition did not hold" | per-transition verdict REVERSED |
| "verdict: FLICKER — brief by threshold" | per-transition verdict FLICKER |
| "verdict: ACCELERATING — stress slope shifted" | per-transition verdict ACCELERATING |
| "verdict: PERSISTENT — held N snapshots" | per-transition verdict PERSISTENT |
| "oscillation flagged (N transitions in 1 h)" | per oscillation_period entry |
| "current state held N snapshots" | summary of `current_state_duration_*` |

**Enforcement.** A label outside the approved set is a [lip-governance §3](lip-governance.md) row 9 + row 11 violation. UI changes that introduce new labels require either §14 acceptance contract for the underlying anchor or [lip-governance §2](lip-governance.md) Class A documentation update to this table.

---

## 19. Governance integration

| Aspect | Status |
|---|---|
| **Change class for this document** | Class A (documentation-only) per [lip-governance §2](lip-governance.md). Authorized during Operational Observation Period |
| **Change class for any threshold adjustment** | Class C. Requires `calibration_version` bump (NOT IMPLEMENTED — governance debt) and [lip-execution-validation §22](lip-execution-validation.md)-style acceptance |
| **Change class for verdict precedence change** | Class B. Requires emit-equivalence check + golden vector regression |
| **Change class for adding `common_shock_unresolved` or other new emit fields** | Class B + Class E (new persistence if persisted) |
| **Allowed consumers (current)** | Research endpoints (`/research/intelligence-forecast`, regime narrators, operator views per [freeze §3](2026-05-23-architecture-freeze.md)) — read-only |
| **Forbidden consumers (permanent)** | Any auto-action layer; any order-routing layer; any composite that maps verdict → trade signal; any external publication of verdicts framed as predictions |
| **Versioning requirements (current)** | `schema_version` / `calibration_version` / `runtime_generation` NOT IMPLEMENTED in emit — platform-wide gap per [lip-governance §8](lip-governance.md). Until implemented, any threshold change creates undetectable boundary in historical interpretation |
| **Observation-period restrictions** | This layer is in-scope for the [Operational Observation Period](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_operational_observation_period.md). New emit fields, new states, new thresholds: **NOT AUTHORIZED**. Doc-only refinement, wording, accessibility, UI label hardening: authorized |
| **Maturity stage** | **Observational** per [lip-governance §9](lip-governance.md). Promotion to Operator-visible (formally) requires §15 validation backlog to clear. (Informally already surfaced; promotion would be a documentation reconciliation, not a deployment) |

---

## 20. Regime Temporal Granularity Contract (load-bearing)

**Purpose.** Make explicit that every regime emit is a *classification under a bounded observation window evaluated at a specific call time*, not a continuous authoritative read of market state.

### 20.1 Observation window

| Property | Specification |
|---|---|
| Slice | `[now − lookback_days × 86_400_000 ms, now]` at the call's `now` |
| Shape | **Rolling**, recomputed on each invocation. Not a fixed-boundary historical window |
| Continuity | **Bounded**, not continuous: window is opened on call, closed on return. There is no streaming evaluator that maintains regime state between calls |
| Source | Persisted `LiquidityIntelligenceHistory` rows whose `coordinated_state IS NOT NULL` |

### 20.2 Evaluation cadence

| Aspect | Specification |
|---|---|
| Trigger | **Caller-driven**, not engine-driven. Each `market_state_transitions(db, lookback_days=N)` invocation is one evaluation |
| Coupling to upstream | Indirect: an evaluation only sees `coordinated_state` values that the upstream `synthesize_intelligence` writer has already persisted at call time. The evaluation cadence is **not** coupled to the synthesizer cadence |
| Persistence threshold dependency | None at the evaluation level — `PERSISTENCE_THRESHOLD = 3` operates inside the per-transition verdict, not as a gate on evaluation |
| Idempotence | Same inputs → same output (deterministic). Two evaluations at the same `now` over the same DB rows return identical results |

### 20.3 Emission cadence

| Aspect | Specification |
|---|---|
| Per evaluation | One result dict per call, containing all detected transitions in the window |
| Per transition | Each detected `coordinated_state[i] ≠ coordinated_state[i-1]` boundary produces one record in `transitions[]` regardless of verdict |
| Post-persistence-gate emissions | None separately. Persistence is evaluated retroactively *within* the same call; there is no second emission "after the gate fires" |
| Push semantics | None. The layer is **pull-only**. Callers observe regime only by invoking |

### 20.4 Transition visibility delay (load-bearing)

A transition `A → B` at upstream timestamp `t` cannot be classified as `PERSISTENT` until **at least 3 snapshots beyond `t` exist in `intelligence_history`** (the persistence gate). Concretely:

- If a caller invokes the layer at `now = t + 1 snapshot`, the transition will appear with `persistence ≤ 2` and verdict FLICKER (or REVERSED if a bounce already occurred).
- If the same caller invokes again at `now = t + 4 snapshots` and the state has held, the verdict for the same `ts_ms` becomes PERSISTENT.

**Therefore:**

- The transition `ts_ms` is **the upstream snapshot's `ts_ms`**, not the moment of classifier confidence.
- The classifier's verdict on a transition is a **function of how far the window extends past it**, not a property of the transition alone.
- `regime timestamp ≠ exact market-transition timestamp`. The transition `ts_ms` reflects when the upstream writer first persisted the new state — itself a cadence-quantized event, not a continuous market instant.

### 20.5 Partial-window ambiguity

| Edge case | Behavior |
|---|---|
| Window beginning | The first observed snapshot has no predecessor in-window; cannot generate a transition record. If a transition exists *across* the window boundary (state changed at `since_ms − ε`), it is invisible |
| Window end | The last observed snapshot may be in a state that just entered. `persistence_snapshots` for the most recent transition is bounded by remaining window. Caller cannot distinguish "held briefly because it just happened" from "held briefly because it flickered" without waiting |
| Sparse upstream | `synthesize_intelligence` cadence gaps create apparent transitions where two adjacent rows belong to different cadence buckets; `acceleration` becomes unreliable if `PRE_WINDOW = 6` / `POST_WINDOW = 6` underflow |
| Missing rows (NULL filter) | Excluded before transition detection (§7); gap collapses two cadence buckets into one apparent transition |
| Insufficient continuity | `n < 24` → `data_quality = INSUFFICIENT`, `exploratory = True`. Transitions still emitted; their interpretation is gated by the flag |

### 20.6 Stale transition handling

**The layer has no autonomous decay.** If the upstream stream pauses (process down, DB write failure, intelligence_history retention prune):

- Subsequent calls see whatever rows persist; the window is unaware of the pause.
- A transition emitted before the pause does **not** have its confidence reduced by elapsed wall-clock time at evaluation. `current_state_duration_seconds` continues to be computed from `intelligence_history.ts_ms` deltas, not from `(now − last_ts)`.
- Stale-input handling is **caller-tier responsibility**: the operator UI must cross-check `last_ts vs now` to detect pause; the layer itself does not propagate UNKNOWN purely from wall-clock staleness.

**This is a known epistemic gap.** A long pause may leave the layer reporting a transition that "held for hours" when in fact the stream has been silent. The mitigation is operator-tier today; a runtime `stale_upstream` flag is **NOT IMPLEMENTED**.

### 20.7 Transition invalidation

| Mechanism | Effect on prior emit |
|---|---|
| **Reversal (REVERSED verdict)** | The transition record still exists; its verdict is REVERSED and `reversal_factor = 0.25` demotes confidence. The record is not deleted |
| **Invalidation by data correction** | If a historical `intelligence_history` row is corrected or deleted (admin action), subsequent calls produce different output. There is no audit of "previous emit" — the layer is recompute-from-source on each call |
| **Downgrade by lengthening evaluation window** | A previously PERSISTENT transition can become contextualized differently (e.g., as one transition inside a now-detected `oscillation_period`); the per-transition verdict does not change but the surrounding emit does |

**Important:** the layer does **not** retract a previously emitted result. There is no "the previous transition is now cancelled" mechanism. Callers that cache emits across calls must reconcile by re-reading.

### 20.8 Critical invariant (Section A close)

**Regime transitions are classification events under bounded observation windows, NOT authoritative timestamps of objective market-state change.**

A `transition.ts_ms` is the upstream writer's first-persistence timestamp of a state change *as recorded in our DB*. It is not "the moment the market entered state B". It does not claim that the market entered any state. It is the moment the synthesis layer's threshold ladder crossed a cutoff and the persistence layer wrote the resulting label.

Approved phrasing for surface and downstream:

- "classifier emitted a transition at `ts_ms = X`"
- "transition became observable after the persistence gate at `ts_ms + N × cadence`"
- "state crossed persistence gate"
- "observed configuration satisfied the threshold for `to_state`"

Forbidden phrasing on every surface:

- "the market entered regime B at `ts_ms`"
- "regime began at"
- "the market switched state at"
- "transition detected exactly at"
- "the market changed character at"

---

## 21. Regime Classification Boundary (load-bearing)

**Purpose.** Close the residual epistemic gap where an enum value like `ACTIVE_CASCADE_PROPAGATION` or a verdict like `ACCELERATING` could be misread as an *authoritative claim about market reality* rather than a *classification of the observed metric configuration*.

### 21.1 Boundary statement

A regime output is a **classification of the observed metric configuration at the call's evaluation time, against the layer's L0 thresholds**. It is not:

- An authoritative market condition.
- An objective systemic state.
- A macro interpretation.
- A risk assessment.
- A strategy posture.
- An assertion about counterparty behavior or participant intent.

The same set of upstream rows under different threshold values (a Class C change) would produce a different label. The same set of rows on a different cadence might produce no transition at all. The enum value is a function of the configuration, not a function of the market.

### 21.2 Enum allowed vs forbidden interpretation

`coordinated_state` values:

| Enum | ALLOWED interpretation | FORBIDDEN interpretation |
|---|---|---|
| `STABLE_COORDINATED_MARKET` | "synthesized_stress is below the lowest configured cutoff (25) and no escalation strategic_state is active" | "the market is calm", "conditions are safe", "no risk", "favorable to trade" |
| `EARLY_STRUCTURAL_STRESS` | "synthesized_stress crossed the 25 cutoff, or structural_break ≥ 50 with degrading meta_health" | "the market is turning", "early warning to act", "regime change imminent" |
| `STRUCTURAL_MARKET_DETERIORATION` | "structural_break_score ≥ 50 AND meta_intelligence_health ∈ {DEGRADING, CRITICAL} (lower stress condition unmet)" | "the market is breaking", "fundamental shift", "exit positions" |
| `FRAGMENTING_LIQUIDITY_ENVIRONMENT` | "synthesized_stress crossed 40 OR strategic_state = FRAGILE_SPECULATIVE_MARKET" | "liquidity is failing", "evacuation regime", "unsafe to trade" |
| `ESCALATING_SYSTEMIC_INSTABILITY` | "synthesized_stress crossed 55 OR strategic_state ∈ {TRANSITIONAL_UNSTABLE, LIQUIDITY_DETERIORATION_PHASE}" | "panic regime", "cascade incoming", "defensive posture warranted" |
| `ACTIVE_CASCADE_PROPAGATION` | "synthesized_stress crossed 70 OR strategic_state = CASCADE_RISK_ENVIRONMENT" | "cascade in progress", "exit everything", "market in crisis" |

Verdict values:

| Verdict | ALLOWED interpretation | FORBIDDEN interpretation |
|---|---|---|
| `REVERSED` | "recent transition did not sustain by the configured `REVERSAL_WINDOW = 3` gate" | "the market recovered", "stress passed", "back to normal" |
| `FLICKER` | "transition's persistence fell below `PERSISTENCE_THRESHOLD = 3`" | "regime is unstable" (without window qualifier — see §6), "noise to ignore" |
| `ACCELERATING` | "synthesized_stress pre/post slope diverged by ≥ 5.0 across the transition" | "the market is accelerating", "trend forming", "trade the momentum" |
| `PERSISTENT` | "transition held for ≥ 3 snapshots by configured threshold without significant slope change" | "regime change confirmed", "new market regime", "stable to act on" |

Data-quality values:

| State | ALLOWED interpretation | FORBIDDEN interpretation |
|---|---|---|
| `INSUFFICIENT` / `LOW` (`exploratory = True`) | "the layer lacks sufficient sample count to support classification authority; verdicts emitted but tagged exploratory" | "the market is stable" (default absence ≠ stability), "nothing happening" |

### 21.3 Multi-operator legitimacy

Two independent operators reading the same regime emit may legitimately interpret it differently. The layer publishes a **state classification**, not a **decision obligation**.

- Operator A may treat `ACCELERATING` as a signal to investigate cross-venue alignment via Phase 18.
- Operator B may treat the same `ACCELERATING` as noise pending data-quality confirmation.
- Neither interpretation is wrong; the layer makes no recommendation between them.

A surface that converts a regime emit into a uniform action obligation (auto-alert, escalation, ranking, posture change) is **outside this layer's mandate** and a [lip-governance §6](lip-governance.md) firewall violation.

### 21.4 Critical invariant (Section B close)

**Regime output = classification of observed metric configuration. Regime output ≠ authoritative market condition.**

---

## 22. Regime Surface Governance (load-bearing)

**Purpose.** The Regime Engine is the platform's most semantically dangerous layer — its enum labels are short, suggestive, and easily mistaken for market authority. This section enumerates exactly which surfaces are permissible.

### 22.1 What regime layer CAN surface

| Surfaceable element | Source field |
|---|---|
| Current state | `current_state` |
| Current state duration | `current_state_duration_snapshots`, `current_state_duration_seconds` |
| Transition records | `transitions[]` with per-transition verdict + rationale |
| Instability indication | stability label (`stable / noisy / unstable`) per §3.4 |
| Reversal indication | per-transition verdict `REVERSED` |
| Persistence-gate failure indication | per-transition verdict `FLICKER` |
| Insufficient continuity indication | `exploratory = True`, `data_quality` enum |
| Decomposition | full emit field list per §10 |
| Confidence | per-transition `confidence` scalar |
| Confidence downgrade reasons | implied by the multiplicative factors; surface MUST link to the §3.3 / §11 explanation |
| Missing upstream coverage | `snapshot_count`, gap-derived from `state_counts` distribution |
| Oscillation flag | `oscillation_periods[]` |

### 22.2 What regime layer MUST NEVER surface

| Forbidden surface element | Reason |
|---|---|
| Leverage posture (e.g., "reduce leverage") | Execution guidance — [lip-governance §6](lip-governance.md) firewall |
| Portfolio posture (e.g., "defensive allocation") | Execution guidance |
| Strategy recommendation | Execution guidance |
| Execution posture (e.g., "stop trading") | Execution guidance |
| Risk-on / risk-off label | §9 directional vocabulary ban |
| Defensive trading framing | Execution guidance |
| "Market safe" / "market unsafe" | §21 classification boundary violation |
| "Avoid market" / "favorable to trade" / "unfavorable conditions" | Execution guidance + classification boundary violation |
| Trader actionability score | §11 — no standalone score; would also be Class G learned-weight territory |
| Directional interpretation (any "up / down / continuation / reversal" outside the REVERSED verdict's narrow meaning) | §9 |
| Composite alert combining regime + per-symbol signals into a single actionable verdict | [lip-governance §7](lip-governance.md) composite contract violation + firewall |
| Regime-derived watchlist ordering presented as priority for trading | Borderline — permitted only as diagnostic ranking per [lip-governance §6](lip-governance.md), never as "trade these first" |

### 22.3 Critical invariant (Section C close)

**No regime output, alone or in combination with any other emit, constitutes execution guidance.**

This statement holds across:

- All operator dashboards.
- All notebook surfaces consuming regime emits.
- All alert formats incorporating regime context.
- All exports (CSV, JSON, screenshots) of regime emits.
- All replay overlays annotating historical regime context.
- All cross-layer composites that include regime as an input.

A consumer that maps a regime emit to an action does so outside the platform's mandate. The platform neither endorses nor recognizes that mapping.

---

## 23. Replay-Temporal Discipline (load-bearing)

**Purpose.** Strengthen [§13 Replay semantics](#13-replay-semantics) specifically along the temporal axis introduced by §20.

### 23.1 Replay window ambiguity

A replay invocation at wall-clock `T` with `lookback_days = N` evaluates the window `[T − N×86_400_000, T]`. A *different* replay at `T'` over the same upstream rows may produce:

- A different `current_state` (the window's last snapshot differs).
- A different `current_state_duration_*` (computed from window end).
- A different verdict for the most recent transition (if `T'` extends past the persistence gate that `T` did not).
- The same per-transition verdicts for all transitions strictly internal to `min(window(T), window(T'))`.

This is **not** drift; it is the expected behavior of a rolling-window classifier. Callers comparing replays at different `T`s must align windows explicitly.

### 23.2 Replay edge truncation

The earliest transition near `since_ms` cannot benefit from `PRE_WINDOW = 6` pre-snapshots if the window started fewer than 6 snapshots ago. The latest transition near `now` cannot benefit from `POST_WINDOW = 6` post-snapshots if it occurred fewer than 6 snapshots ago.

| Edge | Consequence |
|---|---|
| Early-edge transition | `pre_slope` may be `None`; `acceleration` cannot be computed; verdict cannot be ACCELERATING (falls through to PERSISTENT or FLICKER based on persistence/reversal) |
| Late-edge transition | `post_slope` and post-snapshots for persistence are partial; verdict may be FLICKER purely because the data hasn't aged enough yet |

Edge artifacts are **structural**, not defects. They cannot be eliminated without violating the bounded-window invariant in §20.

### 23.3 Transition continuity loss

If `intelligence_history` has a gap (writer down, DB outage, retention prune of an interior segment), two distinct upstream events may collapse into one apparent transition:

- `synthesize_intelligence` wrote `STATE_A` at `t1`, paused, resumed and wrote `STATE_B` at `t1 + Δ`.
- A replay sees a single transition `A → B` at `ts_ms = t1 + Δ`.
- No record indicates the intervening Δ was a continuity gap.

There is **no continuity-gap detection** in the current emit. `current_state_duration_seconds` will reflect the gap as if the state had been observed continuously. **NOT IMPLEMENTED:** a `continuity_gap_ms` field or a `coverage_fraction` over the window.

### 23.4 Replay-state dependency on upstream persistence

The layer can only reconstruct what `LiquidityIntelligenceHistory` retains. If retention prunes rows older than the desired replay window, the affected transitions are unreconstructable. Per [lip-execution-validation §21](lip-execution-validation.md) documentation labels:

- Pre-retention transitions → `REPLAY_NOT_PERSISTED` for those windows.
- Partial retention (window straddles boundary) → effectively `PARTIAL_RECONSTRUCTION` for the affected portion; verdicts within the retained portion remain valid.

These labels are **documentation vocabulary**, not runtime emits (see [lip-execution-validation §21](lip-execution-validation.md) discipline).

### 23.5 No exact historical regime-boundary guarantee

A replay does not recover **the exact moment** the market crossed a synthesized-stress threshold. It recovers **the snapshot at which the writer first persisted the post-threshold label**. The granularity is the upstream cadence (≈ 5 min historically per the function comment); finer-grained timing is unrecoverable from `intelligence_history` alone.

### 23.6 Critical invariant (Section D close)

**Replay reconstructs what the classifier emitted at the persisted snapshot cadence — NOT objective historical market state.**

A replay-reconstructed transition is a reconstruction of *the layer's prior emission for the same window*, not a re-observation of the market at higher fidelity. If the upstream synthesizer's thresholds change between original write and replay invocation, the replay may yield different verdicts on the same underlying upstream rows ([§13](#13-replay-semantics); calibration-version stamping NOT IMPLEMENTED per [lip-validation-and-calibration §5](lip-validation-and-calibration.md)).

---

## 24. Anti-overclaim invariant (load-bearing)

**Final invariant for this layer.**

> If any of: timestamp precision, upstream continuity, persistence-gate satisfaction, upstream availability, or replay coverage is insufficient, the layer MUST degrade certainty, visibility, or interpretability — and MUST NOT silently preserve regime authority.

**Currently-implemented degradations:**

- Insufficient sample count → `exploratory = True` flag + `scarcity_factor` damping confidence.
- Insufficient pre/post window → `acceleration = None` → verdict cannot escalate to ACCELERATING.
- Insufficient persistence → verdict demoted to FLICKER.
- Reversal within `REVERSAL_WINDOW` → confidence × 0.25.
- Missing `meta_confidence` → meta_conf_factor floored at 0.2 (capping confidence at 20% of remaining product).
- No transitions in window → no per-transition confidence; aggregate `flicker_ratio = 0.0` by definition without claiming stability narrative.

**Documented but NOT IMPLEMENTED degradations:**

- Wall-clock staleness of upstream stream → no `stale_upstream` flag (§20.6).
- Continuity-gap detection → no `continuity_gap_ms` (§23.3).
- Common-shock contamination → no `common_shock_unresolved` (§8).
- Cross-venue confirmation absence → no flag (§17).

**Operational rule.** Adding any of the "NOT IMPLEMENTED" degradations above is a Class B change requiring [lip-governance §2](lip-governance.md) audit and [lip-execution-validation §22](lip-execution-validation.md)-style acceptance contract. Until added, the operator surface MUST carry the corresponding caveat at the documentation tier (this companion is the load-bearing surface for those caveats).

**Inverse rule.** Removing any *implemented* degradation (e.g., loosening the `0.2` floor, raising `REVERSAL_WINDOW` to permit longer reversals to count, or suppressing the `exploratory` flag in operator UI) is a Class C calibration change and a candidate for [lip-governance §3](lip-governance.md) review depending on the rationale. Silent removal — change without §10 audit-trail entry — is a governance violation regardless of intent.

---

## 25. What this document is not

- Not a predictive regime model.
- Not a macro classifier.
- Not a market-narrative engine.
- Not a trading signal layer.
- Not a volatility-regime oracle.
- Not a "market is now bullish/bearish" surface.
- Not an authority on when the market entered or left any state (§20).
- Not a verdict on whether market conditions are safe, dangerous, favorable, or unfavorable (§21).
- Not a source of execution guidance, alone or composited (§22).
- Not a reconstruction of objective historical market state (§23).
- Not an authorization to add new emit fields, states, or thresholds during the Operational Observation Period.
- Not the source of `coordinated_state` or `dominant_regime` — those come from upstream layers.

It is a documentation hardening pass that formalizes the already-existing `market_state_transitions` observational layer, makes its constants, refusal-equivalents, verdict precedence, blind spots, temporal granularity, classification boundary, surface governance, replay-temporal discipline, and anti-overclaim invariant explicit, and pre-commits the layer to refusal-first and observation-only semantics under the governance contracts already in place.
