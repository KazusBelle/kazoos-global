# PHASE 3B Observation Report — SPECIFICATION

**Status: SPECIFICATION + RESULTS (2026-05-29). Read-only analysis over persisted PHASE 3B data.** Implements nothing in the runtime. Reusable read-only tooling: [`shared/kazus_logic/liquidity/exec_validation_report.py`](../shared/kazus_logic/liquidity/exec_validation_report.py).

**Outcome (on record).** The report was run on 41,597 MEASURED bursts. Finding: `NO_DEFENSIBLE_THRESHOLD` — `|divergence_bps|` is monotone-decaying (no natural break); per-symbol p90 disperses ~3× (BTC 0.91 → XRP 2.78); divergence scales with notional; neither `persistence_quality` (flat) nor a volatility proxy (weak/inverse) explains it. **A single global divergence band is not supported.** Consequently the **PHASE 3C verdict layer was REJECTED and its pre-commit code discarded**; `divergence_bps` stays a **continuous** measurement, not a categorized state. PHASE 3C is **NOT AUTHORIZED**. This document remains the canonical methodology for any future re-evaluation if more data accrues.

**Purpose (as originally specified).** Decide, from observed data, **whether a divergence verdict layer is justified at all** and, if so, how a divergence band would have to be calibrated. The methodology must be able to conclude *"no defensible threshold exists"* (which it did). It must never label individual bursts good/bad, nor assert manipulation / intent / quality.

**Companion to:** [`lip-execution-validation.md`](lip-execution-validation.md) (§4), [`lip-validation-and-calibration.md`](lip-validation-and-calibration.md), [`lip-governance.md`](lip-governance.md), [`project_exec_verdict_phase3c`](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_exec_verdict_phase3c.md).

---

## 0. Method constraints

- **Read-only** over persisted tables; no engine/runtime interaction; no new persistence; no new emitted field.
- **Forward-only / observation-window-bounded**: analyze `[T0, T1]` where `T0` ≥ first PHASE 3B activation (`liquidity_exec_validation` earliest `local_recv_ts`). State the window and row counts up front; never backfill.
- **Coverage-first**: every burst in-window is accounted for by `execution_validation_state`. Divergence statistics are computed **only over `MEASURED` rows** (the only rows with non-null `divergence_bps`); `EXHAUSTED/DROPPED/UNKNOWN` are reported as coverage fractions, never silently excluded.
- **Deterministic**: every figure must be reproducible from the persisted rows by the stated query. No sampling without a fixed seed.
- **No new vocabulary**: the report describes distributions; it does not introduce VALIDATED/DIVERGENT or any per-burst label.

## 1. Data sources & scope

| Source | Fields used |
|---|---|
| `liquidity_exec_validation` (3B) | `symbol, execution_validation_state, divergence_bps, expected_impact_bps, realized_impact_bps, exhaustion_state, burst_side, burst_notional, burst_start_ts, burst_end_ts, local_recv_ts` |
| `liquidity_bursts` (3A) | `burst_trade_count, burst_duration_ms` (join on `symbol, burst_start_ts, burst_end_ts`) — burst shape context |
| `liquidity_samples` | `persistence_quality`, `atr_liquidity`, `spread` (per-symbol 1 Hz; as-of joined to `burst_end_ts`) |
| `liquidity_runtime_health` | `failure_boundary` — to **exclude/segment** windows where ingest was degraded (e.g. drop bursts whose window overlaps `SCHEDULER_STARVATION`/`FEED_NETWORK_SILENCE`), so divergence isn't contaminated by instrumentation gaps |

**Coverage table (mandatory first output):** per symbol and overall — count and % of each `execution_validation_state`, and the `MEASURED` count that feeds all downstream divergence stats. A divergence analysis on a small `MEASURED` fraction is itself a finding.

## 2. Required analyses

### §1 Divergence distribution analysis
Over `MEASURED` rows:
- Both **signed** `divergence_bps` (detects directional bias: does realized systematically exceed/undershoot visible expectation?) and **absolute** `|divergence_bps|` (magnitude — the quantity a band would cut on).
- Report: n, mean, median, std, IQR, and percentiles **p50, p75, p90, p95, p99** of `|divergence_bps|`; histogram with fixed bins; and the signed mean (bias).
- **Segment by notional bucket** (S `<50k` / M `<500k` / L) and by `burst_side`, because `expected_impact_bps` is sub-bps for small bursts (gated by `EXPECTED_FLOOR_BPS=0.5`) → small-notional divergence is dominated by realized noise. Also report the distribution **conditioned on `expected_impact_bps ≥ EXPECTED_FLOOR_BPS`** separately.
- **Shape question to answer explicitly:** is `|divergence_bps|` unimodal-decaying (→ any band is an arbitrary cut on a continuum) or does it show separation / a heavy tail / bimodality (→ a band may correspond to a real structural break)? This is the single most decision-relevant figure.

