# Operational Review — kazus-global Liquidity Intelligence Platform

**Review date:** 2026-05-24
**Scope:** end-to-end forensic audit of phases 1–19 as if real operators are already running this in production.
**Method:** walk the operator workflow, name every friction point and architectural tension found, score each by severity (HIGH / MEDIUM / LOW), recommend the smallest viable mitigation. Nothing in this document is implemented; this is review-only.

Severity rubric:
- **HIGH** — operator can be misled or load-bearing audit property can silently break. Address before next intelligence-layer phase.
- **MEDIUM** — friction or invariant erosion that compounds with scale; fix in the next maintenance window.
- **LOW** — cosmetic or future-proofing concern.

---

## 1. UX / friction findings

### 1.1 INV drawer tab proliferation — MEDIUM
The case drawer now hosts seven tabs: `evidence` · `notes` · `timeline` · `tree` · `similar` · `replay` · `export`. Two of them (`timeline` and `replay`) sound semantically identical to a cold operator but expose different data:
- `timeline` (Phase 18, `investigation_timeline`) JOINs upstream events keyed off linked evidence.
- `replay/timeline` (Phase 19, `investigation_replay_timeline`) returns window-scoped keyframes around `replay_anchor_ms`.

**Tradeoff:** merging removes a clean separation between "what touched the case" vs "what was happening around the anchor". Keeping them separate is correct, but the names don't telegraph it.
**Recommendation:** rename Phase 18 tab to `events` or `audit`; keep `timeline` for the replay surface. No code change needed beyond labels.

### 1.2 The `inv` button on operator-queue rows offers no dedup or preview — MEDIUM
Clicking `inv` on a queue row creates a draft case immediately, fires `alert()`, and dispatches a navigation event. There is no:
- preview of what will be created (title / tags / severity);
- check for an existing active investigation already pointing at the same `priority_key`;
- undo path (the draft is committed before the alert appears).

A noisy CRITICAL period (many high-score rows) makes it easy to spam multiple parallel cases on the same underlying finding.
**Recommendation:** before insert, query `investigations` for any non-ARCHIVED case with the operator_priority's `ref_key` as evidence; if found, navigate to it instead.

### 1.3 Recapture is destructive and quiet — **HIGH**
`investigation_replay_capture(force=True)` overwrites the single `investigation_replay_snapshots` row. After overwrite, the prior FROZEN state — the audit primitive the entire forensic layer is built around — is permanently gone. The UI guards with a `confirm()` and the recapture is logged as an event (`replay_recaptured` with `previous_captured_at_ms`), but the *payload* itself is not preserved anywhere.

A forensic system whose forensic primitive can be overwritten without retention is structurally weaker than its design claims.
**Recommendation:** either
- (a) drop the `force=True` path entirely (capture only once, never re-capture); or
- (b) extend `_inv_log_event` to attach the previous `payload_json` as a payload of the `replay_recaptured` audit event; or
- (c) move to a multi-row snapshot table (small per-case cost, full history). Option (b) is the smallest change that preserves the audit property.

### 1.4 FROZEN-vs-LIVE banner is uninformative immediately after capture — LOW
At case creation, auto-capture fires and the diff endpoint computes `diff_count = 0`. The banner says "no material drift since frozen snapshot" — true but vacuous; a new operator may interpret it as a positive signal.
**Recommendation:** dim or hide the banner while `frozen_age_seconds < 600`.

### 1.5 Causal-tree edge-kind proliferation — MEDIUM
The tree emits ~14 distinct `kind` values: `case_subject`, `case_finding`, `case_reference`, `caused_by`, `evolved_into`, `historically_similar`, `preceded`, `destabilized`, `stabilized`, `propagation`, `causal_directional` / `causal_common_driven` / `causal_under_evidenced` / `causal_ambiguous` / `causal_coincidence` / `causal_exploratory`, `influence_chain`, `dominant_driver`, `co_driver`, `transition`. An operator cannot internalize this taxonomy.
**Recommendation:** group at the UI layer into 4 semantic buckets (case-internal · cross-symbol · cross-time · anomaly-genealogy) and show a flat list per group.

