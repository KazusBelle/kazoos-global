# Credible Depth — Persistence Quality (`persistence_quality`) — design contract (companion)

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md) (§9.1 Credible Depth, §13 known limitations), [`docs/lip-metric-registry.md`](lip-metric-registry.md) §A.1 / §A.1a, [`docs/lip-ingestion-contract.md`](lip-ingestion-contract.md), [`docs/lip-epistemic-boundaries.md`](lip-epistemic-boundaries.md), [`docs/lip-governance.md`](lip-governance.md).

**Status: IMPLEMENTED (2026-05-29, Phase 2B).** `persistence_quality` ships as a realtime emit — [`metrics.py:persistence_quality()`](../shared/kazus_logic/liquidity/realtime/metrics.py), emitted per tick from `engine._sample_all`, registered at [lip-metric-registry §A.1b](lip-metric-registry.md#a1b-persistence-quality-persistence_quality). It joins the already-shipping Credible Depth family (`credible_depth`, `credible_bid_depth`, `credible_ask_depth`, `credible_depth_delta` — [§A.1 / §A.1a](lip-metric-registry.md#a1-credible-depth-credible_depth)). This document is now the **canonical contract** for the output, not a speculative design.

**Governance classification (deliverable).** Per [lip-governance §2](lip-governance.md): **Class B + Class E**. Class B because it introduces a new measurement computation; Class E because it persists a new field. It is permitted during the Operational Observation Period under the **Class E carve-out for additive, append-only fields with no behaviour dependence** — and it qualifies: (a) it changes no existing emit's value, distribution, timing, order, or suppression; (b) nothing downstream consumes it (pure diagnostic, no composite, no firewall surface per §6); (c) no schema migration — `liquidity_samples` is key/value, so it is a new metric *name*, new rows only. The operator additionally authorized it explicitly on 2026-05-29 (PHASE 2B), removing any ambiguity. Audit entry: [lip-governance §14](lip-governance.md) `2026-05-29-01`. **What it is NOT:** not a new intelligence layer, not a spoof/manipulation detector, not a venue-quality or trust score, not a market observation. The §11 invariant holds — it increases *measurability/falsifiability* (the platform can now see when Credible Depth was poorly measured) without increasing interpretation, inference, or autonomy.

**Prior phase note.** In Phase 2A this was deferred (operator chose "split/delta only, stay in freeze") and this file existed as a DESIGN CANDIDATE. Phase 2B is the explicit authorization to build it. The Phase 2A reasoning — that a new emitted field with new states is a new capability not covered by Credible Depth's prior approval — still stands; it is precisely why this required separate authorization rather than folding in as "completion".

**Boundary statement (load-bearing).** A persistence-quality output, if built, would measure **how stably visible liquidity and the snapshot stream itself hold up between observed frames, under the current instrumentation surface (Binance depth20, ~1 Hz visible snapshots, visible-only liquidity, no sub-second persistence visibility)**. It would not measure, infer, or assert: true executable liquidity, hidden/iceberg liquidity, spoofing, manipulation, or any structural-irregularity *verdict*. Sub-400 ms quote behaviour is structurally unresolved at this surface and would remain so. The banned vocabulary of [lip-semantic-vocabulary-boundaries](lip-semantic-vocabulary-boundaries.md) applies in full: **real liquidity · honest book · true depth · fake liquidity · manipulation detected · spoof detected** are out-of-vocabulary. Permitted vocabulary: *persistent visible liquidity · survivorship · observable imbalance · structural irregularity (as a described observation, never as an emitted verdict)*.

**Cross-cutting ontology invariant** (per [`lip-ontology-boundaries.md`](lip-ontology-boundaries.md)): the output, if built, would emit bounded observational classifications under current instrumentation constraints. It would not establish authoritative market ontology.

---

## 1. Definition

**Persistence quality = an observable, graded assessment of how well-formed the depth20 snapshot sequence was over the measurement window — its freshness, completeness, and freedom from gaps — i.e. how trustworthy the substrate underneath the current Credible Depth reading is.**

It is **not**:

- An estimate of true executable liquidity.
- A spoof / manipulation / structural-irregularity verdict.
- A confidence score *about price*.
- A hidden-liquidity inference.
- A directional signal.

A "high persistence quality" measurement under this contract means *the depth20 stream arrived freshly, completely, and without significant gaps over the window, so the survivorship ages behind Credible Depth rest on a sound sequence*. It says nothing about what happens between snapshots, off-book, or below the ~1 Hz / 400 ms resolution floor.

---

## 2. Scope vs adjacent outputs

This output **consumes**, never re-derives, the following existing state:

| Source | What it provides to persistence_quality (as implemented) |
|---|---|
| `SymbolState.book_history` (bounded ring of frozen depth20 frames) | The frame **timestamps** over the window — the substrate for freshness, coverage, and gap continuity. Read-only; also used by the exec-impact layer |
| `SymbolState.mid_price()` | UNKNOWN gate: if mid is None, Credible Depth is unmeasurable and so is its quality → `None` |

Adjacent state **not** used by the current implementation but available for a future calibrated version: per-level `first_ts` (level-hold survival fraction), `last_depth_ts`, and [`liquidity_ws_status`](2026-05-23-architecture-freeze.md) (to corroborate a gap as stream-degradation vs quiet book). The metric **does not** introduce a new data source, expand the instrumentation boundary, add an operator surface, or feed any prediction/score downstream.

---

## 3. Formula specification (as implemented)

`persistence_quality(state, now_ms) → Optional[float]`, range `[0, 1]`. Computed over the depth20 frames in `state.book_history` whose `ts ∈ [now_ms − PQ_WINDOW_MS, now_ms]`. Three observations, **multiplied**:

1. **freshness** = `ramp(now_ms − latest_in_window_ts, good=PQ_FRAME_INTERVAL_MS, bad=PQ_STALE_MS)`. Guards against a stalled stream: if no fresh frame has arrived, the live `bids/asks` (and thus their `first_ts` ages) are stale, which would otherwise make levels look *artificially persistent* and inflate Credible Depth. A stale latest frame drives freshness — and the whole score — to 0.

2. **coverage** = `min(frames_in_window / (PQ_WINDOW_MS / PQ_FRAME_INTERVAL_MS), 1.0)`. Sequence **completeness**: how many of the expected ~100 ms-cadence frames actually arrived. Missing frames lower it. This is the load-bearing rule — *missing snapshots must lower persistence quality*, never be silently interpolated over.

3. **continuity** = `ramp(max_inter_frame_gap_in_window, good=PQ_FRAME_INTERVAL_MS, bad=PQ_MAX_GAP_MS)`. Presence of **gaps**: a single inter-frame gap ≥ `PQ_MAX_GAP_MS` zeroes it. Distinguishes "90% coverage spread evenly" (continuity ≈ 1) from "90% coverage with one big hole" (continuity → 0).

`ramp(x, good, bad)` = `1.0` for `x ≤ good`, `0.0` for `x ≥ bad`, linear between. **Result = freshness · coverage · continuity.** Multiplicative because quality is only as good as its weakest axis; a quality gate must under-claim, never over-claim.

Interim constants (all uncalibrated, Class C): `PQ_WINDOW_MS = 5_000`, `PQ_FRAME_INTERVAL_MS = 100`, `PQ_MIN_FRAMES = 10`, `PQ_STALE_MS = 1_000`, `PQ_MAX_GAP_MS = 1_000`. `PQ_FRAME_INTERVAL_MS` (the `@depth20@100ms` cadence) is the load-bearing assumption behind `coverage`.

**Maps to the four required aspects:** completeness → coverage; gaps → continuity; survivorship-window stability → freshness + continuity (irregular/stale frames destabilise the 400 ms age basis); data sufficiency → the `PQ_MIN_FRAMES` gate (→ None below it).

---

## 4. Mandatory invariants (enforced in code + tests)

- **UNKNOWN propagates; INSUFFICIENT is no-score.** Mid unavailable → `None` (UNKNOWN — quality of an unmeasurable Credible Depth is itself unknown). Fewer than `PQ_MIN_FRAMES` frames in window → `None` (INSUFFICIENT). At this realtime tier both are represented as `None` (no score) in `liquidity_samples`, consistent with `resiliency_score`'s no-events → None; the *distinguishing reason* is not separately persisted. Neither is ever coerced to a numeric default.
- **Measured-bad (`0.0`) is distinct from no-measurement (`None`).** A stale book or a ≥ `PQ_MAX_GAP_MS` gap yields a float `0.0` — "measured, and bad" — which must never be conflated with `None`. This is the load-bearing distinction; tested directly.
- **Missing snapshots degrade, never interpolate.** Gaps lower the score and are visible as such. No gap-filling, no carry-forward of a stale assessment as fresh.
- **No hidden fallback calculations.** Every degradation path is explicit; no alternate "best effort" estimate is substituted when inputs are thin — thin inputs yield `None`.
- **No manipulation / spoof / irregularity verdict is ever emitted.** The output is a measurement-quality scalar. A low score says *the measurement was poor*, never *the market did X*. It does not name a cause.
- **Replay-deterministic, append-only.** Pure function of `(state, now_ms)`; the live computation depends on in-memory frame timestamps, and the persisted row is authoritative for replay (same model as Credible Depth). New metric name, new rows only — no schema change, no row mutation.
- **Confidence degradation is explicit.** Thin windows, partial frames, and stream staleness each visibly lower the score rather than being absorbed.

---

## 5. Vocabulary table

| Forbidden | Permitted replacement |
|---|---|
| real liquidity / true depth / executable liquidity | persistent visible liquidity |
| honest book / fake liquidity | (no replacement — out of scope; describe survival, not authenticity) |
| spoof detected / manipulation detected | low measurement quality over the window (observation about the *measurement*, not the market) |
| "the book is degraded" (as a verdict) | snapshot continuity degraded / persistence quality lowered (observation) |

---

## 6. Open calibration questions (unresolved — score is uncalibrated)

These are why §A.1b is marked uncalibrated; the score is usable for relative diagnosis but its absolute level is not yet a calibrated judgement.

- Does the real depth20 arrival cadence match `PQ_FRAME_INTERVAL_MS = 100`? If it is systematically slower, `coverage` under-reads on healthy streams. This is the load-bearing constant; it should be set from the observed inter-arrival distribution, per symbol.
- What `PQ_WINDOW_MS` / `PQ_STALE_MS` / `PQ_MAX_GAP_MS` values distinguish a genuinely degraded stream from a quiet-but-healthy book? Current values are interim guesses.
- What score bands should map to operator-facing grades, per symbol? No baseline exists today; read relative-to-symbol, not absolute.
- Does sub-400 ms churn that is invisible at this surface systematically bias the assessment? Structurally unresolved — a stated blind spot, not closed by this metric.

Until these are answered against labelled samples (see [validation-and-calibration](lip-validation-and-calibration.md)), any `persistence_quality` grade would be uncalibrated and must be read as relative-to-symbol, not absolute.
