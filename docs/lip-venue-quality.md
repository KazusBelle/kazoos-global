# Venue Quality Layer — design contract (companion)

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md) (§9.7 `crossex`, §14.8 cross-venue confirmation, §13 known limitations), [`docs/lip-metric-registry.md`](lip-metric-registry.md) §A.7, [`docs/lip-epistemic-boundaries.md`](lip-epistemic-boundaries.md), [`docs/lip-governance.md`](lip-governance.md).

**Status: DESIGN CANDIDATE.** This document specifies a layer that **does not exist in code today**. No `venue_quality_*` metric is emitted; no venue-quality score, state, or enum is computed at runtime. The document defines *what such a layer would have to measure and not claim* if it were ever implemented. Per [lip-governance §2](lip-governance.md), this doc itself is a **Class A** artifact (documentation-only). Implementation would be **Class B + Class E** (new measurement layer + new persistence) and is **not authorized during Operational Observation Period** (see [project_operational_observation_period](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_operational_observation_period.md)).

**Boundary statement (load-bearing).** A Venue Quality Layer, if built, would measure **observable reliability of a venue's market data, visible book state, trade/book consistency, and visible execution conditions under replayable measurement constraints**. It would not measure venue honesty, exchange trustworthiness, manipulation, fraud, wash trading, hidden actors, real global liquidity, or future execution certainty. Those phrases are out-of-vocabulary for this layer — see §14 below and [lip-governance §3](lip-governance.md).

**Cross-cutting ontology invariant** (per [`lip-ontology-boundaries.md`](lip-ontology-boundaries.md)): the layer, if built, would emit bounded observational classifications under current instrumentation constraints. It would not establish authoritative venue or market ontology.

---

## 1. Definition

**Venue quality = observable reliability of a venue's market data, visible book state, trade/book consistency, and visible execution conditions under replayable measurement constraints.**

It is **not**:

- Trustworthiness in any legal sense.
- Proof of manipulation, fraud, or wash trading.
- Liquidity truth.
- A best-venue recommendation.
- An execution-routing instruction.
- A score of the exchange operator's intent or quality.

A high "venue quality" measurement under this contract means *the visible data this platform receives from the venue is consistent with itself and with peers, and the execution-condition diagnostics computed on top of it are not visibly degraded*. It says nothing about what is happening inside the matching engine, off-book, or off-venue.

---

## 2. Scope vs adjacent layers

This layer **would consume**, never re-derive, the following existing emits:

