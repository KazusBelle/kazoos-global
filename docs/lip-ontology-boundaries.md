# Ontology Boundaries — canonical companion

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md), [`docs/lip-epistemic-boundaries.md`](lip-epistemic-boundaries.md), [`docs/lip-metric-registry.md`](lip-metric-registry.md), [`docs/lip-execution-validation.md`](lip-execution-validation.md), [`docs/lip-regime-engine.md`](lip-regime-engine.md), [`docs/lip-causal-propagation.md`](lip-causal-propagation.md), [`docs/lip-venue-quality.md`](lip-venue-quality.md), [`docs/lip-governance.md`](lip-governance.md), [`docs/lip-validation-and-calibration.md`](lip-validation-and-calibration.md).

**Status: Class A documentation hardening pass.** This document does not add code, does not propose new states, does not change any emit. It enumerates ontology claims that the platform is **not allowed to make** and the operational reformulations that are allowed instead. Per [lip-governance §2](lip-governance.md), this is documentation-only work authorized during [Operational Observation Period](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_operational_observation_period.md).

**Boundary statement (load-bearing).** The Liquidity Intelligence Platform measures, classifies, validates, degrades certainty, and replays persisted outputs. It does not access "what the market is". Every layer of the platform emits bounded observational classifications under current instrumentation constraints. No layer establishes authoritative market ontology. This is the cross-cutting invariant; per-layer companions inherit it without divergence.

---

## 1. Why ontology boundaries matter

A layer's output is a function of: (a) the inputs it received, (b) the thresholds it was configured with, (c) the persistence and retention of its sources, (d) the clock and cadence under which inputs were captured, (e) the gaps and refusals encoded in its code path. None of (a)–(e) is the market.

If documentation reads as "the layer detects market structure" or "the regime transitioned", a downstream consumer treats the output as a market property rather than a measurement artifact. That mis-reading violates the platform's existing contracts ([lip-execution-validation §11](lip-execution-validation.md), [lip-epistemic-boundaries §3](lip-epistemic-boundaries.md), [lip-regime-engine §21](lip-regime-engine.md), [lip-causal-propagation §3](lip-causal-propagation.md)) and is exactly the surface this companion closes.

**Operational benefit of ontology discipline:** PR reviews, incident docs, and operator handoffs can use the same vocabulary the platform actually computes in. Disagreements about thresholds are visible (Class C). Disagreements about "what the market did" do not enter the artifact.

**Governance benefit:** every ontology claim is a candidate [lip-governance §3](lip-governance.md) row 9 (inferred intent) or row 11 (semantic relabeling) violation. Surfacing them at the doc tier prevents them from entering code or operator UI.

---

## 2. Observable vs unknowable

| Observable (this platform can measure) | Unknowable from this platform's inputs |
|---|---|
| Persisted WS depth20 frames (Binance), trade tape, basic REST snapshots | Hidden / iceberg / RPI / OTC fills |
| Per-burst expected_bps and realized_bps over visible top-20 | Hidden queue priority, true fill probability for hypothetical orders |
| Coordinated-state labels, propagation edges, transition verdicts | "What the market is doing" in any standing sense |
| Persistence-gate satisfaction at configured `PERSISTENCE_THRESHOLD = 3` | The moment the market crossed any internal microstructure boundary |
| Sub-window survival of lagged pairs | Whether A "caused" B |
| Distributed-stress probe counts | "Whether a crisis is happening" |
| Cross-venue divergence vs Binance reference | Whether either venue is "honest" or "manipulating" |
| Replay reconstruction of persisted emit | Pre-activation events, pruned-retention events, off-tape drivers |

Pointers to authoritative unknowability inventories: [lip-execution-validation §10 / §24](lip-execution-validation.md), [lip-epistemic-boundaries §2 / §5](lip-epistemic-boundaries.md), [lip-causal-propagation §6.2](lip-causal-propagation.md).

---

## 3. Classification vs discovery

A platform layer **classifies** observed measurements into a finite enum of labels. It does **not** discover natural objects in the market.

