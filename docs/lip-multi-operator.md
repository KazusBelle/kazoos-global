# Multi-Operator Workflow — canonical companion

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md) §1 (Layer 10 Operator, Layer 11 Investigation, Layer 12 Replay), [`docs/lip-governance.md`](lip-governance.md), [`docs/lip-ontology-boundaries.md`](lip-ontology-boundaries.md), [`docs/lip-epistemic-boundaries.md`](lip-epistemic-boundaries.md), [`docs/lip-execution-validation.md`](lip-execution-validation.md).

**Status: Class A documentation hardening pass** of the already-implemented operator + investigation + replay infrastructure. This document does not add code, does not propose new states, does not change emit shapes. It formalizes attribution, visibility, replay-time reconstruction, disagreement legitimacy, and audit lineage for multi-operator workflow. Per [lip-governance §2](lip-governance.md), this is documentation-only work authorized during [Operational Observation Period](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_operational_observation_period.md).

**Boundary statement (load-bearing).** The platform stores, attributes, isolates, replays, compares, and audits multi-operator observations. It does **not** compute consensus, average interpretations, resolve disagreements, or assert a "collective truth". Two operators reading the same replay with the same inputs may reach different conclusions; this is legitimate and replay must preserve that legitimacy.

**Cross-cutting ontology invariant** (per [`lip-ontology-boundaries.md`](lip-ontology-boundaries.md)): operator-level artefacts are *attributable observations under bounded instrumentation*, not authoritative market ontology and not authoritative operator truth.

---

## 1. Multi-operator scope

