# Propagation / Event-Chain Reconstruction — canonical companion contract

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md), [`docs/lip-metric-registry.md`](lip-metric-registry.md) §B.4 / §B.5 / §B.6, [`docs/lip-epistemic-boundaries.md`](lip-epistemic-boundaries.md) §3 (authoritative for the load-bearing ceiling), [`docs/lip-validation-and-calibration.md`](lip-validation-and-calibration.md), [`docs/lip-governance.md`](lip-governance.md), [`docs/lip-regime-engine.md`](lip-regime-engine.md).

**Status: Class A documentation hardening pass** of already-implemented functions in [`shared/kazus_logic/liquidity/research.py`](../shared/kazus_logic/liquidity/research.py): `propagation_graph()` (line 4475), `causal_propagation()` (line 5347), `structural_dependencies()` (line 5681), `influence_hierarchy()` (per registry §B.6), `narrative_causality()` (line 6932 — **legacy code name**; this companion reframes its semantic identity as **Event Chain Reconstruction**, code rename is a separate Class B candidate).

**Boundary statement (load-bearing).** The propagation stack in this platform measures **repeated temporal adjacency between observed emitted events, under bounded observation conditions, with refusal-first verdicts**. It does not measure causation, transmission, leadership, market influence, market memory, or narrative. Phrases of that kind are out-of-vocabulary for the stack — see §10 banned-vocabulary table. The load-bearing epistemic ceiling lives in [`lip-epistemic-boundaries.md §3`](lip-epistemic-boundaries.md); this companion is consistent with that ceiling and decomposes the stack's semantic surface into the seven primitives below.

**Cross-cutting ontology invariant** (per [`lip-ontology-boundaries.md`](lip-ontology-boundaries.md)): the stack emits bounded observational classifications under current instrumentation constraints. It does not establish authoritative market ontology.

---

## 1. Semantic decomposition — seven primitives

Each primitive maps to specific code and persisted state. None of them name a market property; all of them name an observation property.

### 1.1 Propagation Edge

**Definition.** Measured lag consistency between two observed-emitted events A and B over a lookback window.

