# Execution Validation Layer — companion contract

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md) (§9.5 Realized vs Predicted Impact) and [`docs/lip-metric-registry.md`](lip-metric-registry.md) §A.5.

**Boundary statement (load-bearing).** The Execution Validation Layer measures **divergence between expected visible execution and observed visible execution**. It does not measure "market truth", "real liquidity", "honest execution", "hidden flow", or "execution intent". Those phrases are out-of-vocabulary for this layer — see §15 below. The layer is a deterministic per-burst comparison runtime operating only on persisted observable state, with structurally bounded blind spots enumerated in §10.

**Cross-cutting ontology invariant** (per [`lip-ontology-boundaries.md`](lip-ontology-boundaries.md)): the layer emits bounded observational classifications under current instrumentation constraints. It does not establish authoritative market ontology.

---

## 1. Execution Validation Contract (canonical compact)

The layer:

1. **Compares** book-walk-predicted visible impact against post-settle realized mid move, per same-side taker-print burst.
2. **Operates only** on the in-memory `book_history` ring (≤ 60 snapshots) and the live trade tape — no historical reconstruction, no synthesis.
3. **Emits no result before activation.** L2 depth is not persisted to disk; bursts before the layer subscribed are structurally unmeasurable and not approximated.
4. **Treats missing pre-event or post-settle book state as event-drop**, not as zero or interpolated value.
5. **Measures divergence**, not market truth. A high `divergence_bps` is a *measurement of mismatch*, not a *claim about hidden liquidity*.
6. **Bounds its inputs to visible top-20.** Execution mechanics outside that surface (iceberg fills, RPI, OTC, hidden matching, queue priority) are not observed and not inferred (see §10).

The publishable per-burst quantities are exhaustively: `expected_bps · realized_bps · divergence_bps · ratio · book_exhausted` (plus side · bucket · notional · ts). There are no derived "execution quality" scores, no confidence values, no verdicts.

---

## 2. What the layer is, what it is not

**The layer DOES:**

- Compute, per closed burst, the book-walk impact implied by the pre-burst visible top-20.
- Measure the signed mid move from pre-burst snapshot to post-settle snapshot.
- Take the arithmetic difference (`realized − expected`) and the ratio (gated by an expected-magnitude floor).
- Flag bursts whose notional did not fit in the visible top-20 (`book_exhausted = True`).
- Publish per-(side, bucket) rolling 5-minute medians of the four quantities, plus event counts and exhaustion counts.

**The layer DOES NOT:**

- Predict future impact.
- Score execution quality.
- Infer hidden liquidity from divergence.
- Claim that the book "lied" when `realized > expected`.
- Reconstruct historical execution before activation.
- Backfill missing snapshots with interpolation, averages, or model output.
- Combine its outputs with other layers (Credible Depth, Fragility, Resiliency, Phase 15 Causal, Phase 16 Adaptation) into composite scores — by mandate of [project_operational_observation_period](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_operational_observation_period.md).
- Issue alerts, badges, or warnings.

---

## 3. Measurement contract (13 dimensions)

| Dimension | Specification |
|---|---|
| **Purpose** | Per-burst comparison of book-walk-predicted visible impact vs. realized mid move, over a fixed `SETTLE_MS = 500` ms post-burst horizon |
| **Inputs** | (a) trade tape (`state.trades`, advanced past `exec_cursor_ts`), (b) `state.book_history` ring (in-memory, ≤ 60 depth20 snapshots), (c) wall-clock `now_ms` |
| **Prediction baseline** | `_find_pre_snapshot(state.book_history, burst[0].ts)` → latest snapshot with `ts ≤ first_trade.ts`. Static snapshot, not rolling baseline. Top-20 only |
| **Observed baseline** | `_find_post_snapshot(state.book_history, burst[-1].ts + SETTLE_MS)` → first snapshot with `ts ≥ target_ts`. Pre and post mids both required strictly positive |
| **Prediction horizon** | Implicit in book-walk: the order of magnitude of liquidity that would be needed to fill `total_qty` of the burst, walked level-by-level over the *pre-snapshot* asks (for taker-buy) or bids (for taker-sell) |
| **Observation horizon** | `burst[-1].ts + SETTLE_MS` (500 ms after the last print of the burst). Chosen short enough to capture the impulse, long enough for touch quotes to refresh. Not configurable per symbol |
| **Aggregation horizon** | `EVENT_WINDOW_MS = 5 × 60 × 1000` ms rolling window for published per-(side, bucket) medians. Events outside the window are pruned by `_prune` |
| **Divergence computation** | `divergence_bps = realized_bps − expected_bps`. **Signed**, **not normalized**, **not volatility-adjusted**, **not spread-adjusted**, **not clipped**. Positive sign = market moved more than book-walk predicted; negative = less |
| **Ratio gating** | `ratio = realized_bps / expected_bps` published only when `expected_bps ≥ EXPECTED_FLOOR_BPS = 0.5`. Below floor, ratio = None (noise-amplification suppression) |
| **Replay dependencies** | Pre-burst book snapshot + post-settle book snapshot + trade tape continuity, all in-memory only |
| **Forward-only constraint** | `exec_cursor_ts` advances monotonically; bursts before subscription are unmeasurable; pre/post snapshots aged out of the ring are not reconstructed (event dropped) |
| **Failure conditions** | See §4 outcome enum. Sub-floor notional, missing pre/post snapshot, non-positive mid, walk-exhausted top-20 — each produces a specific outcome, none produces a fabricated value |
| **Blind spots** | See §10. Hidden / iceberg / RPI / OTC / queue priority / venue routing all unobservable; the layer measures visible-only execution |

---

## 4. Execution outcomes — the real per-burst enum