### 1.6 Similarity scoring weights are documented in the function, but invisible in operator output — MEDIUM
`_inv_similarity_compare` adds 40 / 25 / 15 / 10 / 5 / 5 per criterion. The `reasons[]` array names what overlapped but not how many points each contributed. A case scoring 65 looks like "the engine likes this case" rather than "30 from fingerprint match + 25 from symbol overlap + 10 from tag overlap". Mild violation of the project's explicit "no hidden scoring" principle.
**Recommendation:** return `score_contribution` per reason; render in the UI as `"+30 same origin fingerprint…"`.

### 1.7 Markdown export carries no integrity stamp — MEDIUM
`investigation_export_markdown` renders from the current DB state. Two exports of the same case at different times can differ (tree edges drift as `propagation_graph` updates, similar cases change as new ones land). The export header has `Exported at <iso>` but no `content_hash` and does not stamp the frozen snapshot's `captured_at_ms` in section 1.
**Recommendation:** add a content hash (SHA-256 of the markdown) and the frozen snapshot's `captured_at_ms` to the audit footer. Auditors can compare hashes to detect mutation.

### 1.8 Empty windows in replay surface confuse — LOW
A case window with no alerts → propagation playback shows all-zero bars across whatever symbols are seeded; state-evolution sparklines are flat. The current behavior is "render nothing visible". Operators may read this as "tool broken".
**Recommendation:** explicit "no activity inside the window" placeholder in both panels; tested via `prop.frame_count > 0 && total > 0` check.

### 1.9 Note-type semantics are not surfaced to the operator — LOW
The dropdown lists 8 types (`note`, `hypothesis`, `conclusion`, `false_positive`, `needs_monitoring`, `confirmed_structural`, `coincidence`, `comment`). The semantic difference between `note` and `comment` is invisible in the UI.
**Recommendation:** add hover-tooltip per option describing intent. Don't add more types.

### 1.10 ARCHIVED is one-way but the UI doesn't say so — LOW
The status dropdown lets the operator pick ARCHIVED on any case. Once archived, the API rejects status changes. UI does not communicate this until the operator tries and sees an error.
**Recommendation:** disable ARCHIVED option in the dropdown with a `disabled` tooltip; expose a separate "Archive…" confirm button.

### 1.11 Slider/floor absent for similarity `min_score` — LOW
`investigation_similar` accepts `min_score` (default 10). UI doesn't expose it; operator can't see weaker matches without a manual URL fetch.
**Recommendation:** add a slider 0–50 above the list.

---

## 2. Architectural tensions

### 2.1 Operator queue ↔ investigation are two parallel state machines — MEDIUM
Both have lifecycle, status, priority, and the operator interacts with both about the same finding. When `operator_priority_ack(action="resolve")` resolves a row, an investigation linked to that priority is unaware. Conversely, resolving the investigation doesn't acknowledge the queue. Two truths.
**Recommendation:** when a `priority_key` with an active investigation transitions to `resolved` in `operator_priorities`, emit a `linked_priority_resolved` event on the case. Reverse direction (case-resolved → queue-acked) is more invasive and can wait.