| Source | What it provides to Venue Quality |
|---|---|
| [`crossex`](lip-metric-registry.md#a7-cross-venue-divergence-crossex) | Pairwise divergence between this venue and the reference (Binance). Persisted 90 d in `liquidity_crossex_history` |
| [`credible_depth`](lip-metric-registry.md#a1-credible-depth-credible_depth) | Visible-depth survivability already measured per-symbol (Credible Depth ≠ Venue Quality; see §13) |
| [`exec_impact`](lip-execution-validation.md) | Per-burst divergence between book-walk and realized mid; an input to Trade/Book Coherence dimension |
| [`liquidity_ws_status`](2026-05-23-architecture-freeze.md#L707) | WS connection state, last frame ts — input to Data Availability |
| `intelligence.py:RecoveryEvent` (resiliency, recovery_time, refill_velocity per [A.2 / A.9 / A.10](lip-metric-registry.md)) | Refill reliability + post-stress recovery, input to Executable Depth Reliability |
| Freeze §13 known-limitations inventory | Pre-existing list of failure modes (WS desync, timestamp drift, venue outage) — input to Data Availability |

This layer would **not duplicate** any of the above. It would publish a venue-level *summary* whose components are pointers to existing emits, not re-computations.

**Adjacency contract.** Venue Quality is a **per-(venue, symbol, window)** aggregate; the existing layers above are per-symbol or per-event. The venue dimension is the new axis. Any field that already exists at the symbol level lives in its owning layer; Venue Quality only carries the venue-level rollup.

---

## 3. Implementation status overview (honest)

Current state for each proposed dimension. None of "Venue Quality" exists as an emitted layer; the underlying inputs vary widely in availability.

| Dimension | Input today | Today's gap | Verdict |
|---|---|---|---|
| A. Data Availability | `liquidity_ws_status` exists; ts of last frame tracked | No aggregation, no stale-frame frequency metric, no reconnect rate emission | **Partial input present; no aggregation** |
| B. Book Integrity | Live book state in `SymbolState.bids/asks` | No crossed-book counter, no top-of-book flicker metric, no diff-mismatch detection | **Inputs raw; no integrity metric** |
| C. Quote Persistence | Near-touch state observed per-frame | No lifetime distribution measured; Credible Depth's 400 ms persistence is closest proxy but is a symbol-level emit, not a venue diagnostic | **Proxy via Credible Depth only** |
| D. Executable Depth Reliability | `credible_depth`, `recovery_time_ms`, `refill_velocity`, `exec_impact` exist | No venue-level rollup; no depth-evaporation-frequency metric | **Inputs present; rollup missing** |
| E. Spread Stability | Spread computable per-frame | No spread-volatility metric, no expansion-frequency counter | **Computable but not computed** |
| F. Trade/Book Coherence | `exec_impact.divergence_bps` per burst; `book_exhausted` flag | Symbol-level only; no venue rollup of impact-mismatch frequency | **Inputs present; venue rollup missing** |
| G. Cross-Venue Consistency | `crossex` snapshots vs Binance reference; persisted 90 d | The CONFIRMED/LOCAL_ONLY/CONTRADICTED/UNAVAILABLE/INSUFFICIENT enum is **TZ-proposed but NOT IMPLEMENTED** (freeze §14.8). `crossex` is on-demand, not time-series; "sustained divergence" undefined | **Primitive present; classification not implemented** |
| H. Perp Stress Distortion | `liq_stress` ([A.6](lip-metric-registry.md#a6-liquidation-stress-liq_stress)) exists | No funding distortion metric, no mark/index divergence emit, no OI shock metric | **Partial input present** |
| I. Replay Reliability | Layer 12 (Phase 19) replays from `liquidity_samples`, `liquidity_alert_history`, `frozen_snapshots` | No venue-level replay-coverage rate; gap-frequency not measured per venue | **Replay exists; venue rollup missing** |
| J. Measurement Blind Spots | Documented in [lip-execution-validation §10](lip-execution-validation.md) + [lip-epistemic-boundaries §2](lip-epistemic-boundaries.md) | (this dimension is by nature negation — see §10) | **Already documented; reuse** |

**Coverage asymmetry.** Even where inputs exist, **Binance is deeply observed** (WS L2 depth20 + trade tape + book_history ring) and **Bybit is shallowly observed** (only through `crossex` REST snapshots). Any Venue Quality output for Bybit would be missing dimensions B, C, D, E, F, H, I entirely. This is a structural data-availability constraint, not a design choice (see freeze §14.8: "structurally Binance-centric").

---

## 4. Measurement dimensions

Each dimension is specified by observable inputs, the contract it would publish, and its current implementation status. All numeric thresholds below are **DESIGN PROPOSALS**, not implementation values; if the layer were built, they would enter [lip-governance §5](lip-governance.md) as Implementation constants at L0 maturity.

### 4.A Data Availability

| Aspect | Specification |
|---|---|
| **Observable inputs** | `liquidity_ws_status.last_frame_ts`, reconnect counter (if tracked), REST fetch outcomes from `crossex` poller |
| **Measurable contract** | Fraction of measurement window with `now − last_frame_ts < STALE_FRAME_THRESHOLD`; count of WS reconnects per window; count of REST fetch failures per window |
| **Output fields** | `data_availability_state ∈ {AVAILABLE, INTERMITTENT, STALE, OUTAGE}`; `stale_fraction`, `reconnect_count`, `rest_fail_count` |
| **Failure / unknown** | If window has no frames at all, `data_availability_state = OUTAGE` and no other dimension emits |
| **Blind spots** | Cannot distinguish "venue down" from "our subscription dropped"; cannot detect silent drift (server-side `lastUpdateId` skip per freeze §13) without a diff-vs-rest reconciliation loop, which is not implemented |
| **Calibration status** | All thresholds NOT IMPLEMENTED. `STALE_FRAME_THRESHOLD` is a design candidate; would start at L0 |
| **Validation status** | PENDING. Distribution of stale fractions across venues never measured |
| **Governance status** | Class B implementation; gated on Observation Period exit |

### 4.B Book Integrity

| Aspect | Specification |
|---|---|
| **Observable inputs** | Live `SymbolState.bids/asks`; per-frame snapshot of best bid/ask |
| **Measurable contract** | Count of crossed-book frames (`best_bid ≥ best_ask`), empty-side frames, top-of-book flicker rate, snapshot/diff mismatches (when both surfaces available) |
| **Output fields** | `book_integrity_state ∈ {CLEAN, FLICKERING, DEGRADED, INVALID}`; `crossed_frame_count`, `empty_side_count`, `flicker_rate` |
| **Failure / unknown** | If book is INVALID, no downstream dimension on this venue/symbol emits |
| **Blind spots** | Flicker may be legitimate market behavior, not a venue defect — cannot disambiguate without external reference |
| **Calibration status** | NOT IMPLEMENTED. No crossed-book counter today |
| **Validation status** | PENDING. Distribution of integrity events per venue not measured |
| **Governance status** | Class B; gated |

### 4.C Quote Persistence

| Aspect | Specification |
|---|---|
| **Observable inputs** | Per-frame depth snapshots; near-touch level lifetimes derivable from frame diffs |
| **Measurable contract** | Median lifetime of best-N levels; survival rate at 300 ms / 500 ms / 1 s; cancel-burst frequency (rapid disappearance of multiple levels) |
| **Output fields** | `quote_persistence_state ∈ {STABLE, BURSTY, EVAPORATIVE}`; `median_lifetime_ms`, `survival_300ms`, `survival_500ms`, `cancel_burst_count` |
| **Failure / unknown** | If frame cadence < survival horizon being measured, that survival fraction is UNKNOWN for the window |
| **Blind spots** | Cannot distinguish MM cancels from external sweeps; cannot observe queue priority within a level |
| **Calibration status** | Credible Depth's `CREDIBLE_MIN_AGE_MS = 400` is a partial proxy at the symbol level; venue-level rollup NOT IMPLEMENTED |
| **Validation status** | PENDING |
| **Governance status** | Class B; gated |

### 4.D Executable Depth Reliability

| Aspect | Specification |
|---|---|
| **Observable inputs** | `credible_depth` (per-symbol), `recovery_time_ms`, `refill_velocity`, `exec_impact.book_exhausted` frequency |
| **Measurable contract** | Venue-level rollup of: credible-depth-to-raw-depth ratio distribution; depth-evaporation frequency under stress windows; refill-success rate after evaporation |
| **Output fields** | `credible_depth_reliability_state ∈ {RELIABLE, VARIABLE, UNSTABLE}`; per-side rollups, exhaustion frequency, recovery-time median |
| **Failure / unknown** | If no `MEASURED` exec_impact events in window, exhaustion rollup is INSUFFICIENT |
| **Blind spots** | Cannot observe whether off-top-20 depth would have held; cannot disambiguate venue evaporation from MM withdrawal across all venues simultaneously |
| **Calibration status** | All component inputs exist at L0 ([lip-execution-validation §23](lip-execution-validation.md)); venue rollup NOT IMPLEMENTED |
| **Validation status** | PENDING for both inputs and rollup |
| **Governance status** | Class B; gated |

### 4.E Spread Stability

| Aspect | Specification |
|---|---|
| **Observable inputs** | Per-frame best bid / best ask |
| **Measurable contract** | Median quoted spread; spread volatility (e.g., IQR/median); count of spread expansions > k× median; median recovery time from expansion |
| **Output fields** | `spread_stability_state ∈ {STABLE, VOLATILE, DEGRADED}`; `spread_median_bps`, `spread_iqr_bps`, `expansion_count`, `expansion_recovery_median_ms` |
| **Failure / unknown** | Sub-cent symbols / wide-tick symbols may have structurally noisy spread; threshold must be per-symbol, not per-venue |
| **Blind spots** | Cannot distinguish MM-pulling spread (informational withdrawal) from order flow widening it |
| **Calibration status** | NOT IMPLEMENTED |
| **Validation status** | PENDING |
| **Governance status** | Class B; gated |

### 4.F Trade / Book Coherence

| Aspect | Specification |
|---|---|
| **Observable inputs** | `exec_impact` per-burst events (`expected_bps`, `realized_bps`, `divergence_bps`, `book_exhausted`); trade tape ts; book frame ts |
| **Measurable contract** | Venue-level distribution of `divergence_bps` per (side, bucket); rate of prints-through-visible-levels; trade/book timestamp alignment quality |
| **Output fields** | `trade_book_coherence_state ∈ {COHERENT, MISALIGNED, ANOMALOUS}`; divergence median, exhaustion frequency, timestamp-skew median |
| **Failure / unknown** | Insufficient `MEASURED` event count in window → INSUFFICIENT. Large `divergence_bps` is a **measurement of mismatch**, not "the venue lied" (mandatory framing per [lip-execution-validation §6, §11, §26](lip-execution-validation.md)) |
| **Blind spots** | Hidden / iceberg / RPI participation in bursts (per [lip-execution-validation §10](lip-execution-validation.md)) |
| **Calibration status** | exec_impact inputs at L0; venue rollup NOT IMPLEMENTED |
| **Validation status** | PENDING |
| **Governance status** | Class B; gated |

### 4.G Cross-Venue Consistency

| Aspect | Specification |
|---|---|
| **Observable inputs** | `crossex` snapshots (Bybit vs Binance reference); `liquidity_crossex_history` (90 d) |
| **Measurable contract** | Persistence of divergence above operator-set threshold (the "sustained" qualifier mentioned in [lip-metric-registry §A.7](lip-metric-registry.md) but **not formalized today**); depth-share vs volume-share divergence; basis divergence for perps; co-occurrence of local stress without peer confirmation |
| **Output fields** | `cross_venue_status ∈ {CONFIRMED, LOCAL_ONLY, CONTRADICTED, UNAVAILABLE, INSUFFICIENT}` per §7. Plus `divergence_persistence_s`, `peer_venues_used` |
| **Failure / unknown** | If reference venue fetch fails, UNAVAILABLE. If only one venue available, INSUFFICIENT |
| **Blind spots** | Bybit is the *only* current peer (freeze §14.8); "cross-venue" is binary, not distributed |
| **Calibration status** | NOT IMPLEMENTED. TZ-proposed enum since 2026-05; never built |
| **Validation status** | PENDING |
| **Governance status** | Class B; gated. Reuses persisted `liquidity_crossex_history`, so Class E impact is minimal |

### 4.H Perp Stress Distortion

| Aspect | Specification |
|---|---|
| **Observable inputs** | `liq_stress` per [A.6](lip-metric-registry.md#a6-liquidation-stress-liq_stress); funding rate (REST); mark price / index price if exchange API exposes both |
| **Measurable contract** | Share of volume attributable to liquidations; OI shock frequency; funding distortion (delta from peer venue funding); mark/index divergence |
| **Output fields** | `perp_stress_state ∈ {NORMAL, ELEVATED, DISTORTED, NOT_APPLICABLE}`. NOT_APPLICABLE for spot |
| **Failure / unknown** | Spot symbols → NOT_APPLICABLE (not UNKNOWN). Missing funding feed → relevant sub-field INSUFFICIENT |
| **Blind spots** | OTC liquidation, ADL queue position, cross-margin offsetting |
| **Calibration status** | `liq_stress` exists at L0; other inputs NOT IMPLEMENTED |
| **Validation status** | PENDING |
| **Governance status** | Class B; gated |

### 4.I Replay Reliability

| Aspect | Specification |
|---|---|
| **Observable inputs** | Coverage of persisted tables for the venue/symbol over the window; gap detection in `liquidity_samples` cadence |
| **Measurable contract** | Fraction of window with replay-reproducible state; count of replay gaps; per-venue replay-input completeness |
| **Output fields** | `replay_availability ∈ {AVAILABLE, PARTIAL, UNAVAILABLE}` using the [lip-execution-validation §21](lip-execution-validation.md) labels (documentation vocabulary, not invented runtime states) |
| **Failure / unknown** | UNAVAILABLE when no persisted rows for window; PARTIAL when gaps present |
| **Blind spots** | L2 depth20 not persisted to disk (freeze §13) — per-burst replay structurally absent regardless of venue |
| **Calibration status** | NOT IMPLEMENTED (no per-venue replay-coverage metric exists) |
| **Validation status** | PENDING |
| **Governance status** | Class B; gated |

### 4.J Measurement Blind Spots

This dimension is by nature a negation. The blind-spot inventory **already documented** in [lip-execution-validation §10](lip-execution-validation.md), [lip-execution-validation §24](lip-execution-validation.md) (structurally unknowable conditions), and [lip-epistemic-boundaries §2 / §5](lip-epistemic-boundaries.md) applies in full to Venue Quality. No re-derivation; the Venue Quality layer would **emit a pointer**, not a list.

**Specifically inaccessible at venue level:** hidden / iceberg / RPI flow, internalized / OTC routing, queue priority, matching-engine internals, self-trade groups, beneficial ownership, true wash-trading, legal manipulation intent.

**Consequence (load-bearing).** Venue Quality output **cannot be used as fraud proof, manipulation proof, wash-trading evidence, or legal exhibit**. The score, if built, would summarize visible-data reliability; it could not constitute or contribute to an accusation. This is explicit in §14 below and in [lip-governance §3 (Inferred intent scoring)](lip-governance.md).

---

## 5. Proposed output structure

Per (venue, symbol, measurement_window) tuple, the layer would emit a single record. Fields below are **proposed**; the record is not written today.

```
venue                          # e.g., "binance", "bybit"
symbol                         # e.g., "BTCUSDT"
product_type                   # "spot" | "perp"
ts                             # window end timestamp (UTC ms)
measurement_window_ms          # e.g., 5_min, 1_h (configurable)
schema_version                 # per lip-governance §8 — NOT IMPLEMENTED in platform today
calibration_version            # per lip-governance §8 — NOT IMPLEMENTED in platform today

data_availability_state        # AVAILABLE | INTERMITTENT | STALE | OUTAGE
book_integrity_state           # CLEAN | FLICKERING | DEGRADED | INVALID
quote_persistence_state        # STABLE | BURSTY | EVAPORATIVE
credible_depth_reliability     # RELIABLE | VARIABLE | UNSTABLE
spread_stability_state         # STABLE | VOLATILE | DEGRADED
trade_book_coherence_state     # COHERENT | MISALIGNED | ANOMALOUS
cross_venue_status             # CONFIRMED | LOCAL_ONLY | CONTRADICTED | UNAVAILABLE | INSUFFICIENT
perp_stress_state              # NORMAL | ELEVATED | DISTORTED | NOT_APPLICABLE
replay_availability            # AVAILABLE | PARTIAL | UNAVAILABLE

score_components               # named per-dimension sub-scores; see §10
evidence_status                # which dimensions had sufficient samples
blind_spots                    # pointer to lip-execution-validation §10/§24, lip-epistemic §2/§5
refusal_reason                 # populated when overall_state = UNKNOWN; one of §8 conditions
calibration_status             # link to current calibration record per dimension
validation_status              # PENDING for all dimensions today
```

**No "verdict" field.** The output does not say "good venue / bad venue". The operator reads the dimensional decomposition.

**No "trade here" or "avoid here" field.** Per §13 and [lip-governance §6](lip-governance.md) firewall.

---

## 6. State machine (DESIGN ONLY)

The states below are **documentation labels for the design**, not runtime enums. If the layer is built, the implementation might choose this state graph or a different one; the doc must be updated to match the chosen graph at that time. Per [lip-execution-validation §4](lip-execution-validation.md) discipline: do not claim an implemented state machine when the code does not implement one.

| Proposed state | Entry condition (proposed) | Notes |
|---|---|---|
| **UNKNOWN** | Insufficient data, venue unavailable, no recent samples, or any §8 refusal condition | Initial state for any new (venue, symbol) pair; absorbing on outage |
| **OBSERVABLE** | Data available, minimum sample coverage met, no severe integrity flags | The baseline non-degraded state |
| **DEGRADED** | One or more of: stale periods within window, spread instability above threshold, book flicker above threshold, low quote persistence, replay gaps | Recoverable; on next window may return to OBSERVABLE |
| **UNRELIABLE** | Repeated integrity failures across consecutive windows, severe stale periods, sustained cross-venue contradiction, persistent trade/book mismatch | Sticky; requires k consecutive clean windows to leave |
| **STRUCTURALLY_LIMITED** | Venue data lacks fields needed for one or more dimensions (e.g., Bybit shallow observation), or blind-spot inventory dominates the measurable surface for the symbol | Permanent for the data-coverage cause; not a defect |

**Promotion / demotion thresholds: PROPOSED, NOT IMPLEMENTED.** The "k consecutive windows" rule for leaving UNRELIABLE is a design candidate.

**Cross-venue independence.** A venue is in STRUCTURALLY_LIMITED if peer data is unavailable regardless of its own data quality — i.e., this state is not solely the venue's "fault" (the word "fault" is itself out of vocabulary; see §14).

---

## 7. Cross-venue status enum (DESIGN; reuses freeze §14.8 TZ proposal)

Reuses the enum already proposed-but-NOT-IMPLEMENTED in [freeze §14.8](2026-05-23-architecture-freeze.md). No new vocabulary invented.

| State | Meaning | What it does NOT mean |
|---|---|---|
| **CONFIRMED** | Venue's measured behavior agrees with at least *k* configured peer venues over the window | Not "the venue is honest" — only "peers agree on the visible state" |
| **LOCAL_ONLY** | Deterioration appears on this venue only; peers show no analogous signal | Not "the venue is faulty" — could be local market-microstructure event, peer subscription issue, or genuine venue-local stress. No causal attribution |
| **CONTRADICTED** | Venue's signal conflicts with peers (e.g., persistent price separation beyond threshold) | Not "the venue is wrong" or "the venue is manipulating" — could be reference venue lagging, perp/spot basis divergence, peer feed degraded |
| **UNAVAILABLE** | Peer venue data is unavailable for the window | Not a venue-quality verdict; a measurement-availability statement |
| **INSUFFICIENT** | Not enough peer samples to reach a verdict | Not a verdict — explicit non-emission |

**Threshold semantics: NOT IMPLEMENTED.** "Sustained divergence", configured `k` peers, contradiction magnitude — all proposed thresholds, would enter [lip-governance §5](lip-governance.md) at L0.

**Single-peer reality.** Today the only peer is Bybit (freeze §14.8). "Configured peer venues" is plural in design but singular in practice; the layer must honestly emit `peer_venues_used = 1` and any "CONFIRMED" verdict is "agrees with Bybit", not "agrees with the market".

---

## 8. Refusal / UNKNOWN conditions

The layer would emit `overall_state = UNKNOWN` with `refusal_reason` set to one of the following, rather than fabricate a verdict:

| Refusal reason | Condition | Implementation status |
|---|---|---|
| `NO_RECENT_WS_FRAMES` | `now − last_frame_ts > STALE_FRAME_THRESHOLD` for the entire window | Input available (`liquidity_ws_status`); threshold NOT IMPLEMENTED |
| `SNAPSHOT_STALE` | Most-recent depth snapshot older than window/2 | NOT IMPLEMENTED |
| `INSUFFICIENT_BOOK_SAMPLES` | Fewer than `MIN_BOOK_SAMPLES_PER_WINDOW` snapshots ingested | NOT IMPLEMENTED |
| `MISSING_TRADE_TAPE` | Window contains no parsed trades for the venue/symbol | Input available; counter NOT IMPLEMENTED |
| `SYMBOL_NOT_SUBSCRIBED` | Symbol not in active WS subscription for venue | Input available |
| `REPLAY_UNAVAILABLE` | `liquidity_samples` rows for venue/symbol/window missing or gapped | Persistence exists; coverage metric NOT IMPLEMENTED |
| `CROSS_VENUE_REFERENCE_UNAVAILABLE` | Reference venue fetch failed for full window | Partial — `crossex` poller drops silently per freeze §13 |
| `TIMESTAMP_DRIFT_SUSPECTED` | Local clock skew vs server time exceeds threshold | Detection NOT IMPLEMENTED — freeze §13 line 1065 lists this as a known unaddressed limitation |
| `VENUE_OUTAGE_OVERLAP` | Window overlaps a documented venue-outage event (operator-tagged) | Outage-tagging not formalized; design candidate |
| `DATA_QUALITY_CONFIDENCE_BELOW_MIN` | Composite of above falls below acceptance floor | NOT IMPLEMENTED — gated on the composite itself being built |

**Refusal discipline.** Per [lip-governance §3](lip-governance.md), a refusal is not a defect; emitting a fabricated value under any of these conditions would be. The layer would prefer UNKNOWN to invention.

---

## 9. Blind spots (canonical, pointing)

The Venue Quality layer **inherits** the blind-spot inventory of its inputs. It introduces no new blind spots and removes none. Authoritative inventories:

- [lip-execution-validation §10](lip-execution-validation.md) — hidden/iceberg/RPI/OTC/queue priority/cross-venue routing/fill probability/institutional logic
- [lip-execution-validation §24](lip-execution-validation.md) — structurally unknowable conditions (epistemic ceilings)
- [lip-epistemic-boundaries §2](lip-epistemic-boundaries.md) — non-inference list (intent, motive, coordination, etc.)
- [lip-epistemic-boundaries §5](lip-epistemic-boundaries.md) — structurally unknowable conditions

**Consequence.** Venue Quality emit **cannot be used as evidence of fraud, manipulation, wash trading, or any legal claim about the venue or its participants**. This is not a current limitation to be patched later — it is the epistemic ceiling of the input set.

---

## 10. Score structure (DESIGN ONLY; no weights invented)

If a composite venue score is ever built, it would be subject to [lip-governance §7 Composite Creation Contract](lip-governance.md). The proposed structure:

```
venue_quality_score = f(
    DataAvailability,
    BookIntegrity,
    QuotePersistence,
    ExecutableDepthReliability,
    SpreadStability,
    TradeBookCoherence,
    CrossVenueConsistency,
    PerpStressAdjustment,
    ReplayReliability,
    − DataQualityPenalty
)
```

**Constraints if implemented:**

1. **No black-box score.** Every component MUST publish its sub-score alongside the composite.
2. **Weights NOT IMPLEMENTED.** No `w_data`, `w_book`, … values exist or are proposed in this document. Any future weight assignment is a Class C calibration change requiring §22 acceptance per [lip-execution-validation.md](lip-execution-validation.md).
3. **No "learned" weights ever.** Per [lip-governance §3](lip-governance.md) (hidden ML weighting is Class G forbidden).
4. **Stale-input behavior.** If any component is INSUFFICIENT or UNKNOWN, composite is suppressed (per [lip-governance §7](lip-governance.md) default: "composite suppressed", not "treat as zero").
5. **Composite maturity ≤ lowest input maturity.** All current inputs are L0 → composite would be L0 → not publishable as "venue rating", only as diagnostic decomposition.

**No score is implemented today.** Listing the structure here is design discipline, not a deployment plan.

---

## 11. Calibration status per component

Every threshold below is **NOT IMPLEMENTED**. The table records design candidates and the maturity class they would enter at per [lip-governance §5](lip-governance.md). All would start at **Implementation constant (L0)**.

| Component | Proposed anchor | Status |
|---|---|---|
| `STALE_FRAME_THRESHOLD` | TBD; would need per-venue baseline | NOT IMPLEMENTED |
| `MIN_BOOK_SAMPLES_PER_WINDOW` | TBD; cadence-dependent | NOT IMPLEMENTED |
| Book integrity flicker threshold | TBD | NOT IMPLEMENTED |
| Quote persistence horizons (300 ms / 500 ms / 1 s) | Design proposal | NOT IMPLEMENTED |
| Spread expansion `k× median` cutoff | TBD per symbol | NOT IMPLEMENTED |
| Cross-venue "sustained divergence" duration | TBD | NOT IMPLEMENTED. [lip-metric-registry §A.7](lip-metric-registry.md) explicitly notes `divergence_persistence` is missing |
| UNRELIABLE → OBSERVABLE recovery `k` | TBD | NOT IMPLEMENTED |
| Composite weights `w_*` | None proposed | EXPLICITLY NOT PROPOSED per §10 |

**Required pending measurements** before any anchor moves above L0 (per [lip-execution-validation §22](lip-execution-validation.md) governance template):

- Venue uptime distribution across active venues
- Stale-frame frequency under normal vs reconnect-storm conditions
- Quote lifetime distribution per (venue, symbol, side)
- Depth-survival distribution under stress windows
- Impact-mismatch distribution per venue (extends exec_impact backlog to venue rollup)
- Spread outlier recurrence
- Cross-venue contradiction rate
- Replay-availability rate per venue
- Liquidation-distortion frequency (perp only)

---

## 12. Validation plan

Per-dimension governance contract (template from [lip-execution-validation §22](lip-execution-validation.md), abridged for design tier). All rows are **acceptance criteria for future implementation**; none are passed today.

| Dimension | Min sample size | Window | Acceptance | Rejection | Regime coverage | Recalibration cadence | Replay coverage requirement |
|---|---|---|---|---|---|---|---|
| Data Availability | ≥ 30 days continuous emit per (venue, symbol) | 5 min rolling | Stale-fraction distribution stable across two non-overlapping observation windows | Distribution shift > 50% unexplained | Calm + reconnect-storm regimes labelled | Quarterly | From `liquidity_ws_status` history |
| Book Integrity | ≥ 1 000 frames per (venue, symbol) | 5 min | Crossed/empty counts ≈ 0 in normal regime | Persistent non-zero outside outage | Calm + stress | Quarterly | Frame logs (currently not persisted; gap) |
| Quote Persistence | ≥ 1 000 near-touch level events per (venue, symbol, side) | 5 min | Survival rates stable across windows | > 50% shift unexplained | Calm + stress | Quarterly | Per-frame diff history (gap) |
| Credible Depth Reliability | ≥ 30 days per symbol | 5 min | Rollup distribution consistent with input layer | Drift in input layer (`credible_depth`) | All credible-depth regimes | Monthly | From persisted `liquidity_samples` |
| Spread Stability | ≥ 1 000 frames per symbol | 5 min | Median+IQR stable | Persistent expansion | Per-symbol baseline | Quarterly | Frame logs (gap) |
| Trade/Book Coherence | ≥ 1 000 `MEASURED` exec_impact events per (venue, symbol) | 5 min | Distribution stable | Unexplained drift | Per exec_impact §22 | Inherits exec_impact cadence | From persisted exec events (gap — only medians persisted) |
| Cross-Venue Consistency | ≥ 7 days of `crossex` polling per pair | 1 h | Persistence distribution stable | Drift in reference venue | Per-pair | Monthly | From `liquidity_crossex_history` (90 d) |
| Perp Stress Distortion | ≥ 7 days per perp symbol | 1 h | `liq_stress` rollup stable | Drift | Calm + liquidation cascade | Monthly | From `liquidity_samples` |
| Replay Reliability | per-window coverage check | 1 h | Coverage > operator-set floor | Persistent gaps | All regimes | Monthly | Self-referential |

**Governance.** Each row, if executed, becomes a `lip-governance §10` audit-trail entry. None are run; the layer is at "Experimental" stage per [lip-governance §9](lip-governance.md).

---

## 13. Relationship to existing layers

| Layer | Allowed interaction | Disallowed interaction |
|---|---|---|
| **Credible Depth** ([A.1](lip-metric-registry.md#a1-credible-depth-credible_depth)) | Consume `credible_depth` per-symbol emit as input to Executable Depth Reliability rollup | Re-derive Credible Depth; treat Credible Depth as a "venue quality signal" rather than a per-symbol depth measurement |
| **Execution Validation** (`exec_impact`, [lip-execution-validation.md](lip-execution-validation.md)) | Consume per-burst divergence stream as input to Trade/Book Coherence | Aggregate exec_impact into a "venue verdict" without honoring [exec-validation §26 allowed-claims](lip-execution-validation.md). Frame divergence as "the venue lied" |
| **Cross-Venue Divergence** (`crossex`, [A.7](lip-metric-registry.md#a7-cross-venue-divergence-crossex)) | Consume `liquidity_crossex_history` as primary input for §7 enum | Treat sustained divergence as manipulation evidence |
| **Distributed Stress** (freeze §14, [lip-epistemic-boundaries §4](lip-epistemic-boundaries.md)) | Read distributed-stress verdicts as context for §7 LOCAL_ONLY vs CONFIRMED disambiguation | Provide input *back into* Distributed Stress without explicit governance review (would create feedback loop; Class F territory) |
| **Data Quality Gates** | Consume per-input gates as drivers of refusal in §8 | Bypass data-quality gates by treating them as soft hints |
| **Replay Reconstruction** (Layer 12, Phase 19) | Consume per-window replay-availability state as §4.I input | Modify replay semantics — replay is upstream, not downstream |
| **Epistemic Boundaries** ([lip-epistemic-boundaries.md](lip-epistemic-boundaries.md)) | Inherit non-inference list and unknowable conditions wholesale | Re-derive or relax |
| **Calibration Governance** ([lip-governance §5](lip-governance.md), [lip-validation-and-calibration.md](lip-validation-and-calibration.md)) | Every venue-quality threshold subject to maturity class | "Self-calibrating" venue scoring |

**Allowed usage (operator-facing):**

- Consume component metrics for venue-level diagnostic summary.
- Publish a venue-level dashboard panel (post-Observation-Period only).
- Downgrade operator's confidence in other emits via tagging (e.g., a Cross-Venue CONTRADICTED window tags affected per-symbol alerts as "peer-venue contradicted").
- Tag venue-local anomalies for investigation per Phase 18.
- Support operator investigation by surfacing the dimensional decomposition.

**Not allowed (firewall — [lip-governance §6](lip-governance.md)):**

- Trade routing or routing recommendation.
- Best venue recommendation.
- Autonomous execution.
- Fraud labeling.
- Manipulation attribution.
- Hidden actor inference.

---

## 14. UI language discipline

If venue-quality output is ever surfaced in operator UI, only the following labels are allowed. The mapping is intentionally rigid.

| BANNED label | Approved replacement |
|---|---|
| Fake venue / fake exchange | (no replacement — concept rejected at design time) |
| Trusted venue / trustworthy venue | OBSERVABLE per §6 |
| Dishonest exchange / fraudulent exchange | (rejected) |
| Manipulated venue | LOCAL_ONLY deterioration OR cross-venue CONTRADICTED (per §7), with explicit "not a manipulation claim" tooltip |
| Best exchange / preferred venue | (rejected — firewall §13) |
| Safe venue / unsafe venue | OBSERVABLE / DEGRADED / UNRELIABLE per §6 |
| Wash trading detected | (rejected — see §9 epistemic ceiling) |
| Clean / dirty venue | (rejected) |
| Honest book / dishonest book | data integrity CLEAN / DEGRADED per §4.B |
| Real liquidity / fake liquidity | (rejected — banned platform-wide per [lip-execution-validation §15](lip-execution-validation.md)) |
| Top venue | (rejected — see §18 no-global-ranking invariant) |
| Best venue overall | (rejected — see §18) |
| Most trustworthy exchange | (rejected — see §18; also §1 negation) |
| Highest quality exchange | (rejected — see §18) |

| Approved status phrase | When applicable |
|---|---|
| "Data degraded" | §6 DEGRADED |
| "Cross-venue contradicted" | §7 CONTRADICTED |
| "Local-only deterioration" | §7 LOCAL_ONLY |
| "Low quote persistence" | §4.C BURSTY/EVAPORATIVE |
| "High impact mismatch" | §4.F MISALIGNED/ANOMALOUS |
| "Replay unavailable" | §4.I UNAVAILABLE |
| "Visible liquidity unreliable" | §4.D UNSTABLE |
| "Insufficient evidence" | §8 INSUFFICIENT family |
| "Highest observable reliability under current measurement window" | Comparative phrasing when an operator asks "which venue is doing better right now"; only valid scoped to the window, never as a standing claim (§18) |
| "Locally consistent venue state" | §6 OBSERVABLE in a window where §7 is CONFIRMED or peer data was UNAVAILABLE |
| "Cross-venue-confirmed conditions" | §7 CONFIRMED |
| "Stable observable conditions" | §6 OBSERVABLE held across consecutive windows without DEGRADED entry |

**Enforcement.** A UI label outside the approved set is a [lip-governance §3](lip-governance.md) row 11 violation (semantic relabeling without versioning) AND a §3 row 9 violation (inferred intent scoring) simultaneously.

---

## 15. Governance integration

Per [lip-governance §2 change classification](lip-governance.md), this layer's lifecycle:

| Phase | Class | Authorization |
|---|---|---|
| **This document (design)** | A (documentation-only) | Authorized; consistent with Operational Observation Period (UX/wording/doc-tier work allowed) |
| **Implementation of any single dimension** | B (measurement-layer) + likely E (persistence schema additions) | **NOT AUTHORIZED during Observation Period.** Gated on period exit + governance review |
| **Composite score emission** | B + §7 composite contract declaration | NOT AUTHORIZED today; additionally gated on every input reaching ≥ L1 |
| **Surfacing on operator UI** | C+D in some cases (depending on threshold visibility) | Gated on §9 maturity reaching Operator-visible (currently no L1+ layer in platform) |
| **Cross-layer wiring** (e.g., downgrading other emits' confidence based on Venue Quality) | B per affected emit | NOT AUTHORIZED today; would need acceptance contracts per affected layer |

**Required declarations on every Venue Quality emit (per [lip-governance §8](lip-governance.md), if/when versioning is implemented):**

- `schema_version` — NOT IMPLEMENTED in platform today
- `calibration_version` — NOT IMPLEMENTED in platform today
- `runtime_generation` — NOT IMPLEMENTED in platform today
- `observation_period_state` — would be `OBSERVATION` at any post-period launch

**Allowed consumers (initial, post-Observation-Period):** operator dashboards (read-only), Phase 18 investigation tooling, [lip-validation-and-calibration.md](lip-validation-and-calibration.md) anti-spoof cross-tabulation (already on backlog).

**Forbidden consumers (permanent per [lip-governance §6](lip-governance.md)):** any order-routing layer, any auto-action layer, any composite scoring layer that does not declare per §7, any external reporting that would frame a venue-quality value as an accusation.

**Maturity entry point.** A first emission of any Venue Quality dimension would enter [lip-governance §9](lip-governance.md) at **Experimental**. Promotion to Observational requires the §12 validation row for that dimension to complete its observation-period requirement (≥ 30 d continuous emit + determinism re-verification + blind-spot inventory + operator review).

---

## 16. Execution-capacity boundary (load-bearing invariant)

**Invariant.** Venue Quality ≠ Executable Capacity. A Venue Quality emit at any value, in any state, under any window, does not characterize the venue's ability to absorb a given order.

**The layer measures:**

- Reliability of observable venue conditions over a measurement window (per §4 dimensions).

**The layer does NOT measure:**

- Venue capacity to absorb arbitrary order size.
- Execution quality for any specific order size.
- Queue-position outcome for a hypothetical order.
- Routing efficiency through or around the venue.
- Hidden-liquidity availability (per §9 epistemic ceiling).
- Market impact certainty for a future trade.

**Execution capacity depends on factors outside this layer's mandate:**

- Order size (Venue Quality is order-size-agnostic — it does not know about hypothetical orders).
- Timing within the window (Venue Quality is window-averaged; intra-window microstructure is not summarized).
- Queue position (structurally unobservable — see §9 and [lip-execution-validation §24](lip-execution-validation.md)).
- Hidden liquidity (structurally unobservable).
- Cross-venue routing decisions (made by participants, off-platform).
- Regime state (Venue Quality is per-window; cross-regime behavior is governed by §12 validation plan).
- Venue-local congestion at execution time (not measurable from depth20+tape WS feeds alone).
- Unobservable matching-engine behavior ([lip-epistemic-boundaries §2](lip-epistemic-boundaries.md), [lip-execution-validation §10](lip-execution-validation.md)).

**Therefore the layer's output cannot be interpreted as any of:**

- "Best execution venue."
- "Largest executable venue."
- "Optimal venue."
- "Execution guarantee."
- "Venue with most absorbable depth right now."
- "Where my order will fill best."

Any consumer that maps a Venue Quality emit to an execution decision violates this invariant and the [lip-governance §6](lip-governance.md) firewall simultaneously.

---

## 17. Clock & timestamp discipline (load-bearing invariant)

**The layer assumes:**

- Locally monotonic ingest timestamps within the worker process (the timestamp at which a frame is received by our handler is non-decreasing).
- Bounded receive-time drift between subscribed WS streams within a single worker instance.
- Approximate consistency of exchange-supplied timestamps within a single venue's feed.

**The layer does NOT assume:**

- Synchronized exchange clocks across venues. Binance and Bybit do not share a clock; their epoch fields are not directly comparable to nanosecond precision and often not to millisecond precision.
- Nanosecond cross-venue comparability of trade or book timestamps.
- Globally authoritative timestamps. There is no platform-level time source; all timing is local-host plus venue-reported.
- Host-clock alignment to UTC within any guaranteed bound — the platform does not enforce NTP at the app layer (see [freeze §13 line 1065](2026-05-23-architecture-freeze.md)).

**Distortions that timestamp mismatch can introduce into the layer's output:**

- **False propagation lag** — a "later" event on one venue may have actually preceded the "earlier" event on another; the order in our local view is not authoritative.
- **False LOCAL_ONLY states (§7)** — a peer venue's signal may exist but be timestamp-shifted into a different measurement window.
- **False CONTRADICTED states (§7)** — short-term divergence that resolves within timestamp uncertainty.
- **Spurious trade/book mismatch (§4.F)** — a print may appear to execute through a book level that, on the venue's clock, was already gone.
- **Apparent impact mismatch** — `exec_impact.divergence_bps` can be inflated by post-snapshot lag without any real microstructure event.
- **Stale-window artifacts** — windows aligned to local-host time may include partial coverage from a venue whose clock drifted, biasing per-window aggregates.

**Invariant (load-bearing).** Cross-venue timestamp equality is structurally unavailable. The platform cannot establish that two events on different venues occurred "at the same instant"; it can only observe that they occurred within overlapping receive-time windows on this worker.

**Consequence.** Venue comparisons produced by this layer are **approximate observational comparisons**, not deterministic temporal truth. The §7 cross-venue enum reflects what was observed in our local timeline; it does not reflect what occurred on a unified market clock that does not exist.

**Operational rule.** A consumer that treats Venue Quality cross-venue states as causal-temporal claims (e.g., "Bybit reacted before Binance") is outside the layer's mandate. Such claims, if made at all, belong at the operator analytical tier with explicit timestamp-uncertainty disclosure, and are explicitly prohibited from automated downstream propagation per [lip-epistemic-boundaries §3](lip-epistemic-boundaries.md) (propagation epistemic ceiling).

---

## 18. No global venue ranking (load-bearing invariant)

**Invariant.** The layer MUST NOT produce, expose, or contribute to:

- A universal venue ranking.
- A global "best exchange" ordering.
- A stable venue league table.
- A venue trust hierarchy.
- A standing "preferred venues" list.
- A cross-symbol aggregated venue score.

**Reason.** Venue quality, as defined in §1, is:

- **Symbol-specific.** A venue's reliability for BTC may differ from its reliability for a low-cap alt on the same venue.
- **Product-specific.** Spot and perp on the same venue have different microstructures, different feed cadences, and different blind-spot profiles (perp adds funding, OI, mark/index — none of which spot has).
- **Regime-specific.** A venue may be RELIABLE under calm conditions and DEGRADED under reconnect-storm or stress conditions; the verdicts are not interchangeable.
- **Observation-window-specific.** Verdicts are per-window per §5; aggregating windows into a "standing" verdict erases the temporal structure that justified the verdict.
- **Coverage-dependent.** Bybit is shallowly observed (§3); a "Bybit verdict" is structurally not comparable to a "Binance verdict" because the input set differs.

**Examples (illustrative, not implementation):**

- A venue may appear OBSERVABLE for BTC perpetuals in a calm window and STRUCTURALLY_LIMITED for low-cap alts in the same window.
- A venue may be CONFIRMED cross-venue for one symbol pair and UNAVAILABLE for another at the same instant.
- A venue may have low quote persistence on one product type and stable persistence on another.

Therefore venue quality observations are **local and contextual**, not universal properties of an exchange.

**What this rules out, concretely:**

- A `venue_global_rank` field — forbidden.
- A `venue_overall_score` field aggregating across symbols, products, or regimes — forbidden.
- An operator UI surface labeled "Best Venues" or "Top Exchanges" — forbidden (the §14 banned-vocab additions enforce this at the label tier).
- A standing list of "approved" / "preferred" / "recommended" venues — forbidden (also a [lip-governance §6](lip-governance.md) firewall violation).

**Approved comparative phrasing**, where comparison is genuinely needed, lives in §14's approved-phrases table — bounded by window, scope, and observation context.

**Cross-reference.** §14 banned-vocab table now includes "top venue", "best venue overall", "most trustworthy exchange", "highest quality exchange" — the UI-tier enforcement of this invariant.

---

## 19. What this document is not

- Not a venue trust score.
- Not an exchange-honesty rating.
- Not a manipulation detector.
- Not a wash-trading detector.
- Not a fraud-detection layer.
- Not a best-venue selector.
- Not an execution-routing engine.
- Not an execution-capacity estimator (per §16).
- Not a cross-venue causal-temporal authority (per §17).
- Not a global venue ranking system (per §18).
- Not a current runtime layer of any kind.
- Not authorization to build the layer during Operational Observation Period.

It is a design-tier companion that pre-commits the layer to observable-only semantics, to inheriting the platform's existing blind-spot inventory, and to the governance contracts already in place — *before* implementation effort can drift the design toward overclaiming. If the layer is built later, this document becomes the operative contract; until then, it is documentation of what would and would not be permissible.