| Phrase suggesting discovery (banned) | Operational reformulation (approved) |
|---|---|
| "detects structure" | "emits classification under configured thresholds" |
| "finds regimes" | "labels observed metric configurations against threshold ladder" |
| "discovers states" | "classifier emits state label per evaluation" |
| "identifies manipulation" | (rejected — not a measurable surface) |
| "detects causal chains" | "emits edges where lagged adjacency survived refusal gates" |
| "uncovers hidden patterns" | (rejected — hidden by definition not observable) |
| "reveals market structure" | "summarizes observed alert / state / transition data" |

**Invariant: categories are measurement abstractions, NOT discovered natural objects.**

A label like `ACTIVE_CASCADE_PROPAGATION` ([regime-engine §3.1](lip-regime-engine.md)) is a residue of a threshold ladder, not a thing in the world. Renaming the threshold (Class C) renames the residue; the underlying upstream rows do not change.

---

## 4. Market-state non-authority invariant

**Statement.** No layer's emit constitutes an authoritative claim about market state.

| Surface | Authoritative claim that would violate the invariant | Operational reformulation |
|---|---|---|
| Regime layer | "the market is in cascade" | "synthesis layer's threshold ladder placed `synthesized_stress` above its highest cutoff at `ts_ms`" |
| Propagation layer | "BTC drove ETH" | "BTC alerts repeatedly led ETH alerts within the configured `[5s, 30min]` window over the lookback period" |
| Execution validation | "the market has 5 BTC of real liquidity at touch" | "the visible top-20 walked 5 BTC of expected fill against the pre-burst snapshot" |
| Venue quality (design) | "Bybit is unreliable" | "Bybit's observable conditions under our subscription showed `data_availability_state = STALE` over the window" |
| Distributed stress | "the market is in crisis" | "synchronized observable deterioration across N symbols crossed the `hot_count ≥ 3` cutoff" |

Per-layer load-bearing reaffirmations: [regime-engine §21.4](lip-regime-engine.md), [causal-propagation §3](lip-causal-propagation.md), [execution-validation §11](lip-execution-validation.md), [venue-quality §1](lip-venue-quality.md), [epistemic-boundaries §4](lip-epistemic-boundaries.md).

---

## 5. Propagation non-authority invariant

**Statement.** Observed lag consistency does not establish physical transmission, market leadership, or causal authority.

Already load-bearing in [lip-epistemic-boundaries §3.1](lip-epistemic-boundaries.md) and decomposed in [lip-causal-propagation.md](lip-causal-propagation.md). Restated for cross-doc coherence:

- **First observed ≠ origin.**
- **Replay order ≠ transmission order.**
- **Edge activation order ≠ causal direction.**
- **Earlier observation ≠ market leadership.**
- **DIRECTIONAL verdict ≠ causation.**

The `causal_propagation()` function name and the `narrative_causality()` function name are **legacy code identifiers** (per [lip-causal-propagation §7.4](lip-causal-propagation.md), §8.3). Documentation and operator UI use the new semantic vocabulary; code rename is a deferred Class B candidate.

---

## 6. Replay non-authority invariant

**Statement.** Replay reconstructs persisted emitted outputs under historical configuration state. It does not reconstruct objective market reality.

| Replay claim that would violate the invariant | Operational reformulation |
|---|---|
| "Replay shows what the market did at T" | "Replay re-derives the layer's emit for window `[T − N, T]` from persisted `liquidity_*_history` rows" |
| "Historical truth" | Persisted emit + audit-trail entries (per [lip-governance §10](lip-governance.md)) |
| "Exact market reconstruction" | Replay-bounded sequence reconstruction (per [lip-causal-propagation §1.7](lip-causal-propagation.md)) |
| "What the market was" | "What the platform had recorded and what classifiers had emitted at that time" |

Replay determinism (per [lip-governance §4](lip-governance.md)):

- Same `(window, schema_version, calibration_version, runtime_generation)` tuple → same output.
- `calibration_version` / `schema_version` / `runtime_generation` **NOT IMPLEMENTED** on persisted rows today ([lip-governance §8](lip-governance.md), [lip-validation-and-calibration §5](lip-validation-and-calibration.md)).
- Until implemented, cross-threshold-boundary aggregation in replay silently mixes configurations. This is acknowledged governance debt — not an ontology claim about the market.

---

## 7. Regime non-authority invariant

**Statement.** Regime labels are classifier outputs over a bounded observation window, NOT authoritative market states.

