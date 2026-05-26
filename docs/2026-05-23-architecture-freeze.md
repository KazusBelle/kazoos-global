# Architecture Freeze — kazus-global Liquidity Intelligence Platform

**Snapshot date:** 2026-05-23
**Status:** stable-core / experimental split documented below
**Scope:** complete system audit for handoff, ops recovery, and continued development without losing architectural intent

This document is meant to be read **cold**, by someone who has never seen the codebase, and to be sufficient for them to (a) operate the system, (b) extend it without breaking it, or (c) recover it from scratch.

---

## Terminology

Layer names in this document describe **what the layer measures**, not metaphors. The 2026-05-26 documentation passes renamed several layer labels to remove storytelling and AI-cognition flavor; engine code identifiers (function names, table names, endpoint paths, enum values, table column names) stay frozen for stability and are listed verbatim where they appear.

| documentation label | engine code identifier (frozen) | what it measures |
|---|---|---|
| **Distributed Stress Detection** | `crisis_genesis(db, lookback_days)` · endpoint `/research/crisis-genesis` · operator-priority source key `genesis` | 7-probe composite; emits a discrete verdict ∈ {CALM, EARLY_DISTORTION, ELEVATED_RISK, PRE_CASCADE, INSUFFICIENT} from measurable conditions |
| **Event Chain Reconstruction** | `narrative_causality(db, lookback_days)` · endpoint `/research/narrative-causality` · field `narrative_confidence_modifier` | deterministic 5-section template that composes outputs from causal/structural/transition/stress layers into a fixed-form summary with explicit per-section confidence and a mandatory "what we don't know" section. No model calls, no interpretation of intent |
| **Causal Inference Layer** | Layer 8 functions `causal_propagation` · `structural_dependencies` · `market_state_transitions` · `crisis_genesis` · `narrative_causality` (formerly headed "Causal Intelligence") | verdict-emitting functions over already-measured propagation/transition data |
| **Regime Transition Engine** | `market_state_transitions(db, lookback_days)` · DISC panel previously labeled "State Transition Intelligence" | per-transition verdict (PERSISTENT · ACCELERATING · FLICKER · REVERSED) with measurable persistence + acceleration + reversal flags |
| **Replay Reconstruction Engine** | Layer 12 (formerly "Replay Intelligence") · `investigation_replay_*` in `research.py` · endpoints under `…/replay/*` | as-of reconstruction of layer outputs from history tables; per-surface `data_quality` ∈ HIGH/PARTIAL/INSUFFICIENT/PRUNED is mandatory |
| **Anomaly Memory & Edge Graph** | tables `liquidity_anomaly_memory` + `liquidity_anomaly_edges` · readers `anomaly_lineage` · `crisis_evolution_tree` · `regime_ancestry` · `edge_lineage` · `narrative_chronicle` (legacy code names; surface labels prefer "edge trace" / "stress evolution" / "stress clusters" / "event-chain timeline") | persistent record of structural anomalies + typed edges with a measurable basis per edge |

### What the platform does and does not do

The platform **measures · validates · reconstructs · aggregates · compares · suppresses · emits · tracks · rejects · scores**. It does **not** understand the market, see structure, interpret intent, narrate causality, or assert hidden actors.

### Epistemic boundaries (load-bearing)

These phrases appear throughout the document with their literal meaning — they are not hedges, they are output states the engine actually emits:

| phrase | meaning |
|---|---|
| **INSUFFICIENT** / **UNDER_EVIDENCED** | the layer refuses to commit a verdict; never silently substituted with a default |
| **structurally unknowable** | the input data does not carry the property at all (e.g. `propagation_graph` aggregates over the window, so per-frame transmission order is not derivable from it) |
| **no measurable basis** | a candidate edge / link / verdict was rejected because no probe could attach a number to it |
| **replay unavailable before activation** | a layer that was deployed at time T cannot reconstruct itself at any time < T |
| **causality not asserted** | a directional lead-lag pattern exists in the data, but the layer publishes it as a candidate verdict, not a causal claim |

Every layer answers three questions: *what is measured · how it is measured · when the operator is allowed to see it*.

---

## Contents