The code does **not** implement a market-state machine. There is no `VISIBLE_DEPTH_OK → PARTIAL_DEPLETION → BOOK_EXHAUSTED → RECOVERY_PENDING` graph in `exec_impact.py`. What exists is a per-burst **outcome enum** with three values:

| Outcome | Conditions (grounded in `exec_impact.py:204-254`) | Published fields |
|---|---|---|
| **DROPPED** | `notional < NOTIONAL_FLOOR_USD = 5_000` OR `total_qty ≤ 0` OR pre-snapshot missing OR post-snapshot missing OR `pre.mid ≤ 0` OR `post.mid ≤ 0` | None. Cursor advances; event never appears in `state.exec_events` |
| **EXHAUSTED** | Pre + post snapshots present; book-walk over visible top-20 cannot fill `total_qty` (returns `exhausted=True`) | `realized_bps` only. `expected_bps · divergence_bps · ratio = None`. `book_exhausted = True`. Counted in `exec_exhausted_<side>_<bucket>` and global `exec_book_exhausted` |
| **MEASURED** | Pre + post present; walk completed with finite vwap; all mids positive | All four values populated. `ratio` is None when `expected_bps < EXPECTED_FLOOR_BPS = 0.5`; otherwise populated |

**Transition between outcomes** is per-event and stateless across events. The layer does not track sequences of outcomes, does not classify regimes of exhaustion, does not infer market state from the outcome history. If sequence-level analysis is wanted, it lives at the observation/notebook tier (see [project_exec_impact_layer.md](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_exec_impact_layer.md) Observation Protocol), not in this layer.

**What the enum is not:** it is not a liquidity health classifier, not a market-regime label, not a stress level. EXHAUSTED means *one specific burst's notional exceeded one specific snapshot's visible top-20* — not "the market is exhausted".

---

## 5. Horizons (explicit)

| Horizon | Value | Source |
|---|---|---|
| Burst-grouping gap | `BURST_GAP_MS = 250` ms | Same-side prints within this gap belong to one burst |
| Settle delay | `SETTLE_MS = 500` ms | Wait from `burst[-1].ts` before reading post-settle mid |
| Tail-of-tape extension grace | `BURST_GAP_MS + SETTLE_MS = 750` ms | If burst is at the end of the visible tape, hold one more cycle before closing (more same-side prints could still extend it) |
| Per-event aggregation window | `EVENT_WINDOW_MS = 5 × 60 × 1000` ms (5 min) | Rolling window for published per-(side, bucket) medians |
| Book-history retention | ≤ 60 depth20 snapshots in-memory | Determines how far back a pre-snapshot can be looked up |
| Recovery horizon | **Not in this layer** | Recovery is measured by `intelligence.py` (`resiliency_score`, `recovery_time_ms`, `refill_velocity`) on a separate event ring; do not conflate |
| Replay horizon | **Activation forward only** | No bursts before subscription are measurable; no backfill |

---

## 6. Divergence semantics (explicit)

`divergence_bps = realized_bps − expected_bps`

where:

- `realized_bps`: signed mid move in the taker's direction. For taker-buy: `(post.mid − pre.mid) / pre.mid × 1e4`. For taker-sell: `(pre.mid − post.mid) / pre.mid × 1e4`.
- `expected_bps`: signed book-walk impact in the taker's direction. For taker-buy: `(vwap_asks − pre.mid) / pre.mid × 1e4`. For taker-sell: `(pre.mid − vwap_bids) / pre.mid × 1e4`.
- vwap is computed by `_walk` over price-sorted pre-snapshot levels in the consumption direction.

**What divergence is:**

- Sign-preserving difference of two visible-only measurements.
- Symbol-relative through the `/ pre.mid` denominator (bps are by construction a percentage of the pre-mid).

**What divergence is not:**

- Not normalized for volatility (no σ-scaling, no z-score).
- Not adjusted for spread.
- Not clipped to any range — extreme values pass through unchanged.
- Not "the amount of hidden liquidity". `realized > expected` means *the mid moved more than walking the visible top-20 would suggest*; the explanation may be cross-venue arbitrage, MM cancellations, iceberg fills, mid recalculation, or measurement noise. The layer measures the mismatch; it does not name the cause.
- Not a stable across-symbol quantity at the *absolute* level — bps anchored to mid are comparable across symbols only insofar as their mid-price scale comparison is meaningful for the operator's question.

**Ratio gating.** `ratio = realized_bps / expected_bps` is published only when `expected_bps ≥ 0.5` bps (in absolute value sense, applied to the signed expected). Below the floor, the denominator is in the noise band of book-walk arithmetic — a 0.1 bps expected with 5 bps realized gives ratio=50, which is not informative about market behavior but about arithmetic. The floor suppresses this.

---

## 7. Book exhaustion — exact observable conditions

`book_exhausted = True` iff, when walking the pre-snapshot top-20 in the taker's direction with `target_qty = total_qty` of the burst:

- the walk consumes all 20 levels without filling `target_qty` (`remaining > 1e-12` at end of levels), OR
- the levels are degenerate (every level has `price ≤ 0` or `qty ≤ 0`, so `filled ≤ 0`)

In code: `_walk` returns `(None, True)`; `_measure` emits an `ExecEvent` with `expected_bps = divergence_bps = ratio = None` and `book_exhausted = True`.

**What exhaustion is not:**

- Not "no liquidity beyond top-20" — there may be deep liquidity that the layer cannot see; we observe only top-20.
- Not "execution actually failed" — the trades cleared (we have their tape); the *predictor* failed (the visible book did not have room for them).
- Not "the market is broken" — frequent exhaustion may reflect a burst-size profile larger than the visible-tier window provides for that symbol.
- Not depth-collapse or refill-absent — those concepts live in `intelligence.py:RecoveryEvent`, on a different event model.

`exec_book_exhausted` (global per-window counter) and per-(side, bucket) `exec_exhausted_*` counts are the only diagnostics published for this outcome.