Already load-bearing in [lip-regime-engine §21](lip-regime-engine.md). Restated for cross-doc coherence:

| Regime claim that would violate the invariant | Operational reformulation |
|---|---|
| "the market entered `ACTIVE_CASCADE_PROPAGATION`" | "classifier emitted `ACTIVE_CASCADE_PROPAGATION` at upstream snapshot `ts_ms`" |
| "the market recovered" | "transition verdict `REVERSED` was emitted for the prior transition" |
| "regime is unstable" | "stability label = `unstable` (`flicker_ratio ≥ 0.5`) over the configured window" |
| "regime transition at T" | "classifier-emitted transition record with `ts_ms = T`, became `PERSISTENT` only after `t + 3 × cadence`" |
| "the market is in regime X" | "current_state field = X; held N snapshots" |

A regime label is the residue of a threshold ladder on a synthesized score. The label is a function of the configuration; under different thresholds (Class C change) the same upstream rows produce different labels.

---

## 8. Stress non-authority invariant

**Statement.** Distributed-stress labels describe classification of synchronized observable degradation across the subscribed symbol set, NOT objective market-wide systemic state.

[lip-epistemic-boundaries §4](lip-epistemic-boundaries.md) is authoritative; restated:

| Stress claim that would violate the invariant | Operational reformulation |
|---|---|
| "systemic stress is rising" | "distributed-stress probe `hot_count` crossed the configured threshold over the evaluation window" |
| "the market is in crisis" | (rejected) |
| "market-wide event" | "synchronized observable deterioration across N symbols on the subscribed venue" |
| "systemic deterioration" | "distributed stress with `hot_count ≥ 3`" (per [freeze §15 phrase-compression](2026-05-23-architecture-freeze.md)) |
| "distributed instability" | "cross-symbol concurrent degradation cluster" |
| "PRE_CASCADE verdict means cascade is coming" | "PRE_CASCADE = composite of seven independent probes crossed configured thresholds; verdict is point-in-time, not predictive" |

Per [lip-epistemic-boundaries §4.2](lip-epistemic-boundaries.md), the stress layer **does not infer**: future direction, originating asset, participant intent, cross-venue confirmation (Binance-centric), event lifecycle timing, causal transmission between probes.

---

## 9. Forecast non-authority invariant

**Statement.** Forecast endpoints emit **bounded OLS extrapolation with explicit caps**, conditioned on observed historical analogs. They do not predict future market state.

Authoritative source: [freeze line 1115](2026-05-23-architecture-freeze.md): "Every forecast endpoint is OLS extrapolation with explicit `slope_capped` / `extrapolation_capped` / `horizon_decay` / `cap_factor` discounts. No layer publishes a directional trade signal."

| Forecast claim that would violate the invariant | Operational reformulation |
|---|---|
| "the market is likely to" | (rejected) |
| "expected move" | (rejected) |
| "market preparing for X" | (rejected) |
| "market pressure building" | (rejected) |
| "future deterioration expected" | (rejected) |
| "risk is increasing" | (rejected unless qualified to specific bounded probe — see [epistemic-boundaries §4](lip-epistemic-boundaries.md)) |
| "forecast says X will happen" | "extrapolation of `metric` over `horizon`, with `slope_capped`, `horizon_decay`, anchored at last observed value" |

A forecast endpoint's output is a bounded projection of observable history, not a claim about the future. Reading it as a prediction is reading past the published ceiling.

---

## 10. Category abstraction invariant

**Statement.** Categories emitted by the platform (`coordinated_state` enum, `causal_propagation` verdicts, `dominant_regime` values, `influence_hierarchy` role labels, distributed-stress verdict states, transition verdicts) are **measurement abstractions over configured thresholds**, NOT discovered natural objects in the market.

| Claim that would violate the invariant | Operational reformulation |
|---|---|
| "the platform discovered N regimes" | "the platform classifies into M predefined labels per the threshold ladder" |
| "the layer found a leader" | "the role classifier emitted `LEADER` for node `s` (legacy enum; out-dense node — see [causal-propagation §8.3](lip-causal-propagation.md))" |
| "the system uncovered hidden structure" | (rejected — hidden by definition not observable) |
| "natural regimes of the market" | (rejected) |
| "the cascade was identified" | "the `PRE_CASCADE` verdict was emitted by `crisis_genesis` at `ts`" |

