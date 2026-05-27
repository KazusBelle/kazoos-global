# LIP Epistemic Boundaries

**Canonical source for what the Liquidity Intelligence Platform refuses to infer.**

Extracted from `docs/2026-05-23-architecture-freeze.md` on 2026-05-26 (Option-A documentation decomposition pass). The freeze document remains the snapshot at freeze time; this doc is the living reference for the platform's epistemic ceiling from this point forward.

Cross-doc back-references:
- **Runtime mechanics** that enforce these boundaries: `docs/2026-05-23-architecture-freeze.md` §1, §3, §13.2–13.4, §13.7, §14.
- **Per-metric contracts**: `docs/lip-metric-registry.md`.
- **Failure modes / blind spots**: `docs/2026-05-23-architecture-freeze.md` §10.
- **Calibration status**: `docs/lip-validation-and-calibration.md`.

---

## 1. Foundational epistemic states

The following are **first-class output states the runtime emits**, not hedges. A reader who treats them as soft-uncertainty is reading them wrong.

| state | meaning |
|---|---|
| **INSUFFICIENT** / **UNDER_EVIDENCED** | the layer refuses to commit a verdict; never silently substituted with a default |
| **structurally unknowable** | the input data does not carry the property at all (e.g. `propagation_graph` aggregates over the window, so per-frame transmission order is not derivable from it) |
| **no measurable basis** | a candidate edge / link / verdict was rejected because no probe could attach a number to it |
| **replay unavailable before activation** | a layer that was deployed at time T cannot reconstruct itself at any time < T |
| **causality not asserted** | a directional lead-lag pattern exists in the data, but the layer publishes it as a candidate verdict, not a causal claim |

What the runtime **does** (verb list, complete and load-bearing): measures · validates · reconstructs · aggregates · compares · suppresses · emits · tracks · rejects · scores.

What the runtime **does not** do: understand the market · see structure · interpret intent · narrate causality · assert hidden actors.

---

## 2. Non-inference list

The runtime does **not** infer the following. A new layer that emits any of them violates the invariants documented in `docs/2026-05-23-architecture-freeze.md` §8.

- **Market intent.** No layer characterizes what a participant is trying to achieve. Observable: order placement, fill, cancellation. Not observable: motive.
- **Manipulation attribution.** Credible Depth flags persistence below 400 ms as non-credible. It does not label that flicker "spoofing" — it labels it "did not meet the persistence threshold." A 200 ms quote could be a market-maker re-quoting on a refresh tick, not a spoof.
- **Coordinated hidden actors.** Synchronized cross-symbol liquidity deterioration triggers Distributed Stress Detection's `anomaly_synchronization` probe and increases `propagation_graph`'s `symmetry_penalty`. None of this attributes causation to "a coordinated group" — synchronized stress and shared shock look identical to the layer, and the layer says so by demoting the verdict rather than committing it.
- **Future price direction.** Every forecast endpoint (`/research/intelligence-forecast`, regime transition forecast, multi-horizon) is OLS extrapolation with explicit `slope_capped` / `extrapolation_capped` / `horizon_decay` / `cap_factor` discounts. No layer publishes a directional trade signal.
- **Causality without measurable lag.** `causal_propagation` requires (a) `asymmetry ≥ 0.40`, (b) `evidence_factor ≥ 2/n_windows`, (c) `common_driver_factor` survival, (d) `symmetry_penalty ≤ 0.70`, AND (e) the underlying `propagation_graph` already dropped any pair with `lead < min_lead_ms = 5_000` ms — failure on any of these forces the verdict to UNDER_EVIDENCED / AMBIGUOUS / COMMON_DRIVEN / COINCIDENCE / EXPLORATORY. A DIRECTIONAL verdict is structurally rare on current data and that is correct.
- **Propagation ≠ causation.** A DIRECTIONAL verdict means "B repeatedly followed A with a stable measurable lag, on independent windows, and not in lockstep, and not jointly driven by a third symbol we could find." It does **not** establish economic causality, transmission certainty, or directional influence in the sense a research paper would use those terms.
- **Actor identity.** No layer reads exchange-side maker/taker account information or attempts to fingerprint flow to known actors. The data sources (public REST + public WS) do not carry this information.
- **Strategic objectives of participants.** No semantic interpretation of a flow as "accumulation," "distribution," "shakeout," etc. These labels are absent from the codebase by design.
- **Free-form narrative.** Event Chain Reconstruction (`narrative_causality`) is a deterministic template composed from already-published layer outputs. No model calls, no language generation, no inference of a market story.

If a verdict, edge, or score appears without one of the upstream factors above being either present-and-measurable or explicitly flagged INSUFFICIENT / UNDER_EVIDENCED, treat it as a bug.

---

## 3. Propagation epistemic ceiling

**Canonical decomposition of the propagation/event-chain stack:** [`lip-causal-propagation.md`](lip-causal-propagation.md) — seven primitives (propagation edge · temporal adjacency · dependency graph · event chain reconstruction · conditional propagation candidate · common-shock suppression · replay-bounded sequence reconstruction), banned-vocabulary table, anti-overclaim invariant. This section is the load-bearing ceiling; that companion is the decomposition. Any divergence is a defect of the companion.

### 3.1 The load-bearing invariant

> **Propagation edges represent repeated lagged association under observed conditions. They do not establish causal certainty.**

When the layer publishes an edge A → B with `confidence = HIGH`, the literal meaning is: across the lookback window, B's alerts repeatedly started ≥ 5 s and ≤ 30 min after A's, with stable lag, on independent sub-windows, with no common-driver candidate found among observed symbols, and not as a bidirectional mirror. That is what the formula measures. It is not a claim that A *caused* B in any market-microstructure sense — only that the timestamps line up that way, repeatedly, under the conditions the data exposes.