---

## 8. Replay semantics

**What replay reconstructs:** nothing in this layer. The per-burst measurement is computed at observation time and persisted as published metric samples (`liquidity_samples` rows). The underlying `ExecEvent` ring is in-memory only; the L2 book snapshots used to compute `expected_bps` are in-memory only; the trade tape window walked for burst detection is in-memory only.

**What replay cannot reconstruct:**

- Per-burst `expected_bps` for any burst whose pre-snapshot depth20 frame is not in `book_history` at measurement time (which is *any* burst after the in-memory ring has rolled).
- Any burst before subscription / layer activation.
- Any burst during a gap in book_history (e.g., reconnect, frame loss).
- Cross-venue execution paths.

**Status when replay-required state is absent:** `REPLAY_UNAVAILABLE` is not a published value; the layer instead **drops the event silently** and the cursor advances. There is no "partial reconstruction" mode — partial measurement of execution divergence is structurally undefined (`realized_bps` without `expected_bps` is just a mid move, not a divergence).

**What persists across restarts:** only the published per-event median samples in `liquidity_samples`. On restart, `book_history` and `exec_events` are empty; no historical bursts are re-measured.

---

## 9. Forward-only discipline (the invariant)

The Execution Validation Layer must never:

1. Synthesize a historical `expected_bps` for a burst whose pre-snapshot is missing.
2. Backfill a `realized_bps` value from pre-activation trade history.
3. Recompute an `ExecEvent` after the cursor has passed it (cursor is monotonic).
4. Interpolate a missing `post.mid` from before/after surrounding snapshots.
5. Substitute a model-predicted impact for an observed-but-missing measurement.
6. Emit an `expected_bps` from any source other than `_walk(pre.levels, total_qty)` over an actual `state.book_history` snapshot.

If any of (1)–(6) appears in the codepath, it is a defect. The invariant is preserved in `exec_impact.py` by: `book_history` retention bound + `_find_pre_snapshot` returning `None` (not interpolated) + `_measure` returning `None` on missing snapshots + `exec_cursor_ts = last.ts` after every burst (emitted or dropped).

This invariant is the operational expression of the boundary statement at the top of this document.

---

## 10. Blind-spot inventory

The layer **cannot observe**:

| Phenomenon | Why unobservable |
|---|---|
| Hidden / iceberg orders | Not in the visible top-20; appears only if/when it fills, with no advance signal |
| RPI (Retail Price Improvement) or hidden-mid liquidity | Not in the visible book at all |
| OTC / internalized flow | Routed off-venue; never appears in trade tape |
| Hidden matching layers | Not represented in the WS depth20 stream |
| Queue priority within a price level | Aggregated `qty` per level only; no per-order visibility |
| Venue-internal smart-order routing | The layer sees one venue's tape; cross-venue routing is invisible |
| Actual fill probability for any individual hypothetical order | Requires order-level state we do not have |
| Institutional execution logic (parent order, slicing strategy) | Out of vocabulary for a market-data layer |
| Market participant intent | Out of vocabulary for any layer in this platform per §11 of the freeze |
| Counterfactual: "what would have happened if the burst had been bigger / smaller" | The layer measures *what occurred*, not counterfactuals |

The layer **can observe only**:

- Visible top-20 state at snapshot timestamps.
- Visible trade tape (price, qty, side, timestamp).
- Pre→post mid movement at fixed 500 ms horizon.

Any statement about execution that goes beyond this column is **outside the layer's mandate** and must be rejected at design time, not patched at output time.

---

## 11. Execution realism boundaries

The layer **does** measure:

- Visible execution mismatch (divergence between book-walk and realized mid move).
- Visible liquidity depletion (top-20 walk-exhaustion).
- Comparison of expected vs observed visible impact.
- Distribution of those quantities over (side, bucket) buckets and 5-min rolling windows.

The layer **does not** prove or attempt to prove:

- That "real liquidity" exists or does not exist.
- That manipulation occurred.
- That hidden actors were present.
- That execution had a specific intent.
- The "true" fill path of any participant.
- The existence, depth, or composition of any invisible liquidity pool.

The reframe is exact and mandatory: divergence is a **measurement**, not a **verdict**. If a downstream consumer wants to treat large `divergence_bps` as evidence of "hidden flow" or "deceptive book", that interpretation lives at the operator's analytical tier (notebook, observation protocol), not in this layer's emit.

---

## 12. Calibration status

**Status: not empirically calibrated. Every threshold below is an implementation-time anchor pending observation.**

| Anchor | Value | Status |
|---|---|---|
| `BURST_GAP_MS` | 250 ms | Implementation default. Choice of "what counts as one burst" not empirically validated against trade-microstructure clustering |
| `SETTLE_MS` | 500 ms | Implementation default. "Long enough for touch quotes to refresh" — not measured against actual refresh latency distributions |
| `NOTIONAL_FLOOR_USD` | 5 000 | Implementation default. Noise-vs-signal cutoff not measured |
| `EXPECTED_FLOOR_BPS` | 0.5 | Implementation default for ratio gating; below-floor noise band not empirically characterized |
| `BUCKET_M_USD`, `BUCKET_L_USD` | 50 000, 500 000 | Interim absolute USD thresholds. Per-symbol quantile-based buckets explicitly deferred until enough history (per docstring line 28–29 and memory `project_exec_impact_layer`) |
| `EVENT_WINDOW_MS` | 5 min | Implementation default. Stability of medians at this window not characterized |
| `book_history` ring size | ≤ 60 snapshots | Memory-cost compromise. Coverage gap rate (bursts dropped due to aged-out pre-snapshot) not measured |

