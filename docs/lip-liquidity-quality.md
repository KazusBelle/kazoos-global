# Liquidity Quality — hardening companion

**Companion to:** [`docs/lip-metric-registry.md`](lip-metric-registry.md) §A.1–§A.10, [`docs/lip-execution-validation.md`](lip-execution-validation.md), [`docs/lip-venue-quality.md`](lip-venue-quality.md), [`docs/lip-epistemic-boundaries.md`](lip-epistemic-boundaries.md), [`docs/lip-governance.md`](lip-governance.md), [`docs/lip-semantic-vocabulary-boundaries.md`](lip-semantic-vocabulary-boundaries.md), [`docs/lip-ontology-boundaries.md`](lip-ontology-boundaries.md), [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md) §1 (Layer 2 Realtime WS Engine, §9–§14).

**Status: Class A hardening companion** for the already-implemented liquidity-quality measurement stack: Credible Depth, per-burst Execution Validation (exec_impact), Liquidation Stress, Resiliency / Recovery Time / Refill Velocity, Cross-Venue Divergence (crossex). No code changes; no new emit fields; no new thresholds.

**Cross-cutting ontology invariant** (per [`lip-ontology-boundaries.md`](lip-ontology-boundaries.md)): the layer emits bounded observational classifications under current instrumentation constraints. It does not establish authoritative claims about "real" or "synthetic" liquidity. Where the visible book's behavior differs from the realized impact, the layer measures the **divergence**; it does not attribute cause.

**Cross-cutting context.** Crypto liquidity at the visible top-of-book is partially survivorship-driven — quotes that vanish before they could fill are not the same as quotes that persisted across a measurement window. This companion frames the existing metrics around that distinction without claiming to detect specific actors, intents, or manipulation.

**Distinction from `lip-venue-quality.md`.** Venue Quality is a per-(venue, symbol, window) **DESIGN CANDIDATE** — no runtime. Liquidity Quality is the **already-implemented** per-symbol measurement stack documented here. The two are complementary axes, not competing.

---

## 1. Boundary statement

The liquidity-quality layer publishes per-symbol observables for:

- How much of the visible top-of-book depth survives long enough to be considered for execution (**Credible Depth**).
- How the visible book's predicted impact compares to the actual mid-price displacement following a same-side burst (**Execution Validation**, exec_impact).
- How quickly depth replenishes after a stress event, and whether it replenishes at all (**Resiliency / Recovery Time / Refill Velocity**).
- How much forced-liquidation flow is present in the trade tape, on a separate channel from voluntary flow (**Liquidation Stress**, `liq_stress`).
- How the visible state on this venue compares against a reference venue (**Cross-Venue Divergence**, `crossex`).

The layer **does not**:

- Claim a portion of liquidity is "synthetic" or "real". The terms are out of mandate.
- Detect manipulation, wash trading, or specific actor intent. These are off-tape claims; see [lip-execution-validation §10](lip-execution-validation.md) and [lip-epistemic-boundaries §2](lip-epistemic-boundaries.md) for the blind-spot inventory.
- Resolve sub-second spoof patterns. The sampler cadence is ~1 Hz; sub-cadence flicker is not observable.

---

## 2. Scope

| Source | Surface | Documented in |
|---|---|---|
| Visible top-of-book depth | `credible_depth`, raw depth, top-20 levels | [lip-metric-registry §A.1](lip-metric-registry.md) |
| Per-burst execution comparison | `exec_impact` (`expected_bps`, `realized_bps`, `divergence_bps`, `ratio`, `book_exhausted`) | [lip-execution-validation.md](lip-execution-validation.md) |
| Post-stress recovery | `resiliency_score`, `recovery_time_ms`, `refill_velocity` | [lip-metric-registry §A.2 / §A.9 / §A.10](lip-metric-registry.md) |
| Forced-liquidation flow | `liq_stress`, `state.liquidations` tape | [lip-metric-registry §A.6](lip-metric-registry.md) |
| Cross-venue reference | `crossex` (Bybit vs Binance pairwise divergence) | [lip-metric-registry §A.7](lip-metric-registry.md) |
| Visible per-bucket trade-impact magnitude (Kyle λ) | `impact_score`, `fragility_score` | [lip-metric-registry §A.3 / §A.4](lip-metric-registry.md) |