### 2.2 `investigation_replay_capture` cascades 8 intelligence layers inside the create-transaction — **HIGH**
[research.py:9720-9727](shared/kazus_logic/liquidity/research.py#L9720-L9727) — `_replay_capture_payload` calls `operator_priorities`, `sanity_audit`, `crisis_genesis`, `adaptation_state`, `narrative_causality`, `market_state_transitions`, `structural_dependencies`, `causal_propagation`. All are TTL-cached but on a cold cache `crisis_genesis` alone can be 200ms+, `causal_propagation` 250ms, `sanity_audit` cold-uncached up to seconds. Auto-capture fires inline at `investigation_create`, holding the DB connection.

The expensive `synthesis` endpoint (30s cold, see freeze doc §3) is NOT in the cascade, but the rest of the cascade represents almost everything that contributes to it. The first case created after a worker restart can take seconds.
**Recommendation:** move auto-capture out of the create-request path. Options:
- A worker queue / Celery-style task; or
- A new "needs-capture" flag on `investigations` consumed by the existing `investigation-autodraft` worker loop on its next tick.

### 2.3 `investigation_replay_diff` semantically diffs FROZEN against "right now", not against cursor — **HIGH**
The endpoint re-evaluates all 5 LIVE surfaces at call time and diffs against the frozen blob. The UI labels this "FROZEN vs LIVE". The replay surface ALSO exposes a scrubber and a `cursor snapshot` that reconstructs LIVE at the cursor. An operator scrubbing back to `anchor − 4h` and reading the diff banner naturally assumes "this is the drift between FROZEN and CURSOR". It is not.

This is the headline forensic surface and its labeling is misleading.
**Recommendation:** rename the banner to "FROZEN vs NOW (engine current view)" and add a smaller "compare to cursor…" affordance that recomputes the diff against the cursor's reconstructed state. Same backend math, different `at_ms`.

### 2.4 `investigation_replay_diff` recomputes all 5 live surfaces every call — MEDIUM
TTL caches on the upstream functions usually hide this, but on the first call after a cache eviction the diff endpoint can stack 5 cold computations. The endpoint is called by the INV drawer on every open of the replay tab.
**Recommendation:** debounce the diff call client-side, or memoize the LIVE side per-case for 30s in the function.

### 2.5 Two timeline functions, two semantics, similar names — MEDIUM
`investigation_timeline` and `investigation_replay_timeline` overlap in scope (both pull `operator_priority_events`) and differ in window selection rule. Future maintenance will get them confused.
**Recommendation:** rename one (see §1.1). Long term, consider folding both into a single function with a `scope=` parameter (`evidence_joined` | `anchor_window`). Not urgent.

### 2.6 `investigation_similar` does per-case round-trips — LOW
[research.py — `_inv_similarity_compare` calls `_inv_evidence_summary(other_id)` for every candidate](shared/kazus_logic/liquidity/research.py). O(N) `investigation_evidence` reads per call. At 10–100 cases this is invisible; at 10k cases it dominates.
**Recommendation:** batch-load all evidence rows for the candidate ids in a single `IN (...)` query, build the summary dict once, compare in-memory.

### 2.7 `investigation_causal_tree` runs 4 intelligence-layer calls per open — MEDIUM
Tree composes `propagation_graph`, `causal_propagation`, `structural_dependencies`, `market_state_transitions`, plus a `liquidity_anomaly_edges` JOIN. All TTL-cached, but cold-cache opens can be slow. Tree is called whenever the operator opens the `tree` tab in the drawer.
**Recommendation:** memoize per-case for 5 minutes inside the function, or expose `?refresh=false` to skip rebuilding.

### 2.8 `investigation_replay_propagation` derives `lookback_days` from the window — LOW
The function calls `propagation_graph(db, lookback_days=...)` with a `lookback_days` computed from `(window_end − window_start)` in days. For windows < 24h the computation yields 0 and is coerced to 1; for windows of 7d it asks for 7d. So the static edges shown to the operator are window-coupled rather than fixed.
**Recommendation:** decouple: edges should reflect a configured lookback (e.g. 7d) regardless of window. Otherwise short-window playbacks show edges from a tiny look-back window with low support.

### 2.9 Frozen replay snapshot does NOT include `synthesis` or `propagation_graph` — LOW
The 8 captured sections in `_replay_capture_payload` cover operator-facing intelligence but exclude the most expensive composite (`intelligence_synthesis`) and the raw graph (`propagation_graph`). If a future operator review wants "what did synthesis say at capture?", that data is gone.
**Recommendation:** intentional decision. Document explicitly in the function docstring that synthesis is excluded for cost reasons.

### 2.10 Frozen-state lineage gap with retention — **HIGH**
`investigation_evidence.snapshot_json` exists but is populated only when the caller passes `snapshot=...`. Auto-linked operator-priority evidence (from the queue's `inv` button) currently sends a snapshot dict — good. But manual evidence links from the drawer's `+ link evidence` form do NOT, and worker auto-draft only snapshots the genesis verdict, not the broader queue state.

When the upstream row is pruned (90d for `operator_priority_events`, 180d for `operator_acknowledgements`), the case's timeline / tree / similarity quietly degrades. The case THINKS it has full context.
**Recommendation:** make `_investigation_link_evidence_inner` ALWAYS fetch and store the referenced row's content at link-time (via small per-type dispatch). The snapshot field already exists; just populate it.

---

## 3. Institutional discipline checks

### 3.1 Tree edges with COINCIDENCE / EXPLORATORY verdicts still carry numeric confidence — MEDIUM
[research.py:9170-9195](shared/kazus_logic/liquidity/research.py) — `causal_propagation` returns a `causal_confidence` even for `COINCIDENCE` or `UNDER_EVIDENCED` verdicts. `investigation_causal_tree` passes that confidence through to the edge. The operator sees `kind=causal_coincidence confidence=0.30` and naturally reads "30% confident this is causal" — when the verdict is EXPLICITLY that this is NOT causal.
**Recommendation:** in `investigation_causal_tree`, zero out `confidence` for `COINCIDENCE`, `UNDER_EVIDENCED`, `EXPLORATORY`, `AMBIGUOUS` verdicts. The verdict is in the `kind`; numeric confidence on a negative verdict is misleading.

### 3.2 Replay live-reconstruction numbers display with full authority regardless of `data_quality` — LOW
The PARTIAL / PRUNED flag exists in the response but the operator-facing display renders `synthesized_stress=84.2` the same way whether it's HIGH or PARTIAL.
**Recommendation:** in `LiveReconstructionSummary`, dim or italicize values when `data_quality ∈ {PARTIAL, PRUNED, INSUFFICIENT}`. Already have the field.

### 3.3 `replay_diff` thresholds are hard-coded gates — LOW
`Δ ≥ 5` for genesis score, `Δ ≥ 0.05` for adaptation modifiers. Below-threshold drifts vanish entirely from operator view.
**Recommendation:** surface the raw deltas with a flag, not gate them out. The threshold can be the display priority, not the filter.

### 3.4 PRE_CASCADE is the only auto-draft trigger — MEDIUM
[research.py — `investigation_auto_draft_from_genesis`](shared/kazus_logic/liquidity/research.py). On current data PRE_CASCADE has fired 0 times in production (per arch freeze §7); the entire auto-draft path is exercised only by tests.
**Recommendation:** add telemetry (count of auto-drafts/week) to the runtime-health endpoint so dormancy is visible. Do NOT widen the trigger to ELEVATED_RISK — premature creation of cases is a known anti-pattern (the original Phase 18 decision).

### 3.5 Auto-draft fingerprint = probe-composition only — LOW
Two PRE_CASCADE events with identical contributing probe sets collapse to one case even if the market state shape is otherwise unrelated. Acceptable tradeoff; should be documented.
**Recommendation:** add explicit note in the `investigation_auto_draft_from_genesis` docstring.

### 3.6 `replay_capture` reports `captured=True` even when sections error — MEDIUM
`_safe()` stores `{"error": "..."}` per failed section; the snapshot is still written with `payload_size > 0`. The API response has no `sections_with_errors`.
**Recommendation:** return `sections_with_errors: List[str]` alongside `sections`. Surface in UI as an amber chip on the diff banner.

### 3.7 Similarity weights not exposed in response — see §1.6 — MEDIUM
Restated here: documentation-only weights violate the "no hidden scoring" principle when an operator can't see contributions per-reason.

### 3.8 Pre-retention timeline events vanish, cases keep claiming complete timeline — **HIGH**
Restated from §2.10 because this is the strongest invariant erosion in the system. A 6-month-old case re-opened today will silently have a less-complete timeline than when it was closed. For an audit-grade system this is the wrong default.

### 3.9 Operator queue lifecycle and `investigation` lifecycle can disagree — see §2.1

### 3.10 `adaptation_state` modifier bounds are load-bearing — LOW
[ADAPTATION_BOUNDS](shared/kazus_logic/liquidity/research.py). Currently only `adapted_recommendations` reads them. If more wiring lands, a bug that relaxes a bound becomes a silent behavioral change.
**Recommendation:** add an `assert` at the end of `adaptation_state` that each modifier is within `ADAPTATION_BOUNDS[name]`. Cheap, catches future regressions.

### 3.11 No notification fan-out for mentions / collaborators / assignments — MEDIUM (LOW today, HIGH at multi-operator)
`@mention` parsing exists; mentions emit `mention` events. Nobody is notified. Collaborator list exists; nobody is notified on add. Assignment changes log a `handoff` note in the audit event; nobody is notified.

At single-operator scale this is invisible. The moment a second user joins, the system silently fails to deliver coordination signals.
**Recommendation:** keep current behavior, but document the gap in the architecture freeze under "what this layer does NOT do". When the user count exceeds 1, this becomes a real bug. (And: never auto-route to Slack/etc without an explicit opt-in scope.)

---

## 4. Operator workflow end-to-end — walk and findings

The intended happy path: Queue → `inv` → Investigations → drawer → evidence → notes → resolution → recurrence (similar) → export.

Walking it:

**Queue → inv:** §1.2 (no dedup, no preview, no undo). MEDIUM.

**Drawer → evidence:** Auto-linked operator_priority appears as the only initial evidence. Operator wants to add a symbol / alert / anomaly — has to type `ref_key` and optionally `ref_id` into a freeform input. No autocomplete from existing entities. MEDIUM friction; fixable with a typeahead populated from `liquidity_alert_history.id` etc.

**Drawer → notes:** Append-only is correct and well-designed. Type dropdown semantics opaque (§1.9). LOW.

**Drawer → tree / similar:** Both work; both can be slow on cold cache (§2.4, §2.7). MEDIUM.

**Drawer → replay:** Works on cases with `primary_symbol` set; degenerate without one (propagation playback shows empty symbol list). FROZEN-vs-LIVE banner mislabels semantics (§2.3). HIGH.

**Resolution:** Forces a `resolution_summary` — correct. But UX gap: the operator who resolves doesn't see the linked operator_priority still ACTIVE in the queue (§2.1). MEDIUM.

**Recurrence:** Similar tab surfaces prior cases — works. Score weights opaque (§1.6). MEDIUM.

**Export:** Markdown download works. No content hash, drifts silently (§1.7). MEDIUM.

**Cross-workflow:** Operator can mute/ignore a priority while an investigation pointing at the same `priority_key` is open. The mute lives in `operator_acknowledgements`; the case lives in `investigations`. Two truths. MEDIUM.

---

## 5. Lists

### 5.1 Strengths (preserve as-is)
- Append-only history wherever it matters (notes, lifecycle events, replay capture log).
- No auto-trading, no auto-execution — every action is a workflow marker.
- Scarcity gates cascade correctly: `data_quality` propagates from sample-count check to operator queue.
- Layer composition is acyclic: `adaptation_state` reads but never writes back; `operator_priorities` reads but never feeds upstream.
- DB-backed lifecycle survives restarts.
- Evidence linking is idempotent on `(case, type, key)` triple.
- Replay snapshot is opaque JSON + UPSERT — storage cost stays bounded.
- Markdown export is reproducible from current state.
- Causal-tree rationale strings come from real upstream values, not invented.
- 69 backend tests; type checks + builds clean.
- Investigation auto-draft is gated to a single narrow verdict (PRE_CASCADE) — discipline maintained.

### 5.2 Weaknesses (address)
- Recapture overwrites the FROZEN audit primitive without payload retention (§1.3 / HIGH).
- FROZEN-vs-LIVE banner labels are misleading vs cursor (§2.3 / HIGH).
- Pre-retention timeline events vanish; cases lose context silently (§2.10, §3.8 / HIGH).
- Auto-capture cascades 8 layers inline in the create transaction (§2.2 / HIGH).
- Causal-tree edge taxonomy explosion (§1.5).
- Similarity weights not exposed per-reason (§1.6).
- Operator queue ↔ investigation are unbridged state machines (§2.1).
- 7-tab INV drawer naming is ambiguous between two timelines (§1.1).
- `replay_capture` reports success even on partial-section failure (§3.6).
- COINCIDENCE / EXPLORATORY tree edges still carry numeric confidence (§3.1).
- No notification fan-out for mentions / collaborators (§3.11).
- `inv` button has no dedup / preview / undo (§1.2).

### 5.3 Dangerous future directions (avoid)
- **ML / LLM similarity or tree rationale.** Replaces deterministic, auditable scoring with a black box; violates the project's core explainability principle.
- **Multi-snapshot replay storage** ("every escalation jars a frozen frame"). Sounds reasonable, balloons storage, returns little. One well-captured frozen + on-demand reconstruction is the right design.
- **"AI auto-resolve" of investigations.** Closes the loop toward auto-execution. The system is intelligence, not action.
- **Cross-case learned similarity weights** ("operator's resolution becomes training signal"). Introduces silent learned bias and breaks reproducibility.
- **Adding `confidence` fields to operator-facing investigation objects** (tags, evidence links). Confidence-inflation surface.
- **Streaming live replay over websocket.** Turns the forensic surface into a live dashboard; collapses the FROZEN/LIVE separation that gives the layer its value.
- **`INSIGHT` or `RECOMMENDATION` blocks in export.** One step from "engine tells operator what to do".
- **Per-symbol confidence on the propagation playback bars.** Confidence-on-counts is a category error; counts ARE the data.
- **Auto-drafting cases on anything below PRE_CASCADE.** Spam-creates cases; erodes operator trust in the auto-draft signal.
- **Pushing alerts to external channels (Slack / Telegram / email) for investigation events** without opt-in scope. Silent perimeter expansion.

### 5.4 Safest next expansions (do these before more intelligence layers)
1. **Bridge queue ↔ investigation** (§2.1): one event in each direction. Small, additive, fixes the most-quoted operator friction.
2. **Always-snapshot upstream rows into `investigation_evidence.snapshot_json`** (§2.10, §3.8). Field already exists; just populate consistently. Closes the audit-degradation gap.
3. **Move auto-capture out of the create-request path** (§2.2). Either fire-and-forget or queue-based; either way unblocks the request.
4. **Preserve prior payload on recapture** (§1.3). Smallest fix: attach previous payload as the audit event's payload.
5. **Add `score_contribution` to similarity reasons** (§1.6 / §3.7). Restores the explainability invariant.
6. **Add `content_hash` to markdown export** (§1.7). Required for any real audit pipeline.
7. **Add `sections_with_errors` to `replay_capture` response and UI banner** (§3.6).
8. **Rename "FROZEN vs LIVE" → "FROZEN vs NOW (engine current view)"** and add an optional "compare against cursor…" affordance (§2.3).
9. **Zero out tree-edge confidence for negative verdicts** (`COINCIDENCE` / `UNDER_EVIDENCED` / etc.) (§3.1).
10. **Dim non-HIGH `data_quality` numbers in replay live view** (§3.2).
11. **Add evidence-form typeahead** populated from existing alerts / anomalies (§4). Removes the biggest "have to type IDs by hand" friction.
12. **Document the no-notification gap** in the architecture freeze under "what this layer explicitly does NOT do" (§3.11). Set expectations honestly.
13. **Add a runtime-health counter** for auto-drafts/week (§3.4). Surfaces dormancy.
14. **Decouple `replay_propagation` edge lookback from window length** (§2.8). Configurable.
15. **Memoize `investigation_causal_tree` per-case for ~5 minutes** (§2.7). Pure perf.

### 5.5 NEVER do
- **Auto-trade / auto-execute / auto-cancel orders.** The system is operator intelligence, not execution.
- **"AI decided to resolve this case".** Resolution is operator workflow; resolution_summary is the operator's voice.
- **Replace deterministic narrative templates with LLM-generated text.** Templates are auditable; generated text is not.
- **Introduce any hidden scoring component anywhere in operator-facing output.** Every contribution must be exposed.
- **Cross-investigation learned similarity weights.** Silent bias from operator behavior history.
- **Push notifications to external channels** (Slack/Discord/email/Telegram for investigations) without explicit per-case opt-in and audit logging.
- **Auto-create cases below PRE_CASCADE.** Erosion of the auto-draft signal.
- **Auto-resolve cases ever.** Even RESOLVED + MONITORING + ARCHIVED are operator transitions.
- **Silent retention prune of `investigations*` tables.** These are the institutional memory; prune must require explicit operator action.
- **Concurrent writes to `investigation_replay_snapshots`** without row-level locking or `SELECT ... FOR UPDATE`. The UPSERT is racy under concurrent recapture; today only one user so it doesn't matter, but the moment multi-op lands this becomes a data-loss vector.
- **Treat the FROZEN snapshot as a regular cache.** It is the audit primitive; mutation requires audit-event with prior payload preserved.
- **Mix `intelligence_synthesis` into the replay capture cascade.** 30s cold cost on every case creation — would re-introduce the original P0 from the production audit.

---

## 6. Prioritized recommendation set

Ordered by `severity × ease`. The first five are HIGH-severity OR cheap wins that should land before the next intelligence-layer phase; the rest are good maintenance items.

| # | Item | Severity | Effort | Section |
|---|---|---|---|---|
| 1 | Preserve prior payload on recapture | HIGH | 1 h | §1.3 |
| 2 | Move auto-capture out of create-transaction | HIGH | 2 h | §2.2 |
| 3 | Rename FROZEN vs LIVE → FROZEN vs NOW; add compare-at-cursor | HIGH | 2 h backend + 1 h UI | §2.3 |
| 4 | Always-snapshot upstream rows in evidence linking | HIGH | 3 h | §2.10 / §3.8 |
| 5 | Bridge queue ↔ investigation (one event each way) | MEDIUM | 2 h | §2.1 |
| 6 | Add `score_contribution` to similarity reasons | MEDIUM | 30 min | §1.6 |
| 7 | Add `content_hash` to markdown export | MEDIUM | 15 min | §1.7 |
| 8 | Add `sections_with_errors` to replay_capture | MEDIUM | 30 min | §3.6 |
| 9 | Zero out negative-verdict tree-edge confidence | MEDIUM | 15 min | §3.1 |
| 10 | Dedup the `inv` button per priority_key | MEDIUM | 1 h | §1.2 |
| 11 | Rename evidence-JOIN timeline tab | MEDIUM | 5 min | §1.1 |
| 12 | Evidence-form typeahead | MEDIUM | 4 h | §4 |
| 13 | Dim non-HIGH data_quality numbers in live view | LOW | 30 min | §3.2 |
| 14 | Hide diff banner while frozen_age < 10 min | LOW | 10 min | §1.4 |
| 15 | Memoize causal_tree per-case 5 min | MEDIUM | 30 min | §2.7 |

Total: ~17 hours of work to address every HIGH-severity finding plus most MEDIUM ones. None require a new intelligence layer; all are incremental refinements that strengthen invariants the architecture already claims.

---

## 7. Closing posture

The system is functionally complete and operationally disciplined. The five HIGH-severity findings are not architectural rewrites — they are places where current behavior contradicts a stated invariant of the architecture (audit-grade, explainable, replay-safe). Each can be fixed in hours, and each prevents a future operator from being misled or losing audit evidence.

The strongest property of the system is what it *refuses* to do: no auto-trading, no LLM-generated reasoning, no hidden scoring, no learned bias. That refusal posture should be preserved in every future decision; the "NEVER do" list (§5.5) is not advisory — it defines the perimeter that separates this from a generic AI dashboard.

The architecture freeze (`docs/2026-05-23-architecture-freeze.md`) remains the single source of truth for layer boundaries and should be updated whenever any of the recommendations in §5.4 land.