No threshold above has been calibrated against labelled regimes, against operator-validated event sets, or against any external execution-quality benchmark. The layer is in **pure observation mode** per `project_exec_impact_layer.md` — see Observation Protocol axes 1–16 for what is being collected before any threshold revisit.

---

## 13. Validation backlog (what is not measured)

| Validation question | Status |
|---|---|
| Distribution of `divergence_bps` across symbols / regimes | Not measured |
| Replay reproducibility (same input → same output) | Not measured (implicit from determinism of `_walk` + median, but not test-covered with golden vectors) |
| Stability of per-(side, bucket) medians across rolling windows | Not measured |
| Threshold stability (do anchors above need adjustment?) | Not measured — gated on observation period completion |
| Cross-regime behavior (calm vs panic divergence patterns) | Not measured |
| False-positive `book_exhausted` rate (exhaustion when deeper liquidity *was* present off-top-20) | Not directly measurable from this layer alone — would need an out-of-band ground truth |
| Buy/sell asymmetry stability | Not measured; observation in progress per Protocol axis 3 |
| S/M/L bucket coherence (do bursts route into intended buckets sensibly?) | Not measured; observation in progress per Protocol axis 4 |
| Pre-snapshot coverage rate (fraction of bursts dropped due to missing pre/post) | Not measured directly; `exec_count` vs total burst count is a proxy if both were published |
| Anti-spoof validation of `credible_depth` via persistent `realized ≫ expected` with `book_exhausted = False` | In backlog ([lip-validation-and-calibration.md](lip-validation-and-calibration.md)) |

Each row is a measurement that **has not been run**. None are blocking the layer's observational use; all are required before any threshold tuning or any downstream wiring (Fragility, Phase 16, etc.) is considered.

---

## 14. Precedence ordering

When multiple conditions could affect the per-burst outcome, the deterministic precedence is:

1. **Notional floor** — `notional < NOTIONAL_FLOOR_USD` ⇒ DROPPED. Overrides all other checks.
2. **Zero-quantity guard** — `total_qty ≤ 0` ⇒ DROPPED.
3. **Pre-snapshot availability** — pre-snapshot missing ⇒ DROPPED. Cannot compute `expected_bps`; layer never substitutes.
4. **Post-snapshot availability** — post-snapshot missing ⇒ DROPPED. Cannot compute `realized_bps`; no interpolation.
5. **Mid validity** — `pre.mid ≤ 0` OR `post.mid ≤ 0` ⇒ DROPPED. Avoids div-by-zero and sign corruption.
6. **Walk-exhaustion** — `_walk` returns exhausted ⇒ EXHAUSTED. `expected_bps / divergence_bps / ratio = None`; `realized_bps` still populated.
7. **Expected-floor** — `expected_bps < EXPECTED_FLOOR_BPS` ⇒ `ratio = None` (but other fields populated; outcome is still MEASURED).

The precedence is enforced by the structural order of checks in `_measure` (lines 212, 217, 219, 231–238, 246). No condition can be silently relaxed without changing this ordering, and any change must update this section.

**Cross-layer precedence (load-bearing):**

- A `realized_bps` value without an `expected_bps` (EXHAUSTED outcome) is **not** a divergence measurement. Downstream consumers must treat `expected_bps = None` as "no comparison possible", not as "zero predicted impact".
- `book_exhausted = True` is **not** a stress signal of its own. It is a measurement-mode flag. Frequency of exhaustion can be analyzed at the notebook tier, but not collapsed into the layer's emit as a verdict.
- If `expected_bps = 0` exactly (e.g., burst notional rounds to zero impact in book-walk) and `realized_bps ≠ 0`, `ratio` is suppressed by the floor and `divergence_bps = realized_bps`. This is correct behavior, not a defect.

---

## 15. Vocabulary discipline

The following phrases are **banned** from this layer's documentation, commits, code comments, and any operator-facing surface that exposes this layer's output:

| Banned | Replace with |
|---|---|
| truth engine | divergence measurement layer |
| execution truth | observed vs predicted visible execution |
| real liquidity | visible liquidity / observable depth |
| true executable liquidity | visible-tier walkable liquidity |
| actual market reality | observed market state |
| validated market truth | measured execution mismatch |
| market honesty / liquidity honesty | match between book-walk prediction and realized mid move |
| truth-validation layer | execution validation layer |
| the market lied | `realized_bps − expected_bps` was large |
| actual institutional flow | (not a thing this layer measures — reject) |
| hidden intent | (not a thing this layer measures — reject) |
| ground-truth execution | observed execution at SETTLE_MS horizon |
| execution intelligence | per-burst divergence diagnostics |

**Why this matters operationally.** Words like "truth", "real", "actual" embed an epistemic claim that the layer cannot back. If the documentation, code, or memory describes the layer as a "truth engine", every downstream consumer is licensed to read its outputs as authoritative claims about the market — which is exactly the overclaim this hardening pass exists to eliminate. The replacement vocabulary above keeps every claim co-extensive with what is actually computed.

**Memory note.** The prior memory entry `project_exec_impact_layer.md` line 10 described this layer as a "truth-engine, который мерит насколько honest рынок исполняется". That line is now superseded by the boundary statement at the top of this document. The memory entry has been updated correspondingly (commit log for this hardening pass).

---

## 16. Relationship to other layers — no merge