A category emit is reproducible from inputs + thresholds. Changing the thresholds (Class C) changes the categories; changing the input window (Class A within bounded-replay rules per [lip-governance §4](lip-governance.md)) may change the category for the same underlying rows.

---

## 11. Structurally unknowable market properties

Inventory of properties **no future calibration, no additional sensor in this platform's configuration, no expanded subscription, no longer retention window** would let the platform measure. Distinct from the validation backlog ([lip-execution-validation §13](lip-execution-validation.md), [lip-causal-propagation §2 audit](lip-causal-propagation.md)) which contains *measurable-but-unmeasured* items.

| Property | Why structurally unknowable from this platform |
|---|---|
| Participant intent | Not in any market-data surface; out of vocabulary per [lip-epistemic-boundaries §2](lip-epistemic-boundaries.md) |
| Hidden / iceberg / RPI fills | Not exposed by depth20 protocol; per [lip-execution-validation §10](lip-execution-validation.md) |
| Queue priority within a price level | Aggregated `qty` per level; protocol does not expose per-order position |
| Off-tape / OTC / internalized flow | Does not appear in venue trade tape |
| True cross-venue clock alignment | Independent venue clocks; per [lip-venue-quality §17](lip-venue-quality.md) cross-venue timestamp equality is structurally unavailable |
| Matching-engine internal logic | Venue-internal, not exposed on any public WS feed |
| "True" causal direction at sub-resolution lag | `min_lead_ms = 5_000` ms is the conservative resolution envelope; pairs below are structurally unresolved ordering per [lip-causal-propagation §4.1](lip-causal-propagation.md) |
| Counterfactual market behavior | The platform observes what occurred, not what would have occurred |
| Identity unification (same actor across visible counterparties) | Off-tape data not consumed |
| Legal manipulation / fraud / wash-trading intent | Out of mandate; cannot be inferred from visible data |
| Macro causation of synchronized moves | Off-tape drivers not flagged beyond `find_common_driver()` over observable symbols |

**Discipline.** A proposed feature, validation, or backlog item that implicitly requires one of these is **rejected at design time**, not deferred. It does not belong on the roadmap.

---

## 12. Allowed vs forbidden ontology claims

### 12.1 Ontology blacklist (cross-cutting)

The following phrases are forbidden across all platform documentation, code comments, operator UI, alerts, exports, replay overlays, and any cross-layer composite:

| Banned (ontology claim) | Approved replacement |
|---|---|
| "the market is" / "the market was" | "the layer emitted" / "the classifier labeled" / "the persisted state is" |
| "true market state" | "observed configuration class" |
| "real liquidity" | "visible top-20 walkable depth" |
| "actual market depth" | "depth20-observable depth at snapshot" |
| "true fill probability" | (rejected — structurally unknowable) |
| "market entered" | "classifier emitted" |
| "market recovered" | "verdict `REVERSED` emitted" |
| "market regime" (as standing object) | "regime label" (as classifier output) |
| "market transmitted" | "edge emitted; A then B observed" |
| "honest book" / "fake book" | "book integrity CLEAN / DEGRADED per measurement" |
| "real-but-short liquidity" | "near-touch state below the `400 ms` persistence floor" |
| "true causation" | "DIRECTIONAL verdict; refusal gates rejected" |
| "the market reacted because" | "B emission observed after A emission under bounded conditions" |
| "natural regimes" | "predefined enum labels of the threshold ladder" |
| "cascade exists" | "cascade-class verdict emitted" |
| "crisis is happening" | "PRE_CASCADE composite verdict crossed configured thresholds" |
| "structural turn in the market" | (rejected — see [lip-regime-engine §9](lip-regime-engine.md)) |
| "hidden structure revealed" | (rejected — structurally unknowable + classification, not discovery) |
| "the layer understands" | "the layer classifies" |

### 12.2 Allowed framing patterns

When writing about any platform emit, allowed prose patterns:

- "Layer X emitted Y at `ts`."
- "Classifier Y assigned label Z to the observed configuration."
- "Verdict W was emitted because refusal gates G1..Gn rejected."
- "Persistence gate satisfied at `t + N × cadence`."
- "Replay re-derives the emit for window `[since, now]` from persisted rows."
- "Configuration crossed threshold `θ = C`."
- "Edge survived refusal ladder; conditional propagation candidate."
- "Observed degradation cluster across N symbols."
- "Bounded OLS extrapolation with caps; not a directional signal."