Out of scope here: latent stress prediction, fraud detection, manipulation verdicts, smart-money inference, trade-action recommendations.

---

## 3. Credible Depth as a coarse survivorship heuristic

### 3.1 What it measures (load-bearing)

`credible_depth` sums the USD value of visible top-of-book levels that satisfy **two structural conditions** at the moment of measurement:

| Gate | Threshold | Source |
|---|---|---|
| **Band** — the level's price lies within `CREDIBLE_BAND_PCT = 0.005` (50 bps) of the current mid | `mid · (1 ± 0.005)` ([metrics.py:62-63](../shared/kazus_logic/liquidity/realtime/metrics.py#L62)) | Implementation constant L0 |
| **Persistence** — the level's `first_ts` is at least `CREDIBLE_MIN_AGE_MS = 400` ms in the past | `now_ms − first_ts ≥ 400` ([metrics.py:68, 74](../shared/kazus_logic/liquidity/realtime/metrics.py#L68)) | Implementation constant L0; **anti-spoof primitive** per [lip-metric-registry §A.1](lip-metric-registry.md) |

A level that flickered in and out within 400 ms **never contributes** to credible_depth. A level that survived 400 ms is treated as having earned a place in the survivorship count for this snapshot. The metric is therefore a **coarse survivorship heuristic over the top-of-book quoting process** at the sampler's ~1 Hz cadence.

### 3.2 What it does NOT establish

| Claim outside the metric's mandate | Reason |
|---|---|
| "This is the real liquidity available" | The metric measures *visible* survivorship; off-tape (hidden / iceberg / RPI / OTC) liquidity is structurally unobservable per [lip-execution-validation §10](lip-execution-validation.md) |
| "Sub-400ms spoof patterns detected" | **Explicit limitation.** Below the persistence floor, the metric cannot distinguish a genuine short-lived quote from a spoof |
| "The book is honest / dishonest" | Per [lip-ontology-boundaries §12.1](lip-ontology-boundaries.md), "honest" / "dishonest" are out of vocabulary |
| "A specific actor is providing this liquidity" | Identity unification is structurally unknowable per [lip-execution-validation §24](lip-execution-validation.md) |

### 3.3 Sub-second spoof — explicit limitation

The sampler cadence is approximately 1 Hz. The persistence floor is 400 ms. Below the floor:

- A quote may appear and vanish multiple times within a single snapshot interval, leaving no trace in `credible_depth`.
- The metric reports the **net surviving** state, not the per-flicker count.
- A consumer reading `credible_depth ≈ 0` cannot distinguish "genuinely thin book" from "book full of sub-400ms flicker". The two states **must be disambiguated by cross-referencing the raw `depth` field**; the platform does not auto-flag this.

Per [freeze §13 line 1076](2026-05-23-architecture-freeze.md): *"Spoof saturation regimes. When > 50% of displayed depth is sub-400ms quote flicker, Credible Depth does its job correctly (reports near-zero) but the operator-visible field is the same as a genuinely-empty book. The two states are distinguishable only by cross-referencing the raw book — no automated flag is emitted."*

### 3.4 Calibration status

| Anchor | Value | Class |
|---|---|---|
| `CREDIBLE_BAND_PCT` | 0.005 (50 bps) | L0 |
| `CREDIBLE_MIN_AGE_MS` | 400 ms | L0 — anti-spoof primitive per A.1 validation constraints |

Lowering the persistence floor weakens the metric's core property; raising it makes the metric blind to near-touch state that lived shorter than the floor. Any change must be paired with a recalibration against persistence-labelled samples — not currently measured.

---

## 4. Impact Mismatch (Execution Validation)

### 4.1 Where the metric lives

`exec_impact` is fully documented in [`lip-execution-validation.md`](lip-execution-validation.md). This section is the **liquidity-quality framing** of that layer's emit — it does not redefine the contract.

### 4.2 What the layer publishes for liquidity quality