**Code.** [`propagation_graph()`, research.py:4475](../shared/kazus_logic/liquidity/research.py#L4475). Produces per-pair edge with: `volume_strength`, `lead_clarity`, `lead_consistency`, `temporal_consistency`, `recurrence_stability`, `symmetry_penalty`, `confidence_score`, `confidence label ∈ {HIGH, MEDIUM, LOW}`. Formulas in [lip-metric-registry §B.4](lip-metric-registry.md).

**A propagation edge IS:**

- A 6-factor scalar composite over A→B alert pair counts, lag distribution, sub-window survival, mirror-pair ratio, and recurrence stability across observation days.

**A propagation edge IS NOT:**

- Influence.
- Leadership.
- Cause.
- Transmission.
- Market-mechanism reconstruction.
- An assertion that A "drives" B.

### 1.2 Temporal Adjacency

**Definition.** Events repeatedly observed near each other in time within `min_lead_ms ≤ Δt ≤ lead_window_ms`. Currently `min_lead_ms = 5_000` ms and `lead_window_ms = 30 × 60_000` ms = 30 min.

**Code.** Same function; pre-scoring drop at the simultaneity floor ([lip-metric-registry §B.4](lip-metric-registry.md): "Pairs with `lead < min_lead_ms = 5_000` ms are **dropped at ingestion** before any score is computed").

**Temporal adjacency IS:**

- A count of observed pairs `(A_ts, B_ts)` with `B_ts − A_ts ∈ [5 s, 30 min]`.

**Temporal adjacency IS NOT:**

- Causal relationship.
- Coordination.
- Joint mechanism.

### 1.3 Dependency Graph

**Definition.** Persisted observed edge structure assembled from per-pair edges over a window.

**Code.** Graph-level emission via `propagation_graph()` (per-edge records + graph-level `integrity_score` per [lip-metric-registry §B.4](lip-metric-registry.md)), `structural_dependencies()` ([research.py:5681](../shared/kazus_logic/liquidity/research.py#L5681)) consumes `causal_propagation()` verdicts to produce structural findings (no new SQL).

**Dependency graph IS:**

- A snapshot of which `(A, B)` pairs survived all per-edge gates and produced `confidence_score > 0` over the observation window.

**Dependency graph IS NOT:**

- Market topology.
- Market structure truth.
- Semantic map of asset relationships.
- Authority on what depends on what.

### 1.4 Event Chain Reconstruction

**Definition.** Ordered replay-visible sequence of emitted events from already-published layer outputs.

**Code.** [`narrative_causality()`, research.py:6932](../shared/kazus_logic/liquidity/research.py#L6932) (**legacy function name**). Per [lip-epistemic-boundaries §3.4 line 45](lip-epistemic-boundaries.md): "a deterministic template composed from already-published layer outputs. No model calls, no language generation, no inference of a market story."

**Event chain reconstruction IS:**

- A deterministic ordering of already-emitted (alert, transition, propagation edge, distributed-stress probe) records by their `ts_ms` over a replay window.
- Output structure: tuples `(symbol, ts, edge_id, lag_bucket, confidence_state)` — not prose narrative.

**Event chain reconstruction IS NOT:**

- A story.
- A narrative.
- A causal explanation.
- A market reconstruction.
- Event genealogy.
- Event ancestry.
- Event lineage in any sense beyond replay-visible ordering.

**Legacy-name disclosure.** The code identifier `narrative_causality` predates this companion. **Renaming the function is a [`lip-governance.md`](lip-governance.md) §2 Class B change** (semantic relabeling that touches code surface); deferred as a candidate. The function's actual behavior — deterministic template assembly with no inference — matches the Event Chain Reconstruction definition above. Documentation surfaces and operator UI MUST use the new semantic identity ("event chain reconstruction") rather than the legacy name.

### 1.5 Conditional Propagation Candidate

**Definition.** The highest epistemic ceiling allowed by this stack. A pair `(A, B)` is a *conditional propagation candidate* when repeated lagged association survived all refusal gates under current observation constraints.

**Code.** `causal_propagation()` returns `DIRECTIONAL` only after the 6-step refusal ladder rejected COINCIDENCE / EXPLORATORY / COMMON_DRIVEN / UNDER_EVIDENCED / AMBIGUOUS verdicts ([lip-epistemic-boundaries §3.3](lip-epistemic-boundaries.md)).

**Conditional propagation candidate IS:**

- "Repeated lagged association survived refusal gates under current observation constraints."

**Conditional propagation candidate IS NOT:**

- Causation.
- Confirmed transmission.
- Influence proof.
- Sufficient grounds for action.

A `DIRECTIONAL` verdict's literal meaning ([epistemic-boundaries §3.1](lip-epistemic-boundaries.md)): "B's alerts repeatedly started ≥ 5 s and ≤ 30 min after A's, with stable lag, on independent sub-windows, with no common-driver candidate found among observed symbols, and not as a bidirectional mirror." That is what the formula measures. Reading further is reading past the ceiling.

### 1.6 Common-Shock Suppression

**Definition.** Default-to-ambiguity behavior when synchronized degradation under a common driver is observed or unresolved.

**Code.** `causal_propagation()` `common_driver_factor = 0.35 if common_driver else 1.0`; verdict `COMMON_DRIVEN` when `find_common_driver()` returns a candidate (refusal ladder per [lip-metric-registry §B.5](lip-metric-registry.md)).

**Common-shock suppression IS:**

- A multiplicative damping (× 0.35) on confidence when a third symbol is observed to lead both A and B with shorter lag.
- A verdict override (COMMON_DRIVEN) that takes precedence over DIRECTIONAL when triggered.

**Common-shock suppression IS NOT:**

- A complete detector. Off-tape macro drivers, BTC-wide moves without an observable BTC alert in the dataset, venue outages, liquidation cascades, funding resets — these are documented blind spots ([epistemic-boundaries §3.3](lip-epistemic-boundaries.md) lines 78-80). Their absence from `find_common_driver()` does not constitute their non-existence.

### 1.7 Replay-Bounded Sequence Reconstruction

**Definition.** Ordered reconstruction of emitted events strictly within the persisted replay window, respecting retention and pre-activation unavailability.

**Code.** Functions read `liquidity_alert_history` (90 d retention per freeze §1 Layer 1) and `liquidity_intelligence_history`. No backfill, no synthesis. Pre-activation windows produce `data_quality = INSUFFICIENT/LOW` → verdict floored at EXPLORATORY.

**Replay-bounded sequence reconstruction IS:**

- Deterministic re-derivation of edges and verdicts from persisted alert/intelligence rows over the requested window.

**Replay-bounded sequence reconstruction IS NOT:**

- Recovery of pre-activation events.
- Reconstruction of the "full historical chain" of any market event.
- Authority on what happened outside the persisted window.

---

## 2. Implementation audit (functions + persistence)

| Component | Location | Status |
|---|---|---|
| `propagation_graph(db, lookback_days)` | [research.py:4475](../shared/kazus_logic/liquidity/research.py#L4475) | **Implemented**. Per-edge confidence + graph integrity. Drops sub-`min_lead_ms = 5_000` ms pairs at ingestion |
| `causal_propagation(db, lookback_days)` | [research.py:5347](../shared/kazus_logic/liquidity/research.py#L5347) | **Implemented**. 6-step refusal ladder; verdicts `{DIRECTIONAL, AMBIGUOUS, UNDER_EVIDENCED, COMMON_DRIVEN, COINCIDENCE, EXPLORATORY}` |
| `structural_dependencies(db, lookback_days)` | [research.py:5681](../shared/kazus_logic/liquidity/research.py#L5681) | **Implemented**. Composes causal_propagation verdicts into structural findings; no new SQL |
| `influence_hierarchy()` (per registry §B.6) | research.py (role classification at registry §B.6) | **Implemented** with role labels `{ISOLATED, INSTABILITY_HUB, LEADER, FOLLOWER, AMPLIFIER}`. **Legacy enum values** — see §8.3 for vocabulary discipline |
| `narrative_causality(db, lookback_days)` | [research.py:6932](../shared/kazus_logic/liquidity/research.py#L6932) | **Implemented** as deterministic template assembler. Legacy function name; semantic identity reframed in this companion as Event Chain Reconstruction (§1.4) |
| `liquidity_alert_history` retention | 90 d per freeze §1 | **Implemented**. Bounds replay window |
| **Calibration-version stamping on edges** | — | **NOT IMPLEMENTED** (platform-wide gap per [lip-validation-and-calibration §5](lip-validation-and-calibration.md), [lip-governance §8](lip-governance.md)) |
| **Timestamp-drift detector** | — | **NOT IMPLEMENTED** ([freeze §10.2](2026-05-23-architecture-freeze.md), epistemic-boundaries §3.3 line 79) |
| **Synchronized-global-move detector beyond `find_common_driver`** | — | **NOT IMPLEMENTED** ([freeze §10.3](2026-05-23-architecture-freeze.md), epistemic-boundaries §3.3 line 80) |
| **DIRECTIONAL false-positive rate** | — | **PENDING MEASUREMENT** per [lip-metric-registry Part C](lip-metric-registry.md) |
| **Edge lifetime / lag stability across regimes** | — | **PENDING MEASUREMENT** |
| **HIGH-confidence edge degradation over time** | — | **PENDING MEASUREMENT** |

---

## 3. Propagation epistemic ceiling (cross-reference)

**Authoritative source: [`lip-epistemic-boundaries.md §3`](lip-epistemic-boundaries.md).** This companion restates the ceiling for self-containment but does not extend it; any divergence between this section and the authoritative source is a defect of this companion.

**The load-bearing invariant:**

> Propagation edges represent repeated lagged association under observed conditions. They do not establish causal certainty.

**Repeated lag consistency does NOT establish:**

- Causation.
- Market influence.
- Dominant driver.
- Origin.
- Transmission mechanism.
- Source asset.
- Directional authority.
- Hidden coordination.
- Strategic intent.

**Layer only establishes:** repeated temporal adjacency under bounded observation conditions, with refusal-first downgrade on every measurable deficiency.

**Four supporting non-equivalences (load-bearing):**

| ≠ statement | Meaning |
|---|---|
| **First observed ≠ origin** | The earliest A in `(A, B)` adjacency was the earliest *the layer observed*; the layer cannot see pre-activation or pre-`liquidity_alert_history` events. An off-tape driver may precede both |
| **Replay order ≠ transmission order** | Replay reconstructs the order of *persisted emissions* (subject to writer cadence and timestamping). It does not reconstruct the order in which underlying market events occurred |
| **Edge activation order ≠ causal direction** | The pre-scoring drop at `min_lead_ms = 5_000` enforces *structural undecidability* on sub-resolution pairs ([epistemic-boundaries §3.2](lip-epistemic-boundaries.md)). Within-resolution lag *can* establish ordering of emissions but *cannot* establish causal direction |
| **Earlier observation ≠ market leadership** | The `LEADER` enum value in `influence_hierarchy()` is a property of `out_ratio` and `avg_out_confidence` — not of market leadership in any standing sense (§8.3) |

---

## 4. Temporal resolution discipline

### 4.1 Sampling-resolution ceiling

**Invariant (already enforced in code):** `if observed_lag ≤ effective_sampling_resolution: propagation_claim = invalid`.

Current resolution proxy: WS sampler cadence (1 Hz) + alert-engine M5-boundary alignment. Conservative envelope: `min_lead_ms = 5_000` ms ([epistemic-boundaries §3.2](lip-epistemic-boundaries.md)).

**Behavior when `observed_lag ≤ min_lead_ms`:**

- **Pair dropped before scoring.** Not "low confidence". Not "weak propagation". Not "penalized". Pair leaves no edge at all.
- This is **structurally unresolved ordering**, not a degraded measurement.

A future change to `min_lead_ms` is a Class C calibration change per [lip-governance §5](lip-governance.md) requiring [lip-execution-validation §22](lip-execution-validation.md)-style acceptance contract; the value is currently at L0 (Implementation constant) and has not been calibrated against an observed-lag distribution.

### 4.2 Replay-window dependence

Two replay invocations with different `lookback_days` may legitimately produce:

- Different first-observed edges (a `(A, B)` pair entering the longer window may have a predecessor in `liquidity_alert_history` that the shorter window cannot see).
- Different `confidence_score` for the same edge (volume_strength, temporal_consistency, recurrence_stability all scale with window length).
- Different verdicts for the same pair when data-quality category shifts.

This is **bounded replay visibility**, not drift. The layer is window-deterministic, not session-deterministic: same `(db, lookback_days, now)` → same output.

### 4.3 Edge truncation

Replay window truncation may hide:

- A prior qualifying edge.
- A prior activation event.
- An earlier adjacency that would have changed `lead_consistency` had it been visible.

Therefore the **visible replay chain ≠ full historical chain**. The visible chain is "all qualifying pairs we could see in the window we asked for". Extending the window may add edges; shortening it may remove them.

**Operational rule.** Edge presence in a 7-day window does not imply edge presence in a 14-day window; absence at one length does not imply absence at any other length. Cross-window aggregation requires explicit operator decision per [lip-governance §4](lip-governance.md) replay stability contract.

---

## 5. Replay-bounded sequence reconstruction

### 5.1 What replay reconstructs

Per [lip-execution-validation §21](lip-execution-validation.md) documentation labels (vocabulary for review / incident docs; not runtime enums):

| Documentation label | Manifestation in this stack |
|---|---|
| `REPLAY_AVAILABLE` | Window fully within `liquidity_alert_history` retention (90 d) AND post-activation |
| `PARTIAL_RECONSTRUCTION` | Window straddles retention boundary OR activation timestamp |
| `INSUFFICIENT_PRE_EVENT_STATE` | Pre-activation window — propagation cannot synthesize edges where the alert history does not begin |
| `FORWARD_ONLY_UNAVAILABLE` | Window predates layer activation entirely |
| `STALE_REPLAY_INPUT` | Operator-defined for stale-analysis tier; not a runtime emit |
| `REPLAY_NOT_PERSISTED` | Pre-retention windows (rows pruned) |

### 5.2 What replay does NOT reconstruct

- Pre-activation events that were never written to `liquidity_alert_history`.
- Pre-retention windows after pruning.
- Off-tape drivers whose alerts were never emitted (per §1.6 common-shock blind spots).
- Inter-emission market events that occurred *between* persisted alerts.

### 5.3 Replay does not reinterpret history under new thresholds

If `min_lead_ms`, `lead_window_ms`, `evidence_count` floor, `symmetry_penalty` cutoff, or any other threshold changes between original computation and replay invocation, the replay produces output under the **new** thresholds. There is no per-row `calibration_version` stamp ([§2 audit](#2-implementation-audit-functions--persistence); platform-wide gap).

**Until calibration-version stamping is implemented, every cross-threshold-boundary aggregation is a known governance debt** ([lip-validation-and-calibration §5](lip-validation-and-calibration.md), [lip-governance §8](lip-governance.md)).

---

## 6. Common-shock discipline

### 6.1 What `common_driver` covers

`causal_propagation()` calls `find_common_driver()` over observed symbols in the window. For a candidate `(A, B)`, if a third symbol C is found whose alerts lead both A and B with shorter lag than A→B, the verdict becomes `COMMON_DRIVEN` (refusal ladder per [lip-metric-registry §B.5](lip-metric-registry.md)).

### 6.2 What `common_driver` does NOT cover (load-bearing)

Synchronized degradation under any of the following defaults to **ambiguity**, **NOT** propagation confidence, but is **not currently flagged** by the engine:

- **BTC-wide moves** without an observable BTC alert in the dataset (BTC may not have alerted during the window, or its alert classifier suppressed the event).
- **Venue outages** that affect all subscribed symbols simultaneously.
- **Liquidation cascades** that synchronously hit many symbols on the same venue (per [freeze §10.3](2026-05-23-architecture-freeze.md), liquidation-cascade windows are not currently detected as such).
- **Funding resets** at the funding interval boundary.
- **Macro shocks** (CPI prints, FOMC, geopolitical events) that arrive off-tape.

**Operational rule (load-bearing).** Refusal verdicts (`COINCIDENCE`, `COMMON_DRIVEN`, `EXPLORATORY`, `UNDER_EVIDENCED`) are evaluated **before** any propagation interpretation is surfaced. A consumer that bypasses the verdict to read raw `confidence_score` is bypassing the refusal layer; that bypass is forbidden in operator-facing surfaces.

### 6.3 Closest existing counterweight

The `anomaly_synchronization` probe in Distributed Stress Detection ([epistemic-boundaries §3.3 line 80](lip-epistemic-boundaries.md)) is the closest implemented counterweight for synchronized cascade windows. It is **not** wired into `causal_propagation()` as a common-driver candidate today; consumer-side cross-tabulation is operator-tier work.

---

## 7. Event chain reconstruction (legacy `narrative_causality`)

### 7.1 Semantic identity

**Event Chain Reconstruction** = deterministic template assembly of already-published layer outputs into an ordered tuple sequence.

### 7.2 Code reality (per [epistemic-boundaries §3.4 line 45](lip-epistemic-boundaries.md))

- **No model calls.**
- **No language generation.**
- **No inference of a market story.**
- Pure template composition with deterministic ordering by `ts_ms`.

### 7.3 Output structure

The output is a structured tuple stream of the shape:

```
(symbol, ts, edge_id, lag_bucket, confidence_state)
```

Not prose, not narrative, not human-readable storyline. UI rendering may format these tuples for operator reading, but the layer's emit is structured.

### 7.4 Renaming as a future Class B candidate

The legacy function name `narrative_causality()` predates this companion. Per [lip-governance §2](lip-governance.md), renaming a function emit-shape is a **Class B change** (semantic relabeling that touches the API surface — endpoints, persisted column references, operator notebooks). It is **deferred**, not "ignored":

- Documentation surfaces and operator UI MUST use "Event Chain Reconstruction" as the semantic identity (§10 banned-vocabulary "narrative" / "narrative causality" replacements).
- A future Class B PR may rename the function; that PR is gated on (a) call-site inventory, (b) endpoint-name migration plan, (c) operator-tier surface relabeling, (d) governance audit-trail entry per [lip-governance §10](lip-governance.md).
- Until renamed, the function continues to operate; this companion is its operative semantic contract.

---

## 8. Graph semantics hardening

### 8.1 Graph reading discipline

A dependency graph's edges represent **observed repeated lag relationships under current observation coverage** — they are not market structure truth.

**Edge absence ≠ absence of relationship.** A pair `(A, B)` may have a real off-tape lagged relationship that no alert pair captures because:

- One of A or B never alerted in the window.
- Their alerts were filtered by data-quality gates.
- The lag fell outside `[min_lead_ms, lead_window_ms]`.
- Their evidence_count was ≤ 1.

**Edge presence ≠ confirmed causation.** A pair `(A, B)` with a HIGH-confidence edge means the pair survived all per-edge gates. Per §3, this is exactly: stable lag, multi-window evidence, mirror-pair below penalty floor, no common-driver candidate among observed symbols. It is **not** "A causes B".

### 8.2 Graph-level metrics reading discipline

`integrity_score` (per [lip-metric-registry §B.4](lip-metric-registry.md)) is a weighted blend of `avg_confidence`, `(1 − sym_share)`, `(1 − weak_share)`, and `coverage`. It is a **measurement of graph internal coherence**, not a measurement of "how well the graph maps the market".

A high `integrity_score` means: the graph has high-confidence, asymmetric, non-weak edges over a covered symbol set. It does not mean: the graph correctly reflects market structure. A graph with sparse upstream observation can have high integrity yet miss the dominant market relationships entirely.

### 8.3 `influence_hierarchy` role labels (legacy vocabulary)

The role classification at [lip-metric-registry §B.6](lip-metric-registry.md) uses code labels `{ISOLATED, INSTABILITY_HUB, LEADER, FOLLOWER, AMPLIFIER}`. These are **observable properties of out-ratio and average confidence**, computed deterministically from the dependency graph.

**Critical reading discipline:**

| Code label | What it actually measures | What it does NOT mean |
|---|---|---|
| `LEADER` | `out_ratio > 0.70 AND avg_out_confidence ≥ 0.20` | "asset led the market", "asset is a market leader", "asset drove others" |
| `FOLLOWER` | `out_ratio < 0.30 AND avg_in_confidence ≥ 0.20` | "asset followed the market", "asset is reactive", "asset lacks autonomy" |
| `AMPLIFIER` | balanced out-ratio with sufficient confidence on either side | "asset propagates shocks" in any active sense |
| `INSTABILITY_HUB` | `stability < 0.30 AND ≥ 2 low-quality edges` | "asset is unstable" — it is the node's **edge structure** in this window that is unstable |
| `ISOLATED` | `total < 3` OR none of the above | "asset is disconnected from the market" |

The labels are **legacy code vocabulary**; renaming them is a Class B+E change (touches persisted state, operator surfaces, and emit shape). **Until renamed, every operator-facing surface using these labels MUST carry an inline disclosure that the label is an observed-property classification, not a market-role claim.** UI tooltips and accordion text are the load-bearing surface for that disclosure.

A future Class B rename candidate vocabulary (proposed, NOT IMPLEMENTED): `OUT_DENSE_NODE` (replaces `LEADER`), `IN_DENSE_NODE` (replaces `FOLLOWER`), `BALANCED_DENSE_NODE` (replaces `AMPLIFIER`), `EDGE_UNSTABLE_NODE` (replaces `INSTABILITY_HUB`), `SPARSE_NODE` (replaces `ISOLATED`).

---

## 9. Verdict taxonomy — allowed vs forbidden interpretation

`causal_propagation()` verdicts (priority order is refusal-first; precedence enforced at [lip-metric-registry §B.5 lines 246-253](lip-metric-registry.md)):

| Verdict | ALLOWED interpretation | FORBIDDEN interpretation |
|---|---|---|
| `COINCIDENCE` | "Symmetry penalty crossed ≥ 0.70; the pair's reverse direction has comparable evidence; ordering undecidable" | "A and B are unrelated", "no relationship" |
| `EXPLORATORY` | "Data quality (sample count) is INSUFFICIENT or LOW; the layer refuses to commit on thin evidence" | "edge is weak", "market in transition", "wait and see" |
| `COMMON_DRIVEN` | "A third observed symbol leads both A and B with shorter lag; common-driver candidate found" | "A and B are not related", "the market caused both", "C is the cause" |
| `UNDER_EVIDENCED` | "Evidence count ≤ 1; one observed adjacency is not enough to constitute a pattern" | "rare event", "noise", "anomalous" |
| `AMBIGUOUS` | "Asymmetry < 0.40; A→B and B→A have similar but not identical counts; direction unclear" | "uncertain causation", "mutual influence" |
| `DIRECTIONAL` | "All refusal gates rejected; repeated B-after-A adjacency survived under current observation constraints" | "A causes B", "A drives B", "A leads B in any market sense", "transmission confirmed" |

**Critical observation.** A `DIRECTIONAL` verdict is **structurally rare on current data, and that is correct** ([epistemic-boundaries §3.1 line 41](lip-epistemic-boundaries.md)). Verdict scarcity is a feature of refusal-first design; it is not a deficiency to be tuned away.

---

## 10. Banned vocabulary table

The following terms are out-of-vocabulary for this stack's documentation, commits, code comments, operator UI, alerts, exports, replay overlays, and any cross-layer composite that consumes this stack's emits.

| Banned | Approved replacement |
|---|---|
| narrative | structured event sequence / event chain reconstruction |
| storyline | observed sequence of emitted events |
| market story | structured tuple sequence |
| chain meaning | edge structure under observation window |
| propagation meaning | repeated temporal adjacency |
| structural meaning | observed edge composition |
| market memory | persisted alert history (90 d retention) |
| event ancestry | ordered replay-visible sequence |
| event genealogy | replay-bounded sequence reconstruction |
| event lineage | replay-bounded sequence reconstruction |
| cascade origin | first observed qualifying event |
| source asset | first observed edge participant |
| dominant driver | observed earlier edge participant |
| leader asset | observed out-dense node (legacy: `LEADER` enum per §8.3) |
| follower asset | observed in-dense node (legacy: `FOLLOWER` enum per §8.3) |
| market transmitted | (rejected — transmission is not measured) |
| transmission order | observed emission order under bounded sampling |
| propagation source | first observed qualifying event |
| root cause | (rejected — cause is not measured) |
| market influence | (rejected — influence is not measured) |
| influence hierarchy | observed graph role classification (legacy registry §B.6 name; reframed per §8.3) |
| intelligent propagation | (rejected — no inference layer) |
| causality engine | propagation / event-chain reconstruction stack |
| semantic map | dependency graph (under bounded observation coverage) |
| market dependency map | dependency graph (under bounded observation coverage) |
| market reacted because | observed emission order: B emitted after A |
| the market propagated | observed temporal adjacency between emissions |
| asset led | observed out-dense node in this window |
| asset followed | observed in-dense node in this window |

**Approved phrasings for consumer-side language:**

- "observed earlier edge participant"
- "observed later edge participant"
- "upstream edge" / "downstream edge"
- "lag-consistent pair"
- "repeated temporal adjacency"
- "replay-visible ordering"
- "first observed qualifying event"
- "replay-bounded sequence"
- "edge activation" / "edge participation"
- "measured lag structure"
- "dependency edge"
- "observed association"
- "bounded propagation candidate"

**Enforcement.** A label outside the approved set is simultaneously a [lip-governance §3](lip-governance.md) row 9 (inferred intent scoring) + row 11 (semantic relabeling) violation. UI tooltips, accordion text, and export-side renderers MUST conform.

---

## 11. Anti-overclaim invariant (load-bearing)

> If any of: lag resolution, replay continuity, upstream availability, common-shock disambiguation, or persistence coverage is insufficient, the layer MUST degrade, downgrade, or refuse propagation interpretation. The layer MUST NOT silently preserve causal authority.

**Currently-implemented degradations (grounded in code):**

| Insufficiency | Implemented response |
|---|---|
| `observed_lag < min_lead_ms = 5_000` ms | Pair dropped at ingestion before scoring |
| `observed_lag > lead_window_ms = 30 min` | Pair dropped at ingestion before scoring |
| `evidence_count ≤ 1` | Verdict → `UNDER_EVIDENCED` |
| `symmetry_penalty ≥ 0.70` | Verdict → `COINCIDENCE`; ordering undecidable |
| `common_driver` candidate found | Verdict → `COMMON_DRIVEN`; confidence × 0.35 |
| `data_quality ∈ {INSUFFICIENT, LOW}` | Verdict floored at `EXPLORATORY`; scarcity_factor damping |
| `asymmetry < 0.40` | Verdict → `AMBIGUOUS` |
| Pre-activation / pre-retention window | Window narrows; no synthesis |

**Documented but NOT IMPLEMENTED degradations:**

| Insufficiency | Required response | Status |
|---|---|---|
| Timestamp drift on the worker host | Output marked structurally suspect for affected window | Detector NOT IMPLEMENTED (freeze §10.2; epistemic-boundaries §3.3 line 79) |
| Synchronized-global-move overlap | `COMMON_DRIVEN` analogue without observable common-driver alert | Detector NOT IMPLEMENTED (freeze §10.3) |
| Cross-venue confirmation absence | Edge marked single-venue-only | Cross-venue field NOT IMPLEMENTED (this stack is structurally Binance-centric) |

**Inverse rule (load-bearing).** Removing or loosening any of the **implemented** degradations is a Class C calibration change and a candidate for [lip-governance §3](lip-governance.md) review. Silent removal — change without [lip-governance §10](lip-governance.md) audit-trail entry — is a governance violation regardless of intent. In particular:

- Raising `min_lead_ms` weakens the simultaneity guard.
- Lowering `asymmetry < 0.40` cutoff promotes ambiguous pairs to DIRECTIONAL.
- Lowering `symmetry_penalty ≥ 0.70` cutoff allows reverse-leaning pairs to escape COINCIDENCE.
- Removing the `EXPLORATORY` floor under low data-quality silently promotes thin-evidence verdicts.

Each requires the [lip-execution-validation §22](lip-execution-validation.md) acceptance contract template before any value moves.

---

## 12. Relationship to other layers

| Layer | This stack's relation |
|---|---|
| **Alert engine** (Layer 3 per [freeze §1](2026-05-23-architecture-freeze.md)) | Input source. `propagation_graph()` reads `liquidity_alert_history` — propagation operates on already-classified alerts, not on raw microstructure |
| **Regime Engine** ([lip-regime-engine.md](lip-regime-engine.md)) | Adjacent. `market_state_transitions` observes `coordinated_state`; propagation observes alert pairs. The two are independent epistemic surfaces. Per [regime-engine §17](lip-regime-engine.md): "Operates on alert lineage, not on `coordinated_state`. Transitions and propagation events may co-occur but the transition layer does not consume propagation graph." |
| **Distributed Stress Detection** ([epistemic-boundaries §4](lip-epistemic-boundaries.md)) | Adjacent. Distributed-stress probes are independent measurements over the same time window, not a causal chain ([epistemic-boundaries §4.2](lip-epistemic-boundaries.md)). The `anomaly_synchronization` probe is the closest counterweight to common-shock windows but is **not wired into `causal_propagation()`** today (§6.3) |
| **Influence hierarchy** ([lip-metric-registry §B.6](lip-metric-registry.md)) | Downstream consumer. Reads `causal_propagation()` verdicts; produces role classification. Legacy enum labels reframed in §8.3 |
| **Execution Validation** ([lip-execution-validation.md](lip-execution-validation.md)) | Independent. exec_impact is per-burst per-symbol; propagation is per-pair per-window. No data flow. Both inherit the same blind-spot inventory ([execution-validation §10 / §24](lip-execution-validation.md)) |
| **Sanity Audit** ([freeze §3](2026-05-23-architecture-freeze.md)) | Consumes `flicker_ratio` of regime transitions and propagation-graph stability metrics. One-way relation: audit reads; propagation stack does not consume audit output |
| **Governance** ([lip-governance.md](lip-governance.md)) | This document. Every threshold, every enum is a governance-controlled anchor |
| **Replay Reconstruction (Layer 12, Phase 19)** | Replay reads persisted alert/intelligence rows; propagation re-derives on demand. Replay does not store propagation edges; each replay invocation recomputes |

**Allowed for this stack:**

- Read `liquidity_alert_history` and `liquidity_intelligence_history` over a window.
- Emit per-pair edges, per-pair verdicts, graph-level integrity, role classifications.
- Apply refusal verdicts and confidence demotions per implemented gates.
- Surface verdicts and edges in operator investigation tools (Phase 18).

**Not allowed for this stack:**

- Infer causation, transmission, or influence.
- Recommend trade action based on a verdict, an edge, or a role label.
- Override execution validation verdicts.
- Aggregate verdicts into a composite "stress index" with other layers (per [lip-governance §7](lip-governance.md) composite contract).
- Persist edges as if they were ground-truth market structure (`propagation_edges` table would be Class B+E and require composite-contract clearance).
- Auto-update thresholds based on observed verdict-distribution drift (would be Class G hidden ML weighting).

---

## 13. Calibration, governance, maturity

| Aspect | Status |
|---|---|
| **This document's change class** | Class A (documentation-only) per [lip-governance §2](lip-governance.md). Authorized during [Operational Observation Period](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_operational_observation_period.md) |
| **Any threshold change** (`min_lead_ms`, `lead_window_ms`, `evidence_count` floor, `symmetry_penalty` cutoff, `asymmetry` cutoff, factor weights in `base_confidence`, scarcity map) | Class C. Requires [lip-execution-validation §22](lip-execution-validation.md)-style acceptance + audit-trail entry per [lip-governance §10](lip-governance.md). Until `calibration_version` stamping (§5.3) is implemented, replay across change-boundary is unmarked |
| **Verdict-precedence reorder** | Class B. Requires emit-equivalence check + golden-vector regression |
| **Renaming `narrative_causality` function or `influence_hierarchy` role labels** | Class B (semantic relabeling). Deferred candidate; the new vocabulary is operative in documentation now (§7.4, §8.3) |
| **Adding `timestamp_drift_flag` or `synchronized_global_move_flag`** | Class B + Class E (new emit field + likely persistence). NOT AUTHORIZED during Observation Period |
| **Adding a `propagation_edges` persisted table** | Class B + Class E + [lip-governance §7](lip-governance.md) composite contract (since edges aggregate from alerts). NOT AUTHORIZED during Observation Period |
| **Maturity stage** | Observational per [lip-governance §9](lip-governance.md). Promotion gated on [lip-metric-registry Part C](lip-metric-registry.md) PENDING measurements (DIRECTIONAL false-positive rate, edge lifetime, HIGH-confidence degradation) |
| **All threshold constants** | L0 (Implementation constant) per [lip-execution-validation §23](lip-execution-validation.md). None empirically calibrated |

---

## 14. What this document is not

- Not a new layer specification — the layer exists in code (research.py).
- Not a redesign — no runtime change proposed.
- Not a new graph architecture.
- Not a predictive engine.
- Not a market-causality engine.
- Not a narrative system.
- Not an influence hierarchy in any market-role sense.
- Not a transmission analyzer.
- Not an authorization to rename `narrative_causality()` or `influence_hierarchy` enum labels — those renames are Class B candidates, deferred.
- Not an authorization to add new persisted emit fields during the Operational Observation Period.

It is a documentation-only epistemic decomposition pass that takes a previously-undifferentiated "narrative causality" semantic blob and resolves it into seven primitives, each grounded in code, each bounded by what is measurable. The layer's behavior is unchanged; the semantic surface around it now reads as a bounded replay-aware lag-observation and event-order reconstruction system rather than as a causal interpreter of the market.