### 12.3 Replay-authority boundaries

| What replay establishes | What replay does NOT establish |
|---|---|
| Layer Y emitted value V for input set I at calibration C, schema S | What "really happened" in the market at the corresponding wall-clock time |
| Deterministic reproducibility of emit V given (I, C, S, runtime generation) | Authority over inter-emission events |
| Persistence of audit-trail entries per [lip-governance §10](lip-governance.md) | Pre-activation events, pruned-retention events, off-tape events |
| Documentation-label severity (REPLAY_AVAILABLE / PARTIAL / NOT_PERSISTED) per [lip-execution-validation §21](lip-execution-validation.md) | A new runtime enum (these labels are documentation vocabulary) |

### 12.4 Classification-not-discovery invariant table

| Layer | Emits classification of | Does NOT discover |
|---|---|---|
| Regime Engine (`market_state_transitions`) | Transitions in `coordinated_state` stream | "the market's true regimes" |
| Propagation (`causal_propagation`) | Lag-consistent pairs surviving refusal | "the market's causal structure" |
| Event Chain Reconstruction (legacy `narrative_causality`) | Ordered tuples of emitted events | "the market's story" |
| Distributed Stress (`crisis_genesis`) | Composite of seven probe verdicts | "the market's crisis state" |
| Execution Validation (`exec_impact`) | Per-burst divergence between book-walk and realized mid | "the market's actual liquidity" |
| Venue Quality (design candidate per [lip-venue-quality.md](lip-venue-quality.md)) | Observable reliability of a venue's data and visible book | "venue trustworthiness" |
| Influence Hierarchy (`influence_hierarchy`) | Observable out/in-ratio role labels | "market leaders / followers" |
| Forecast endpoints | Bounded OLS extrapolation with caps | "future market state" |

---

## 13. Governance classification

| Aspect | Status |
|---|---|
| **This document** | Class A (documentation-only) per [lip-governance §2](lip-governance.md). Authorized during Operational Observation Period |
| **Adding ontology-violating wording to any companion** | Documentation regression; Class A reversal with rationale required |
| **Removing the load-bearing boundary statement of any companion** | Documentation regression; effectively Class C semantic relabeling — requires governance audit per [lip-governance §3 row 11](lip-governance.md) and [lip-governance §10](lip-governance.md) audit-trail entry |
| **Code change that would emit a new label not in the documentation enum** | Class B (semantic) + [lip-governance §7](lip-governance.md) if composite. NOT AUTHORIZED during Observation Period |
| **UI surface that introduces ontology language not in §12.2 allowed framing** | [lip-governance §3](lip-governance.md) row 9 + row 11 simultaneous violation. PR rejected at review |
| **Cross-layer composite framing emits as "market property"** | [lip-governance §6](lip-governance.md) firewall + §7 composite + this document's §4 violation. Rejected |

**Global invariant line (mandatory in every companion that emits classifications):**

> "The layer emits bounded observational classifications under current instrumentation constraints. It does not establish authoritative market ontology."

This line is now added to: [`lip-regime-engine.md`](lip-regime-engine.md), [`lip-causal-propagation.md`](lip-causal-propagation.md), [`lip-execution-validation.md`](lip-execution-validation.md), [`lip-venue-quality.md`](lip-venue-quality.md). Companions added later carry the line in their boundary block.

---

## 14. What this document is not

- Not a philosophy of markets.
- Not an abstract epistemology essay.
- Not pseudo-academic prose about "what is real".
- Not an AI-style semantics layer.
- Not a discussion of the "nature" of any market.
- Not a redesign of any layer.
- Not authorization to add new emit fields or states.
- Not an inventory of all unknowable things (only the cross-cutting ones; per-layer inventories live in their companions).

It is a documentation hardening pass that pre-commits the entire platform stack to observational-classification semantics, enumerates the ontology claims that are forbidden, and provides the operational reformulations that consumers must use instead. Every per-layer companion inherits the invariants here without divergence; any drift is a defect of the companion, not of this document.