| Emit field | Meaning at the liquidity-quality tier |
|---|---|
| `expected_bps` | Book-walk impact implied by the pre-burst visible top-20 |
| `realized_bps` | Actual mid-price displacement at `t + SETTLE_MS = 500ms` |
| `divergence_bps = realized − expected` | **The measurement** — how the visible book's prediction compared to the realized mid move |
| `ratio = realized / expected` (when `|expected| ≥ 0.5 bps`) | Bounded comparison; below the floor, ratio is suppressed to None to avoid noise amplification |
| `book_exhausted = True` | The visible top-20 was insufficient for the burst's notional. `expected_bps`, `divergence_bps`, `ratio` are all None in this case |

### 4.3 "Discount on suspicious-looking states" — what is implemented

The platform does **not** apply a hidden multiplier to discount suspicious-looking outputs. Instead, it surfaces explicit signals that a consumer must use:

| Signal | Operational interpretation |
|---|---|
| `book_exhausted = True` | The visible book was not sufficient to fill the burst as predicted. **Per [lip-execution-validation §7](lip-execution-validation.md)**: this is a measurement-mode flag, not a verdict. Frequent exhaustion = the visible-tier window does not size to typical burst notional for this symbol — not a manipulation claim |
| `expected_bps < EXPECTED_FLOOR_BPS = 0.5` | `ratio = None`. Below the floor the denominator is in the noise band of book-walk arithmetic ([lip-execution-validation §6](lip-execution-validation.md)) |
| Persistent `realized ≫ expected` with `book_exhausted = False` | **Would be** empirical evidence against the credible-depth anti-spoof claim. Aggregating this relationship is in the validation backlog ([lip-execution-validation §16](lip-execution-validation.md)); not currently emitted as a runtime signal |

### 4.4 What "suspicious-looking liquidity" means in this layer

A large positive `divergence_bps` (realized move exceeded book-walk prediction) does NOT mean the liquidity was synthetic. It means the visible book did not have room for the burst as predicted, **and the layer measures that mismatch.** Causes are not attributed:

- Cross-venue arbitrage absorbing the move on this side.
- MM cancellations between pre-snapshot and the trade.
- Hidden / iceberg / RPI fills (unobservable).
- Mid recalculation across the burst window.
- Measurement noise.

Per [lip-execution-validation §6](lip-execution-validation.md): *"The layer measures the mismatch; it does not name the cause."*

---

## 5. Liquidation handling — separation by construction

### 5.1 Schema-level isolation

The runtime maintains **two independent tapes** in `SymbolState`:

- `state.trades` — voluntary trade prints (from `aggTrade`).
- `state.liquidations` — forced-liquidation prints (from `@forceOrder`).