**Currently implemented (single-user reality, multi-user surface).** Per [`models.py:572`](../shared/kazus_db/models.py#L572): *"Future-proof: forward owner. Single-user today."* The schema supports multi-operator: `assigned_to` (owner), `collaborators_json` (array of user ids), `last_touched_by` audit field, `actor_id` on every `investigation_events` row, `author_id` on every `investigation_notes` row, `captured_by` on every `investigation_replay_snapshots` row. Collaborator semantics today: notification-via-mentions, **no edit ownership** ([models.py:589-591](../shared/kazus_db/models.py#L589)).

This companion formalizes the contract that holds *as the system moves into actual multi-operator use*. Until then, the contract still binds documentation, code review, and any new infrastructure proposal.

**Out of scope.** Real-time chat, presence indicators, comment threads beyond `investigation_notes`, ticket lifecycle outside the investigation surface, social features. Per §16 — this is execution-observability platform, not a collaboration product.

---

## 2. Shared vs local state

| State | Surface | Implementation | Replay-visible? |
|---|---|---|---|
| **Personal notes / hypotheses** | (proposed; would be a new private `investigation_notes.visibility = 'local'` value) | **NOT IMPLEMENTED**. Today every `investigation_note` is shared with all operators who can read the case | N/A until visibility scope is added |
| **Temporary bookmarks / local replay markers** | (proposed; out-of-band tooling) | **NOT IMPLEMENTED** in platform; operator tier today | N/A |
| **Published investigation notes** | `investigation_notes` rows | **Implemented**. Append-only ([models.py:651-670](../shared/kazus_db/models.py#L651)) | Yes — replay reconstructs all notes with `created_at_ms ≤ as_of` |
| **Escalations / status transitions** | `investigation_events` rows (`event_type ∈ {status_change, severity_change, assigned, ...}`) | **Implemented** ([models.py:733-757](../shared/kazus_db/models.py#L733)) | Yes |
| **Review outcomes** | `investigation_notes` with `note_type ∈ {conclusion, false_positive, confirmed_structural, coincidence}` + status transitions | **Implemented** via note_type enum + event log | Yes |
| **Governance actions** (suppression, archive, force-recapture) | `investigation_events` row + `investigation_replay_snapshots` revision flip | **Implemented** | Yes |
| **Replay-visible audit artefacts** | `investigation_replay_snapshots` rows | **Implemented**, append-only with revisions ([models.py:673-730](../shared/kazus_db/models.py#L673)) | Self-referential (replay snapshots are themselves replay-visible) |

**Discipline (load-bearing).** The system MUST NOT silently merge local → shared. Today no `local` scope exists at the DB layer — every note is shared. **Adding a `visibility` column to `investigation_notes` is a Class B + Class E change** ([lip-governance §2](lip-governance.md)), NOT AUTHORIZED during Observation Period. Until added, operator discipline is the only mechanism distinguishing private hypotheses from published findings; this companion documents the requirement.

---

## 3. Attribution contract

Every operator-originated artefact MUST carry attribution at write time and MUST NOT lose it at read time.

| Artefact | Attribution field(s) | Implementation status |
|---|---|---|
| `investigation_notes` | `author_id`, `created_at_ms` | **Implemented** |
| `investigation_events` | `actor_id`, `ts_ms`, `event_type`, `payload_json` | **Implemented** |
| `investigation_replay_snapshots` | `captured_by`, `captured_at_ms`, `captured_kind ∈ {auto_create, auto_draft, operator_recapture}`, `revision` | **Implemented** |
| `investigations` | `created_by` (NULL = auto-drafted), `assigned_to`, `collaborators_json`, `last_touched_by`, `last_touched_at_ms` | **Implemented** |
| `operator_acknowledgements` | (per Phase 17 schema) | **Implemented** |
| `operator_priority_events` | (per Phase 17 schema) | **Implemented** |
| `liquidity_annotations` | `author_id` (per [models.py:278-288](../shared/kazus_db/models.py#L278)) | **Implemented** |
| **`calibration_version` per operator artefact** | — | **NOT IMPLEMENTED** (platform-wide gap per [lip-governance §8](lip-governance.md)) |
| **`schema_version` per operator artefact** | — | **NOT IMPLEMENTED** (same gap) |
| **`visible_scope` (local vs shared)** | — | **NOT IMPLEMENTED** (§2) |
| **`review_state` (review_required / review_completed / etc.)** | partially via status enum | **PARTIALLY IMPLEMENTED**; explicit review-state machine NOT IMPLEMENTED (§11) |

**Critical invariant (load-bearing).** **Operator attribution ≠ truth attribution.** An `author_id` on a note records *who wrote it*, not *whether it is correct*. An `actor_id` on a status transition records *who initiated*, not *whether the transition was warranted*. Every reader of these fields MUST resist the inference "X wrote it → X is the authority on this case".

---

## 4. Investigation lifecycle

**Implemented states** ([models.py:564-566](../shared/kazus_db/models.py#L564)):

| Code state | Documentation label | Meaning |
|---|---|---|
| `OPEN` | "case open" | Case exists; no triage step performed |
| `INVESTIGATING` | "under operator review" | Operator has acknowledged and is working |
| `MONITORING` | "open; not actively progressing" | Watch state; left intentionally idle |
| `RESOLVED` | "operator-closed with resolution_summary" | Requires `resolution_summary` text |
| `ARCHIVED` | "removed from active queue" | Audit history preserved |

**Severity values** ([models.py:563-564](../shared/kazus_db/models.py#L563)): `info` / `warn` / `critical`. Surface label rendered as "priority" per [freeze line 255 attention-trust pass](2026-05-23-architecture-freeze.md).

**Origin kinds** ([models.py:573-574](../shared/kazus_db/models.py#L573)): `manual` / `auto_pre_cascade` / `reopen`. Auto-drafts: `created_by IS NULL`.

**Capture status** ([models.py:599-605](../shared/kazus_db/models.py#L599)): `PENDING` / `CAPTURED` / `FAILED`. Decouples case creation from the 8-layer aggregation cascade.

**Lifecycle discipline (load-bearing):**

- These are **workflow markers**, NOT severity truth, NOT market-state truth.
- A case at `INVESTIGATING` does not mean "the market is in stress"; it means an operator is reviewing it.
- A case `RESOLVED` does not mean "the market issue is gone"; it means the operator wrote a `resolution_summary`.
- A case `ARCHIVED` does not mean "the underlying observation is invalid"; it means the case was removed from the active queue.

| Claim that would violate the lifecycle discipline | Operational reformulation |
|---|---|
| "this case shows the market is unstable" | "case `INVESTIGATING` by operator X since `ts`; severity = warn at creation" |
| "this case proves no issue exists" | "case `RESOLVED`; `resolution_summary` reads: [...]; the underlying evidence remains in the case timeline" |
| "the senior operator decided to archive" | "operator X archived case; reasoning lives in `investigation_events.payload_json` if recorded" |

---

## 5. Replay visibility discipline

Replay must reconstruct **operator-visible state as of a timestamp**, not retrospective merged state.

**Implemented capability:**

- Per-case FROZEN reference: `investigation_replay_snapshots` with `is_active=True` row carries the engine state at capture time ([models.py:673-727](../shared/kazus_db/models.py#L673)).
- Revisions: every recapture inserts a new row; prior payloads preserved as `is_active=False` ([freeze line 231](2026-05-23-architecture-freeze.md)).
- `GET …/replay/state?revision=N` and `GET …/replay/diff/revisions?from=&to=` ([freeze line 231](2026-05-23-architecture-freeze.md)).
- LIVE-vs-FROZEN diff banner at the case surface ([freeze line 238](2026-05-23-architecture-freeze.md)).

**NOT IMPLEMENTED:**

- **Per-operator replay visibility scope.** Today, any operator with read access to a case sees the same replay state — there is no `(operator_id, as_of) → visible_notes` filter that hides notes the operator hadn't been able to see at the time.
- **"Who saw what when" discipline at endpoint tier.** The notes, events, and evidence rows can be filtered by `created_at_ms ≤ as_of` (client-side), but no platform endpoint enforces "show me what operator X could have seen at time T".
- **Blinded replay mode** for review (§10).

**Replay-visibility invariant (load-bearing).** A replay `as_of = T` should reconstruct:

- Only annotations / notes with `created_at_ms ≤ T`.
- Only escalations (status changes) with `ts_ms ≤ T`.
- Only review outcomes with `ts_ms ≤ T`.
- The FROZEN snapshot revision `is_active` *at time T* (the revision with `captured_at_ms ≤ T` and not yet superseded), not today's `is_active` row.

The first three are achievable client-side with timestamp filtering on the existing append-only tables (replay-friendly). The fourth — historical `is_active` reconstruction — requires reading `investigation_replay_snapshots` rows and selecting `captured_at_ms ≤ T < next_capture_at_ms`. This is structurally possible from the schema today but **no endpoint enforces it**; consumer-side discipline is load-bearing until endpoint hardening (Class B).

---

## 6. Operator disagreement legitimacy (load-bearing)

**Invariant.** Two operators may classify the same replay differently, escalate different risks, reject different hypotheses, or disagree on propagation interpretation **without implying runtime inconsistency, replay drift, or system failure.**

**Disagreement IS legitimate when:**

| Scenario | Why legitimate |
|---|---|
| Operator A treats `ACCELERATING` regime-engine verdict as actionable; Operator B treats it as noise pending data-quality confirmation | Per [lip-regime-engine §21.3](lip-regime-engine.md) multi-operator legitimacy clause |
| Operator A escalates a `COMMON_DRIVEN` propagation verdict to ARCHIVE; Operator B keeps it OPEN for the duration of the observation window | Per [lip-causal-propagation §6.3](lip-causal-propagation.md) — common-shock detection is incomplete; operator interpretation legitimately varies |
| Operator A notes `note_type = hypothesis` saying "BTC beta"; Operator B notes `note_type = hypothesis` saying "venue-local funding squeeze" | Both are hypotheses; the platform does not adjudicate |
| Operator A marks a finding `false_positive`; later Operator B marks a related finding `confirmed_structural` | Different evidence, different cases; the platform stores both attributively |
| Operator A captures FROZEN at `t1`; Operator B captures FROZEN at `t2` for the same case | Both revisions preserved; replay surfaces the revision-history per §5 |

**Disagreement does NOT mean:**

- The system has a bug.
- Replay is drifting.
- One operator is wrong.
- The platform should compute a consensus.

**Forbidden vocabulary:**

| Banned | Reason |
|---|---|
| "correct interpretation" | Implies adjudication the platform does not perform |
| "ground truth operator" | Implies authority the platform does not assign |
| "consensus reading" | Implies the platform computes consensus; it does not |
| "senior operator truth" | Implies hierarchy of correctness the platform does not encode |
| "canonical interpretation" | Same |
| "wisdom of analysts" | Implies aggregation across operators that the platform does not perform |
| "collective intelligence" / "collective truth" | Same |
| "operator truth layer" | Same |
| "market interpretation engine" | Implies the platform performs interpretation; it stores attribution |

---

## 7. Evidence provenance

Every investigation artefact MUST reference its upstream evidence with full provenance.

**Implemented:**

| Field | Source | Status |
|---|---|---|
| Upstream row reference (`evidence_type`, `ref_id`, `ref_key`) | `investigation_evidence` ([models.py:610-650](../shared/kazus_db/models.py#L610)) | **Implemented** |
| Snapshot-at-link-time (`snapshot_json`) | `investigation_evidence.snapshot_json` ([freeze line 233](2026-05-23-architecture-freeze.md)) | **Implemented**. Retention-safe: case survives upstream pruning |
| Pruning flag at read-time (`is_pruned=True`) | `investigation_timeline` synthesis logic | **Implemented** ([freeze line 233](2026-05-23-architecture-freeze.md)) |
| Replay anchor (`replay_anchor_ms`, window) | `investigations` table fields | **Implemented** |
| Per-evidence calibration_version | — | **NOT IMPLEMENTED** (platform-wide gap) |
| Per-evidence schema_version | — | **NOT IMPLEMENTED** |
| Per-evidence replay_availability label | implicit via `is_pruned=True` | **PARTIAL** — single bit; full label set (REPLAY_AVAILABLE / PARTIAL / NOT_PERSISTED) per [lip-execution-validation §21](lip-execution-validation.md) is documentation-tier |

**Provenance discipline.** An artefact without provenance is **downgraded**, not auto-invalid:

| Provenance state | Surface treatment |
|---|---|
| Full provenance (upstream ref + snapshot + non-pruned) | Standard rendering |
| Upstream pruned, snapshot present | `is_pruned=True` flag rendered; evidence usable but tagged |
| Upstream pruned, snapshot absent (legacy rows) | Evidence marked unreconstructable; case timeline shows gap |
| No upstream ref at all | Should not occur in current code path; if it does, governance defect |

---

## 8. Concurrent operator discipline

| Concurrent scenario | Existing platform handling | Required discipline |
|---|---|---|
| Two operators add notes to the same case simultaneously | Both notes write; append-only ([models.py:651](../shared/kazus_db/models.py#L651)); ordered by `created_at_ms` | None — append-only is the resolution |
| Two operators transition status simultaneously | Both events log; status field is whatever the second commit wrote (last-write-wins on the row); both `investigation_events` rows preserved | Reader MUST read the **event log** for true history; the current status field is convenience, not authority |
| Two operators recapture FROZEN snapshot at near-identical timestamps | Each capture inserts a new revision; `is_active=True` flips to the latest by `captured_at_ms`; prior rows preserved with `is_active=False` | Standard replay-friendly behavior |
| Operator A escalates while Operator B is mid-investigation | Both actions log to `investigation_events`; case state may oscillate | Operators reconcile via case-timeline review; platform does not auto-merge |
| Operator A views a stale replay window while Operator B has just recaptured | Operator A's view is `is_active=True` at *their* read time; on refresh the new revision becomes visible | Operator UI MUST surface revision number; banner per [freeze line 238](2026-05-23-architecture-freeze.md) |

**Out of scope:** distributed locking theory, CRDT essays, database-internal transaction semantics. This document covers *observability governance for concurrent operators*, not engineering implementation of consistency.

---

## 9. Operator contamination boundaries

**Operator annotations can contaminate** later interpretation, replay review, escalation severity, and investigation framing. This is a known and legitimate property; the platform documents its surface but does not auto-remediate.

**Mechanisms by which contamination occurs:**

- A note added at `t1` is read at `t2 > t1` by a different operator; the second operator's interpretation may be anchored to the first operator's framing.
- An escalation to `critical` at `t1` shifts how subsequent operators triage adjacent cases.
- A `confirmed_structural` conclusion in one case affects how operators classify similar cases later.

**Implemented controls:**

- Append-only audit trail makes the original framing recoverable.
- Replay `as_of` reconstruction (§5) preserves what was visible at each timestamp.
- `author_id` attribution allows operators to weight notes per source (operator discretion).

**NOT IMPLEMENTED:**

- **Hidden-review mode** — review a case with operator annotations and identities hidden.
- **Blinded replay review** — reconstruct evidence without seeing prior operator notes.
- **Annotation visibility controls** (per-operator, per-role visibility flags on notes).
- **Post-review reveal workflow** — reviewer commits a conclusion, then the prior annotations become visible for comparison.

These would be **governance surfaces**, NOT "AI scoring" or "operator quality" features. Implementation = Class B + Class E (new emit fields + new persistence), NOT AUTHORIZED during Observation Period.

**Contamination-risk matrix (documentation tier):**

| Risk | Mitigation today | Mitigation if hidden-review implemented |
|---|---|---|
| Confirmation bias on linked-case findings | Operator discipline | Blinded-review default for cases with shared evidence |
| Senior-operator framing anchoring | Operator discipline | Author identity hidden during review |
| Escalation cascade (one critical → others biased high) | Replay `as_of` allows isolated review | Same |
| Resolution-summary anchoring on reopened cases | Append-only resolution history (`event_type = reopened` preserves prior `resolved_at_ms`) | Same + reveal-after-conclusion workflow |

---

## 10. Review / approval lifecycle

**Today's review surface** is encoded informally via `note_type` enum ([models.py:665-666](../shared/kazus_db/models.py#L665)):

| `note_type` | Interpretation |
|---|---|
| `note` | Free-form observation |
| `hypothesis` | Operator's working theory |
| `conclusion` | Operator's stated conclusion |
| `false_positive` | Case was a false alarm in operator's judgment |
| `needs_monitoring` | Hold for additional observation |
| `confirmed_structural` | Operator confirms the finding |
| `coincidence` | Operator attributes to coincidence |
| `comment` | Adjacent commentary, not a conclusion |

Plus status transitions per §4.

**Explicit review-state machine: NOT IMPLEMENTED.**

A future Class B+E proposal might add:

| Review state (proposed, not implemented) | Meaning |
|---|---|
| `review_required` | Case flagged for second-operator review |
| `review_in_progress` | Reviewer claimed |
| `review_completed` | Reviewer signed off |
| `rejected` | Reviewer disagrees; reasons in a follow-up note |
| `superseded` | A later case supersedes this finding |
| `deprecated_investigation` | Case withdrawn for governance reasons |

**Forbidden surface features** (NEVER permissible regardless of authorization):

| Forbidden | Reason |
|---|---|
| Operator ranking | [lip-governance §3 row 9](lip-governance.md) (intent / quality inference); + §6 firewall analog |
| Trust score | Same |
| Analyst quality score | Same |
| Reputation engine | Same |
| Auto-elevation of "senior" operator review | Implies hierarchy of correctness; same |
| ML-derived review priority across operators | [lip-governance §3 row 1](lip-governance.md) (hidden ML weighting) |

A review-state machine is permissible as **workflow tracking**; an operator-quality score derived from review outcomes is **forbidden**.

---

## 11. Audit lineage contract

Every operator action that affects shared state MUST be:

| Property | Source |
|---|---|
| **Timestamped** | `created_at_ms` / `ts_ms` / `captured_at_ms` on the relevant row |
| **Attributable** | `author_id` / `actor_id` / `captured_by` / `created_by` / `assigned_to` / `last_touched_by` |
| **Replay-visible** | Row persisted to append-only tables; reachable via `as_of` filtering |
| **Append-only** | Notes never edited or deleted ([models.py:651-657](../shared/kazus_db/models.py#L651)); events append-only ([models.py:733-757](../shared/kazus_db/models.py#L733)); replay snapshots append-only with revisions ([models.py:673-730](../shared/kazus_db/models.py#L673)) |

**No silent overwrite.** The case `status` field on `investigations` is *eventually consistent with* the event log; the event log is authoritative for "what happened". Same applies to `severity`, `assigned_to`, `tags_json` — convenience fields, authority is in `investigation_events`.

| Mutation | Authoritative record | Convenience reflection |
|---|---|---|
| Status change | `investigation_events` row with `event_type = status_change`, `payload_json = {"from": "X", "to": "Y"}` | `investigations.status` |
| Severity change | `investigation_events` row | `investigations.severity` |
| Assignment change | `investigation_events` row | `investigations.assigned_to` |
| Note addition | `investigation_notes` row (immutable) | N/A — no convenience aggregate |
| Evidence link/unlink | `investigation_events` row with `event_type ∈ {evidence_linked, evidence_unlinked}` | `investigation_evidence` rows |
| Recapture | `investigation_replay_snapshots` row with new revision | `is_active=True` flag |

**No silent removal.** Archiving a case sets `status = ARCHIVED` and logs `event_type = archived`; the case rows remain. Evidence pruning is governed by upstream retention; the case timeline marks `is_pruned=True` per §7.

---

## 12. Replay-time reconstruction discipline (load-bearing)

**Invariant.** Replay reconstructs operator-visible surfaces *as-of the requested timestamp*, not today's final merged state.

**Replay(as_of = T) reconstructs:**

| Surface | How |
|---|---|
| Notes visible at T | Filter `investigation_notes.created_at_ms ≤ T` |
| Status at T | Read `investigation_events` rows up to T; latest `status_change.payload_json.to` |
| Severity at T | Same pattern with `severity_change` |
| Assigned operator at T | Same pattern with `assigned` events |
| Evidence linked at T | `investigation_evidence` rows whose link event has `ts_ms ≤ T` |
| FROZEN snapshot active at T | `investigation_replay_snapshots` row with `captured_at_ms ≤ T < min(captured_at_ms_of_subsequent_active_revisions)` |
| Resolution at T | NULL unless `resolved_at_ms ≤ T` |
| Tags at T | Replay `tags_change` events up to T |

**Replay MUST NOT show:**

- Annotations created after T.
- Status transitions after T.
- Reviews completed after T.
- Frozen-snapshot revisions captured after T.
- Resolution summaries committed after T.

**Replay MUST NOT compute:**

- "What the operator should have concluded at T."
- "What the consensus interpretation at T would have been."
- "Whose interpretation was correct."

These are forbidden by the §6 disagreement-legitimacy invariant and the [lip-ontology-boundaries.md](lip-ontology-boundaries.md) cross-cutting invariant.

**NOT IMPLEMENTED** (endpoint hardening required, Class B):

- A `GET …/replay/case/{id}?as_of=T` endpoint that returns the case in its T-time visible state.
- Per-operator visibility filtering (operator X's view at T may differ from operator Y's view at T if `visibility_scope` is ever added per §2).

Until endpoint hardening lands, `as_of` reconstruction is a **discipline carried by consumers** (operator UI, notebook analyses, audit tooling). Cross-window aggregation that mixes pre-T and post-T artefacts without filtering is a defect; the platform does not currently flag it.

---

## 13. Forbidden interpretations (cross-cutting)

| Banned phrasing | Approved replacement |
|---|---|
| "the operator team concluded" | "operators X, Y, Z each filed notes / conclusions; see attribution" |
| "the consensus reading is" | "per-operator note set: [list with `author_id`]" |
| "correct interpretation" | "operator X's interpretation per `note_type = conclusion`" |
| "ground truth operator" | (rejected; the platform does not assign operator authority) |
| "wisdom of analysts" | (rejected) |
| "collective intelligence" | (rejected) |
| "shared market understanding" | "operator notes set at `as_of = T`" |
| "consensus truth" | (rejected) |
| "operator truth layer" | (rejected) |
| "market interpretation engine" | (rejected) |
| "the team escalated this case" | "operator X escalated at `ts`; see `investigation_events`" |
| "senior operator decision" | (rejected — no operator hierarchy is encoded) |
| "the right call was made" | (rejected — no correctness adjudication) |
| "disagreement = bug" | (rejected — see §6) |
| "reconciliation produces truth" | (rejected — see §6) |
| "escalation = correctness" | (rejected — escalation is a workflow state) |
| "the platform understands the case" | "the platform aggregates evidence + notes + events for the case" |
| "AI-assisted review" | (rejected) |

**Approved patterns (operator-tier prose):**

- "Operator X filed `note_type = hypothesis` at `ts`."
- "Status transition `OPEN → INVESTIGATING` by operator X at `ts`."
- "Case has N notes from operators {X, Y}; conclusions disagree."
- "Replay `as_of = T` shows the case with status W and N visible notes."
- "Evidence was linked at `ts`; upstream subsequently pruned; snapshot preserved."
- "Operator X recaptured FROZEN at `ts`; prior revision preserved as `is_active=False`."

---

## 14. Multi-operator critical invariants

| Invariant | Source |
|---|---|
| **Replay reconstructs historical visibility, not retrospective consensus.** | §12 + [lip-ontology-boundaries §6](lip-ontology-boundaries.md) |
| **Operator disagreement does not imply replay inconsistency.** | §6 |
| **Escalation is a workflow state, not a market-state claim.** | §4 |
| **No operator action constitutes execution guidance.** | [lip-governance §6](lip-governance.md) firewall — restated |
| **Annotations are attributable observations, not authoritative market ontology.** | [lip-ontology-boundaries](lip-ontology-boundaries.md) — restated |
| **Shared visibility does not imply institutional consensus.** | §6, §13 |
| **Append-only is the resolution to concurrent edits, not last-write-wins.** | §8, §11 |
| **`author_id` is attribution, not correctness.** | §3 |

---

## 15. Governance / maturity

| Aspect | Status |
|---|---|
| **This document** | Class A (documentation-only) per [lip-governance §2](lip-governance.md). Authorized during Observation Period |
| **Adding `visibility = 'local'` to `investigation_notes`** | Class B + Class E. NOT AUTHORIZED during Observation Period |
| **Adding endpoint `/replay/case/{id}?as_of=T`** | Class B (new emit surface). NOT AUTHORIZED during Observation Period |
| **Adding hidden-review / blinded replay mode** | Class B + Class E. NOT AUTHORIZED |
| **Adding explicit `review_state` machine** | Class B (semantic surface change). NOT AUTHORIZED |
| **Adding per-artefact `calibration_version` / `schema_version` stamping** | Class B + Class E. Platform-wide governance debt per [lip-governance §8](lip-governance.md); same governance gate as every other layer |
| **Renaming any operator-facing UI label not in §13 approved set** | Class A (doc) or Class B (UI emit); requires this document's updates synchronously |
| **Adding any "trust score" / "operator quality" / "reputation" / "consensus" feature** | Forbidden by §10 + [lip-governance §3 row 9](lip-governance.md). Permanently rejected at design time |
| **Maturity stage of multi-operator infrastructure** | **Observational** per [lip-governance §9](lip-governance.md). Schema supports multi-operator; today's deployment is single-user. Promotion to Operator-visible (formally) gated on actual multi-operator usage + completion of attribution / visibility / blinded-review hardening |

---

## 16. What this document is not

- Not a chat / Slack / collaboration product spec.
- Not a ticket-system spec.
- Not a consensus engine.
- Not a "collective intelligence" layer.
- Not an AI-assisted review system.
- Not an operator trust / quality / reputation layer.
- Not a hierarchy / seniority encoding.
- Not a CRDT / distributed-systems essay.
- Not philosophy about collaboration.
- Not authorization to add new emit fields or states during the Operational Observation Period.
- Not a claim that today's deployment is multi-user — today is single-user.

It is a documentation hardening pass that formalizes the existing append-only investigation + replay + operator-event infrastructure as the load-bearing surface for multi-operator workflow, pre-commits the platform to attribution-without-authority semantics, and rejects consensus-truth / operator-ranking surfaces at design time. The audit lineage is preserved by the existing schema; this companion makes the contract around it explicit.