### §2 Symbol-by-symbol divergence statistics
- The §1 percentile table computed **per pinned symbol** (BTC/ETH/SOL/BNB/XRP), plus `MEASURED` count per symbol.
- **Cross-symbol stability metric:** spread of each percentile across symbols (e.g. p90 range / dispersion). If p90 varies widely across symbols, a **single global** `DIVERGENCE_BAND_BPS` is not defensible and the report must say so (→ per-symbol bands or no 3C).

### §3 Correlation with `exhaustion_state`
- Divergence distribution of `MEASURED` rows split by carried `exhaustion_state` (`WITHIN_VISIBLE` vs the EXHAUSTED population which has null divergence — report the latter as a rate, not a divergence).
- Question: does `|divergence_bps|` differ materially when the book was near-exhausted vs comfortably within visible depth? (Informs whether exhaustion should pre-empt the VALIDATED/DIVERGENT split — which 3C already does by mapping EXHAUSTED separately.)

### §4 Correlation with `persistence_quality`
- **As-of join**: for each `MEASURED` burst, take the most recent `persistence_quality` sample for that `symbol` with `ts ≤ burst_end_ts` (and within a freshness bound, e.g. ≤ 5 s; else mark `pq = NULL/unknown` — no interpolation).
- Bin `persistence_quality` (e.g. by its own per-symbol terciles, or fixed [0,0.5),[0.5,0.8),[0.8,1.0]) and report `|divergence_bps|` percentiles per bin.
- Question: is divergence **inflated when measurement quality was low** (i.e. is apparent divergence partly an artifact of a poorly-measured book rather than a real expected-vs-realized gap)? If yes, 3C must gate on `persistence_quality` and any band calibrated on low-pq data is suspect.

### §5 Correlation with volatility regime
- **No persisted volatility-regime label exists** → derive a proxy at analysis time, observation-only (NOT a new output):
  - Primary proxy: per-symbol realized volatility over a fixed pre-burst window from the mid/`price` series in `liquidity_samples` (or `atr_liquidity` as-of `burst_end_ts`); optionally `spread` as-of as a secondary axis.
  - Bin into per-symbol percentile regimes (e.g. low/mid/high = terciles of the proxy). State the proxy and binning explicitly; do not name it a "regime classifier."
- Report `|divergence_bps|` percentiles per volatility bin per symbol.
- Question: does the divergence distribution (and any candidate band) **drift with volatility**? If a band stable in calm vol fails in high vol, a static `DIVERGENCE_BAND_BPS` is not defensible.

### §6 Candidate threshold discovery methodology
The decision procedure for `DIVERGENCE_BAND_BPS` (only reached if §1 shows separable structure):
1. **Separation test:** look for a natural break in `|divergence_bps|` (e.g. kernel-density antimode, knee/elbow of the sorted curve, or a gap in the empirical CDF). Absence of a break ⇒ **report "no empirical threshold" and recommend NOT building 3C.**
2. **Candidate bands:** if a break exists, enumerate candidates (the break location, plus p90/p95 as reference cuts).
3. **Stability validation:** a candidate band qualifies only if it is **stable across symbols (§2), across persistence_quality bins (§4), and across volatility regimes (§5)** within a stated tolerance. Report each candidate's induced DIVERGENT-rate per segment; high variance ⇒ reject the single global band.
4. **Sensitivity:** report how the DIVERGENT-rate moves as the band varies ±50%; a band on a steep part of the curve (tiny change → large rate swing) is fragile and should be rejected or widened.
5. **Sample-sufficiency gate:** thresholds may only be proposed when each segment has ≥ a stated minimum `MEASURED` count; otherwise return `INSUFFICIENT_DATA` for that segment (refusal-first, consistent with the rest of LIP).
6. **Output:** either (a) a recommended band (global or per-symbol) **with** its qualifying evidence and a `calibration_version` proposal, or (b) an explicit **"3C not justified by current data"** verdict-on-the-method (not on bursts).

## 3. Report outputs (artifacts)

- A coverage table (§1 mandatory first output).
- The distribution figures/tables of §1–§5.
- A §6 decision section ending in one of: **`THRESHOLD_RECOMMENDED`** (with values + evidence + scope: global vs per-symbol), or **`NO_DEFENSIBLE_THRESHOLD`**, or **`INSUFFICIENT_DATA`** (with the minimum-sample shortfall).
- All numbers traceable to a stated query + window + row counts.

## 4. Governance & boundaries

- This is a **Class A** (documentation/analysis) artifact. The report it specifies is **read-only**; producing or running it changes no runtime, emits no new field, and creates no per-burst classification.
- The report informs a **future, separately-authorized** PHASE 3C decision per `observe → accumulate → analyze → authorize → implement`. It does not authorize 3C.
- The report must preserve LIP epistemic boundaries: it describes **observable divergence distributions**, never manipulation, intent, hidden liquidity, execution quality, or future direction. "DIVERGENT-rate" in §6 is a property of a *candidate cut on observed data*, not a statement about any market participant.
- Run only over windows where `liquidity_runtime_health.failure_boundary` indicates healthy ingest for the relevant interval; degraded-ingest windows are segmented out so instrumentation gaps do not masquerade as divergence.
