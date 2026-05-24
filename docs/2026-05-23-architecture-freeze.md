# Architecture Freeze — kazus-global Liquidity Intelligence Platform

**Snapshot date:** 2026-05-23
**Status:** stable-core / experimental split documented below
**Scope:** complete system audit for handoff, ops recovery, and continued development without losing architectural intent

This document is meant to be read **cold**, by someone who has never seen the codebase, and to be sufficient for them to (a) operate the system, (b) extend it without breaking it, or (c) recover it from scratch.

---

## Contents

1. [Architecture map — layer by layer](#1-architecture-map)
2. [Data lineage](#2-data-lineage)
3. [Formula registry](#3-formula-registry)
4. [Endpoint inventory](#4-endpoint-inventory)
5. [Table inventory](#5-table-inventory)
6. [Operator workflow guide](#6-operator-workflow-guide)
7. [Known risks & backlog](#7-known-risks--backlog)
8. [Architecture freeze — stable core vs experimental](#8-architecture-freeze)

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

These are intelligence aggregators that **synthesize** across the lower layers. All are in `research.py`.

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

### Layer 6 — Memory & Anomaly Genealogy

| | |
|---|---|
| Tables | `liquidity_anomaly_memory` + `liquidity_anomaly_edges` |
| Purpose | Persistent record of structural anomalies + typed edges between them (caused_by / evolved_into / historically_similar / preceded / destabilized / stabilized) |
| Writer | Worker `anomaly_recorder` task (300s cadence) |
| Readers | `anomaly_lineage`, `memory_graph`, `crisis_evolution_tree`, `regime_ancestry`, `edge_lineage`, `crisis_clusters`, `narrative_chronicle` |
| Retention | 180d (Pass-A retention layer) |

### Layer 7 — Discovery (data-driven mining)

| function | finds | TTL |
|---|---|---|
| `discover_patterns(db, since_ms, min_support, bucket_minutes)` | Recurring (metric-tertile) signatures → downstream alert rates; emits `effective_lift`, `stability_score`, `pattern_confidence`, `robustness_flags`, `suppressed_reason` | 300s |
| `crisis_archetypes(db, max_archetypes)` | Anomaly-memory clusters → archetype labels | — |
| `hidden_regimes(db, lookback_days, max_clusters)` | Clusters in engine-state space (intelligence_history) | 300s |
| `propagation_graph(db, lookback_days, lead_window_ms, min_lead_ms)` | Symbol→symbol pair edges with `confidence_score` decomposition + `integrity_components`; returns `all_symmetric_pairs` for sanity loop check | 300s |

### Layer 8 — Causal Intelligence (Phase 15)

| function | does | TTL |
|---|---|---|
| `causal_propagation(db, lookback_days, n_windows)` | Per-pair verdict over 4 tests: asymmetry · multi-window persistence · common-driver elimination · scarcity gate. Verdicts: DIRECTIONAL · COMMON_DRIVEN · COINCIDENCE · UNDER_EVIDENCED · AMBIGUOUS · EXPLORATORY | 300s |
| `structural_dependencies(db, lookback_days)` | Composes causal verdicts into 4 structural findings: influence chains · dominant drivers · co-driver clusters · synchronized stress groups | 300s |
| `market_state_transitions(db, lookback_days)` | Per-transition verdict + lifecycle: PERSISTENT · ACCELERATING · FLICKER · REVERSED; aggregates: flicker_ratio, oscillation_periods, transition_rate | 300s |
| `crisis_genesis(db, lookback_days)` | 7-probe composite: fragmentation_growth · resiliency_decay · propagation_widening · dependency_concentration · anomaly_synchronization · transition_instability · stress_acceleration → verdict CALM/EARLY_DISTORTION/ELEVATED_RISK/PRE_CASCADE/INSUFFICIENT | 120s |
| `narrative_causality(db, lookback_days)` | Deterministic 5-section narrative; template-built (no model calls); explicit confidence per section + "what we don't know" | 120s |

### Layer 9 — Feedback & Adaptation (Phase 16)

| | |
|---|---|
| Code | `adaptation_state(db, lookback_days)` in `research.py` |
| Purpose | Computes 5 bounded modifier coefficients with audit trail. Acyclic — reads but never writes back to observed layers |
| Modifiers | `narrative_confidence_modifier` [0.5,1.0] · `alert_sensitivity_modifier` [1.0,1.5] · `causal_strictness_modifier` [1.0,1.5] · `discovery_suppression_modifier` [0.5,1.0] · `global_trust_modifier` [0.5,1.0] |
| Real downstream wiring | `adapted_recommendations(db)` wraps `adaptation_recommendations` and applies `discovery_suppression_modifier` to every `importance_shift` |
| TTL | 120s |
| Reversibility | Pure read; turning the loop off = downstream stops reading the modifier |

### Layer 12 — Replay Intelligence (Phase 19, Pass A backend)

| | |
|---|---|
| Code | `investigation_replay_*` in `research.py` |
| Purpose | Forensic FROZEN-vs-LIVE replay of an investigation case |
| Tables | `investigation_replay_snapshots` (one row per case, UPSERT) |
| Capture | Auto-fires on `investigation_create` (kind=`auto_create` or `auto_draft`). Operator can recapture via `force=true` |
| State modes | `frozen` reads the opaque JSON snapshot; `live` reconstructs from `liquidity_intelligence_history` + `liquidity_alert_history` + `liquidity_anomaly_memory` + `operator_priority_*` tables. Same response schema, distinguished by `is_frozen` |
| Replay safety | Each reconstructed surface publishes its own `data_quality` ∈ HIGH/PARTIAL/INSUFFICIENT/PRUNED. PRUNED triggers when a window is past retention; the engine refuses to invent a value |
| Diff | Narrow semantic comparison at anchor: genesis verdict + score, sanity overall_state, adaptation modifier values (Δ ≥ 0.05), operator queue size + escalation counts, narrative headline. Every drift carries before/after/delta |
| Timeline | Scrubber keyframes — material events (operator_priority_events + alerts + anomalies + case lifecycle) inside a `[anchor − pre, anchor + post]` window |
| Propagation | Frame-bucketed alert-start counts per symbol over the case window + static propagation edges. Per-frame edge transmission is intentionally NOT inferred — propagation_graph doesn't carry timestamped pair data |

**Integrity Repair Pass (2026-05-24)** hardened four invariants under Layer 12 without expanding scope. Documented separately in [`docs/2026-05-24-operational-review.md`](docs/2026-05-24-operational-review.md) and addressed by:

* **Frozen snapshot is now APPEND-ONLY.** `investigation_replay_snapshots` has a `revision` (1..N per case) + `is_active` pointer; the old unique-on-investigation_id is gone in favor of unique-on-(investigation_id, revision). Recapture inserts a new revision and flips the prior to `is_active=False`; payloads are never destroyed. New surfaces: `GET .../replay/history`, `GET .../replay/state?revision=N`, `GET .../replay/diff/revisions?from=&to=`.
* **Diff semantics made explicit.** The diff response now always carries `comparison_mode ∈ {frozen_vs_now, frozen_vs_frozen}`; the UI labels the banner accordingly and no longer says "FROZEN vs LIVE". The cursor snapshot remains a separate panel — no fake cursor-diff is computed.
* **Retention-safe evidence linking.** `_investigation_link_evidence_inner` now auto-fetches the upstream row (per `evidence_type` dispatch: alert / anomaly / operator_priority) and stores it in `investigation_evidence.snapshot_json` at link time. `investigation_timeline` falls back to that snapshot when the upstream row has been pruned and tags the event with `is_pruned=True` so the operator sees the gap explicitly. Silent shrinkage of the case timeline is eliminated.
* **Async capture decoupling.** `investigations.capture_status ∈ {PENDING, CAPTURED, FAILED}` queue field. `investigation_create` sets PENDING; a new worker loop `investigation-capture` (30s cadence) drains the queue via `investigation_capture_pending(db)`. Case creation no longer holds the request on the 8-layer intelligence cascade. Failures are recorded (`capture_error`) and the operator can re-queue via `POST .../replay/retry`.

Pass B (Phase 19) lands the operator-facing UI on top of the Pass A endpoints, inside the INV drawer as a new lazy-mounted `replay` tab:

* **FROZEN vs LIVE diff banner** — prominent header that shows the count + per-field deltas (genesis verdict, sanity overall_state, adaptation modifier values, queue size + escalation counts, narrative headline). Color band escalates with drift count. Includes one explicit `recapture` button that is the only frontend write path mutating the frozen reference.
* **Scrubber** — SVG strip with click-to-seek, play/pause loop (`requestAnimationFrame` × speed factor; 1s wall ≈ 1m case time at speed=1, capped at window_end), step ±keyframe, prev/next critical-keyframe, jump-to-anchor, speed selector (0.5×–16×).
* **Overlay toggles** — operator_priority / alert / anomaly / case sources can each be hidden from the keyframe strip; severity-colored ticks (info/warn/critical).
* **Cursor snapshot** — toggle between live-reconstruction at cursor (debounced 250 ms refetch on cursor settle) and the frozen blob; live view surfaces per-section `data_quality` (HIGH/PARTIAL/INSUFFICIENT/PRUNED) explicitly.
* **State evolution mini-charts** — keyframe density + alert activity per bucket, derived from already-fetched timeline + propagation data. Vanilla SVG sparklines with a synchronized cursor line. No interpolation, no smoothing.
* **Propagation playback** — frame-bucketed per-symbol activation bars (current frame indexed by cursor); historical lead-lag edges from `propagation_graph` rendered as a static list, NOT animated per frame (no fake transmission order).

UX discipline: no cinematic effects, no glow, no auto-camera, no AI storytelling. The only moving element is the scrubber cursor. Every series and every overlay is sourced from already-fetched real data; missing surfaces stay missing (data_quality flagged) rather than being interpolated.

Performance: lazy-mounted (no fetches until the operator opens the `replay` tab); a single round-trip on mount (state + timeline + diff + propagation in parallel) plus debounced cursor-position fetches.

### Layer 11 — Investigation & Casework (Phase 18)

| | |
|---|---|
| Code | `investigation_*` functions in `research.py` |
| Purpose | Operator-owned forensic casework. Aggregates evidence + append-only notes + lifecycle history; renders causal trees, surfaces similar prior cases, exports audit-friendly markdown |
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

## 2. Data lineage

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
                       alert engine             metric aggregators       intelligence snapshot
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
                                                      crisis_genesis (7 probes)
                                                              │
                                                              ▼
                                                  narrative_causality (template)
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

Per-edge:

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

verdict (priority order):
  COINCIDENCE         sym_penalty ≥ 0.70
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

### Crisis genesis (Phase 15 #4)

```
genesis_score      = mean(probe.score for contributing probes)        ∈ [0, 100]
confidence         = contributing_probes / 7

scarcity cap:      verdict capped at EARLY_DISTORTION if > 3 probes INSUFFICIENT
verdict:
  PRE_CASCADE      score ≥ 75 AND hot_count ≥ 3
  ELEVATED_RISK    score ≥ 50
  EARLY_DISTORTION score ≥ 25
  CALM             score < 25
  INSUFFICIENT     no probes contributing
```

7 probes: see [Layer 8 table](#layer-8--causal-intelligence-phase-15).

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
| `/research/narrative-causality` | Deterministic narrative | <200 ms | 7 ms | 120s | DISC narrative | 🟢 |
| `/research/crisis-genesis` | 7-probe composite | <200 ms | 7 ms | 120s | DISC banner | 🟡 |
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

### Phase 19 — Replay Intelligence (Pass A)

| route | purpose | cold | warm | TTL |
|---|---|---|---|---|
| `POST /research/investigations/{id}/replay/capture` | Capture or recapture (`force=true`) the frozen snapshot | ~400 ms (composes all surfaces) | — | none |
| `GET .../replay/state?mode=frozen` | Return the opaque snapshot payload | <50 ms | — | none |
| `GET .../replay/state?mode=live&at_ms=…` | Reconstruct surface from history tables at `at_ms` | <150 ms | — | none |
| `GET .../replay/timeline` | Scrubber keyframes around the case anchor | <150 ms | — | none |
| `GET .../replay/diff` | FROZEN vs LIVE narrow semantic diff at anchor | <500 ms | — | none |
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

### Heavy / pre-Phase-14 intelligence

| route | purpose | cold | warm | TTL |
|---|---|---|---|---|
| `/research/synthesis` | 6-layer composite — **30 s cold** | 26-31 s | 7 ms | 300s |
| `/research/multi-horizon` | Multi-horizon outlook | 5 s | 6 ms | 300s |
| `/research/intelligence-history` | Recent snapshots | ~25 ms | — | — |
| `/research/structural-breaks`, `/risk-state`, `/regime-shift-warning`, `/meta-confidence`, `/meta-intelligence-health`, `/strategic-state` | Individual layers of synthesis | ms-range | — | — |

### Memory / lineage

| route | purpose | risk |
|---|---|---|
| `/research/anomaly-memory` (GET/POST) | Memory rows + insert | 🟡 if abused |
| `/research/anomaly-lineage/{id}` | BFS up to depth 3 | 🟢 (capped) |
| `/research/memory-graph` | Full nodes+edges | 🟡 (scales with memory) |
| `/research/crisis-evolution-tree`, `/regime-ancestry`, `/edge-lineage/{kind}` | Specific lineage queries | 🟢 |
| `/research/narrative-chronicle` | Memory→narrative timeline | 🟢 |

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
3. **Crisis genesis banner** — 7-probe composite for "pre-cascade structural distortion?". Probabilistic, never claims to predict.
4. **Adaptation loop banner** — 5 modifier coefficients; explains which downstream behavior is being suppressed/strengthened and why.
5. **Narrative causality panel** — deterministic 5-section paragraph composed from causal/structural/transition/genesis layers. Italic "what we don't know" section is always present.
6. **Causal Propagation** panel + **Structural Dependencies** panel + **State Transition Intelligence** panel — deeper drill-down.
7. **Pattern Discovery** + **Hidden Regimes** + **Crisis Archetypes** + **Memory Abstraction** — pre-Phase-15 mining layers.
8. **Intelligence Forecast** + **Adaptation Recommendations** + **Evolutionary Behavior** — projection / recommendation surfaces.

### Reading escalation levels

| label | meaning | what to do |
|---|---|---|
| **NORMAL** | Score < 25. Below the floor. | Nothing required. Visible if filter is `all`. |
| **WATCH** | 25 ≤ score < 50. Signal worth monitoring. | Note it. Don't act yet. |
| **IMPORTANT** | 50 ≤ score < 75. Material concern. | Investigate. Cross-check with sanity + crisis genesis. |
| **CRITICAL** | Score ≥ 75. System integrity / pre-cascade signal. | Diagnostic, NOT a trade signal. Read the rationale, then ack/resolve/escalate manually. |

**CRITICAL never means "buy" or "sell". It means "the engine is telling you something is structurally off; do not ignore."**

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

### P0 — fix before next intelligence phase

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

The downstream effect is `discovery_suppression_modifier = ×0.50` on adaptation recommendations. This is **the feedback loop working as designed** — sanity sees noise, adaptation halves its action confidence.

---

## 8. Architecture freeze

### Stable core (production-grade, deeply tested, low-churn)

These layers have been live with real data, audited under load, and the operator workflow above the stable core is depended-on for daily operation. **Treat changes here as high-risk; require commit-by-commit review.**

- **LIQ Scanner** + **Realtime WS Engine** + **Alert Engine** — the data-acquisition tier
- **`liquidity_samples` + `liquidity_alert_history`** — the immutable record
- **Sanity Audit** + **Runtime Health** — operational visibility
- **Research aggregators**: synthesis, multi-horizon, risk_state, structural_breaks, regime_shift_warning, meta_confidence, meta_intelligence_health, strategic_state, signal_reliability, transition_forecast
- **Operator Queue + Persistence** (Phase 17) — operator workflow now durable
- **Investigation lifecycle / persistence / append-only history** (Phase 18 Pass A & B core) — DB schema, CRUD, evidence linking, notes, lifecycle audit, markdown export
- **Retention loop** — protects against unbounded storage growth
- **Adaptation modifiers** (Phase 16) — bounded, explainable, reversible

### Experimental (working, exposed to operator, but treat outputs as research-grade)

These layers are valuable but their absolute numbers should be read as "best current estimate" not "truth". They are still useful for diagnosis and they degrade gracefully (every one of them has explicit scarcity gates). **Iterate freely here; just keep the safety properties.**

- **Causal Propagation** (verdicts, common-driver detection) — works, but DIRECTIONAL count = 0 on current data. Output stabilizes only after weeks of accumulated history.
- **Structural Dependencies** (chains, drivers, clusters, sync) — entirely scarcity-gated. Currently exploratory.
- **Market State Transition Intelligence** — emits real findings now (data_quality=MEDIUM), but `flicker_ratio` interpretation needs calibration over longer horizons.
- **Crisis Genesis Detection** — 7 probes, 4 currently contributing on live data. The composite verdict is honest about which probes are missing.
- **Narrative Causality** — deterministic template-built; safe to read. The probabilistic phrasing is the feature.
- **Memory Graph / Anomaly Lineage** — small now, will need rendering limits (P2 backlog) at scale.
- **Pattern Discovery / Hidden Regimes / Crisis Archetypes** — data-driven mining, all guarded by `data_quality`.
- **Investigation causal tree / similarity** (Phase 18 Pass B) — investigation-support graphs and deterministic case similarity. Tree edges come from already-stable upstream layers (anomaly genealogy, propagation, structural deps, transitions); similarity is rule-based (no ML). Useful diagnostically; reasons always exposed.
- **Investigation auto-draft** — only fires on `crisis_genesis = PRE_CASCADE` with deduped fingerprint. Treat the absolute count of auto-drafts as exploratory until the genesis layer itself stabilizes.
- **Replay reconstruction / FROZEN-vs-LIVE diff** (Phase 19 Pass A) — frozen snapshot store is stable, but per-surface live reconstruction inherits the experimental status of its inputs (causal/structural/genesis/narrative). Diff entries should be read as "engine interpretation changed" — not a market prediction signal.

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

- **Honest uncertainty** is the core invariant. Every layer must gate its outputs by data_quality and must publish its decomposition. New layers that do "we have a model that says X" without exposing factors do not fit.
- **Acyclic dependencies** between intelligence layers. `adaptation_state` reads from observed layers but never writes back. Operator priorities reads from everything but never feeds back into upstream computations.
- **Bounded, reversible, explainable** is the rule for any modifier that affects downstream behavior. Phase 16's `ADAPTATION_BOUNDS` is the pattern to copy.
- **No trading actions**. The system is operator intelligence, not execution. ACK/MUTE/RESOLVE are workflow markers, not trade triggers.

### Companion docs

- `docs/2026-05-23-production-hardening-audit.md` — P0 audit + applied fixes (commit 6b64a76)
- `docs/2026-05-23-p1-hardening-plan.md` — P1 design + scaling estimates (commit c8246f4)
- This document — system-wide freeze (commit pending)

Together these three give the full picture: where we are, what's been hardened, what's planned, and how the pieces fit.