1. [Architecture map — layer by layer](#1-architecture-map)
2. [Data flow & dependency trace](#2-data-flow--dependency-trace)
3. [Formula registry](#3-formula-registry)
4. [Endpoint inventory](#4-endpoint-inventory)
5. [Table inventory](#5-table-inventory)
6. [Operator workflow guide](#6-operator-workflow-guide)
7. [Known risks & backlog](#7-known-risks--backlog)
8. [Architecture freeze — stable core vs experimental](#8-architecture-freeze)
9. [Quantitative metric registry — realtime tier](#9-quantitative-metric-registry--realtime-tier)
10. [Failure modes & observability limits](#10-failure-modes--observability-limits)
11. [Non-inference boundaries](#11-non-inference-boundaries)
12. [Validation framework — calibration backlog](#12-validation-framework--calibration-backlog)
13. [Propagation & causality limits](#13-propagation--causality-limits)
14. [Distributed Stress — quantitative state machine](#14-distributed-stress--quantitative-state-machine)
15. [Phrase compression reference](#15-phrase-compression-reference)

---

## 1. Architecture map

The system is composed of **ten layers**, each with a clear role. Layers run inside three deployment units:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  worker container (single process, async)                               │
│    LIQ scanner  ·  Realtime WS  ·  Alert engine  ·  Intel snapshotter   │
│    Anomaly recorder  ·  Hourly prune                                    │
└─────────────────────────────────────────────────────────────────────────┘
              │ writes
              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Postgres (single instance)   — 22 tables                               │
└─────────────────────────────────────────────────────────────────────────┘
              │ reads
              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  backend container (FastAPI + SQLAlchemy)                               │
│    84 endpoints under /api/liquidity, TTL-cached research aggregators  │
└─────────────────────────────────────────────────────────────────────────┘
              │ HTTP
              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  frontend container (nginx + vite-built React)                          │
│    DISC · Coordination · Strategy · Operations · Meta · Memory · …      │
└─────────────────────────────────────────────────────────────────────────┘
```

### Layer 1 — LIQ Scanner (REST poller)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/poller.py`](shared/kazus_logic/liquidity/poller.py) |
| Purpose | REST polling of Binance / CoinGecko / Bybit; computes per-symbol per-metric values; bulk-inserts into `liquidity_samples` |
| Inputs | Exchange REST APIs |
| Outputs | `liquidity_samples` rows (1M/day at 104 symbols × 14 metrics) |
| Cadence | `POLL_INTERVAL_S = 60` (1 cycle per minute) |
| Owns prune | `liquidity_samples` 35d retention (hourly), plus research-table prune cycle (see [Layer 10](#layer-10--worker--retention-loop)) |
| Limitations | Single-process; REST budget capped by exchange rate limits; metrics added by appending row, never migrating |

### Layer 2 — Realtime WS Engine

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/engine.py`](shared/kazus_logic/liquidity/realtime/engine.py), [`shared/kazus_logic/liquidity/realtime/ws_client.py`](shared/kazus_logic/liquidity/realtime/ws_client.py) |
| Purpose | 1 Hz WS sampler; per-symbol orderbook + trades; emits derived metrics; writes to `liquidity_samples` and updates `liquidity_ws_status` |
| Inputs | Binance WS streams, multiplexed via `conn_id` reconnect detection |
| Outputs | High-frequency `liquidity_samples` rows + `liquidity_ws_status` row |
| Cadence | `SAMPLE_INTERVAL_S=1.0`, `FLUSH_INTERVAL_S=5.0`, `STATUS_INTERVAL_S=3.0`, `RECONCILE_INTERVAL_S=5` |
| Resilience | Exp-backoff 1s→30s + jitter; ping/pong 30s/10s; consumer re-issues SUBSCRIBE on conn_id bump |
| Limitations | Single exchange (Binance) for realtime; symbols limited by WS subscription count |

### Layer 3 — Alert Engine

| | |
|---|---|
| Code | [`worker/app/runner.py`](worker/app/runner.py) (alert states, events, history writes) |
| Purpose | Detects alert conditions per symbol/timeframe; tracks state machine; writes durable history |
| Inputs | `liquidity_samples`, `snapshots`, computed thresholds |
| Outputs | `alert_states` (current), `alert_events` (top-100 ring), `liquidity_alert_history` (durable) |
| Cadence | M5-boundary aligned; full re-check at startup + when missed tick > 7 min |
| Validation | Frontend writes `validated_outcome` (followed_through / noise) back to `liquidity_alert_history` after 12s persistence threshold |
| Limitations | Historical re-validation requires manual repair pass (see [docs/runbooks](docs/) if added) |

### Layer 4 — Research Aggregators (read-only)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/research.py`](shared/kazus_logic/liquidity/research.py) (≈ 7800 lines) |
| Purpose | All derived computations: per-symbol per-metric stats, drift, similarity, venue quality, regime stats, interactions, edge ranking, signal reliability, transition forecast, risk state, structural breaks, … |
| Inputs | `liquidity_samples`, `liquidity_alert_history`, `liquidity_intelligence_history`, `liquidity_anomaly_memory` |
| Outputs | JSON dicts → API |
| Caching | `_ttl_cached(seconds)` decorator wraps heavy functions (typ. 300s TTL) |
| Limitations | All functions are pure read; no derived storage tier; full scan over windows up to 30 days |

### Layer 5 — Operations / Strategy / Meta / Coordination

These are read-only aggregators that **compose** outputs from the lower layers into fixed-schema summaries. No new measurements; every value is derivable from inputs already published by Layers 1–4. All in `research.py`.

| function | role | TTL |
|---|---|---|
| `risk_state(db)` | composite risk dial across symbols | n/a |
| `regime_shift_warning(db)` | per-state shift probability with ranking | n/a |
| `structural_breaks(db, window_days)` | structural break score over window | n/a |
| `meta_confidence(db, since_ms)` | engine's own meta-confidence | n/a |
| `meta_intelligence_health(db)` | health-state classifier | n/a |
| `strategic_state(db)` | strategic state label + rationale | n/a |
| `intelligence_synthesis(db)` | **composite** — synthesizes 6 layers above | 300s |
| `multi_horizon(db)` | multi-horizon outlook | 300s |
| `evolutionary_behavior(db, lookback_days, bucket_days)` | long-horizon drift in engine behavior | 300s |
| `intelligence_evolution_forecast(db, horizon_days)` | OLS extrapolation of intel metrics with hardened confidence | 300s |
| `adaptation_recommendations(db)` | per-metric importance shift suggestions | — |
| `memory_abstraction(db)` | compressed archetype view of anomaly memory | — |

### Layer 6 — Anomaly Memory & Edge Graph

| | |
|---|---|
| Tables | `liquidity_anomaly_memory` + `liquidity_anomaly_edges` |
| Purpose | Persistent record of structural anomalies + typed edges between them. Edge kinds are an enum, not a free-text annotation: `caused_by` / `evolved_into` / `historically_similar` / `preceded` / `destabilized` / `stabilized`. Every edge carries a measurable basis (lag window, similarity score, or co-occurrence count) and is never written without one |
| Writer | Worker `anomaly_recorder` task (300s cadence) |
| Readers | `anomaly_lineage`, `memory_graph`, `crisis_evolution_tree`, `regime_ancestry`, `edge_lineage`, `crisis_clusters`, `narrative_chronicle` (legacy code identifiers; surface labels prefer "stress evolution" / "stress clusters" / "event-chain timeline") |
| Retention | 180d (Pass-A retention layer) |

### Layer 7 — Discovery (data-driven mining)

| function | finds | TTL |
|---|---|---|
| `discover_patterns(db, since_ms, min_support, bucket_minutes)` | Recurring (metric-tertile) signatures → downstream alert rates; emits `effective_lift`, `stability_score`, `pattern_confidence`, `robustness_flags`, `suppressed_reason` | 300s |
| `crisis_archetypes(db, max_archetypes)` | Anomaly-memory clusters → archetype labels | — |
| `hidden_regimes(db, lookback_days, max_clusters)` | Clusters in engine-state space (intelligence_history) | 300s |
| `propagation_graph(db, lookback_days, lead_window_ms, min_lead_ms)` | Symbol→symbol pair edges with `confidence_score` decomposition + `integrity_components`; returns `all_symmetric_pairs` for sanity loop check | 300s |

### Layer 8 — Causal Inference Layer (Phase 15)

| function | does | TTL |
|---|---|---|
| `causal_propagation(db, lookback_days, n_windows)` | Per-pair verdict over 4 tests: asymmetry · multi-window persistence · common-driver elimination · scarcity gate. Verdicts: DIRECTIONAL · COMMON_DRIVEN · COINCIDENCE · UNDER_EVIDENCED · AMBIGUOUS · EXPLORATORY | 300s |
| `structural_dependencies(db, lookback_days)` | Composes causal verdicts into 4 structural findings: influence chains · dominant drivers · co-driver clusters · synchronized stress groups | 300s |
| `market_state_transitions(db, lookback_days)` | Per-transition verdict + lifecycle: PERSISTENT · ACCELERATING · FLICKER · REVERSED; aggregates: flicker_ratio, oscillation_periods, transition_rate | 300s |
| `crisis_genesis(db, lookback_days)` — **Distributed Stress Detection** | 7-probe composite over a `lookback_days` window: fragmentation_growth · resiliency_decay · propagation_widening · dependency_concentration · anomaly_synchronization · transition_instability · stress_acceleration. Each probe emits an independent [0,100] score with its own scarcity gate; composite = mean over *contributing* probes. Verdict thresholds: PRE_CASCADE (score ≥ 75 AND hot_count ≥ 3) · ELEVATED_RISK (≥ 50) · EARLY_DISTORTION (≥ 25) · CALM (< 25) · INSUFFICIENT (no probes contributing). Scarcity cap: verdict floored at EARLY_DISTORTION when > 3 probes are INSUFFICIENT. No forecast claim — every output is "current measured state across the window". | 120s |
| `narrative_causality(db, lookback_days)` — **Event Chain Reconstruction** | Deterministic 5-section template that composes already-validated outputs from causal/structural/transition/stress layers into a fixed-form summary. No model calls. Per-section confidence is taken from upstream layer confidence — never invented. Mandatory "what we don't know" section enumerates layers that returned INSUFFICIENT/UNDER_EVIDENCED. The template is the contract: if a section's inputs are absent, the section reports absence rather than filling silence. | 120s |

### Layer 9 — Feedback & Adaptation (Phase 16)

| | |
|---|---|
| Code | `adaptation_state(db, lookback_days)` in `research.py` |
| Purpose | Computes 5 bounded modifier coefficients with audit trail. Acyclic — reads but never writes back to observed layers |
| Modifiers | `narrative_confidence_modifier` [0.5,1.0] · `alert_sensitivity_modifier` [1.0,1.5] · `causal_strictness_modifier` [1.0,1.5] · `discovery_suppression_modifier` [0.5,1.0] · `global_trust_modifier` [0.5,1.0] |
| Real downstream wiring | `adapted_recommendations(db)` wraps `adaptation_recommendations` and applies `discovery_suppression_modifier` to every `importance_shift` |
| TTL | 120s |
| Reversibility | Pure read; turning the loop off = downstream stops reading the modifier |

### Layer 12 — Replay Reconstruction Engine (Phase 19, Pass A backend)

| | |
|---|---|
| Code | `investigation_replay_*` in `research.py` |
| Purpose | Deterministic as-of FROZEN-vs-LIVE reconstruction of an investigation case |
| Tables | `investigation_replay_snapshots` (one row per case, UPSERT) |
| Capture | Auto-fires on `investigation_create` (kind=`auto_create` or `auto_draft`). Operator can recapture via `force=true` |
| State modes | `frozen` reads the opaque JSON snapshot; `live` reconstructs from `liquidity_intelligence_history` + `liquidity_alert_history` + `liquidity_anomaly_memory` + `operator_priority_*` tables. Same response schema, distinguished by `is_frozen` |
| Replay safety | Each reconstructed surface publishes its own `data_quality` ∈ HIGH/PARTIAL/INSUFFICIENT/PRUNED. PRUNED triggers when a window is past retention; the engine refuses to invent a value |
| Diff | Narrow field-level comparison at anchor: stress-detection verdict + score, sanity overall_state, adaptation modifier values (Δ ≥ 0.05), operator queue size + escalation counts, event-chain top section header. Every drift carries before/after/delta. No interpretation — pure value comparison |
| Timeline | Scrubber keyframes — material events (operator_priority_events + alerts + anomalies + case lifecycle) inside a `[anchor − pre, anchor + post]` window |
| Propagation | Frame-bucketed alert-start counts per symbol over the case window + static propagation edges. Per-frame edge transmission is intentionally NOT inferred — propagation_graph doesn't carry timestamped pair data |

**Integrity Repair Pass (2026-05-24)** hardened four invariants under Layer 12 without expanding scope. Documented separately in [`docs/2026-05-24-operational-review.md`](docs/2026-05-24-operational-review.md) and addressed by:

* **Frozen snapshot is now APPEND-ONLY.** `investigation_replay_snapshots` has a `revision` (1..N per case) + `is_active` pointer; the old unique-on-investigation_id is gone in favor of unique-on-(investigation_id, revision). Recapture inserts a new revision and flips the prior to `is_active=False`; payloads are never destroyed. New surfaces: `GET .../replay/history`, `GET .../replay/state?revision=N`, `GET .../replay/diff/revisions?from=&to=`.
* **Diff semantics made explicit.** The diff response now always carries `comparison_mode ∈ {frozen_vs_now, frozen_vs_frozen}`; the UI labels the banner accordingly and no longer says "FROZEN vs LIVE". The cursor snapshot remains a separate panel — no fake cursor-diff is computed.
* **Retention-safe evidence linking.** `_investigation_link_evidence_inner` now auto-fetches the upstream row (per `evidence_type` dispatch: alert / anomaly / operator_priority) and stores it in `investigation_evidence.snapshot_json` at link time. `investigation_timeline` falls back to that snapshot when the upstream row has been pruned and tags the event with `is_pruned=True` so the operator sees the gap explicitly. Silent shrinkage of the case timeline is eliminated.
* **Async capture decoupling.** `investigations.capture_status ∈ {PENDING, CAPTURED, FAILED}` queue field. `investigation_create` sets PENDING; a new worker loop `investigation-capture` (30s cadence) drains the queue via `investigation_capture_pending(db)`. Case creation no longer holds the request on the 8-layer aggregation cascade. Failures are recorded (`capture_error`) and the operator can re-queue via `POST .../replay/retry`.

Pass B (Phase 19) lands the operator-facing UI on top of the Pass A endpoints, inside the INV drawer as a new lazy-mounted `replay` tab:

* **FROZEN vs LIVE diff banner** — prominent header that shows the count + per-field deltas (stress-detection verdict, sanity overall_state, adaptation modifier values, queue size + escalation counts, event-chain top section). Color band escalates with drift count. Includes one explicit `recapture` button that is the only frontend write path mutating the frozen reference.
* **Scrubber** — SVG strip with click-to-seek, play/pause loop (`requestAnimationFrame` × speed factor; 1s wall ≈ 1m case time at speed=1, capped at window_end), step ±keyframe, prev/next critical-keyframe, jump-to-anchor, speed selector (0.5×–16×).
* **Overlay toggles** — operator_priority / alert / anomaly / case sources can each be hidden from the keyframe strip; severity-colored ticks (info/warn/critical).
* **Cursor snapshot** — toggle between live-reconstruction at cursor (debounced 250 ms refetch on cursor settle) and the frozen blob; live view surfaces per-section `data_quality` (HIGH/PARTIAL/INSUFFICIENT/PRUNED) explicitly.
* **State evolution mini-charts** — keyframe density + alert activity per bucket, derived from already-fetched timeline + propagation data. Vanilla SVG sparklines with a synchronized cursor line. No interpolation, no smoothing.
* **Alert counts at cursor** (initial name: "propagation playback" — renamed in the 2026-05-24 Attention Pass, see below) — frame-bucketed per-symbol activation bars (current frame indexed by cursor); historical lead-lag edges from `propagation_graph` rendered as a static list, NOT animated per frame. `propagation_graph` edges are aggregated over the full lookback window, not timestamped, so per-frame transmission order is structurally unknowable from the data.

UX discipline: no cinematic effects, no glow, no auto-camera, no inferred transmission order. The only moving element is the scrubber cursor. Every series and every overlay is sourced from already-fetched real data; missing surfaces stay missing (`data_quality` flagged HIGH/PARTIAL/INSUFFICIENT/PRUNED) rather than being interpolated.

Performance: lazy-mounted (no fetches until the operator opens the `replay` tab); a single round-trip on mount (state + timeline + diff + propagation in parallel) plus debounced cursor-position fetches.

### Attention & Trust Simplification Pass (2026-05-24, presentation-only)

After the Integrity Repair Pass closed the four HIGH-severity findings from the operational review, a follow-up review (`docs/2026-05-24-stability-review.md`) identified six presentation-layer HIGH findings around operator fatigue, signal-vocabulary collisions, and trust semantics. Closed in a single presentation-layer pass — no new layer, no new endpoint, no formula change:

* **Chronic vs new severity differentiation.** The sanity banner and operator-queue rows now classify each item into an attention bucket (`fresh / escalating / calming / stable / resolved`) derived from the existing `trend` and `lifecycle` fields. Persistent / chronic items render with muted color and lower visual energy; only fresh and escalating items get full saturation. The "persistent CRITICAL becomes wallpaper" failure mode is structurally addressed.
* **Action-tier / diagnostic-tier separation on DISC.** The page splits into three visual blocks: action surfaces (Operator Queue · Sanity · Adaptation · Distributed Stress · Event Chain), an expandable "diagnostic context" accordion (Pattern Discovery · Propagation · Causal · Structural · Transitions), and an expandable "research drill-down" accordion (Archetypes · Hidden Regimes · Evolutionary · Memory · Forecast · Adaptation Recs). Nothing removed; defaults collapse the cold panels so they stop polling and stop competing for attention.
* **Severity-vocabulary disambiguation.** Sanity findings are now prefixed with `integrity:` in the UI. Investigation severity is presented as "priority" (`investigationSeverityLabel`); alert severity keeps the canonical bare word. API contracts unchanged.
* **Softer verdict / role wording.** `PRE_CASCADE` → "pre-cascade conditions present"; `DIRECTIONAL` → "directional pattern (lead-lag)"; `dominant_driver` → "candidate driver"; `AMPLIFIER` → "appears in chains"; `LEADER` → "appears as leader (candidate)". Every label remap lives in `frontend/src/lib/labels.ts`; raw enum values stay in the API.
* **Replay propagation animation removed.** The per-symbol bars no longer animate (`transition` removed; label changed from "propagation playback" → "alert counts at cursor"). Animation implied per-frame causal transmission that `propagation_graph` does not carry — pair edges are aggregated over the full lookback window, not timestamped. Operator scrubs manually and reads a static per-bucket count.
* **Calmer chrome.** Action-tier panels keep saturation; persistent / chronic items render at ~60% opacity with neutral borders. Less simultaneous urgency; stronger contrast reserved for new/escalating signals.

The strongest property of this pass is what it *did not* add: no new score, no new modifier, no new queue, no new measurement layer.

### Layer 11 — Investigation & Casework (Phase 18)

| | |
|---|---|
| Code | `investigation_*` functions in `research.py` |
| Purpose | Operator-owned case container. Aggregates evidence + append-only notes + lifecycle history; renders typed-edge graphs from upstream layers, surfaces deterministically-scored similar prior cases, exports markdown |
| Tables | `investigations`, `investigation_evidence`, `investigation_notes`, `investigation_events` |
| Lifecycle | OPEN · INVESTIGATING · MONITORING · RESOLVED · ARCHIVED (RESOLVED requires `resolution_summary`; ARCHIVED is one-way) |
| Auto-draft | Worker opens a draft on `crisis_genesis` verdict=`PRE_CASCADE`, deduped by sorted-contributing-probes fingerprint. Worker loop cadence 300s |
| Evidence types | alert · anomaly · operator_priority · propagation_edge · causal_chain · narrative_section · symbol · transition · dependency_cluster · file |
| Causal tree | Joins linked evidence with `liquidity_anomaly_edges` + `propagation_graph` + `structural_dependencies` + `market_state_transitions`. Every edge carries explicit `kind` + `confidence` from upstream + free-text `rationale`. Not deterministic causality — investigation support |
| Similarity | Deterministic scoring (origin_fingerprint=40, symbol Jaccard×25, op-priority overlap×15, tag overlap×10, severity=5, origin_kind=5). Every reason exposed in `reasons[]`. No ML, no embeddings |
| Export | Stable 8-section markdown: Summary · Resolution · Evidence · Notes · Tree · Timeline · Similar · Audit metadata. Served as JSON + plain `.md` download |
| Multi-operator | `assigned_to` + `collaborators_json` + `last_touched_by/at`. Handoff note logged on assignment change. `@handle` mentions in notes emit `mention` events |
| Properties | Append-only history (notes never edited, events never deleted), explainable, replay-aware (`replay_anchor_ms` + window), scarcity-aware via upstream layers, NOT a trading engine |

### Layer 10 — Operator Layer (Phase 17)

| | |
|---|---|
| Code | `operator_priorities` + `operator_priority_ack` + `operator_escalation_history` + `operator_digest` in `research.py` |
| Purpose | Unified attention queue across all upstream layers; DB-backed lifecycle, durable across restarts |
| Tables | `operator_priority_history`, `operator_priority_events`, `operator_acknowledgements` |
| Priority decomposition | `priority_score = severity_raw × confidence × recency × source_weight` (always exposed in tooltip) |
| Escalation bands | NORMAL < 25 · WATCH < 50 · IMPORTANT < 75 · CRITICAL |
| Lifecycle | NEW · WORSENING · STABILIZING · PERSISTENT · RESOLVED (DB-backed) |
| Operator actions | ACK · MUTE · IGNORE · RESOLVE (NOT trading — pure workflow) |
| Digest | 1h / 6h / 24h windows over `operator_priority_events` |

### Worker / Retention Loop

[`worker/app/runner.py`](worker/app/runner.py) main loop is an M5-boundary scheduler. Spawns 4 background tasks:

- `liquidity-poller` (60s)
- `liquidity-realtime` (1Hz sampler, 5s reconcile/flush, 3s status)
- `liquidity-anomaly-recorder` (300s)
- `liquidity-intel-snapshot` (300s)

Plus an hourly prune cycle that calls `prune_old` (samples, 35d) and `prune_research_tables` (alert_history 90d, intel_history 90d, anomaly_* 180d, crossex 90d, operator_priority_events 90d, operator_acknowledgements 180d, operator_priority_history 90d-for-resolved-only).

---

## 2. Data flow & dependency trace

```
                                Binance REST     Binance WS     CoinGecko    Bybit
                                     │              │               │          │
                                     └──────────────┼───────────────┴──────────┘
                                                    │
                                  ┌────────────────────────────────────┐
                                  │  LIQ scanner (60s)                 │
                                  │  Realtime WS engine (1Hz)          │
                                  └────────────────────────────────────┘
                                                    │
                                                    ▼
                                          liquidity_samples
                                                    │
                            ┌───────────────────────┼─────────────────────────┐
                            ▼                       ▼                         ▼
                       alert engine             metric aggregators       engine-state snapshot
                            │                       │                         │
                            ▼                       ▼                         ▼
                  alert_states / events     research.py functions    liquidity_intelligence_history
                  liquidity_alert_history                                     │
                            │                       │                         │
                            └──────────┬────────────┴──────────┬──────────────┘
                                       ▼                       ▼
                            anomaly_recorder                 discovery layers
                                       │                       (pattern, propagation,
                                       ▼                        archetypes, regimes)
                            liquidity_anomaly_memory                  │
                            liquidity_anomaly_edges                   ▼
                                       │                       Phase-15 causal/structural
                                       └───────────┬────────────────────┬─────┘
                                                   ▼                    ▼
                                          structural_dependencies   market_state_transitions
                                                              │
                                                              ▼
                                          Distributed Stress Detection (7 probes)
                                                              │
                                                              ▼
                                          Event Chain Reconstruction (5-section template)
                                                              │
                                                              ▼
                                                  adaptation_state (Phase 16 modifiers)
                                                              │
                                                              ▼
                                                  operator_priorities (Phase 17 queue)
                                                              │
                                                              ▼
                                                        Frontend DISC page
```

Every downstream layer's confidence is **capped** by its upstream `data_quality`. Scarcity cascades all the way to operator priorities — the queue refuses to commit `PRE_CASCADE` if the contributing probes don't have data.

---

## 3. Formula registry

All formulas below are **deterministic, multiplicative, and explainable**. No black-box scoring. Every score is bounded [0, 100] (or [0, 1]) and every factor is exposed somewhere in the API response for inspection.

### Sanity audit

```
severity_score = clip( (value − info_threshold) / (critical − info) × 100, 0, 100 )
overall_state  = CRITICAL if any critical, else WARN if any warn, else INFO if any, else CLEAN
overall_score  = max(severity_score across findings)
```

10 checks: `validation_collapse` · `anomaly_inflation` · `propagation_loop` · `propagation_instability` · `forecast_overshoot` · `pattern_explosion` · `confidence_collapse` · `regime_fragmentation_spike` · `unstable_clustering` · `adaptation_oscillation`. Each with explicit info/warn/critical thresholds.

### Data quality (scarcity)

```
_discovery_quality(samples, low, medium, high) →
  "HIGH"         if samples ≥ high
  "MEDIUM"       if samples ≥ medium
  "LOW"          if samples ≥ low
  "INSUFFICIENT" otherwise

SCARCITY_FACTOR = {INSUFFICIENT: 0.15, LOW: 0.40, MEDIUM: 0.75, HIGH: 1.00}
```

Thresholds chosen per-endpoint (e.g. pattern_discovery `low=20, medium=100, high=500` buckets; forecast `low=24, medium=72, high=288` snapshots).

### Pattern discovery

```
stability_score   = base × (penalty_per_flag^k) × half_balance_factor
                    where base = 1.0; penalties:
                      SINGLE_WINDOW       ×0.30
                      LOW_RECURRENCE      ×0.35
                      HIGH_LIFT_LOW_SUPPORT ×0.50
                      REGIME_FRAGILE      ×0.65
                      BUCKET_SENSITIVE    ×0.65
                      LOW_SUPPORT         ×0.75
                    half_balance_factor = 0.6 + 0.4 × (2 × minority_share)
effective_lift    = raw_lift × stability_score
pattern_confidence = 100 × stability_score × scarcity_factor
```

Output range: `pattern_confidence` ∈ [0, 100]. Sort key is `effective_lift` (not raw lift).

### Propagation graph

**Sampling-resolution guard.** Pairs whose `lead` is below `min_lead_ms = 5_000` ms (default) are **dropped at ingestion** before any score is computed — not penalized, not flagged, *dropped*. Alerts arriving within ~5 s of each other carry no derivable transmission order from the available timestamps; treating them as a propagation edge would inflate causality from what is effectively co-occurrence. The `lead_window_ms = 30 × 60_000` (30 min) upper bound similarly drops pairs separated by so much time that recurrence cannot be distinguished from background co-incidence.

Per-edge (computed only on pairs that survived the simultaneity + window guards):

```
volume_strength       = 1 − exp(−count / 15)
lead_clarity          = clip( (avg_lead − min_lead) / 60s, 0, 1 )
lead_consistency      = clip( 1 − std_lead/avg_lead, 0, 1 )
temporal_consistency  = days_with_events / lookback_days
recurrence_stability  = 1 − (max_day_count − 1) / (count − 1)
symmetry_penalty      = (min/max reverse_count)²    (0 if no reverse)

base_confidence       = 0.30·volume + 0.20·lead_clarity + 0.15·lead_consist
                      + 0.20·temporal + 0.15·recurrence

leader_stability(s)   = Σ(edge.base_conf × edge.count) / Σ(edge.count)
                        across outgoing edges of node s
leader_pull           = 0.5 + 0.5 × leader_stability(from_node)

confidence_score      = base × (1 − sym_penalty) × leader_pull

confidence label      = HIGH ≥ 0.70 · MEDIUM ≥ 0.45 · LOW otherwise
```

Graph-level:

```
integrity_score = 100 × ( 0.45·avg_confidence + 0.25·(1−sym_share)
                        + 0.20·(1−weak_share) + 0.10·coverage )
```

### Causal propagation (Phase 15 #1)

```
volume_factor          = 1 − exp(−count/15)
asymmetry              = (count − reverse_count) / (count + reverse_count)
asymmetry_factor       = max(0, asymmetry)
evidence_factor        = evidence_count / n_windows           (sub-window survival)
common_driver_factor   = 0.35 if common_driver else 1.0
symmetry_factor        = 1 − sym_penalty
scarcity_factor        = SCARCITY[data_quality]

causal_confidence      = volume × asymmetry × evidence × cd × sym × scarcity   ∈ [0, 1]

verdict (priority order — refusal verdicts come FIRST so a clean DIRECTIONAL
is only emitted when every refusal path was rejected):
  COINCIDENCE         sym_penalty ≥ 0.70          (effectively bidirectional)
  EXPLORATORY         data_quality ∈ {INSUFFICIENT, LOW}
  COMMON_DRIVEN       common-driver candidate found
  UNDER_EVIDENCED     evidence_count ≤ 1
  AMBIGUOUS           asymmetry < 0.40
  DIRECTIONAL         else
```

### Influence hierarchy (Phase 15 #5)

```
stability             = directional_edge_count / total_edges
out_ratio             = out_count / (out + in)
avg_out_confidence    = mean(causal_confidence of outgoing edges)
avg_in_confidence     = mean(causal_confidence of incoming edges)

role classification (with rationale):
  ISOLATED          total < 3
  INSTABILITY_HUB   stability < 0.30 AND ≥2 low-quality edges
  LEADER            out_ratio > 0.70 AND avg_out_conf ≥ 0.20
  FOLLOWER          out_ratio < 0.30 AND avg_in_conf ≥ 0.20
  AMPLIFIER         0.30 ≤ out_ratio ≤ 0.70 AND (avg_out OR avg_in ≥ 0.20)
  ISOLATED          else
```

### Market state transitions (Phase 15 #3)

```
persistence           = count of consecutive snapshots in to_state
was_reverted          = bounced to from_state within REVERSAL_WINDOW=3 snapshots
acceleration          = post_stress_slope − pre_stress_slope (6 snapshots each)
meta_conf_at          = meta_confidence_score at transition moment

persistence_factor    = min(1, persistence / 12)
meta_conf_factor      = meta_conf_at / 100
reversal_factor       = 0.25 if was_reverted else 1.0
scarcity_factor       = SCARCITY[data_quality]
confidence            = persistence_factor × max(0.2, meta_conf_factor)
                        × reversal_factor × scarcity_factor

verdict:
  REVERSED            bounced back within REVERSAL_WINDOW
  FLICKER             persistence < PERSISTENCE_THRESHOLD=3
  ACCELERATING        |acceleration| ≥ 5
  PERSISTENT          else
```

### Distributed Stress Detection (Phase 15 #4)

Engine code identifier: `crisis_genesis()` (retained for stability). Composite verdict over 7 independent probes:

```
contributing_probes  = count of probes whose data_quality ≠ INSUFFICIENT
hot_count            = count of probes whose individual score ≥ 75
genesis_score        = mean(probe.score for contributing probes)        ∈ [0, 100]
confidence           = contributing_probes / 7

verdict (priority order, applied AFTER scarcity cap):
  INSUFFICIENT       contributing_probes == 0
  PRE_CASCADE        genesis_score ≥ 75  AND  hot_count ≥ 3
  ELEVATED_RISK      genesis_score ≥ 50
  EARLY_DISTORTION   genesis_score ≥ 25
  CALM               genesis_score <  25

scarcity cap:
  if > 3 probes INSUFFICIENT → verdict floored at EARLY_DISTORTION
  (the layer refuses PRE_CASCADE / ELEVATED_RISK while too many inputs are blind)
```

7 probes (each emits an independent [0,100] score with its own scarcity gate): `fragmentation_growth` · `resiliency_decay` · `propagation_widening` · `dependency_concentration` · `anomaly_synchronization` · `transition_instability` · `stress_acceleration`. See [Layer 8 table](#layer-8--causal-inference-layer-phase-15).

Operator-visible decomposition is mandatory: every published verdict carries the probe list, per-probe score, per-probe data_quality, and the contributing-probes count. The composite is never published without its parts.

### Forecast hardening

```
slope_capped              = raw_slope clipped to ±SLOPE_CAP=25/day
extrapolation_capped      = forecast_value left [0, 100] band
slope_consistency         = agreement between first/last half slope fits
horizon_decay             = data_span_days / (data_span_days + horizon_days)
rmse_factor               = max(0, 1 − rmse/50)
cap_factor                = 0.5 if (slope_capped OR extrapolation_capped) else 1.0

confidence                = 100 × rmse × horizon_decay × consistency × cap_factor

trajectory only labeled when data_quality MEDIUM+ AND confidence ≥ 30
```

### Adaptation modifiers (Phase 16)

```
narrative_confidence_modifier   = 0.50 + (max(0, nc − 0.30) / 0.40) × 0.50  when nc < 0.70
alert_sensitivity_modifier      = 1.00 + min(1, max(0, gs − 30)/50) × 0.50 × max(0.3, gc)
causal_strictness_modifier      = 1.00 + min(0.30, max(0, flicker − 0.25)) + (0.20 if osc else 0)
discovery_suppression_modifier  = {CRITICAL:0.50, WARN:0.70, INFO:0.90, CLEAN:1.00}[sanity_overall]
global_trust_modifier           = product(meta_conf factor × structural_break factor)

all clipped to ADAPTATION_BOUNDS[name]
```

### Operator priorities (Phase 17)

```
priority_score        = severity_raw × confidence × recency × source_weight   ∈ [0, 100]

source_weights:
  sanity      1.50
  genesis     1.30
  transitions 1.00
  structural  0.80
  causal      0.70
  adaptation  0.90

escalation:
  NORMAL    < 25
  WATCH     25 — 50
  IMPORTANT 50 — 75
  CRITICAL  ≥ 75

lifecycle (DB-backed):
  NEW          age < 5 min OR first appearance after RESOLVED
  WORSENING    Δ ≥ +8 since last call
  STABILIZING  Δ ≤ −8
  PERSISTENT   within ±8
  RESOLVED     disappeared from current run
```

---

## 4. Endpoint inventory

84 endpoints under `/api/liquidity`. Selected by criticality below; full list via `GET /openapi.json`.

### Operator-facing (read often, high priority)

| route | purpose | cold | warm | TTL | consumer | risk |
|---|---|---|---|---|---|---|
| `/research/operator-priorities` | Unified priority queue, DB-backed | ~200 ms | 50-100 ms | none (DB writes) | DISC top | 🔴 if down, op blind |
| `POST /research/operator-priorities/{key}/ack` | ACK/MUTE/IGN/RES action | <50 ms | — | none | DISC row buttons | 🟡 |
| `/research/operator-priorities/{key}/history` | Per-key escalation timeline | <50 ms | — | none | DISC drawer | 🟢 |
| `/research/operator-digest?window_hours=1|6|24` | What materially changed | <100 ms | — | none | DISC bottom | 🟢 |
| `/research/sanity-audit` | Engine integrity checks | ~5 s | 7 ms | 30s | DISC banner | 🔴 if down, integrity blind |
| `/research/adaptation-state` | 5 modifiers + audit trail | <200 ms | 7 ms | 120s | DISC top | 🟡 |
| `/research/narrative-causality` | Event Chain Reconstruction (5-section deterministic template) | <200 ms | 7 ms | 120s | DISC event-chain panel | 🟢 |
| `/research/crisis-genesis` | Distributed Stress Detection (7-probe composite) | <200 ms | 7 ms | 120s | DISC stress banner | 🟡 |
| `/admin/runtime-health` | Pool + cache + heartbeats + tables | <50 ms | <50 ms | none | (ops use) | 🟡 |

### Phase 18 — Investigation & Casework

| route | purpose | cold | warm | TTL |
|---|---|---|---|---|
| `POST /research/investigations` | Create case (manual or with initial evidence) | <50 ms | — | none |
| `GET /research/investigations` | List with status/severity/tag/search filters | <50 ms | — | none |
| `GET /research/investigations/{id}` | Full case detail (+ evidence + notes counts) | <50 ms | — | none |
| `PATCH /research/investigations/{id}` | Update fields; status transitions logged | <50 ms | — | none |
| `POST /research/investigations/{id}/notes` | Append-only note (with `@mention` parsing) | <50 ms | — | none |
| `POST /research/investigations/{id}/evidence` | Link evidence (idempotent on triple) | <50 ms | — | none |
| `DELETE /research/investigations/{id}/evidence/{eid}` | Unlink (audit-logged) | <50 ms | — | none |
| `GET /research/investigations/{id}/timeline` | Hybrid timeline — case events + JOINed upstream | <100 ms | — | none |
| `GET /research/investigations/{id}/causal-tree` | Typed graph with per-edge confidence + rationale | ~200 ms | — | none |
| `GET /research/investigations/{id}/similar` | Deterministic similarity to prior cases | <100 ms | — | none |
| `GET /research/investigations/{id}/export` | Stable 8-section markdown (JSON) | <200 ms | — | none |
| `GET /research/investigations/{id}/export.md` | Same, as plain text/markdown download | <200 ms | — | none |

### Phase 19 — Replay Reconstruction (Pass A)

| route | purpose | cold | warm | TTL |
|---|---|---|---|---|
| `POST /research/investigations/{id}/replay/capture` | Capture or recapture (`force=true`) the frozen snapshot | ~400 ms (composes all surfaces) | — | none |
| `GET .../replay/state?mode=frozen` | Return the opaque snapshot payload | <50 ms | — | none |
| `GET .../replay/state?mode=live&at_ms=…` | Reconstruct surface from history tables at `at_ms` | <150 ms | — | none |
| `GET .../replay/timeline` | Scrubber keyframes around the case anchor | <150 ms | — | none |
| `GET .../replay/diff` | FROZEN vs LIVE narrow field-level diff at anchor | <500 ms | — | none |
| `GET .../replay/propagation` | Frame-bucketed alert counts per symbol + static prop edges | <150 ms | — | none |

### Phase 15 causal layers

| route | purpose | cold | warm | TTL |
|---|---|---|---|---|
| `/research/causal-propagation` | Lead-lag with verdicts | ~250 ms | 7 ms | 300s |
| `/research/structural-dependencies` | Chains / drivers / clusters / sync | ~250 ms | 7 ms | 300s |
| `/research/state-transitions` | PERSISTENT/FLICKER/REVERSED/ACCEL | ~50 ms | 7 ms | 300s |

### Phase 14 discovery layers

| route | purpose | cold | warm | TTL |
|---|---|---|---|---|
| `/research/pattern-discovery` | Recurring metric combos + stability | 400 ms (post-index) | 7 ms | 300s |
| `/research/propagation` | Symbol→symbol graph + integrity | ~70 ms | 11 ms | 300s |
| `/research/hidden-regimes` | Engine-state clusters | <50 ms | 7 ms | 300s |
| `/research/crisis-archetypes` | Auto-labelled anomaly clusters | <50 ms | — | — |
| `/research/memory-abstraction` | Compressed archetype view | <50 ms | — | — |
| `/research/evolutionary-behavior` | Long-horizon engine drift | 1.3 s | 10 ms | 300s |
| `/research/intelligence-forecast` | OLS extrapolation, hardened | <50 ms | 9 ms | 300s |
| `/research/adaptation-recommendations` | Per-metric importance shifts | <50 ms | 7 ms | 300s |
| `/research/adapted-recommendations` | …with Phase-16 suppression applied | <100 ms | — | none |

### Heavy / pre-Phase-14 aggregators

| route | purpose | cold | warm | TTL |
|---|---|---|---|---|
| `/research/synthesis` | 6-layer composite — **30 s cold** | 26-31 s | 7 ms | 300s |
| `/research/multi-horizon` | Multi-horizon outlook | 5 s | 6 ms | 300s |
| `/research/intelligence-history` | Recent snapshots | ~25 ms | — | — |
| `/research/structural-breaks`, `/risk-state`, `/regime-shift-warning`, `/meta-confidence`, `/meta-intelligence-health`, `/strategic-state` | Individual layers of synthesis | ms-range | — | — |

### Memory / dependency trace

| route | purpose | risk |
|---|---|---|
| `/research/anomaly-memory` (GET/POST) | Memory rows + insert | 🟡 if abused |
| `/research/anomaly-lineage/{id}` | BFS up to depth 3 | 🟢 (capped) |
| `/research/memory-graph` | Full nodes+edges | 🟡 (scales with memory) |
| `/research/crisis-evolution-tree`, `/regime-ancestry`, `/edge-lineage/{kind}` | Specific dependency-trace queries (legacy endpoint names; surface labels prefer "edge trace") | 🟢 |
| `/research/narrative-chronicle` | Anomaly-memory rows projected as an event-chain timeline (legacy endpoint name) | 🟢 |

### Pre-existing operator surfaces (alerts, pins, replay)

| route | purpose |
|---|---|
| `/active`, `/alerts`, `/metrics`, `/metrics/{symbol}` | Live operator data |
| `/pins`, `/pins/{symbol}`, `/pins/{symbol}/move` | Symbol pinning |
| `/annotations` | Operator notes |
| `/replay/range` | Historical replay |
| `/crossex/{symbol}` | Cross-exchange snapshot |

---

## 5. Table inventory

Postgres 16, single instance, no replication. Sizes as of 2026-05-23.

| table | size | rows | grow/day | retention | owner | indexes |
|---|---:|---:|---:|---|---|---|
| `liquidity_samples` | 289 MB | 1.6 M | ~1 M | 35d (poller) | LIQ scanner | `(symbol, metric, ts)`, **`(metric, ts)`** (post-P0 audit), pkey |
| `liquidity_alert_history` | 608 kB | 871 | ~700 | 90d | alert engine | `(kind, ts)`, `(symbol, ts)`, unique `alert_id` |
| `server_metrics` | 352 kB | 1.4 K | ~470 | unbounded (small) | server | `(created_at)` |
| `snapshots` | 264 kB | 144 | low | none | alert engine | pkey |
| `alert_states` | 184 kB | 147 | low | none (one row per (symbol, tf)) | alert engine | composite |
| `liquidity_intelligence_history` | 136 kB | 215 | ~130 | 90d | intel snapshot | `(ts_ms)` |
| `alert_events` | 112 kB | 100 | ring | top-100 | alert engine | — |
| `operator_priority_history` | 112 kB | 10 | active set | 90d for resolved only | operator | `(last_seen)`, `(status, escalation)`, unique key |
| `liquidity_active_subs` | 88 kB | 18 | — | — | WS engine | — |
| `liquidity_anomaly_edges` | 72 kB | 61 | low | dangling-cleanup | anomaly recorder | `(from_id)`, `(to_id)`, unique triple |
| `liquidity_anomaly_memory` | 64 kB | 16 | ~10 | 180d | anomaly recorder | `(occurred_at)`, `(kind)` |
| `operator_priority_events` | 64 kB | 11 | varies | 90d | operator | `(key, ts)`, `(ts, type)` |
| `operator_acknowledgements` | 64 kB | 3 | low | 180d | operator | `(key, active)`, `(created_at)` |
| `investigations` | new | 0 | per case | unbounded (small) | operator/worker | `(status)`, `(severity)`, `(updated_at_ms)`, `(origin_fingerprint)`, `(primary_symbol)` |
| `investigation_evidence` | new | 0 | per link | bound to case | operator | `(investigation_id)`, `(evidence_type, ref_key)`, unique triple |
| `investigation_notes` | new | 0 | per note | append-only | operator | `(investigation_id, created_at_ms)` |
| `investigation_events` | new | 0 | per event | append-only | operator | `(investigation_id, ts_ms)`, `(ts_ms, event_type)` |
| `investigation_replay_snapshots` | new | 0 | one per case | bound to case | operator/worker | unique `(investigation_id)`, `(captured_at_ms)` |
| `users`, `coins`, `system_status`, `liquidity_ws_status`, `liquidity_pins`, `liquidity_annotations`, `structure_overrides`, `user_tda_states`, `liquidity_crossex_history` | < 70 kB each | small | low | unbounded (small) | various | — |

**Steady-state projection at 1y:** ~7 GB total. `liquidity_samples` (35d retention) dominates at ~6.6 GB; everything else bounded. Adopt aggregation tier (`docs/2026-05-23-p1-hardening-plan.md` §1) before scaling symbol count 2×.

---

## 6. Operator workflow guide

### What you see first

Open the **DISC page**. From the top:

1. **Operator queue banner** — durable across restarts. Per-row priority chip + escalation + lifecycle + source layer + action buttons.
2. **Sanity banner** — engine integrity checks. CLEAN = subtle dim row; WARN/CRITICAL = colored banner.
3. **Distributed Stress Detection banner** — 7-probe composite. Reports the discrete verdict (CALM / EARLY_DISTORTION / ELEVATED_RISK / PRE_CASCADE / INSUFFICIENT) with `contributing_probes / 7` confidence and per-probe decomposition on click. Not a forecast; reports only the current measured state across the lookback window.
4. **Adaptation loop banner** — 5 modifier coefficients; lists which downstream behavior is suppressed/strengthened, the input that drove the change, and the [min, max] bounds the modifier is clipped to.
5. **Event Chain Reconstruction panel** — deterministic 5-section template composed from causal / structural / transition / distributed-stress outputs. Per-section confidence is taken from the upstream layer, never invented. Mandatory "what we don't know" section enumerates layers currently INSUFFICIENT / UNDER_EVIDENCED.
6. **Causal Propagation** panel + **Structural Dependencies** panel + **Regime Transition Engine** panel — deeper drill-down.
7. **Pattern Discovery** + **Hidden Regimes** + **Stress Archetypes** + **Memory Abstraction** — pre-Phase-15 mining layers.
8. **Engine-State Forecast** + **Adaptation Recommendations** + **Evolutionary Behavior** — projection / recommendation surfaces.

### Reading escalation levels

| label | meaning | what to do |
|---|---|---|
| **NORMAL** | Score < 25. Below the floor. | Nothing required. Visible if filter is `all`. |
| **WATCH** | 25 ≤ score < 50. Signal worth monitoring. | Note it. Don't act yet. |
| **IMPORTANT** | 50 ≤ score < 75. Material concern. | Investigate. Cross-check with sanity + Distributed Stress Detection. |
| **CRITICAL** | Score ≥ 75. ≥1 measured property has crossed its CRITICAL threshold. | Diagnostic, NOT a trade signal. Read the per-finding decomposition, then ack/resolve/escalate manually. |

**CRITICAL is never a trade signal.** It states that one or more measured properties have crossed an operator-visible threshold. The action is investigate · cross-check · ack/resolve, not buy/sell.

### Operator actions on a priority row

Each row has five buttons:

| button | semantics | effect |
|---|---|---|
| **ACK** | "I've seen this." | Row stays visible; gets an `ack` chip. No suppression. |
| **MUTE** | "Quiet this for 60 min." | Row dims to opacity-50; reappears when the mute expires. |
| **IGN** | "Permanently dismiss this signal pattern." | Row hidden from `active` filter; visible in `all`. |
| **RES** | "I've handled this." | Row marked `resolved`. If upstream still reports the finding, a `reappeared` event fires on the next run. |
| **HIST** | "Show me the timeline." | Expands an inline drawer with full event log + ack history. |

Actions are **operator workflow only**. The engine never auto-trades. Acknowledgements are durable in `operator_acknowledgements` and survive restarts.

### Reading the digest

The digest block at the bottom of the Operator Queue panel summarizes "what materially changed" in the last 1h / 6h / 24h:

- **NEW** — first time seen in window
- **WORSENED** — escalation_up or large priority_jump
- **STABILIZED** — escalation_down or large priority_jump in the negative direction
- **RESOLVED** — disappeared from active set
- **REAPPEARED** — was resolved, came back

Plus active CRITICAL / IMPORTANT counts and a `contributing_layers` distribution (which subsystem fired the most events in the window).

### Sanity vs confidence vs scarcity — three different kinds of "are we sure?"

These three labels look similar but answer different questions:

| label | answers | source |
|---|---|---|
| **Sanity** | "Is the engine itself behaving correctly?" | `sanity_audit` — 10 integrity checks |
| **Confidence** | "How much should I trust this specific verdict?" | Per-finding/edge/probe explicit decomposition |
| **Scarcity (data_quality)** | "Do we have enough data to commit to anything?" | Per-layer `_discovery_quality(samples, low, medium, high)` |

A WARN sanity + HIGH confidence + INSUFFICIENT scarcity is a coherent state: "Sanity caught a real issue; the verdict on this issue is well-grounded; but we don't have enough data for any conclusions about the broader picture."

---

## 7. Known risks & backlog

### P0 — fix before next measurement phase

*(All P0 from the 2026-05-23 production audit are landed.)*

- ✅ `synthesis` caching — 30 s → 7 ms with 300s TTL
- ✅ `sanity_audit` sub-call memoization (via wrapped upstream functions)
- ✅ Explicit DB pool (`pool_size=20, max_overflow=20, pool_timeout=10`)
- ✅ Pattern-discovery missing index `(metric, ts)` added

### P1 — design documented, not yet implemented

From `docs/2026-05-23-p1-hardening-plan.md`:

| | item | effort | trigger |
|---|---|---|---|
| 🟠 | Aggregation tier (L0 raw 7d / L1 minute 30d / L2 hour 1y) | 1.5-2d | samples > 10 GB or 2× symbols |
| 🟠 | Propagation bucketed pairing O(N) | ½ d | alerts > 5K/day |
| 🟠 | `pg_stat_statements` + top-N in runtime-health | 30 min + DB restart | when query-level visibility needed |
| 🟡 | Worker WS reconnect counter | 1 h | if WS instability is observed |
| 🟡 | Anomaly recorder circuit breaker | 2 h | if `anomaly_inflation` becomes chronic |
| 🟡 | Replay safety limits | 30 min | nice-to-have |
| 🟡 | Stale-data indicators in UI | 2 h | nice-to-have |
| 🟢 | Memory page SVG node cap + memoization | 3 h | when anomaly graph > 200 nodes |
| 🟢 | Liquidity tab `visibilitychange` pause | 15 min | nice-to-have |

### P2 — experimental / future

- Aggregation tier query rewrite for all `liquidity_samples` consumers
- Multi-exchange WS expansion (currently Binance only)
- Replay graph rendering limits (depth cap, virtualization)
- `pg_stat_statements` enablement (postgresql.conf + DB restart)
- Mute expiry cleanup (currently respected on read; not garbage-collected)

### Active CRITICAL findings (snapshot of live state)

The sanity layer is currently in CRITICAL because of one persistent issue:

- `sanity:propagation_loop` — 84 symmetric pairs ≥70% mirror. Most are between low-volume symbols that fire alerts in lockstep during market events. They're correctly demoted by the propagation layer's `symmetry_penalty` (and thus excluded from causal DIRECTIONAL verdicts), but the count is high enough that sanity flags it.

The downstream effect is `discovery_suppression_modifier = ×0.50` on adaptation recommendations. This is **the feedback loop working as designed** — the sanity layer flagged a noise-driven pattern; the adaptation layer applied its CRITICAL-state suppression coefficient (0.50 per the table at [§3 Adaptation modifiers](#adaptation-modifiers-phase-16)) and halved the importance shift on every recommendation.

---

## 8. Architecture freeze

### Stable core (depended-on, low-churn)

These layers have been live with real data, audited under load, and the operator workflow above the stable core is depended-on for daily operation. **Treat changes here as high-risk; require commit-by-commit review.**

- **LIQ Scanner** + **Realtime WS Engine** + **Alert Engine** — the data-acquisition tier
- **`liquidity_samples` + `liquidity_alert_history`** — the immutable record
- **Sanity Audit** + **Runtime Health** — operational visibility
- **Research aggregators**: synthesis, multi-horizon, risk_state, structural_breaks, regime_shift_warning, meta_confidence, meta_intelligence_health, strategic_state, signal_reliability, transition_forecast
- **Operator Queue + Persistence** (Phase 17) — operator workflow now durable
- **Investigation lifecycle / persistence / append-only history** (Phase 18 Pass A & B core) — DB schema, CRUD, evidence linking, notes, lifecycle audit, markdown export
- **Retention loop** — protects against unbounded storage growth
- **Adaptation modifiers** (Phase 16) — bounded, explainable, reversible

### Experimental (working, exposed to operator, outputs are best-current-estimate)

These layers are useful for diagnosis but their absolute numbers should be read as the layer's best current estimate, not as ground truth. Every one of them has explicit scarcity gates and degrades gracefully when inputs are thin. **Iterate freely here; preserve the safety properties (scarcity gating, decomposition, refusal verdicts).**

- **Causal Propagation** (verdicts, common-driver detection) — works, but DIRECTIONAL count = 0 on current data. Output stabilizes only after weeks of accumulated history.
- **Structural Dependencies** (chains, drivers, clusters, sync) — entirely scarcity-gated. Currently exploratory.
- **Regime Transition Engine** (engine code: `market_state_transitions`) — emits real findings now (data_quality=MEDIUM), but `flicker_ratio` thresholds need calibration over longer horizons.
- **Distributed Stress Detection** (engine code: `crisis_genesis`) — 7 probes, 4 currently contributing on live data. The composite always publishes `contributing_probes / 7` and the per-probe scores; a missing probe is never hidden behind the aggregate.
- **Event Chain Reconstruction** (engine code: `narrative_causality`) — deterministic template, no model calls. Output is safe to read because every section is sourced from a layer that publishes its own confidence; sections with no upstream input render as explicit gaps.
- **Memory Graph / Anomaly Edge Trace** (engine code: `memory_graph`, `anomaly_lineage`) — small now, will need rendering limits (P2 backlog) at scale.
- **Pattern Discovery / Hidden Regimes / Stress Archetypes** (engine code: `crisis_archetypes`) — data-driven mining, all guarded by `data_quality`.
- **Investigation causal tree / similarity** (Phase 18 Pass B) — investigation-support graphs and deterministic case similarity. Tree edges come from already-stable upstream layers (anomaly memory edges, propagation, structural deps, transitions); similarity is rule-based (no ML). Useful diagnostically; reasons always exposed.
- **Investigation auto-draft** — only fires on `crisis_genesis = PRE_CASCADE` (Distributed Stress Detection verdict) with deduped fingerprint. Treat the absolute count of auto-drafts as exploratory until the stress-detection layer itself stabilizes.
- **Replay reconstruction / FROZEN-vs-LIVE diff** (Phase 19 Pass A) — frozen snapshot store is stable, but per-surface live reconstruction inherits the experimental status of its inputs (causal · structural · distributed-stress · event-chain). Diff entries report that a layer's *output* changed between FROZEN time and LIVE time — they are not market predictions.

### Frozen interfaces (do not break)

If the following change shape, downstream consumers (UI panels, future automation hooks) break:

- `GET /research/operator-priorities` response shape — UI depends on it heavily
- `POST /research/operator-priorities/{key}/ack` body shape
- `GET /research/sanity-audit` response shape — operator banner depends on `findings` array
- `GET /research/adaptation-state` modifier names — downstream consumers index by these
- `GET /admin/runtime-health` shape — only safe surface for external monitoring

Internal research aggregators (`_research.*`) are free to evolve provided the endpoint shape stays stable.

### What this document promises

If the system goes down tomorrow and someone needs to rebuild it from scratch:

1. `docker compose up -d db && docker compose run --rm backend python -m app.db.init_db` creates all 22 tables (DDL is in [`backend/app/db/init_db.py`](backend/app/db/init_db.py))
2. Worker auto-starts the four background tasks; samples begin flowing within 60 s
3. After ~6 hours of accumulated data, `data_quality` on most layers crosses MEDIUM
4. After ~24 hours, MEDIUM/HIGH gates open and causal/structural/transition layers begin emitting committed verdicts
5. Operator queue starts populating immediately (sanity findings on day 1)

If someone needs to extend it without losing the architectural intent:

- **Data-quality gating + published decomposition** is the core invariant. Every layer gates its outputs by `data_quality` and publishes its per-factor decomposition. New layers that do "we have a model that says X" without exposing factors do not fit.
- **Acyclic dependencies** between measurement layers. `adaptation_state` reads from observed layers but never writes back. Operator priorities reads from everything but never feeds back into upstream computations.
- **Bounded, reversible, explainable** is the rule for any modifier that affects downstream behavior. Phase 16's `ADAPTATION_BOUNDS` is the pattern to copy.
- **No trading actions**. The platform is operator-facing observability, not execution. ACK/MUTE/RESOLVE are workflow markers, not trade triggers.

### Companion docs

- `docs/2026-05-23-production-hardening-audit.md` — P0 audit + applied fixes (commit 6b64a76)
- `docs/2026-05-23-p1-hardening-plan.md` — P1 design + scaling estimates (commit c8246f4)
- This document — system-wide freeze (commit pending)

Together these three give the full picture: where we are, what's been hardened, what's planned, and how the pieces fit.

---

## 9. Quantitative metric registry — realtime tier

§3 catalogued the research-aggregator formulas. This section catalogues the **realtime-tier microstructure metrics** computed at 1 Hz against the WS-sourced orderbook + tape, written to `liquidity_samples` under the metric names below. Each entry follows the same 7-field structure: Purpose · Inputs · Formula · Threshold/output · Failure conditions · Replay behavior · Validation constraints.

### 9.1 Credible Depth (`credible_depth`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/metrics.py:56`](shared/kazus_logic/liquidity/realtime/metrics.py#L56) `credible_depth_usd()` |
| Purpose | USD value of resting orderbook liquidity around mid that has demonstrated **persistence** — defeats spoof flicker by ignoring quotes younger than a minimum age |
| Inputs | `state.bids` / `state.asks` (top-20 WS book), each level keyed by price with `(qty, first_ts)` first-appearance timestamp; `state.mid_price()` |
| Formula | `band = mid × (1 ± CREDIBLE_BAND_PCT)` with `CREDIBLE_BAND_PCT = 0.005` (±0.5%). For each side, sum `price × qty` over levels where `price ∈ band` **AND** `(now_ms − first_ts) ≥ CREDIBLE_MIN_AGE_MS = 400`. Quotes younger than 400 ms contribute **zero** |
| Threshold | Reported as raw USD. Higher = more genuine resting liquidity at the touch |
| Failure conditions | `mid_price()` returns None → metric returns None (no fabricated value). Empty book → None. Per-symbol thresholds for "low credible depth" are not centralized — read via the per-symbol percentile context the operator pulls from `/metrics/{symbol}` |
| Replay behavior | **Not reconstructible from history**: the metric depends on per-level `first_ts` which is only held in memory in `SymbolState`. Historical samples carry the computed value, not the inputs. Replay tier uses the persisted `liquidity_samples` row as authoritative |
| Validation constraints | The 400 ms persistence floor is the anti-spoof primitive. Lowering it weakens the metric's core property; raising it makes the metric blind to short-but-real liquidity. Any change must be paired with a recalibration against known spoof / non-spoof regimes — currently not measured (see §12) |

### 9.2 Resiliency Score (`resiliency_score`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/intelligence.py:194`](shared/kazus_logic/liquidity/realtime/intelligence.py#L194) `resiliency_score()` |
| Purpose | 0..100 measure of how the book recovers after a stress event. Higher = faster + more complete refill |
| Inputs | `state.events` list of `RecoveryEvent` records produced by `_detect_events`: kinds ∈ {`liq_spike`, `spread_explosion`, `depth_collapse`, `obi_flip`}, each with `pre_depth`, `started_ts`, `recovered_ts`, `recovery_ms`, `refill_velocity`. Event detection is debounced by `EVENT_DEBOUNCE_MS = 10_000` |
| Formula | For each completed event (i.e. `recovery_ms is not None`): `time_part = 100 × exp(−recovery_ms / 30_000)`; `velo_part = 50 × tanh(refill_velocity / 50_000) + 50`; event-score = `0.6·time_part + 0.4·velo_part`. Aggregate: exp-weighted by event age with **5-min half-life** (`weight = exp(−age_s / 300)`). Output clipped to [0, 100] |
| Threshold | Recovery defined as depth climbing back to `RECOVERY_FRACTION × pre_depth = 0.80 × pre`. Events past `RECOVERY_MAX_AGE_MS = 90_000` are marked "did not recover" (recovery_ms = 90s, refill_velocity = 0) — not silently dropped |
| Failure conditions | No completed events → returns **None**, not a fabricated 100. Per-event `pre_depth ≤ 0` → event skipped from recovery advancement. Operator-visible column shows "—" when None |
| Replay behavior | Reconstructible only from `liquidity_samples` (the persisted score); the in-memory `RecoveryEvent` ring is not on disk |
| Validation constraints | Three event-detection thresholds are load-bearing: `DEPTH_COLLAPSE_DROP = 0.40` (40% drop in 10s), `SPREAD_EXPLOSION_BPS = 8.0`, `LIQ_SPIKE_USD = 50_000`. All three are absolute (not per-symbol percentiles) and are interim values that should be recalibrated per-symbol — currently not measured (see §12) |

### 9.3 Impact Score (`impact_score`) — Kyle λ sigmoid

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/intelligence.py:270`](shared/kazus_logic/liquidity/realtime/intelligence.py#L270) `impact_score()` |
| Purpose | 0..100 measure of price sensitivity to signed flow — the **Kyle Lambda** classical microstructure quantity, sigmoid-normalized for display |
| Inputs | `state.trades` over the last `KYLE_WINDOW_MS = 60_000` ms |
| Formula | Bucket trades into `KYLE_BUCKET_MS = 1_000` ms windows. For each bucket: `signed_usd = signed_qty × first_price`; skip if `|signed_usd| < KYLE_MIN_VOLUME_USD = 100`; `ret = (last_p − first_p) / first_p`; `λ = |ret| / |signed_usd| × 1e9`. Take **median** over buckets for robustness. Output: `100 / (1 + exp(−(λ − 1)))` — sigmoid centred at λ = 1 |
| Threshold | λ = 1 ⇒ score ≈ 50 ("typical mid-cap perp under normal flow") per code comment. No hard verdict thresholds — the metric is published raw |
| Failure conditions | `< KYLE_MIN_BUCKETS = 8` filled buckets → returns **None**. Zero or negative prices → bucket skipped. Negligible flow → bucket skipped |
| Replay behavior | Persisted to `liquidity_samples`; the underlying tape is not retained at 1-second granularity past the 60s window |
| Validation constraints | The "λ = 1 → 50" anchor is a documentation claim, not a calibration result — it has not been measured against a labelled corpus of "normal" vs "stressed" flow. Re-anchoring requires a per-symbol baseline (see §12) |

### 9.4 Fragility Score (`fragility_score`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/intelligence.py:279`](shared/kazus_logic/liquidity/realtime/intelligence.py#L279) `fragility_score()` |
| Purpose | 0..100 measure of **price-impact instability** — high variance of bucket λs means the relationship between flow and price is unstable, regardless of its central level |
| Inputs | Same bucket λs as Impact Score |
| Formula | `mean = Σλ / N`; `var = Σ(λ − mean)² / N`; `std = √var`; `cv = std / mean if mean > 1e-12 else 0`; output = `clip(cv × 50, 0, 100)`. CV ≥ 2 ⇒ score = 100 ("very fragile") |
| Threshold | No hard verdict — published raw. CV in [0, 2] maps linearly to [0, 100] |
| Failure conditions | `< KYLE_MIN_BUCKETS = 8` filled buckets → **None**. Mean λ near zero → CV forced to 0 (rather than div-by-zero) |
| Replay behavior | Persisted to `liquidity_samples` |
| Validation constraints | The `cv × 50` scaling and the "CV ≥ 2 = fragile" anchor are interim. Calibration requires labelling regimes of known fragility, not yet collected |

### 9.5 Realized vs Predicted Impact (`exec_impact`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/exec_impact.py`](shared/kazus_logic/liquidity/realtime/exec_impact.py) |
| Purpose | **Forward-only measurement** of how trade bursts actually move the market vs. what book-walk on the visible top-20 predicted. Per memory [project_exec_impact_layer](project_exec_impact_layer.md): pure observation mode, downstream not calibrated |
| Inputs | Consecutive same-side taker prints with gap ≤ `BURST_GAP_MS = 250` (one burst); pre-burst book snapshot from `state.book_history` ring; post-settle mid `SETTLE_MS = 500` ms after the burst's last print |
| Formula | Per burst: `expected_bps` = book-walk impact computed over pre-burst top-20 in the taker's direction; `realized_bps` = signed mid move (pre → post-settle); `divergence_bps = realized_bps − expected_bps`; `ratio = realized_bps / expected_bps` published **only when `|expected_bps| ≥ EXPECTED_FLOOR_BPS = 0.5`**, otherwise None. Bursts bucketed by notional: S < `BUCKET_M_USD = 50_000` ≤ M < `BUCKET_L_USD = 500_000` ≤ L |
| Threshold | No verdict — four numbers per ExecEvent + a `book_exhausted` flag. When `book_exhausted = True` (burst notional exceeded visible top-20), `expected_bps / divergence / ratio` are **None**, but the burst still counts under `exec_book_exhausted` |
| Failure conditions | Burst < `NOTIONAL_FLOOR_USD = 5_000` → skipped. Missing pre or post book snapshot → event **dropped** (not approximated). `expected_bps` below noise floor → ratio = None |
| Replay behavior | **Strictly forward-only**. L2 book state is not persisted to disk, so historical bursts before the layer activated are structurally unmeasurable. Published per-(side, bucket) rolling medians over `EVENT_WINDOW_MS = 5 × 60 × 1000` ms |
| Validation constraints | This layer is the only direct empirical test of [Credible Depth](#91-credible-depth-credible_depth)'s anti-spoof claim — if `realized_bps ≫ expected_bps` while `book_exhausted = False`, the book promised liquidity that did not materialize. Aggregating that relationship over time is the calibration backlog item in §12 |

### 9.6 Liquidation stress (`liq_stress`)

| | |
|---|---|
| Code | [`shared/kazus_logic/liquidity/realtime/metrics.py:80`](shared/kazus_logic/liquidity/realtime/metrics.py#L80) `liquidation_stress_usd()` |
| Purpose | Rolling USD value of forced liquidations — cascade indicator |
| Inputs | `state.liquidations` (WS `@forceOrder` feed — note: switched from dead `forceOrder` stream in commit 5c4acbc) |
| Formula | Sum of `price × qty` over liquidations within `LIQ_WINDOW_MS = 60_000` ms |
| Threshold | Raw USD, no verdict |
| Failure conditions | No liquidations in window → 0.0 (not None — absence of stress is a valid measurement) |
| Replay behavior | Persisted to `liquidity_samples` |
| Validation constraints | Threshold for "cascade conditions" is symbol-dependent; not centrally calibrated |

### 9.7 Cross-venue divergence (`crossex`)

| | |
|---|---|
| Code | [`backend/app/api/liquidity.py:465`](backend/app/api/liquidity.py#L465) `CrossExDivergence` |
| Purpose | Pairwise divergence vs the reference exchange (Binance) — the anti-manipulation primitive. Sustained price separation across major venues is rare and usually means something is wrong on one side |
| Inputs | Per-exchange snapshot from `exchanges.REGISTRY` (currently Binance + Bybit): `funding_rate`, `open_interest_usd`, `spread_fraction`, `mid_price` |
| Formula | For each non-reference exchange: `funding_diff = this.funding − reference.funding` (absolute); `oi_diff_pct = (this.oi − reference.oi) / reference.oi`; `spread_diff_pct = same shape`; `mid_price_diff_pct = same shape` |
| Threshold | No hard verdict — published raw with the reference labelled. Mid-price divergence is the canonical signal (per code comment) |
| Failure conditions | Exchange fetch raises / returns None → snapshot dropped silently. Per `liquidity.py:498-508` errors are caught individually; the response carries only successful venues |
| Replay behavior | Snapshots persisted to `liquidity_crossex_history` (90 d retention per `poller.py:199`) |
| Validation constraints | "Sustained" is not currently formalized — there is no `divergence_persistence` threshold below which a one-tick divergence is suppressed. Adding that gate is in §12 |

### 9.8 Already-formalized in §3 (pointers)

The following are not duplicated here because their formulas already live in §3 [Formula registry](#3-formula-registry):

- **Distributed Stress Detection** (`crisis_genesis`) — 7-probe composite → §3 [Distributed Stress Detection](#distributed-stress-detection-phase-15-4)
- **Causal confidence** per pair → §3 Causal propagation
- **Propagation per-edge confidence** → §3 Propagation graph
- **Regime transition verdict** → §3 Market state transitions
- **Adaptation modifiers** (5 bounded coefficients) → §3 Adaptation modifiers
- **Sanity audit severity** → §3 Sanity audit
- **Operator priority score** → §3 Operator priorities
- **Pattern-discovery stability + confidence** → §3 Pattern discovery
- **Forecast confidence (with cap factor)** → §3 Forecast hardening

---

## 10. Failure modes & observability limits

This section enumerates failure modes the code **actually handles**, failure modes it **does not handle** (known blind spots), and failure modes that are **structurally unrecoverable** from the data the layer holds. Every entry is grounded in code — no aspirational coverage.

### 10.1 Exchange / transport failures (handled)

| failure | how the system handles it | code |
|---|---|---|
| WS connection drop | Exp-backoff 1 s → 30 s + jitter; on reconnect, `conn_id` bumps and consumers re-issue SUBSCRIBE | [`realtime/ws_client.py`](shared/kazus_logic/liquidity/realtime/ws_client.py), Layer 2 row in §1 |
| WS silent stall | ping/pong 30 s send / 10 s read — if no traffic, the connection is recycled rather than left hanging | Layer 2 row in §1 |
| Sub-second message reorder | Sampler reads `state` at `SAMPLE_INTERVAL_S = 1.0`; sub-second jitter is absorbed in the next tick | [`realtime/engine.py`](shared/kazus_logic/liquidity/realtime/engine.py) |
| REST 429 / rate-limit | Poller cadence `POLL_INTERVAL_S = 60`; per-exchange clients catch `httpx.HTTPStatusError` and skip the round | [`liquidity/poller.py`](shared/kazus_logic/liquidity/poller.py) |
| Cross-exchange fetch failure | `asyncio.gather(..., return_exceptions=True)` — failed venues drop out of the response; reference may be missing | [`api/liquidity.py:498`](backend/app/api/liquidity.py#L498) |
| Long missed tick (> 7 min) | Alert engine re-issues a full re-check at startup AND when a missed tick is detected | Layer 3 row in §1 |

### 10.2 Exchange / transport failures (NOT handled — known blind spots)

| failure | consequence | mitigation status |
|---|---|---|
| Partial-book WS desync (server-side `lastUpdateId` skip) | `SymbolState.bids/asks` drift from venue truth until reconnection. There is no diff-vs-rest reconciliation loop | none — relies on periodic reconnects; partial drift is silent for that window |
| Timestamp drift (local clock vs venue clock) | All `_first_ts` ages are measured against `time.time()` locally. A drifting host clock biases [Credible Depth](#91-credible-depth-credible_depth)'s 400 ms persistence floor | none — no NTP enforcement at the app layer |
| Venue outage > 30 s | WS reconnect keeps trying; if Bybit is down, `crossex` simply omits it without flagging which venues are missing in the divergence response | minimal — venues are listed in `snapshots[]` but operator must visually compare |
| Stale REST snapshot (cached at CDN) | Poller cannot distinguish "no change" from "served from cache". Identical samples in `liquidity_samples` could mean either | none |

### 10.3 Market-structure failures (NOT handled — out of scope)

These are properties of the market that the current measurement set cannot disambiguate. They are not bugs — they are stated to set operator expectation.

- **Hidden liquidity dominance.** [Credible Depth](#91-credible-depth-credible_depth) reads only the displayed top-20 levels. A market dominated by iceberg / hidden orders reports a low Credible Depth that does not reflect actual executable size. [Realized vs Predicted Impact](#95-realized-vs-predicted-impact-exec_impact) is the only existing measurement that can contradict this: when `realized_bps ≪ expected_bps` with `book_exhausted = False`, hidden liquidity is the most likely explanation. The platform does not auto-attribute it as such.
- **Liquidation-driven false signals.** A liq cascade fires every alert kind (`liq_spike`, `spread_explosion`, `depth_collapse`) simultaneously, and propagation_graph picks up synchronized cross-symbol activity. The `common_driver` test in `causal_propagation` rejects pairs whose co-movement can be explained by a shared shock — but the test is per-pair, not per-market-wide-event. Distributed Stress Detection's `anomaly_synchronization` probe is the closest signal that "this is a cascade, not many independent stresses."
- **Spoof saturation regimes.** When > 50% of displayed depth is sub-400-ms quote flicker, [Credible Depth](#91-credible-depth-credible_depth) does its job correctly (reports near-zero) but the **operator-visible field is the same as a genuinely-empty book**. The two states are distinguishable only by cross-referencing the raw book — no automated flag is emitted.
- **Quote-stuffing.** Burst rates of WS messages above what the sampler reads at 1 Hz cause **information loss inside the tick**. The current 1 Hz sample rate does not surface this — the metric just sees the last state of the tick.
- **Perp-only distortions** (funding squeezes, OI imbalances) are surfaced via [§9.7 Cross-Venue Divergence](#97-cross-venue-divergence-crossex) only at the per-snapshot level. There is no time-series divergence-persistence metric — see §12.

### 10.4 Measurement failures (handled by `data_quality` gating)

Every research-tier layer publishes its own `_discovery_quality(samples, low, medium, high)` ∈ {INSUFFICIENT, LOW, MEDIUM, HIGH}; downstream confidence is multiplied by `SCARCITY_FACTOR = {INSUFFICIENT: 0.15, LOW: 0.40, MEDIUM: 0.75, HIGH: 1.00}`. Concrete handling:

| measurement failure | gate |
|---|---|
| Low sample count | `data_quality = INSUFFICIENT` → causal verdict forced to EXPLORATORY (`causal_propagation`); pattern_discovery refuses to publish |
| Sparse book (few top-20 levels populated) | [Credible Depth](#91-credible-depth-credible_depth) returns None when `state.bids/asks` empty rather than a synthetic 0 |
| Volatility spike overwhelming Kyle bucket count | `< KYLE_MIN_BUCKETS = 8` → [Impact Score](#93-impact-score-impact_score--kyle-λ-sigmoid) / [Fragility Score](#94-fragility-score-fragility_score) return None |
| Propagation false positives from synchronized cross-symbol shock | `common_driver_factor = 0.35` multiplicative penalty in `causal_confidence` + `symmetry_penalty²` on mirror pairs |
| Distributed Stress Detection input blindness | Scarcity cap — verdict floored at EARLY_DISTORTION when > 3 probes INSUFFICIENT |
| Forecast extrapolation past data span | `horizon_decay = data_span_days / (data_span_days + horizon_days)` decays confidence; `cap_factor = 0.5` when either slope or extrapolation was clipped |

### 10.5 Replay limitations (structural)

These follow from what is and is not persisted; they are not bugs.

- **Replay unavailable before layer activation.** A layer deployed at time T cannot reconstruct itself at any t < T. Affects: Phase 15 (causal/structural/transition/stress), Phase 16 (adaptation), Phase 17 (operator priorities), Phase 18 (investigations), Phase 19 (replay), Exec-Impact (2026-05-25). The replay endpoint returns `data_quality = INSUFFICIENT` for windows before activation rather than fabricating a value.
- **Realtime tier inputs not persisted at level granularity.** `state.bids`, `state.asks`, per-level `first_ts`, `state.trades`, `state.liquidations`, `state.book_history` ring — all in-memory. Persisted: only the metric values written to `liquidity_samples` at 1 Hz. Consequence: [Credible Depth](#91-credible-depth-credible_depth) and [Resiliency Score](#92-resiliency-score-resiliency_score) cannot be **recomputed** from history, only **read back** as the values that were computed live.
- **Calibration-version dependency.** Thresholds like `CREDIBLE_BAND_PCT = 0.005`, `CREDIBLE_MIN_AGE_MS = 400`, `RECOVERY_FRACTION = 0.80`, `EXPECTED_FLOOR_BPS = 0.5`, `DEPTH_COLLAPSE_DROP = 0.40` are version-bound. Historical samples written under one set of constants are not directly comparable to samples written under a different set. The system **does not currently version-stamp** which constants were live when a sample was written.
- **Anomaly Memory edges retention bound.** `liquidity_anomaly_memory` 180 d; `liquidity_anomaly_edges` cleaned up when their endpoints are pruned. Replay of an investigation past 180 d carries `data_quality = PRUNED` rather than a reconstructed graph. The frozen replay snapshot stores the graph **at capture time** instead.

---

## 11. Non-inference boundaries

This section is load-bearing. The platform **measures, validates, suppresses, and reconstructs**. It does not infer the following — and any future layer that does will violate the architectural intent stated in §8.

The platform does **not** infer:

- **Market intent.** No layer attempts to characterize what a participant is trying to achieve. Observable: order placement, fill, cancellation. Not observable: motive.
- **Manipulation attribution.** [Credible Depth](#91-credible-depth-credible_depth) flags persistence below 400 ms as non-credible. It does not label that flicker "spoofing" — it labels it "did not meet the persistence threshold." The semantic gap matters: a 200 ms quote could be a market-maker re-quoting on a refresh tick, not a spoof.
- **Coordinated hidden actors.** Synchronized cross-symbol liquidity deterioration triggers [Distributed Stress Detection](#distributed-stress-detection-phase-15-4)'s `anomaly_synchronization` probe and increases the propagation graph's `symmetry_penalty`. None of this attributes causation to "a coordinated group" — synchronized stress and shared shock look identical to the layer, and the layer says so by demoting the verdict rather than committing it.
- **Future price direction.** Every forecast endpoint (`/research/intelligence-forecast`, regime transition forecast, multi-horizon) is OLS extrapolation with explicit `slope_capped` / `extrapolation_capped` / `horizon_decay` / `cap_factor` discounts. No layer publishes a directional trade signal.
- **Causality without measurable lag.** `causal_propagation` requires (a) `asymmetry ≥ 0.40`, (b) `evidence_factor` ≥ 2/n_windows, (c) `common_driver_factor` survival, (d) `symmetry_penalty ≤ 0.70`, AND (e) the underlying `propagation_graph` already dropped any pair with `lead < min_lead_ms = 5_000` ms — failure on any of these forces the verdict to UNDER_EVIDENCED / AMBIGUOUS / COMMON_DRIVEN / COINCIDENCE / EXPLORATORY. A DIRECTIONAL verdict is structurally rare on current data and that is correct.
- **Propagation ≠ causation.** A DIRECTIONAL verdict means "B repeatedly followed A with a stable measurable lag, on independent windows, and not in lockstep, and not jointly driven by a third symbol we could find." It does **not** establish economic causality, transmission certainty, or directional influence in the sense a research paper would use those terms. The full epistemic boundary is documented in [§13 Propagation & causality limits](#13-propagation--causality-limits).
- **Actor identity.** No layer reads exchange-side maker/taker account information or attempts to fingerprint flow to known actors. The data sources used (public REST + public WS) do not carry this information.
- **Strategic objectives of participants.** No semantic interpretation of a flow as "accumulation," "distribution," "shakeout," etc. These labels are absent from the codebase by design.
- **Free-form narrative.** [Event Chain Reconstruction](#layer-8--causal-inference-layer-phase-15) (`narrative_causality`) is a deterministic template composed from already-published layer outputs. No model calls, no language generation, no inference of a market story.

If a verdict, edge, or score appears without one of the upstream factors above being either present-and-measurable or explicitly flagged INSUFFICIENT / UNDER_EVIDENCED, treat it as a bug, not a feature.

---

## 12. Validation framework — calibration backlog

**Status:** this section is a **calibration backlog**, not a results report. Every entry below is a measurement that **has not been run yet**. No numbers in this section are real — they are placeholders for future calibration passes. Do not cite figures from this section.

### 12.1 What "validation" means here

For an observability platform with no trading actions, validation is the measurement of:

- **Replay reproducibility** — does a FROZEN snapshot match a fresh LIVE reconstruction at the same anchor, field-by-field?
- **Verdict survival** — when a layer commits a verdict at time T, does it still hold at T + Δ on the same window?
- **Confidence calibration** — does a HIGH-confidence output empirically outperform LOW-confidence on the same downstream metric?
- **Threshold stability** — do interim constants (`CREDIBLE_BAND_PCT`, `DEPTH_COLLAPSE_DROP`, `KYLE_MIN_BUCKETS`, `RECOVERY_FRACTION`, etc.) hold across regimes?

### 12.2 Pending calibration measurements

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
| Realized-vs-predicted divergence distribution | per-bucket median + IQR of `divergence_bps` across `exec_impact` events; cross-tabbed by `book_exhausted` | Aggregate from already-emitted ExecEvent stream | **not yet measured** — see [project_exec_impact_layer](project_exec_impact_layer.md): platform still in pure observation mode |
| Credible-depth anti-spoof empirical test | correlation between (Credible Depth at t) and (realized executable depth implied by `exec_impact.realized_bps` at t+ε) | Requires the realized-vs-predicted aggregation above | **not yet measured** — depends on previous item |
| Threshold stability under regime change | how often a threshold-crossing flips on sub-minute volatility | Sliding-window count of state transitions on the same input within `FLICKER_WINDOW` | exists for `flicker_ratio` in `market_state_transitions`; not generalized across layers |

### 12.3 Suppression and demotion rules already in code (operational falsification)

While calibration measurements above are pending, the codebase already encodes explicit demotion paths. These are listed so a reader knows the platform falsifies *something* before they go looking for the calibration sheet:

- `causal_propagation` verdict downgrade chain: DIRECTIONAL → AMBIGUOUS (asymmetry < 0.40) → UNDER_EVIDENCED (evidence_count ≤ 1) → COMMON_DRIVEN → EXPLORATORY (data_quality ∈ {INSUFFICIENT, LOW}) → COINCIDENCE (sym_penalty ≥ 0.70).
- `market_state_transitions` lifecycle: PERSISTENT → ACCELERATING / FLICKER / REVERSED; REVERSED applies a `reversal_factor = 0.25` multiplicative penalty.
- `crisis_genesis` scarcity cap: > 3 probes INSUFFICIENT → verdict floored at EARLY_DISTORTION.
- Pattern discovery: 6 robustness flags (SINGLE_WINDOW, LOW_RECURRENCE, HIGH_LIFT_LOW_SUPPORT, REGIME_FRAGILE, BUCKET_SENSITIVE, LOW_SUPPORT) each apply a fixed multiplicative penalty to `stability_score`.
- Forecast: `cap_factor = 0.5` whenever slope or extrapolation was clipped.
- Sanity audit `propagation_loop` finding feeds `discovery_suppression_modifier = 0.50` when sanity is CRITICAL — the adaptation loop currently halves recommendation importance.

### 12.4 What an operator can verify today (without the calibration backlog)

Without the measurements above, the layer's outputs are still inspectable on five concrete properties:

1. **Published decomposition.** Every verdict carries its per-factor inputs; a reader who disagrees can locate the factor that drove the outcome.
2. **Explicit absence states.** INSUFFICIENT · UNDER_EVIDENCED · PRUNED · `book_exhausted = True` · `None`-return are first-class verdicts, not suppressed errors.
3. **Code-level demotion paths.** §12.3 enumerates demotion chains — they are in code, not in policy.
4. **Acyclic dependencies.** §8 — `adaptation_state` reads from observed layers but never writes back.
5. **Bounded modifiers.** `ADAPTATION_BOUNDS` clips every coefficient; nothing compounds without limit.

When the calibration backlog above is run, this section will move from "framework" to "framework + measured results" — see the entries marked **not yet measured** for what is missing.

---

## 13. Propagation & causality limits

This section enumerates the epistemic ceiling of the propagation / causal layer. It is **complementary** to [§11 Non-inference boundaries](#11-non-inference-boundaries) (which lists what the platform refuses to infer at all) by stating *how far the propagation layer is allowed to go on the data it actually has*. Every constraint below maps to existing code — no new behavior, only an explicit reading of what `propagation_graph` and `causal_propagation` are licensed to claim.

### 13.1 The load-bearing invariant

> **Propagation edges represent repeated lagged association under observed conditions. They do not establish causal certainty.**

When the layer publishes an edge A → B with `confidence = HIGH`, the literal meaning is: across the lookback window, B's alerts repeatedly started ≥ 5 s and ≤ 30 min after A's, with stable lag, on independent sub-windows, with no common-driver candidate found among observed symbols, and not as a bidirectional mirror. That is what the formula measures. It is not a claim that A *caused* B in any market-microstructure sense — only that the timestamps line up that way, repeatedly, under the conditions the data exposes.

### 13.2 Epistemic tiers (a reading of existing verdicts)

The TZ-requested tier framework maps onto the existing verdict enum without changing it. The tiers are an interpretation layer for documentation and operator UI; the engine keeps emitting the same code-level verdicts.

| tier | claim shape | maps to existing verdicts |
|---|---|---|
| **T0 — Temporal adjacency** | "events occurred near each other in time" | dropped pairs (lead < `min_lead_ms`); also any unfiltered `propagation_graph` candidate before scoring |
| **T1 — Stable lag association** | "B followed A with measurable lag across the window" | `propagation_graph` edge with `lead_clarity > 0` and `lead_consistency > 0` |
| **T2 — Conditional propagation candidate** | "T1 + repeated across sub-windows + common-shock screen survived + not mirror" | `propagation_graph` edge with `confidence_score ≥ 0.45` (MEDIUM/HIGH label) — but **before** the causal layer's verdict |
| **T3 — Observational propagation** | "B consistently followed A under observed conditions" (the strongest claim the layer is licensed to make) | `causal_propagation` verdict = DIRECTIONAL |

The tier ladder never reaches "A caused B." T3 is the ceiling, and T3 is still observational, conditional, and refutable — every input that drove a T3 verdict is published with the verdict and can be re-checked or re-disputed.

### 13.3 Simultaneity hardening (already in code)

`propagation_graph` uses `min_lead_ms = 5_000` ms as a hard pre-scoring drop, not as a penalty. The reasoning, made explicit:

- Alerts are timestamped at coarse granularity relative to actual transmission. Within ~5 s, the timestamps do not carry enough resolution to identify a first mover.
- "First-mover" assignment on sub-`min_lead_ms` pairs is therefore **structurally unknowable** — not low-confidence, not uncertain, but unknowable from the data we have.
- Dropping rather than penalizing is the right move: a penalized score is still a score, and a score still appears in the graph. A dropped pair leaves no edge at all, which is the only representation consistent with "first mover is unknowable below this lag."

Generalized rule, for any future propagation layer added to this codebase: `if observed_lag ≤ effective_sampling_resolution: propagation_claim = invalid`. The current resolution proxy is the WS sampler cadence (1 Hz) plus the alert-engine M5-boundary alignment — `min_lead_ms = 5_000` is the conservative envelope around both.

### 13.4 Common-shock aggression (already in code, made explicit)

The codebase already aggressively suppresses propagation claims when a shared driver is plausible. Documented here so the suppression is auditable:

| trigger | code-level effect |
|---|---|
| `symmetry_penalty = (min/max reverse_count)² ≥ 0.70` | verdict forced to **COINCIDENCE** before any other check |
| common-driver candidate found (a symbol whose alerts preceded both A and B on the same windows) | `common_driver_factor = 0.35` multiplicative penalty on `causal_confidence` AND verdict forced to **COMMON_DRIVEN** |
| `data_quality ∈ {INSUFFICIENT, LOW}` (sparse evidence) | verdict forced to **EXPLORATORY** regardless of how clean the headline numbers look |
| `evidence_count ≤ 1` (the pair survived in only one sub-window) | verdict forced to **UNDER_EVIDENCED** |
| `asymmetry < 0.40` | verdict forced to **AMBIGUOUS** |

The verdict-priority order in §3 [Causal propagation](#causal-propagation-phase-15-1) is structured so that **refusal verdicts evaluate first**. A clean DIRECTIONAL is the residue after every refusal path was rejected, not the default.

### 13.5 Causal refusal conditions

The layer **refuses to publish a directional propagation verdict** when any of the following hold. Each maps to a specific code path:

- **Sub-resolution lag.** `observed_lag < min_lead_ms` → pair dropped before scoring (§13.3).
- **Insufficient episodes.** `evidence_count ≤ 1` → UNDER_EVIDENCED.
- **Lag instability.** `lead_consistency < threshold` → `causal_confidence` decays multiplicatively; if data_quality is borderline, the verdict drops to EXPLORATORY.
- **Common-shock contamination unresolved.** `find_common_driver()` returned a candidate → COMMON_DRIVEN.
- **Bidirectional mirror.** `symmetry_penalty ≥ 0.70` → COINCIDENCE.
- **Data scarcity.** `data_quality ∈ {INSUFFICIENT, LOW}` → EXPLORATORY (the layer refuses to commit on thin evidence even if everything else looks clean).
- **Replay unavailable.** Pre-activation windows → `data_quality = PRUNED/INSUFFICIENT`; reconstruction does not invent edges.
- **Timestamp drift detected** *(not currently auto-detected — see [§10.2](#102-exchange--transport-failures-not-handled--known-blind-spots))*. If a future detector flags drift, propagation output must be marked structurally suspect for the affected window.
- **Synchronized global move overlap** *(blind spot per [§10.3](#103-market-structure-failures-not-handled--out-of-scope))*. Liquidation-cascade windows are not currently detected as such; in their presence, the `anomaly_synchronization` probe of Distributed Stress Detection is the closest counterweight, but propagation edges from those windows should be read with extra skepticism. Documented as a known blind spot rather than handled.

### 13.6 Structurally unknowable conditions

The following are **not** uncertainties to be reduced by more data — they are properties the data structurally cannot resolve. They are flagged with their own status rather than degraded confidence:

| condition | flag / state |
|---|---|
| Per-frame transmission order on a `propagation_graph` edge | `structurally unknowable` (already in §1 Layer 12 description) — edges aggregate over the window, no timestamped pair data is carried |
| First mover within `min_lead_ms = 5 s` window | not represented as an edge at all (drop, not flag) |
| Whether a common-shock candidate is real macro or coincident burst | the layer flags COMMON_DRIVEN; it does not attempt to classify the driver |
| Simultaneity vs sub-resolution lag | indistinguishable below `min_lead_ms`; the layer does not try |
| Causal vs anti-causal direction when `symmetry_penalty ≈ 1` | indistinguishable; COINCIDENCE applies |
| Pre-activation replay windows | `data_quality = INSUFFICIENT/PRUNED`; no reconstruction |

### 13.7 UI / language discipline for the propagation layer

The 2026-05-24 Attention Pass (see §1 Attention & Trust Simplification) already softened the highest-risk labels. Documented here so future UI work doesn't regress:

| previously | now (in `frontend/src/lib/labels.ts`) |
|---|---|
| `DIRECTIONAL` | "directional pattern (lead-lag)" |
| `dominant_driver` | "candidate driver" |
| `AMPLIFIER` | "appears in chains" |
| `LEADER` | "appears as leader (candidate)" |
| `PRE_CASCADE` | "pre-cascade conditions present" |

Vocabulary that should **not** appear in any future UI or copy for the propagation / causal surfaces:

| avoid | preferred |
|---|---|
| "source asset" | "candidate leader" / "observed lead in pair" |
| "origin node" | "highest out-degree node" / "lead-side node" |
| "stress transmission" | "co-stressed cluster" / "synchronized deterioration" |
| "cascade origin" | "earliest observed event in cluster" |
| "trigger asset" | "candidate driver" |
| "the market transmitted X" | "B followed A under observed conditions" |
| "A caused B" | "B lagged A with stable measurable interval" |

### 13.8 What the propagation layer does and does not do

The propagation layer **measures**: pair counts of A→B alert sequences, lag distributions, sub-window survival, mirror-pair ratios, common-driver candidates among observed symbols.

The propagation layer **does not measure** and **does not infer**: true economic causality, participant intent, hidden coordination, transmission certainty, directional influence under unresolved simultaneity, hidden actor identity, macro-driven co-stress without an observable common-driver symbol in the dataset, off-exchange flow that drives both endpoints.

If an operator is reading a propagation surface as evidence of causation in the strong sense, they are reading past the layer's published epistemic ceiling. The layer's job is to make that reading harder; the operator's discipline closes the rest of the gap.

---

## 14. Distributed Stress — quantitative state machine

This section reads the [Distributed Stress Detection layer](#layer-8--causal-inference-layer-phase-15) (engine code: `crisis_genesis()` — [`research.py:6221`](shared/kazus_logic/liquidity/research.py#L6221)) as a **strict multi-factor stress-state engine** rather than a "crisis" intuition. Every field, threshold, and status state below maps to actual code. Anything that the TZ asks for and the code does not yet implement is called out explicitly with **NOT IMPLEMENTED** so future readers do not mistake aspiration for current behavior.

### 14.1 What the layer measures (current implementation)

Per-window output of `crisis_genesis(db, lookback_days=7)`:

| field | type | meaning |
|---|---|---|
| `verdict` | enum | CALM / EARLY_DISTORTION / ELEVATED_RISK / PRE_CASCADE / INSUFFICIENT |
| `genesis_score` | float [0, 100] | mean of *contributing* probe scores; equal-weighted |
| `confidence` | float [0, 1] | `contributing_probes / 7` |
| `probe_count` | int = 7 | always; non-contributing probes still appear with `status = "insufficient"` |
| `hot_count` / `elevated_count` / `calm_count` / `insufficient_count` | int | distribution over 7 probes |
| `probes[]` | list[dict] | full per-probe decomposition — see §14.3 |
| `summary` | string | deterministic, verdict-shaped sentence |

The verdict is **point-in-time**, recomputed on each call (300 s cache TTL via `_ttl_cached`). The layer is **not** event-driven; it does not emit start/peak/end markers for a sustained stress episode.

### 14.2 State-name mapping (documentation overlay)

The TZ asks for a NORMAL → ELEVATED → STRESSED → DISTRESSED state machine. The engine emits an existing 5-state enum that maps to those names without renaming code:

| documentation state | code verdict | code condition |
|---|---|---|
| **NORMAL** | `CALM` | `genesis_score < 25` |
| **EARLY STRESS** | `EARLY_DISTORTION` | `genesis_score ∈ [25, 50)` OR scarcity-capped from above |
| **ELEVATED** | `ELEVATED_RISK` | `genesis_score ∈ [50, 75)` |
| **DISTRIBUTED STRESS** | `PRE_CASCADE` | `genesis_score ≥ 75` AND `hot_count ≥ 3` |
| **UNKNOWN** | `INSUFFICIENT` | `contributing_probes == 0` |

Scarcity cap: if `insufficient_count > 3` (more than half of seven probes blind), the verdict is **floored at EARLY_DISTORTION** regardless of score. ELEVATED and DISTRIBUTED STRESS verdicts therefore require at least four contributing probes.

The PRE_CASCADE / DISTRIBUTED STRESS verdict additionally requires `hot_count ≥ 3` — three independent probes individually firing at status `hot` (probe score ≥ 65). A single hot probe at 100/100 cannot promote the verdict beyond ELEVATED_RISK. This is the closest existing analogue to the TZ's "synchronized deterioration" requirement.

### 14.3 Probe-level decomposition (what is published)

Every published verdict carries `probes[]` — a list of 7 entries, each shaped:

```
{
  "kind":          str,    # one of the 7 probe kinds (§14.4)
  "name":          str,    # short human-readable label
  "score":         float,  # [0, 100], or 0.0 when insufficient
  "status":        str,    # "calm" (< 30) | "elevated" (< 65) | "hot" (≥ 65) | "insufficient"
  "rationale":     str,    # one-sentence explanation including the metric values used
  "metric_value":  any,    # the underlying raw value (ratio, slope, delta, etc.)
  "contributes":  bool,   # False ⇔ status == "insufficient"
}
```

The probe `status` thresholds (`< 30 calm`, `< 65 elevated`, `≥ 65 hot`) are encoded in [`_crisis_probe()`](shared/kazus_logic/liquidity/research.py#L6182) — they are **fixed constants in code**, not per-probe percentiles. Recalibrating them would change the meaning of `hot_count`. Currently uncalibrated against any labelled corpus — see §12 calibration backlog.

### 14.4 The seven probes (component decomposition)

| probe `kind` | source layer | what is measured | insufficient gate |
|---|---|---|---|
| `fragmentation_growth` | `liquidity_intelligence_history.coordinated_state` | distinct coordinated_state values in last 24 h vs prior 24 h, ratio mapped 1.0× → 0, 2.5× → 100 | no prior-24h baseline → insufficient |
| `resiliency_decay` | `liquidity_samples` metric `resiliency_score` | Δ between recent-6h avg and prior-6h avg; mapped `−delta × 4` clipped to [0,100] | `n < 20` either side |
| `propagation_widening` | `propagation_graph` output | `integrity_score` dropping, weak/symmetric-edge share rising | propagation result missing or scarcity-gated |
| `dependency_concentration` | `structural_dependencies` output | top dominant-driver's out-degree share of the network | upstream INSUFFICIENT |
| `anomaly_synchronization` | `liquidity_anomaly_memory` write rate | rate acceleration of anomaly writes | empty memory in baseline window |
| `transition_instability` | `market_state_transitions` aggregates | `flicker_ratio` + oscillation_periods | upstream INSUFFICIENT |
| `stress_acceleration` | composite stress slope vs baseline | slope-of-slope on synthesized_stress | < 2 history points |

The composite is `genesis_score = sum(contributing.score) / count(contributing)` — **plain mean, no weights**. The TZ asks for weighted decomposition; **weights are NOT implemented**. Each probe contributes equally when it contributes at all. Future weighting would require calibration against a labelled corpus of stress vs non-stress windows; documented as pending in [§14.10](#1410-not-yet-implemented--calibration-backlog).

### 14.5 Refusal-first verdict order

The composite verdict is computed in priority order — refusal paths evaluate first:

```
1.  contributing == 0                          → INSUFFICIENT          (full refusal)
2.  insufficient_count > 3                     → floor at EARLY_DISTORTION
                                                  (scarcity cap, applied BEFORE score check)
3.  genesis_score ≥ 75 AND hot_count ≥ 3       → PRE_CASCADE
4.  genesis_score ≥ 50                         → ELEVATED_RISK
5.  genesis_score ≥ 25                         → EARLY_DISTORTION
6.  else                                       → CALM
```

A PRE_CASCADE verdict is the residue after (i) at least 4 probes contributed, (ii) `genesis_score ≥ 75`, AND (iii) at least 3 distinct probes individually crossed the `hot` threshold. It is not the default reading of "things look bad" — it is the residue of a four-step refusal ladder.

### 14.6 Common-shock suppression (probe-level, NOT composite-level)

The composite layer does **not** run its own common-shock detector. Suppression is inherited from the probes that themselves suppress shared drivers:

- `propagation_widening` inherits the `common_driver_factor = 0.35` multiplicative penalty and the COMMON_DRIVEN / COINCIDENCE refusal verdicts from [`causal_propagation`](#causal-propagation-phase-15-1) (see §13.4). If propagation is contaminated by a common driver, its `integrity_score` is already discounted before the probe reads it.
- `dependency_concentration` reads `structural_dependencies`, which itself composes causal verdicts — same inheritance path.
- `transition_instability` uses `flicker_ratio` which already represents instability, not magnitude — it tends to **rise** on real regime jitter and **stays flat** on synchronized clean moves.

**Known blind spots** (NOT IMPLEMENTED at composite level):

- **Synchronized liquidation burst.** A market-wide liq cascade can make `fragmentation_growth`, `resiliency_decay`, `anomaly_synchronization`, and `stress_acceleration` all fire simultaneously. The layer correctly registers this as multi-probe heat — but does not distinguish it from a genuinely-distributed deterioration. Documented in [§10.3](#103-market-structure-failures-not-handled--out-of-scope).
- **Macro BTC-beta move.** All-symbol price moves driven by a shared exogenous catalyst show up identically to distributed liquidity stress in the current probe set.
- **Funding-reset windows / exchange-wide outage.** No probe explicitly detects these; they will surface as probe firings without contextual annotation.

Operator-side mitigation: the per-probe `rationale` string includes the metric values used. An operator who reads the rationale can identify a synchronized-shock signature manually. There is currently no automated downgrade for these patterns.

### 14.7 Persistence and hysteresis — NOT IMPLEMENTED

The TZ requests entry/exit threshold asymmetry, minimum hold time, cooldown, and demotion-after-M-windows hysteresis. None of this exists at the verdict layer:

- `crisis_genesis` is **stateless** between calls. Consecutive calls 300 s apart may flip CALM ↔ EARLY_DISTORTION ↔ ELEVATED_RISK with no persistence requirement.
- There is no `entry_threshold` vs `exit_threshold` asymmetry. The same `25 / 50 / 75` boundaries apply on the way up and on the way down.
- There is no minimum-hold period before a verdict promotes.
- There is no cooldown that prevents repeated re-promotion.

**Existing partial counterweights** (probe-level, not composite-level):

- `resiliency_decay` is **window-averaged** over 6 h, so it cannot jitter on a single tick.
- `stress_acceleration` requires ≥ 2 history points, also windowed.
- `anomaly_synchronization` uses a rate baseline, smoothed.
- `transition_instability`'s `flicker_ratio` is itself a jitter measure — high values *signal* that downstream verdicts could be unreliable.

The operator-facing **flicker → adaptation** path (the [Attention Pass](#attention--trust-simplification-pass-2026-05-24-presentation-only) chronic-vs-new bucketing in `frontend/src/lib/labels.ts`) is the closest current substitute for hysteresis at the **presentation layer** — a verdict that has been PERSISTENT for many cycles renders with muted color rather than full saturation. This is presentation, not engine-level hysteresis.

Promoting hysteresis to engine-level is a clean P2 item — see [§14.10](#1410-not-yet-implemented--calibration-backlog).

### 14.8 Cross-venue confirmation — NOT INTEGRATED into the verdict

[Cross-venue divergence](#97-cross-venue-divergence-crossex) exists as a separate API surface (`/crossex/{symbol}`, Binance reference, Bybit comparison) but is **not** read by `crisis_genesis`. The verdict does not have a cross_venue_status field.

The TZ-proposed cross_venue enum (CONFIRMED / LOCAL_ONLY / CONTRADICTED / UNAVAILABLE / INSUFFICIENT) is **NOT IMPLEMENTED**. Currently:

- A PRE_CASCADE verdict makes no claim about whether the stress is venue-local or cross-venue confirmed.
- Bybit-side stress is not surfaced into the probes — Binance is the only WS source for [Resiliency Score](#92-resiliency-score-resiliency_score), [Credible Depth](#91-credible-depth-credible_depth), [Impact Score](#93-impact-score-impact_score--kyle-λ-sigmoid), and [Fragility Score](#94-fragility-score-fragility_score). The `resiliency_decay` probe sees only Binance-derived resiliency_score values.
- The platform should currently be read as a **Binance-centric stress detector** with Bybit as an ad-hoc divergence check on the per-symbol detail modal only.

This means any "distributed stress" claim is **structurally** Binance-spot/perp-distributed, not cross-venue-distributed. The absence is documented as a present limitation rather than represented by an UNAVAILABLE flag on a field that does not exist.

### 14.9 Event scope distinction

The TZ asks the layer to distinguish LOCAL / VENUE-LOCAL / CROSS-ASSET / CROSS-VENUE-CONFIRMED / DISTRIBUTED scopes. Current implementation:

| TZ scope | how the current layer represents it |
|---|---|
| LOCAL (single symbol) | not the layer's level — single-symbol stress shows up in `/metrics/{symbol}` and the operator queue, not in `crisis_genesis` |
| VENUE-LOCAL (multi-symbol, one venue) | this is the layer's *default* scope — see §14.8. The verdict is implicitly venue-local without saying so |
| CROSS-ASSET (multi-symbol, cross-cluster) | partially captured by `dependency_concentration` (top driver's reach) + `propagation_widening` — but not labelled as such on the output |
| CROSS-VENUE-CONFIRMED | NOT IMPLEMENTED — see §14.8 |
| DISTRIBUTED STRESS | maps to PRE_CASCADE (genesis_score ≥ 75 AND hot_count ≥ 3) — but again, structurally venue-bound |
| UNKNOWN | `INSUFFICIENT` verdict |

Adding an explicit `scope` field to the verdict payload is documented as pending. Until then, the `probes[]` decomposition is the audit trail an operator must read to determine scope manually.

### 14.10 NOT YET IMPLEMENTED — calibration / hardening backlog

Items the TZ requests that are not in the codebase. Listed here so a future reader cannot mistake the documentation for a description of current behavior.

| item | status | rationale |
|---|---|---|
| Weighted probe aggregation (Aggregate Stress Score with explicit per-component weights) | NOT IMPLEMENTED | Composite is plain mean of contributing. Weighting requires calibration against labelled stress windows — see §12 |
| Engine-level persistence / hysteresis (entry vs exit thresholds, min hold time, cooldown) | NOT IMPLEMENTED | Layer is stateless between calls. Presentation layer's chronic-vs-new bucketing is a partial substitute |
| Cross-venue confirmation field on verdict | NOT IMPLEMENTED | `/crossex` exists but is not read by `crisis_genesis` |
| Explicit event lifecycle (event_id / started_ts / peak_ts / ended_ts) | NOT IMPLEMENTED | Layer is point-in-time, not event-stream. `liquidity_anomaly_memory` carries persisted anomalies, but they are recorded by a separate writer and not tied to a stress-event lifecycle |
| Scope field (LOCAL / VENUE-LOCAL / CROSS-ASSET / CROSS-VENUE-CONFIRMED) | NOT IMPLEMENTED | Operator must read `probes[]` decomposition to infer scope |
| Composite-level common-shock detector (synchronized-liquidation / macro-beta / funding-reset / outage-window) | NOT IMPLEMENTED | Probe-level inheritance exists for `propagation_widening` and `dependency_concentration` only |
| Timestamp-drift detector | NOT IMPLEMENTED (already noted in §10.2 and §13.5) |
| Refusal annotation in verdict payload (which refusal-ladder step rejected the higher verdict) | NOT IMPLEMENTED | The verdict and the `summary` string carry the residue, but not the refusal trace. An operator can infer it from `insufficient_count`, `hot_count`, and `genesis_score` |

**Pending measurements** (in addition to §12 generic backlog):

- Stress-event recurrence rate per regime
- False-positive rate of PRE_CASCADE vs forward-realized market behavior
- Verdict transition stability (CALM ↔ EARLY_DISTORTION jitter rate)
- Stress persistence distribution (how long PRE_CASCADE typically holds)
- Per-probe contribution rate (already partially trackable via `insufficient_count`)
- Per-probe agreement matrix (do multiple probes correlate, or are they independent?)

All marked **PENDING MEASUREMENT** — no numbers in this document.

### 14.11 UI / language discipline for the stress layer

Already applied in the [Attention Pass](#attention--trust-simplification-pass-2026-05-24-presentation-only): `PRE_CASCADE` renders as "pre-cascade conditions present", not "crisis detected". Continuing the discipline:

| avoid | preferred |
|---|---|
| "crisis origin" | "first probe to fire `hot`" / "earliest contributing probe" |
| "market breakdown started here" | (does not apply — the layer is point-in-time) |
| "systemic collapse detected" | "DISTRIBUTED STRESS verdict (4+ probes contributing, ≥ 3 hot)" |
| "source of crisis" | (does not apply — composite has no single source) |
| "root cause" | (does not apply) |
| "contagion path" | "propagation graph at this window" |
| "the market entered crisis" | "verdict promoted from ELEVATED_RISK to PRE_CASCADE" |
| "X caused the cascade" | "X was the earliest probe to reach `hot` status" |

The verdict label `PRE_CASCADE` is itself the most aggressive copy the layer is licensed to publish; even that is hedged in the UI as "pre-cascade conditions present" rather than "cascade in progress."

### 14.12 What the stress layer does and does not do

The layer **measures**: 7 independent probe scores from already-published upstream metrics, their distribution (hot / elevated / calm / insufficient), the contributing fraction, and a point-in-time verdict over the composite.

The layer **does not measure** and **does not infer**: future market direction, the originating asset of a stress episode, the strategic objectives of any participant, cross-venue confirmed stress (§14.8), event-level lifecycle timing (§14.10), or causal transmission between probes — the probes are independent measurements over the same time window, not a causal chain.

If a downstream consumer (operator UI, alert routing, investigation auto-draft) reads the PRE_CASCADE verdict as a market forecast rather than as the multi-probe residue described in §14.5, the consumer is reading past the layer's epistemic ceiling. The auto-draft path explicitly says so: investigations created from PRE_CASCADE are labelled `kind = auto_draft` and inherit the experimental status of their inputs.

---

## 15. Phrase compression reference

This table is the standing reference for any future addition to this document, to UI copy, or to any companion doc. The left column is a phrase that **sounds strong but carries no operational contract**. The right column is the substitute that makes the same claim verifiable. If a contributor cannot point at a code path that justifies a phrase, they should reach for the right column.

This table replaces what an earlier draft of this document called "operational trustworthiness" with an explicit mapping from rhetoric to behavior.

| avoid (no operational contract) | use instead (verifiable) |
|---|---|
| institutional-grade | replay-consistent measurement pipeline with append-only sample history |
| forensic-grade | append-only deterministic as-of replay against retained history tables |
| production-grade | depended-on for daily operation; covered by `/admin/runtime-health` |
| operator-grade | persisted operator workflow with replay-linked findings (`operator_priority_*`) |
| research-grade | best-current-estimate; gated by `data_quality` and decomposition-published |
| world-class / elite / advanced / high-end | *delete* |
| operational maturity / discipline (as a noun) | the specific code path that enforces the behavior |
| execution intelligence | execution-impact measurement and replay |
| stress intelligence | distributed-stress detection (7-probe composite) |
| causal intelligence | cross-asset lag measurement layer |
| market intelligence | aggregated measurement outputs |
| deep observability | replay visibility · trace inspection · `data_quality` per surface |
| smart-money / hidden actors | (do not infer; see §11) |
| structural certainty | recurrence-confirmed under validation gates |
| structural deterioration | synchronized multi-symbol deterioration above configured thresholds |
| systemic | multi-symbol AND multi-window AND data_quality ≠ INSUFFICIENT |
| systemic deterioration | distributed stress (§14.2) with `hot_count ≥ 3` |
| the system understands / sees / knows / interprets | the layer measures / validates / suppresses / rejects / emits |
| the platform believes | the verdict is computed from these inputs |
| forensic reconstruction | as-of reconstruction with per-surface `data_quality` |
| microstructure observability | the realtime tier — Credible Depth, Resiliency, Kyle λ, Exec-Impact (§9) |
| liquidity intelligence | the §9 metric set + §3 aggregator set |
| memory over reactivity | `liquidity_anomaly_memory` is the persisted basis for any historical claim |
| silence over hallucination | layers return INSUFFICIENT / `None` when validation gates fail (§14.5, §3) |
| honest uncertainty | data_quality gating + published decomposition |
| crisis detected | verdict promoted to PRE_CASCADE (§14.5 conditions) |
| crisis genesis | Distributed Stress Detection (engine code: `crisis_genesis`) — see Terminology preamble |
| narrative causality | Event Chain Reconstruction (engine code: `narrative_causality`) |
| operator attention is finite | the Attention Pass concretely demoted chronic items to muted color (§1) |
| truth engine / truth layer | the specific layer + the data_quality state it publishes |
| operator-facing observability | operator queue + replay + investigations (Phases 17 / 18 / 19) |

**The test, when in doubt:** would removing the phrase make any sentence in this document *false*? If not, the phrase was decoration. Remove it or replace it with the column on the right.