### 3.2 Simultaneity rule

`propagation_graph` uses `min_lead_ms = 5_000` ms as a hard pre-scoring drop, not as a penalty:

- Alerts are timestamped at coarse granularity relative to actual transmission. Within ~5 s, the timestamps do not carry enough resolution to identify a first mover.
- "First-mover" assignment on sub-`min_lead_ms` pairs is **structurally unknowable** from the data — not low-confidence, not uncertain, but unknowable.
- Dropping rather than penalizing is the only representation consistent with this constraint: a penalized score is still a score, and a score still appears in the graph. A dropped pair leaves no edge at all.

Generalized rule for any future propagation layer: `if observed_lag ≤ effective_sampling_resolution: propagation_claim = invalid`. Current resolution proxy is the WS sampler cadence (1 Hz) plus the alert-engine M5-boundary alignment — `min_lead_ms = 5_000` is the conservative envelope around both.

### 3.3 Causal refusal conditions

The propagation layer **refuses to publish a directional verdict** when any of the following holds. Each maps to a specific code path:

- **Sub-resolution lag.** `observed_lag < min_lead_ms` → pair dropped before scoring.
- **Insufficient episodes.** `evidence_count ≤ 1` → UNDER_EVIDENCED.
- **Lag instability.** `lead_consistency < threshold` → `causal_confidence` decays multiplicatively; if data_quality is borderline, the verdict drops to EXPLORATORY.
- **Common-shock contamination unresolved.** `find_common_driver()` returned a candidate → COMMON_DRIVEN.
- **Bidirectional mirror.** `symmetry_penalty ≥ 0.70` → COINCIDENCE.
- **Data scarcity.** `data_quality ∈ {INSUFFICIENT, LOW}` → EXPLORATORY (refuses to commit on thin evidence even if everything else looks clean).
- **Replay unavailable.** Pre-activation windows → `data_quality = PRUNED/INSUFFICIENT`; reconstruction does not invent edges.
- **Timestamp drift detected** *(known blind spot — `docs/2026-05-23-architecture-freeze.md` §10.2)*. If a future detector flags drift, propagation output must be marked structurally suspect for the affected window.
- **Synchronized global move overlap** *(known blind spot — §10.3)*. Liquidation-cascade windows are not currently detected as such; in their presence, the `anomaly_synchronization` probe of Distributed Stress Detection is the closest counterweight.

### 3.4 What the propagation layer does and does not do

**Measures:** pair counts of A→B alert sequences, lag distributions, sub-window survival, mirror-pair ratios, common-driver candidates among observed symbols.

**Does not measure or infer:** true economic causality, participant intent, hidden coordination, transmission certainty, directional influence under unresolved simultaneity, hidden actor identity, macro-driven co-stress without an observable common-driver symbol in the dataset, off-exchange flow that drives both endpoints.

An operator reading a propagation surface as evidence of strong-sense causation is reading past the layer's published ceiling. The downgrades in §3.3 reduce that reading surface; they do not eliminate it.

---

## 4. Distributed Stress epistemic ceiling

### 4.1 What the stress layer measures

The stress layer measures 7 independent probe scores from already-published upstream metrics, their distribution (hot / elevated / calm / insufficient), the contributing fraction, and a point-in-time verdict over the composite. See `docs/2026-05-23-architecture-freeze.md` §14 for the full state-machine.

### 4.2 What the stress layer does not infer

- Future market direction.
- The originating asset of a stress episode (composite has no single source by construction — it is an unweighted mean of contributing probes).
- The strategic objectives of any participant.
- Cross-venue confirmed stress (the layer is structurally Binance-centric — see §14.8 in the freeze doc).
- Event-level lifecycle timing (point-in-time only; no start/peak/end markers).
- Causal transmission between probes — the probes are independent measurements over the same time window, not a causal chain.

A downstream consumer (operator UI, alert routing, investigation auto-draft) that reads PRE_CASCADE as a market forecast is reading past the layer's published ceiling. The auto-draft path labels its output `kind = auto_draft` and inherits the experimental status of its inputs, so the UI surface for these cases already carries the qualifier.

---

## 5. Structurally unknowable conditions

The following are **not** uncertainties to be reduced by more data — they are properties the data structurally cannot resolve. They are flagged with their own status rather than degraded confidence.

| condition | flag / state |
|---|---|
| Per-frame transmission order on a `propagation_graph` edge | `structurally unknowable` — edges aggregate over the window, no timestamped pair data is carried |
| First mover within `min_lead_ms = 5 s` window | not represented as an edge at all (drop, not flag) |
| Whether a common-shock candidate is real macro or coincident burst | the layer flags COMMON_DRIVEN; it does not classify the driver |
| Simultaneity vs sub-resolution lag | indistinguishable below `min_lead_ms`; the layer does not try |
| Causal vs anti-causal direction when `symmetry_penalty ≈ 1` | indistinguishable; COINCIDENCE applies |
| Pre-activation replay windows | `data_quality = INSUFFICIENT/PRUNED`; no reconstruction |
| Cross-venue confirmed stress | NOT INTEGRATED (`/crossex` exists but `crisis_genesis` does not read it) — `docs/2026-05-23-architecture-freeze.md` §14.8 |
| Per-frame transmission in replay playback | `propagation_graph` edges are aggregated over the full lookback window, not timestamped — per-frame transmission order is structurally unknowable |