| Layer | Relationship to Execution Validation |
|---|---|
| **Credible Depth** ([A.1](lip-metric-registry.md#a1-credible-depth-credible_depth)) | Independent measurement. A persistent pattern of `realized ≫ expected` with `book_exhausted = False` *would* be empirical evidence against the credible-depth anti-spoof claim — but aggregating that relationship is in the validation backlog, not in this layer's emit |
| **Resiliency Score / Recovery Time / Refill Velocity** ([A.2](lip-metric-registry.md#a2-resiliency-score-resiliency_score), [A.9](lip-metric-registry.md#a9-recovery-time-recovery_time_ms), [A.10](lip-metric-registry.md#a10-refill-velocity-refill_velocity)) | **Different event model.** Recovery measures depth restoration after a stress trigger, on `RecoveryEvent` records in `intelligence.py`. Execution Validation measures impact divergence on `ExecEvent` records in `exec_impact.py`. The two rings are independent; do not conflate "execution recovery" with "depth recovery" |
| **Kyle λ / Fragility** ([A.3](lip-metric-registry.md#a3-impact-score-impact_score--kyle-λ-sigmoid), [A.4](lip-metric-registry.md#a4-fragility-score-fragility_score)) | Both consume bucketed trade impacts but on a *different* bucketing model (Kyle λ uses time-bucketed rolling impact estimation; Execution Validation uses per-burst direct measurement). The two are not redundant and are not mutually validating — both are uncalibrated |
| **Phase 15 Causal / Propagation** | Out of scope. Causal propagation operates on `liquidity_alert_history` records; no link with `ExecEvent` ring exists or should be added in observation period |
| **Phase 16 Adaptation / Feedback** | Out of scope. No `ExecEvent` data should be wired into adaptation modifiers until calibration is done and observation period concludes per [project_operational_observation_period](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_operational_observation_period.md) |
| **Distributed Stress probes** (§14 freeze) | Out of scope. Execution divergence is not currently a stress probe input and the seven existing probes do not consume `exec_*` metrics |

**Operational rule (load-bearing).** During the Operational Observation Period, no downstream layer may consume `exec_*` published metrics as input. Cross-layer wiring is gated on (a) completion of observation protocol axes 1–16, (b) explicit operator decision to exit observation mode, and (c) at minimum one calibration pass against labelled execution episodes.

---

## 17. What this document is not

- Not a feature spec for a new layer.
- Not a predictive execution model.
- Not a slippage forecasting design.
- Not a market simulator.
- Not a hidden-liquidity detector.
- Not a "truth engine" — the prior framing has been superseded; see §15.

It is a documentation hardening pass that formalizes already-existing observational measurement code, bounds its claims to what is computable from visible state, and makes the blind-spot inventory and vocabulary discipline explicit so future work cannot drift into overclaiming.

---

## 18. Execution observability taxonomy

A canonical classification of every quantity, flag, or claim this layer can touch. The taxonomy is documentation discipline only — it is not an enum the code emits.

| Category | Meaning | Qualifies | Does NOT qualify |
|---|---|---|---|
| **Execution-observable** | Directly measured from persisted visible state at the moment the burst is processed | `realized_bps` (pre→post mid move), `total_qty`, `notional_usd`, `book_exhausted` flag (from `_walk`), burst side, burst timing | Hidden fills, queue position, non-top-20 depth, any pre-activation event |
| **Execution-derived** | Computed deterministically from observable execution measurements | `expected_bps` (book-walk over pre-snapshot), `divergence_bps = realized − expected`, `ratio` (when above floor), per-(side, bucket) medians, `exec_count_*`, `exec_exhausted_*`, `exec_book_exhausted` | Any model output, any volatility-adjusted version, any score, any verdict |
| **Replay-reconstructable** | Reproducible from already-persisted state | The per-event median samples already written to `liquidity_samples` (re-readable across restarts) | Per-burst `expected_bps` after the in-memory ring rolls; any individual `ExecEvent` after its 5-min window prunes |
| **Forward-only observational** | Structurally unavailable before subscription / activation | (no qualifying quantity — this is a negation category) | Every burst before the layer subscribed; every burst during a `book_history` gap (reconnect, frame loss) |
| **Visibility-limited** | Bounded by what the depth20 WS stream contains | Top-20 walkable liquidity, top-of-book mid, visible spread | Beyond-top-20 liquidity, hidden mid, RPI, iceberg residuals, cross-venue depth |
| **Execution-unverifiable** | Cannot be inferred from observable state regardless of sample size | Any claim about which side of a divergence was "right", any attribution of cause for `realized ≫ expected`, any fill-quality score for a hypothetical order | (everything in this row is by definition out of mandate) |
| **Diagnostic-only** | Suitable for operator diagnosis; not for automated action under observation period | All `exec_*` published metrics, the per-burst enum in §4, `book_exhausted` frequency | Any auto-action input; any cross-layer composite score |

A quantity belongs to **exactly one** category. Operator and downstream code must not reclassify (e.g., treating `divergence_bps` as Execution-verifiable rather than Execution-derived/Diagnostic-only) without an explicit calibration pass per §22.

---

## 19. Observable vs executable separation

Distinct from §10 (which inventories specific unobservable phenomena), this section is the **canonical compact CAN/CANNOT table** to remove any remaining ambiguity between *observable execution behavior* and *true execution reality*.

| Layer CAN observe (visible-only) | Layer CANNOT observe (structurally) |
|---|---|
| Visible top-20 depletion in pre-snapshot when walked against burst notional | True queue priority within any price level |
| Visible spread state at pre and post snapshots | Hidden venue internalization, smart-routing decisions |
| Visible refill dynamics (insofar as next snapshot is in `book_history`) | Actual fill probability for any hypothetical order |
| Realized visible mid impact (`realized_bps` at SETTLE_MS) | Hidden / iceberg / RPI liquidity participation in the burst |
| Divergence between book-walk prediction and realized mid (`divergence_bps`) | Institutional execution quality, child-order slicing, parent-order intent |
| Burst-level notional and side from trade tape | True execution intent, manipulation, coordination |
| Frequency of `book_exhausted` outcomes per (side, bucket) | Counterfactual impact (what if burst were larger/smaller) |
| 5-min rolling distribution shape of all four published quantities | The "real" liquidity present at the moment of the burst |

**Operational rule.** Any inference that *crosses* this boundary (right column derived from left) is out of mandate. Such inferences may be drawn at the notebook / analytical tier with explicit assumption disclosure; they must not enter this layer's emit, code, or doc as if they were observations.

---

## 20. Replay edge cases (exact behavior)

This layer has no replay engine — L2 book state is not persisted to disk (see §8). What follows formalizes how the existing forward-only code path responds to the edge cases that *would* require replay if replay existed.

| Case | Code path | Outcome | Emitted state |
|---|---|---|---|
| **A. Truncated book history** — required pre-snapshot aged out of the ≤ 60-snapshot ring | `_find_pre_snapshot` iterates `book_history` oldest→newest; if even the oldest entry has `ts > first_trade_ts`, returns `None` ([exec_impact.py:130-140](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L130-L140)) | DROPPED ([exec_impact.py:220](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L220)) | None. Cursor advances past the burst's last trade ([exec_impact.py:201](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L201)); event never appears in `state.exec_events`. No retry |
| **B. Partial coverage — pre missing, post present** | Same as A: `pre is None` short-circuits the `if pre is None or post is None: return None` guard | DROPPED | None |
| **B'. Partial coverage — pre present, post missing** | `_find_post_snapshot` returns `None` because no snapshot with `ts ≥ burst[-1].ts + SETTLE_MS` is in the ring at measurement time ([exec_impact.py:143-149](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L143-L149)) | DROPPED | None. Note: the in-loop wait `(now_ms - last.ts) < SETTLE_MS` ensures we do not measure prematurely, but it does **not** guarantee a post-snapshot has been captured into `book_history` by then — that depends on the WS depth20 cadence, which is independent. If the cadence is sparse vs. SETTLE_MS, this case can occur and is **honestly dropped**, not interpolated |
| **C. Burst crossing retention boundary** — burst's first trade older than retention floor by the time `_measure` runs (e.g., cursor lag, paused consumer) | `_find_pre_snapshot` returns `None` (oldest ring entry younger than `first_trade_ts`) | DROPPED | None. The intuition "burst started before retention floor, ended after" collapses to case A: the layer only needs the pre-snapshot, and that snapshot is gone |
| **D. Replay cadence mismatch** — historical snapshot cadence differs from runtime cadence | N/A. The layer does not consume historical snapshot streams. `book_history` is appended at runtime by the depth20 WS handler; there is no offline cadence to mismatch with | (case is structurally inapplicable) |
| **E. Partial event reconstruction** | The layer has no mode that emits a partial `ExecEvent`. EXHAUSTED is *not* partial replay — it is a successful measurement of `realized_bps` with a structural reason (visible top-20 insufficient) why `expected_bps` cannot be computed. DROPPED is *not* partial replay — it is the absence of an event | The published surface has exactly: full `MEASURED`, partial-fields `EXHAUSTED` (no `expected/divergence/ratio`), or no event at all |

**Suppression invariant.** None of A, B, B', C ever produces a fabricated `expected_bps`, an interpolated `realized_bps`, or a "best-effort" `ExecEvent`. The cursor's monotonic advance after a drop ([exec_impact.py:201](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L201)) guarantees the same burst is never retried with later state — even if a delayed snapshot would have made measurement possible.

---

## 21. Replay-availability — documentation labels (NOT runtime states)

The following labels describe states the layer's input *can be in* relative to a hypothetical replay request. They are **vocabulary for documentation, reviews, and incident write-ups** — they are not enum values the runtime emits, and adding them as such would constitute the "invented state graph" rejected in §4.

| Label | Meaning | Concrete manifestation in this layer |
|---|---|---|
| `REPLAY_AVAILABLE` | Required input state persists and is reproducible | Per-event median samples in `liquidity_samples` (re-readable). Per-burst `ExecEvent` records: never replay-available (in-memory only, pruned after 5 min) |
| `PARTIAL_RECONSTRUCTION` | Some but not all required inputs persist | Not applicable. The layer does not emit partial events |
| `INSUFFICIENT_PRE_EVENT_STATE` | The pre-snapshot needed to compute `expected_bps` is absent | Manifests as a DROPPED outcome with `pre is None` (case A / C in §20) |
| `FORWARD_ONLY_UNAVAILABLE` | The event predates subscription / layer activation | Burst's first trade `ts < layer_subscription_ts`. No emission, no log; the cursor was initialized to or past this point |
| `STALE_REPLAY_INPUT` | Persisted input exists but is below the staleness threshold of the analysis being run | Not applicable to per-burst measurement. Applies only at the operator tier when reading historical `liquidity_samples` rows: an operator-defined staleness rule lives in the analysis notebook, not in this layer |
| `REPLAY_NOT_PERSISTED` | Required state was never written to durable storage | The permanent label for L2 depth20 frames and per-burst `ExecEvent` records. By design |

**Discipline.** When writing incident reports, post-mortems, or PR descriptions that reference an inability to recompute a historical execution event, use these labels exactly. Do not invent new ones, do not collapse `INSUFFICIENT_PRE_EVENT_STATE` and `REPLAY_NOT_PERSISTED` (one is about ring eviction, the other about the absence of any disk persistence in the first place).

---

## 22. Calibration governance — per-validation acceptance contract

Compact governance contract for each open validation in §13. Every row defines what "validated" would actually mean. None are run today; all are gated on the Operational Observation Period exit.

| Validation target | Min sample size | Replay coverage requirement | Acceptance condition | Rejection condition | Cross-regime requirement | Recalibration trigger | Stale-review cadence |
|---|---|---|---|---|---|---|---|
| Divergence distribution stability per (side, bucket) | ≥ 1 000 `MEASURED` events per (side, bucket) across ≥ 5 symbols | All from already-persisted `liquidity_samples`; no per-burst re-replay needed | Median IQR(`divergence_bps`) consistent across two non-overlapping observation windows of equal duration within 25% | IQR shift > 50% window-to-window without an external regime explanation | At least one calm-regime window and one stress-regime window (operator-labelled) | Bucket threshold change OR symbol set change | Quarterly |
| Replay reproducibility | Determinism is structural in `_walk` + `_median`; sample size for tests = ≥ 50 golden vectors | None (synthetic test inputs) | Bit-exact match on golden vectors for `realized_bps · expected_bps · divergence_bps · ratio · book_exhausted` | Any non-determinism in `_walk`, `_median`, or pruning order | N/A | Any change to `_walk`, `_measure`, `_median`, `_prune` | Per-PR |
| Per-(side, bucket) median stability across rolling windows | ≥ 500 events per (side, bucket) | None | Median absolute deviation across consecutive 5-min windows below an operator-set threshold (TBD per symbol) | Persistent non-stationarity unrelated to known events | Required across ≥ 3 symbols | Threshold revision | Monthly |
| Anchor threshold drift (any of `BURST_GAP_MS`, `SETTLE_MS`, `NOTIONAL_FLOOR_USD`, `EXPECTED_FLOOR_BPS`, `BUCKET_M_USD`, `BUCKET_L_USD`, `EVENT_WINDOW_MS`) | ≥ 30 days of `MEASURED` events across the symbol set | None (uses live emit) | Distribution of relevant quantity stable under candidate threshold change, in dry-run analysis | Material distribution shift (operator-defined) | All anchors require multi-regime evidence before adjustment | Operator-initiated; never auto | Annual |
| Cross-regime behavior (calm vs panic) | ≥ 100 events per regime per (side, bucket) | All from `liquidity_samples` if labelled regimes exist; otherwise blocked on labelling effort | Distribution shape change qualitatively documentable per regime | No labelled regime data available | Two regimes minimum | Labelled-regime set update | Per regime-set revision |
| `book_exhausted` false-positive rate | Out of scope for this layer alone | Requires out-of-band ground truth (off-top-20 liquidity that *was* present); not currently sourced | Cannot accept on internal data only — must remain "not directly measurable" until external source defined | (acceptance precluded) | N/A | External-source change | When external source is added |
| Buy/sell asymmetry stability | ≥ 500 events per side per bucket | None | Sign-coherent asymmetry across observation window per Protocol axis 3 | Sign flips without identifiable cause | Cross-symbol consistency | Bucket scheme change | Quarterly |
| S/M/L bucket coherence | ≥ 1 000 events across buckets per symbol | None | Bucket transitions trace volume curve of trades within symbol | Bucket boundary collapses (one bucket dominates by > 90%) per Protocol axis 4 | Per-symbol | Symbol addition, bucket scheme change | Per symbol addition |
| Pre-snapshot coverage rate | Total burst count must be observable (currently not directly published) | N/A | Coverage ratio `MEASURED / (MEASURED + DROPPED) ≥ operator-set floor` | Coverage < floor sustained over ≥ 1 hour for any active symbol | Per symbol | `book_history` ring size change | Monthly |
| Credible-depth anti-spoof empirical test | See [lip-validation-and-calibration.md](lip-validation-and-calibration.md) — owned by the validation companion | (governed there) | (governed there) | (governed there) | (governed there) | (governed there) | (governed there) |

**Governance rule.** No anchor in §12 may be moved without (a) the relevant row's acceptance condition met, (b) operator sign-off, (c) corresponding update to §12 with the new anchor value plus a one-line link to the calibration record. The doc must reflect calibration state, not aspiration.

---

## 23. Calibration maturity levels

Maturity classification for each numeric anchor and each published quantity. **Operational maturity only.** This is not a marketing scale; it is a documentation discipline that prevents accidental promotion of an L0 anchor to a load-bearing constant.

| Level | Meaning | What it permits |
|---|---|---|
| **L0 — Implementation anchor** | A value picked at implementation time on engineering judgment; never measured against empirical distribution | Use as a runtime default. **Does not permit** publication as a recommended threshold for downstream layers or for operator dashboards' alert lines |
| **L1 — Observed not validated** | The value has been carried through ≥ 30 days of `MEASURED` emission across the active symbol set without producing degenerate behavior (no excessive DROPs, no exhausted-saturation, no zero-band ratio storms) | Adds permission to be referenced in operator documentation as "current observed-stable anchor". Still **does not permit** cross-layer wiring |
| **L2 — Cross-regime measured** | Distribution of the quantity it gates is characterized across ≥ 2 operator-labelled regimes; behavior at the anchor is documented in each | Adds permission to drive a dry-run alert (notebook tier), with the regime label explicit |
| **L3 — Replay-stable across retention window** | Re-derivation on the published `liquidity_samples` history yields qualitatively identical distribution to the live emit; the anchor's quantile rank within that distribution is stable across two non-overlapping windows | Adds permission to be referenced as an anchor with a defined update cadence (§22 stale-review). Still **does not permit** auto-action |
| **L4 — Operationally trusted under documented conditions** | The anchor has passed L0–L3, has a written acceptance record (per §22), has an explicit invalidation condition, and an operator has signed off | Permits being a load-bearing input to a downstream layer under the documented conditions only |

**Current state.** Every anchor in §12 is **L0**. No anchor has progressed past L0. Progression requires the §22 governance contract to be exercised; no anchor self-promotes by elapsed time alone.

**What L0–L4 do NOT classify.** Outputs themselves (`divergence_bps`, `ratio`, etc.) are not subject to maturity levels — they are deterministic computations of their inputs. Maturity attaches only to anchors and to validation backlog items in §13/§22.

---

## 24. Structurally unknowable execution conditions (epistemic ceilings)

Distinct from §10's blind-spot inventory and §13's validation backlog: these are properties that **cannot become observable by improvement of this layer**. They are epistemic ceilings, not engineering TODOs. Listing them prevents the validation backlog from accreting items that are not measurements but category errors.

| Property | Why it is a ceiling, not a backlog item |
|---|---|
| Hidden queue priority within any price level | The depth20 stream aggregates `qty` per level. No protocol surface exposes per-order queue position; no future calibration of this layer recovers it |
| Hidden matching engine logic | Venue-internal; not exposed on any public WS feed regardless of subscription tier |
| Venue internalization decisions for any individual trade | Trade tape reports an executed print, not the routing path that produced it |
| OTC offsetting against the burst | OTC flow does not appear in venue tape; no amount of visible-state observation reconstructs it |
| Hidden smart-order routing across venues | Layer subscribes to one venue's WS streams; cross-venue routing decisions live at the routing layer of off-platform participants |
| Non-visible liquidity sponsorship (MM commitments not exposed in book) | Sponsorship contracts and reserved-quote logic are off-protocol |
| True institutional execution objectives (parent-order target, urgency, benchmark) | Not in any market-data surface; out of vocabulary for any layer in this platform per §11 of the freeze |
| Counterfactual outcomes (what would have happened with a different burst) | The market state was not observable on the counterfactual path because that path did not occur |
| Whether two visible counterparties are the same actor | Identity unification requires off-tape data this platform does not consume |

**Discipline.** If a proposed validation item or feature implicitly requires one of these, it is rejected at design time — not deferred to backlog, not flagged as "future work". It does not belong on the roadmap.

---

## 25. Cross-layer precedence chain (compact)

Consolidates §8/§9/§14 into a single deterministic ordering. The chain is evaluated **per burst** during measurement and **per consumer interaction** when downstream code references this layer's emit (gated by §16's no-merge rule for the observation period).

**Per-burst evaluation (existing precedence — §14 expanded):**

1. `FORWARD_ONLY_UNAVAILABLE` (burst predates subscription) — no emission, no cursor entry. Wins over all.
2. `NOTIONAL_FLOOR_USD` / zero-qty guards — DROPPED. Wins over snapshot availability.
3. `INSUFFICIENT_PRE_EVENT_STATE` (pre-snapshot missing per §20 A/C) — DROPPED. Wins over post-snapshot availability and walk results.
4. `INSUFFICIENT_PRE_EVENT_STATE` (post-snapshot missing per §20 B') — DROPPED. Wins over walk results.
5. Non-positive mid — DROPPED. Wins over walk results.
6. `_walk` exhaustion — EXHAUSTED. Wins over expected-floor.
7. Expected-floor — `ratio = None` (outcome remains MEASURED).

**Per-consumer evaluation (load-bearing for any code that reads this layer's emit):**

1. `REPLAY_NOT_PERSISTED` (per-burst events) suppresses any historical per-burst query. Wins over any analytical request.
2. `STALE_REPLAY_INPUT` (operator-defined for analyses on `liquidity_samples`) suppresses verdict-style summarization at the notebook tier. Wins over presence of data.
3. EXHAUSTED outcome (`expected_bps = None`) suppresses divergence-based reasoning. **Must not** be treated as `expected_bps = 0` (zero predicted impact) by any consumer.
4. `expected_bps < EXPECTED_FLOOR_BPS` suppresses ratio interpretation. **Must not** be re-derived by the consumer with a lower floor.
5. Insufficient (side, bucket) sample count in window — suppresses comparison-of-medians narrative. Threshold is operator's analytical concern, not this layer's.
6. Operational Observation Period active — suppresses cross-layer composite. Wins over all of the above (no consumer wiring exists during the period; see §16).

The chain is acyclic and idempotent. Re-evaluation of the same burst with the same inputs yields the same precedence outcome.

---

## 26. Allowed-claims contract

The exhaustive list of what this layer's emit and documentation are **allowed** to claim, and the exhaustive list of what they are **not** allowed to claim. Phrased as a contract so that PR reviews and incident docs can cite it directly.

**Allowed claims (this layer, its emit, its documentation may state):**

- A specific burst's `realized_bps` was X (signed, in taker's direction, at SETTLE_MS).
- A specific burst's `expected_bps` was Y given the pre-snapshot top-20 (book-walk).
- A specific burst's `divergence_bps` was `realized − expected`; large absolute values indicate measurement mismatch, not market truth.
- The visible top-20 was insufficient to fill the burst's notional (`book_exhausted = True`).
- Across the 5-min rolling window, per-(side, bucket) median of any of the four quantities was Z.
- The frequency of EXHAUSTED outcomes per (side, bucket) was N.
- A burst was dropped because pre or post snapshot was unavailable; the reason is structural per §20.
- The relevant anchor is at maturity level L0 per §23 and has not been calibrated.

**Disallowed claims (this layer's emit and documentation may NOT state):**

- That a divergence "reveals" hidden liquidity, hidden actors, or hidden routing.
- That the market "lied", was "honest", was "manipulated", or "actually behaved differently from how it appeared".
- That `realized > expected` proves the existence of off-top-20 liquidity for that burst.
- That a participant had a specific intent, slicing strategy, or benchmark.
- That `book_exhausted = True` means the market has no deeper liquidity (it means *this snapshot's visible top-20* had none for *this burst*).
- That any anchor is the "right" value; only the maturity level it is at per §23.
- That this layer validates or invalidates Credible Depth, Fragility, Resiliency, or any other layer — the relevant cross-tabulation lives in [lip-validation-and-calibration.md](lip-validation-and-calibration.md) and is currently not measured.
- That a divergence signal warrants any auto-action.

**Enforcement.** A PR, doc revision, commit message, or operator-facing surface that emits a disallowed claim is a defect of equivalent severity to a code defect that violates §9's forward-only invariant. The remediation is the same: revert / reword / re-scope to the allowed column.
