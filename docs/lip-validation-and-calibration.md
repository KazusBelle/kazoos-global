# LIP Validation & Calibration Status

**Calibration backlog and validation framework for the Liquidity Intelligence Platform.**

Extracted from `docs/2026-05-23-architecture-freeze.md` §12 on 2026-05-26 (Option-A documentation decomposition pass). This is a **living calibration tracker**, not a results report.

**Status warning:** every measurement listed below as "not yet measured" / "PENDING MEASUREMENT" has not been run. **No numbers in this document are real.** Do not cite figures from this document as if they were measured outcomes. When a calibration item is completed, replace the placeholder with the measured value + measurement date + source.

Cross-doc back-references:
- **Per-metric contracts** (uncalibrated thresholds, interim constants): `docs/lip-metric-registry.md`.
- **Epistemic boundaries** (what calibration cannot fix because it is structural): `docs/lip-epistemic-boundaries.md`.
- **Demotion / refusal paths** already encoded in code: `docs/2026-05-23-architecture-freeze.md` §3, §13, §14.5.

---

## 1. What "validation" means here

For an observability platform with no trading actions, validation is the measurement of:

- **Replay reproducibility** — does a FROZEN snapshot match a fresh LIVE reconstruction at the same anchor, field-by-field?
- **Verdict survival** — when a layer commits a verdict at time T, does it still hold at T + Δ on the same window?
- **Confidence calibration** — does a HIGH-confidence output empirically outperform LOW-confidence on the same downstream metric?
- **Threshold stability** — do interim constants (`CREDIBLE_BAND_PCT`, `DEPTH_COLLAPSE_DROP`, `KYLE_MIN_BUCKETS`, `RECOVERY_FRACTION`, etc.) hold across regimes?

---

## 2. Pending calibration measurements

| measurement | target metric | how it would be measured | status |
|---|---|---|---|
| Replay reproducibility rate | % of FROZEN-vs-LIVE diff entries where Δ = 0 across the published-diff field set, over N investigations | Re-run `replay_state(mode=live)` at the case anchor for every closed investigation; compare to the frozen payload field-by-field | **not yet measured** |
| Candidate rejection rate | fraction of propagation edges promoted to causal DIRECTIONAL out of all `propagation_graph` candidates | Count over a `lookback_days` window from current data | **not yet measured** |
| Sanity-audit suppression rate | fraction of pattern_discovery candidates suppressed by `discovery_suppression_modifier` over a rolling window | Compare `adaptation_recommendations` to `adapted_recommendations` count and importance-shift distribution | **not yet measured** |
| HIGH-confidence degradation frequency | rate at which an edge published with `confidence = HIGH` falls to MEDIUM/LOW on a subsequent window | Sliding-window tracking of per-edge confidence across causal_propagation runs | **not yet measured** |
| Propagation edge lifetime distribution | median / P90 / P99 days an edge persists in DIRECTIONAL after first publication | Persist edge-confidence snapshots; aggregate survival curves | requires snapshot table not yet created |
| Lag stability distribution | distribution of per-edge `lead_consistency` and `temporal_consistency` over rolling windows | Aggregate directly from `propagation_graph` rolling output | **not yet measured** |
| Stress-probe contribution rate | per-probe fraction of windows where the probe was `contributing` (data_quality ≠ INSUFFICIENT) | Aggregate from `crisis_genesis` history | **not yet measured** |
| Adaptation modifier oscillation rate | rate at which an adaptation modifier swings between bounds on consecutive runs | `adaptation_state` already returns `osc` flag; aggregate over time | partial — `osc` flag exists, no aggregated metric |
| Realized-vs-predicted divergence distribution | per-bucket median + IQR of `divergence_bps` across `exec_impact` events; cross-tabbed by `book_exhausted` | Aggregate from already-emitted ExecEvent stream | **not yet measured** — platform still in pure observation mode |
| Credible-depth anti-spoof empirical test | correlation between (Credible Depth at t) and (realized executable depth implied by `exec_impact.realized_bps` at t+ε) | Requires the realized-vs-predicted aggregation above | **not yet measured** — depends on previous item |
| Threshold stability under regime change | how often a threshold-crossing flips on sub-minute volatility | Sliding-window count of state transitions on the same input within `FLICKER_WINDOW` | exists for `flicker_ratio` in `market_state_transitions`; not generalized across layers |
| PRE_CASCADE false-positive rate | fraction of PRE_CASCADE verdicts followed by no forward-realized adverse market behavior within window W | Persist verdict outputs with timestamp; cross-reference forward market behavior measurements | **not yet measured** |
| Per-probe agreement matrix | pairwise correlation between the 7 stress probes — do they co-fire or are they independent? | Aggregate per-call probe scores; compute correlation matrix | **not yet measured** |
| Verdict transition stability | CALM ↔ EARLY_DISTORTION ↔ ELEVATED_RISK jitter rate at 300-s call cadence | Persist verdict history; count consecutive flips | **not yet measured** |
| Stress persistence distribution | how long PRE_CASCADE typically holds before demoting | Persist verdict history; aggregate hold-time distribution | **not yet measured** |