Per [`orderbook.py:14`](../shared/kazus_logic/liquidity/realtime/orderbook.py#L14): *"Trade tape and liquidation tape are bounded deques retaining only the last `_TAPE_WINDOW_MS = 5 min`."*

Per [`orderbook.py:48-49`](../shared/kazus_logic/liquidity/realtime/orderbook.py#L48): per-liquidation side semantics are explicit:
- `"BUY"` → short was liquidated (forced buy)
- `"SELL"` → long was liquidated (forced sell)

**Voluntary trades and forced liquidations never share a tape.** This is the structural guarantee that voluntary liquidity metrics are not contaminated by forced flow.

### 5.2 Liquidation Stress metric (`liq_stress`)

| Property | Value |
|---|---|
| Definition | Total USD-value of forced liquidations over the last `LIQ_WINDOW_MS` ([metrics.py:80-87](../shared/kazus_logic/liquidity/realtime/metrics.py#L80)) |
| Source | `state.liquidations` only |
| Spike threshold | `LIQ_SPIKE_USD = 50_000` ([intelligence.py:42](../shared/kazus_logic/liquidity/realtime/intelligence.py#L42)) — single-liquidation USD threshold for spike-event detection |

### 5.3 Current upstream status

Per [`engine.py:15-21`](../shared/kazus_logic/liquidity/realtime/engine.py#L15) and [`__init__.py:54-58`](../shared/kazus_logic/liquidity/__init__.py#L54): Binance Futures `@forceOrder` is **silently disabled** on this network. The SUBSCRIBE is accepted; zero frames are delivered.

| Consequence | Behavior |
|---|---|
| `state.liquidations` tape | Stays empty |
| `liq_stress` | Returns 0 / None across all symbols |
| Liquidation-spike alerts | Never fire |
| `liq_cascade` probe in `crisis_genesis` | Reports `INSUFFICIENT` data quality and removes itself from the composite per [freeze §14](2026-05-23-architecture-freeze.md) |

**Contamination prevention is structural** — separate tapes mean voluntary metrics are unaffected by liquidation flow, in either direction. The current zero-frame status of the liquidation feed does not weaken this isolation; it only means one channel is silent.

### 5.4 What the layer does NOT do

- Does not classify a high `liq_stress` reading as a "cascade in progress" — that is a separate composite verdict in `crisis_genesis` ([lip-epistemic-boundaries §4](lip-epistemic-boundaries.md)).
- Does not infer the direction of the broader market from the side of liquidations. Side semantics are recorded; directional interpretation is operator-tier.
- Does not contaminate `divergence_bps` or `credible_depth` with liquidation prints — these read `state.trades` only ([exec_impact.py:155-204](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L155)).

---

## 6. Cross-venue divergence

### 6.1 What `crossex` measures

Per [lip-metric-registry §A.7](lip-metric-registry.md): pairwise divergence between this venue and the reference venue (Binance). Bybit is the comparison venue. Snapshots persisted to `liquidity_crossex_history` with 90-day retention.

`crossex` is **on-demand** (per `/crossex/{symbol}` request), not a streaming metric. Time-series persistence exists, but "sustained divergence" as a runtime gate is **not formalized** per A.7 validation constraints.

### 6.2 Divergence as fragility context — current wiring

**The platform does NOT wire `crossex` into `fragility_score` or any intelligence-tier composite today.** Verified by code audit:

- `fragility_score` composes from Kyle-λ buckets only ([intelligence.py:279-294](../shared/kazus_logic/liquidity/realtime/intelligence.py#L279)).
- `intelligence.py` and `metrics.py` contain **zero references to `crossex` or `cross_venue`**.

Per [freeze §14.8](2026-05-23-architecture-freeze.md): *"Cross-venue confirmation — NOT INTEGRATED into the verdict. Cross-venue divergence exists as a separate API surface but is not read by `crisis_genesis`. ... The TZ-proposed cross_venue enum (CONFIRMED / LOCAL_ONLY / CONTRADICTED / UNAVAILABLE / INSUFFICIENT) is NOT IMPLEMENTED."*

### 6.3 Operational consequence

Cross-venue divergence is currently a **read-only diagnostic surface**, available to operators via `/crossex/{symbol}` and the `crossex` UI panel. It is not consumed by:

- `fragility_score`
- `crisis_genesis`
- `propagation_graph`
- alert engine
- adaptation modifiers

**Per [lip-venue-quality §17](lip-venue-quality.md)**: cross-venue timestamp equality is structurally unavailable. Comparisons are approximate observational comparisons, not deterministic temporal truth.

### 6.4 Avoiding venue-truth semantics

Per [lip-venue-quality §1](lip-venue-quality.md) and [lip-ontology-boundaries §12.1](lip-ontology-boundaries.md):

| Banned framing | Approved framing |
|---|---|
| "Bybit is wrong" / "Binance is right" | "venues disagree by X bps over the window" |
| "Trusted venue" / "untrustworthy venue" | "venue with OBSERVABLE state" / "venue with REPLAY_UNAVAILABLE state" |
| "Manipulation suspected" | "cross-venue divergence persistence above operator-set threshold" — and only as raw observation, not a verdict |
| "True price" | (rejected — no truth claim) |

A sustained divergence is a **fragility context indicator at the operator tier** — it tells the operator "one of these venues is showing different behavior, investigate further" — and that is the limit of the platform's mandate.

---

## 7. Resiliency / Recovery / Refill

### 7.1 The post-impact recovery model

When a stress event triggers ([intelligence.py:RECOVERY_FRACTION = 0.80](../shared/kazus_logic/liquidity/realtime/intelligence.py#L39)):

1. **Pre-event depth** is captured as the `credible_depth` baseline at the moment of trigger.
2. The system **watches subsequent ticks** until depth climbs back to ≥ 80% of pre-event.
3. At that moment, two values are stamped on the event:
   - `recovery_time_ms` — elapsed milliseconds from trigger to ≥ 80% recovery.
   - `refill_velocity` — `(post_depth − pre_depth) / elapsed_seconds`. USD-per-second of depth replenishment.

If recovery never happens within the event window, both fields stay None — UNKNOWN propagates.

### 7.2 Resiliency score

Per [lip-metric-registry §A.2](lip-metric-registry.md):

```
resiliency_score = blend of
   - presence/absence of recovery event in window
   - recovery_time_ms vs 30s exp-decay anchor
   - refill_velocity quantile
```

Constants `RECOVERY_FRACTION = 0.80` and the `30s exp-decay anchor` are **interim** per A.2 validation constraints (not calibrated against labelled regimes).

### 7.3 UNKNOWN when depth insufficient

| Condition | Output |
|---|---|
| No recovery event in window | `recovery_time_ms = None`, `refill_velocity = None` |
| `credible_depth` at trigger time invalid | Event not opened; no recovery tracking begins |
| Depth recovers but slowly (no ≥ 80% reached in window) | Event remains open; fields stay None until eviction by `_TAPE_WINDOW_MS = 5min` |
| Multiple consecutive events | Each tracked independently; no merge into a "super-event" |

Per the refusal-first invariant in [lip-ingestion-contract §4.8](lip-ingestion-contract.md): absence propagates. The layer does not fabricate a recovery time when recovery did not occur.

### 7.4 What the metrics do NOT claim

- They do not predict whether the next event will recover.
- They do not infer market-wide systemic state from one symbol's slow recovery.
- They do not assign cause to slow recovery (could be MM withdrawal, cross-venue arb, off-tape participation, or measurement noise).

---

## 8. UNKNOWN propagation discipline (cross-cutting)

Every metric in this layer has explicit None-return paths when inputs are missing or invalid. Audit findings ([intelligence.py](../shared/kazus_logic/liquidity/realtime/intelligence.py), [metrics.py](../shared/kazus_logic/liquidity/realtime/metrics.py)):

| Metric | Returns `None` when |
|---|---|
| `credible_depth_usd` | mid invalid; no levels in band; no levels past persistence floor |
| `kyle_lambda` | no buckets ([intelligence.py:265](../shared/kazus_logic/liquidity/realtime/intelligence.py#L265)) |
| `impact_score` | `kyle_lambda` is None |
| `fragility_score` | insufficient buckets ([intelligence.py:282](../shared/kazus_logic/liquidity/realtime/intelligence.py#L282)) |
| `recovery_time_ms` | no recovery event |
| `refill_velocity_usd_per_s` | no recovery event ([intelligence.py:190](../shared/kazus_logic/liquidity/realtime/intelligence.py#L190)) |
| `liq_stress` | no liquidations in window OR (currently) `@forceOrder` upstream-disabled |

**No bounded fallback substitution** in this layer beyond the documented exec_impact / regime-engine cases. UNKNOWN at the input level produces UNKNOWN at the metric level.

---

## 9. Banned vocabulary (this layer)

| Banned | Reason | Approved replacement |
|---|---|---|
| "pressure" (as standing noun describing market state) | Anthropomorphic; implies a force the layer does not measure | "asymmetric depth condition" / "imbalance" / cite the specific metric value |
| "spring" / "coil" / "loaded" | Predictive framing forbidden across the stack | (rejected) |
| "signal" (as standing claim of actionable meaning) | Ambiguous; carries trading-system semantics | "metric value" / "observation" / cite the emit name |
| "pump setup" / "pump regime" | Directional + predictive | (rejected) |
| "smart money intent" / "smart money active" | Intent inference forbidden per [lip-governance §3 row 9](lip-governance.md) | (rejected) |
| "manipulation verdict" / "manipulation detected" | Cannot be inferred from visible data per [lip-execution-validation §10](lip-execution-validation.md), [lip-epistemic-boundaries §2](lip-epistemic-boundaries.md) | (rejected) |
| "real liquidity" / "synthetic liquidity" | Truth claims forbidden per [lip-ontology-boundaries §12.1](lip-ontology-boundaries.md) | "credible_depth value" / "visible top-of-book survivorship" |
| "fake depth" / "fake book" | Same | "book_exhausted = True for this burst" / "near-touch state below 400ms persistence floor" |
| "absorbed by hidden actors" | Hidden actors structurally unknowable | "realized impact exceeded book-walk prediction; cause not attributable" |

### 9.1 Approved liquidity-quality vocabulary

- **asymmetric liquidity conditions** — bid/ask depth or recovery time differs by side
- **structural irregularity** — pattern of repeated exhaustion / persistent divergence above threshold across the window
- **survivorship** — fraction of visible quotes that meet the 400ms persistence gate
- **observable imbalance** — bid_depth vs ask_depth ratio outside operator-set band
- **divergence** — `realized_bps − expected_bps` per burst
- **exhaustion frequency** — count of `book_exhausted = True` per (side, bucket) per window
- **refill velocity** — measured USD/sec depth replenishment after stress event
- **cross-venue disagreement** — `crossex.divergence_pct` above operator-set threshold over operator-set duration

All approved terms carry a measurable referent. None is anthropomorphic. None claims market truth.

---

## 10. Governance & maturity

| Aspect | Status |
|---|---|
| **This document** | Class A (documentation-only). Authorized during Observation Period |
| **All liquidity-quality metric thresholds** | L0 (Implementation constant) — `CREDIBLE_BAND_PCT`, `CREDIBLE_MIN_AGE_MS`, `RECOVERY_FRACTION`, `LIQ_SPIKE_USD`, `_TAPE_WINDOW_MS` etc. None empirically calibrated |
| **Wiring `crossex` into `fragility_score`** | Class B (semantic change in fragility composition). NOT AUTHORIZED during Observation Period. Per freeze §14.8: TZ proposal, not implemented |
| **Adding spoof-saturation auto-flag** | Class B (new emit field). NOT AUTHORIZED during Observation Period. The two states (genuinely-empty book vs flicker-saturated book) remain distinguishable only by cross-referencing raw `depth` field today |
| **Adding cross-venue confirmation enum to crisis_genesis** | Class B + Class E (per freeze §14.8 TZ proposal). NOT AUTHORIZED |
| **Maturity of this stack** | Operational. Has been running continuously for 11+ days. Promotion to Validated-operational gated on calibration-version stamping (platform-wide gap per [lip-governance §8](lip-governance.md)) + completion of validation backlog in [lip-validation-and-calibration.md](lip-validation-and-calibration.md) |

---

## 11. Cross-cutting invariants

| Invariant | Source |
|---|---|
| **Credible Depth is a coarse survivorship heuristic — not a claim about "real" liquidity** | §3 |
| **Sub-400ms behavior is below the metric's resolution; cannot be inferred** | §3.3 |
| **Voluntary trade flow and forced-liquidation flow occupy separate tapes by construction** | §5.1 |
| **A high `divergence_bps` measures mismatch between visible-book prediction and realized mid move; it does not attribute cause** | §4.4 + [lip-execution-validation §6](lip-execution-validation.md) |
| **Cross-venue divergence is a read-only diagnostic; not wired into runtime fragility verdicts today** | §6.2–§6.3 |
| **UNKNOWN propagates — absence of recovery does not become a fabricated recovery time** | §7.3 + §8 |
| **All liquidity-quality metrics inherit the platform's blind-spot inventory** (hidden / iceberg / RPI / OTC / off-tape) | [lip-execution-validation §10 / §24](lip-execution-validation.md), [lip-epistemic-boundaries §2 / §5](lip-epistemic-boundaries.md) |

---

## 12. What this document is not

- Not a new layer specification — every metric here is already implemented.
- Not a manipulation detector.
- Not a wash-trading detector.
- Not a synthetic-liquidity classifier.
- Not a venue-truth oracle.
- Not a recommendation about which venue to trust.
- Not a predictive engine.
- Not authorization to wire `crossex` into `fragility_score` or to add spoof-saturation auto-flags.
- Not authorization to change any threshold constant during the Operational Observation Period.

It is a Class A hardening companion that frames the already-implemented liquidity-quality measurement stack around survivorship semantics, contamination-prevention by tape isolation, cross-venue-as-diagnostic-only, refusal-first UNKNOWN propagation, and the banned-vocabulary discipline that keeps the operator surface free of pressure / spring / signal / smart-money / manipulation framings.