---

## 3. Suppression and demotion rules already in code (operational falsification)

Demotion paths already encoded in code, independent of the pending calibration numbers above:

- `causal_propagation` verdict downgrade chain: DIRECTIONAL → AMBIGUOUS (asymmetry < 0.40) → UNDER_EVIDENCED (evidence_count ≤ 1) → COMMON_DRIVEN → EXPLORATORY (data_quality ∈ {INSUFFICIENT, LOW}) → COINCIDENCE (sym_penalty ≥ 0.70).
- `market_state_transitions` lifecycle: PERSISTENT → ACCELERATING / FLICKER / REVERSED; REVERSED applies a `reversal_factor = 0.25` multiplicative penalty.
- `crisis_genesis` scarcity cap: > 3 probes INSUFFICIENT → verdict floored at EARLY_DISTORTION.
- Pattern discovery: 6 robustness flags (SINGLE_WINDOW, LOW_RECURRENCE, HIGH_LIFT_LOW_SUPPORT, REGIME_FRAGILE, BUCKET_SENSITIVE, LOW_SUPPORT) each apply a fixed multiplicative penalty to `stability_score`.
- Forecast: `cap_factor = 0.5` whenever slope or extrapolation was clipped.
- Sanity audit `propagation_loop` finding feeds `discovery_suppression_modifier = 0.50` when sanity is CRITICAL — the adaptation loop currently halves recommendation importance.

---

## 4. What an operator can verify today (without the calibration backlog)

Without the measurements above, the layer's outputs are still inspectable on five concrete properties:

1. **Published decomposition.** Every verdict carries its per-factor inputs; a reader who disagrees can locate the factor that drove the outcome.
2. **Explicit absence states.** INSUFFICIENT · UNDER_EVIDENCED · PRUNED · `book_exhausted = True` · `None`-return are first-class verdicts, not suppressed errors.
3. **Code-level demotion paths.** §3 above enumerates demotion chains — they are in code, not in policy.
4. **Acyclic dependencies.** `adaptation_state` reads from observed layers but never writes back. Operator priorities reads from everything but never feeds back.
5. **Bounded modifiers.** `ADAPTATION_BOUNDS` clips every coefficient; nothing compounds without limit.

When the calibration backlog is run, this document will move from "framework" to "framework + measured results."

---

## 5. Calibration-version dependency (load-bearing)

Thresholds like `CREDIBLE_BAND_PCT = 0.005`, `CREDIBLE_MIN_AGE_MS = 400`, `RECOVERY_FRACTION = 0.80`, `EXPECTED_FLOOR_BPS = 0.5`, `DEPTH_COLLAPSE_DROP = 0.40` are **version-bound**. Historical samples written under one set of constants are not directly comparable to samples written under a different set.

**The runtime does not currently version-stamp** which constants were live when a sample was written. This means:

- A future calibration pass that changes any threshold creates a discontinuity in `liquidity_samples` history.
- Replay reproducibility (measurement #1 above) is sensitive to threshold changes in ways the engine cannot currently detect.
- Cross-window aggregations (e.g. `resiliency_decay` probe's 6h-vs-6h comparison) silently mix constants across the boundary.

Adding version-stamping is itself a calibration prerequisite. Currently NOT IMPLEMENTED.
