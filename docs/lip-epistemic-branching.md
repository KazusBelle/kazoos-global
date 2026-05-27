# Epistemic Branching & Contamination Governance — companion

**Companion to:** [`docs/lip-multi-operator.md`](lip-multi-operator.md), [`docs/lip-governance.md`](lip-governance.md), [`docs/lip-ontology-boundaries.md`](lip-ontology-boundaries.md), [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md) §1 (Layer 11 Investigation, Layer 12 Replay).

**Status: Class A documentation hardening pass.** Adds no code, no states, no emit fields. Formalizes governance for: parallel investigations, branch lineage, contamination-aware replay, stale-authority degradation, and the legitimacy of coexisting unresolved interpretations. Per [lip-governance §2](lip-governance.md), authorized during [Operational Observation Period](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_operational_observation_period.md).

**Boundary statement (load-bearing).** Different investigations may coexist, diverge, remain unresolved, and branch permanently without implying runtime inconsistency, replay corruption, system failure, or a need for forced convergence. The platform preserves investigation lineage; it does not resolve epistemic disagreement automatically. This document specifies the governance contract over the existing append-only investigation + replay infrastructure ([lip-multi-operator §11](lip-multi-operator.md)). It does not specify a CRDT, a merge algorithm, or a consensus mechanism.

**Cross-cutting ontology invariant** (per [`lip-ontology-boundaries.md`](lip-ontology-boundaries.md)): branches preserve epistemic separation, not alternate market realities. A branch is a workflow object, not a hypothesis about the market.

---

## 1. Branching scope

### 1.1 In scope

- Multiple coexisting `investigations` rows referring to overlapping evidence or fingerprints.
- Investigation lineage via `origin_kind = reopen` ([models.py:573](../shared/kazus_db/models.py#L573)) and `investigation_events.event_type = reopened` ([models.py:751](../shared/kazus_db/models.py#L751)).
- Replay snapshot revisions (`investigation_replay_snapshots.revision` with `is_active` flag) as vertical branching of FROZEN reference ([models.py:707-714](../shared/kazus_db/models.py#L707)).
- `origin_fingerprint` clustering of related auto-drafts ([models.py:577](../shared/kazus_db/models.py#L577)).
- Operator-level disagreement over case interpretation (per [lip-multi-operator §6](lip-multi-operator.md)).
- Stale-authority degradation across case windows.
- Contamination of later interpretations by earlier visible annotations ([lip-multi-operator §9](lip-multi-operator.md)).

### 1.2 Out of scope

- Real-time merge algorithms.
- Distributed-systems consistency theory.
- CRDT design.
- Operator chat / social surfaces.
- Any "winner / loser" selection between branches.
- Any "preferred narrative" computation.

---

## 2. Branch identity contract

### 2.1 What exists today

| Branch surface | Implementation | Notes |
|---|---|---|
| Investigation reopen | `origin_kind = reopen` + `investigation_events.event_type = reopened` | Implemented. A reopen creates a new event row; the case row continues with status flipped back |
| Auto-draft dedup grouping | `origin_fingerprint` | Implemented. Two auto-drafts on the same `PRE_CASCADE` fingerprint do not both open |
| FROZEN-revision supersession | `investigation_replay_snapshots.revision` + `is_active` | Implemented. Each recapture is a new revision; prior revisions preserved with `is_active=False` ([models.py:712-714](../shared/kazus_db/models.py#L712)) |
| Operator-priority key supersession | `is_active` flag on `OperatorPriorityHistory` ([models.py:536](../shared/kazus_db/models.py#L536)) | Implemented. New row supersedes prior; prior preserved |
| Multiple investigations sharing evidence | No explicit linking field; `investigation_evidence.ref_key` may collide across cases | Implemented surface (two cases can link the same alert id) |

### 2.2 What is NOT IMPLEMENTED

The following **proposed branching primitives** are not in the schema today. Adding any of them is a **Class B + Class E** change ([lip-governance §2](lip-governance.md)), NOT AUTHORIZED during Operational Observation Period.

| Proposed field | Purpose | Status |
|---|---|---|
| `investigations.branch_id` | Stable identifier for an epistemic branch | NOT IMPLEMENTED |
| `investigations.parent_branch_id` | Lineage pointer to the branch this one forked from | NOT IMPLEMENTED |
| `investigations.fork_reason` | Enum: `propagation_interp`, `replay_interp`, `escalation`, `evidence_scope`, `classification` | NOT IMPLEMENTED |
| `investigations.fork_ts` | Timestamp at which the branch was forked from its parent | NOT IMPLEMENTED |
| `investigations.superseded_by` | Pointer to a successor case if marked superseded | NOT IMPLEMENTED |
| `investigations.abandoned_ts` | Timestamp at which the branch was abandoned without resolution | NOT IMPLEMENTED |
| `investigations.parallel_active_branches[]` | Cross-reference to coexisting branches under the same root | NOT IMPLEMENTED (derivable from `origin_fingerprint` clustering if added) |

Until added, branch identity is **implicit**: cases sharing an `origin_fingerprint`, sharing linked evidence by `ref_key`, or connected via `reopened` event chains constitute an *informal* branch family. Operator discipline carries the lineage today; the companion makes the requirement explicit.

---

## 3. Fork legitimacy invariant (load-bearing)

> **Forking an investigation does not imply prior investigation invalidity.**

A fork is a workflow event, not a verdict on the parent. Reasons a fork is legitimate:

| Fork reason (proposed enum) | When legitimate |
|---|---|
| `propagation_interp` | Operators disagree on whether a `DIRECTIONAL` verdict ([lip-causal-propagation §1.5](lip-causal-propagation.md)) reflects a propagation candidate or a common-driver shadow; both branches investigate the same evidence under different framings |
| `replay_interp` | Two operators recapture FROZEN at different anchors / windows; each becomes the root of an interpretation branch |
| `escalation` | One operator escalates to `critical` severity; another tracks the same finding at `warn`; both branches continue |
| `evidence_scope` | One branch investigates a tight evidence cluster; another investigates a broader cross-symbol cluster |
| `classification` | Branch A treats the finding as `false_positive`; branch B treats it as `confirmed_structural`; both observations are filed per [lip-multi-operator §10 note_type](lip-multi-operator.md) |

**Forbidden fork framings** (regardless of authorization):

- "fork because the original investigation was wrong"
- "fork to find the right interpretation"
- "fork because the team needed convergence"
- "fork because the senior operator overrode"

A fork is named, attributed, timestamped — never adjudicated.

---

## 4. Parallel unresolved branches (load-bearing)

> **Parallel branches may remain unresolved indefinitely.**

The platform does not require:

- Branches to converge.
- Branches to merge.
- Branches to elect a canonical version.
- Stale branches to be archived after a deadline.
- Disagreements to be resolved.

### 4.1 Unresolved-branch legitimacy table

| Scenario | Why legitimate |
|---|---|
| Branch A and Branch B classify the same evidence cluster as `false_positive` vs `confirmed_structural`; both remain `MONITORING` for months | Both are operator observations under attribution; no truth adjudication ([lip-multi-operator §6.1](lip-multi-operator.md)) |
| Branch A escalates to `critical`; Branch B keeps `info`; both stay open | Severity is a workflow marker, not market-state truth ([lip-multi-operator §4](lip-multi-operator.md)) |
| Branch A's FROZEN snapshot was captured pre-event; Branch B's post-event; both are `is_active=True` for their respective cases | Each case independently records its frozen reference |
| Three operators fork from a single auto-draft; one resolves, two remain MONITORING | Each branch's lifecycle is independent |
| Branch A is reopened after RESOLVED; Branch B remains RESOLVED | Reopen is a legitimate lifecycle event, not invalidation of resolution |

The platform stores these states attributively. A consumer that surfaces "this case has unresolved siblings" is acceptable; a consumer that surfaces "this case is wrong because siblings disagree" is forbidden.

---

## 5. Branch state taxonomy (proposed; documentation tier)

The branch-state vocabulary below is **documentation-tier** until the §2.2 proposed fields are implemented. It maps to existing surfaces today:

| Branch state | Existing surface today | Future field |
|---|---|---|
| **ACTIVE** | Case status ∈ `{OPEN, INVESTIGATING, MONITORING}` | (proposed) explicit `branch_state` enum |
| **STALE** | No update in operator-defined window (today: `updated_at_ms` ages without action) | (proposed) computed flag |
| **SUPERSEDED** | A successor case exists for the same `origin_fingerprint` and the prior is marked accordingly via a note or status transition | (proposed) `superseded_by` pointer |
| **HISTORICAL** | Case `ARCHIVED` with audit-preserved evidence | Implemented via `status = ARCHIVED` |
| **UNREVIEWED** | Case has notes but no `note_type ∈ {conclusion, false_positive, confirmed_structural}` | (proposed) computed flag |
| **ABANDONED** | Case open but no recent activity; not explicitly resolved or archived | (proposed) `abandoned_ts` |

### 5.1 Branch-state semantics table

| State | Operational meaning | What it does NOT mean |
|---|---|---|
| ACTIVE | Operator is currently engaged with the branch | "Branch is correct" |
| STALE | No operator engagement in window | "Branch is wrong" or "branch findings are obsolete" |
| SUPERSEDED | A successor branch was filed | "Original branch was false" |
| HISTORICAL | Branch is archived for audit purposes | "Branch findings are invalid" |
| UNREVIEWED | No operator has filed a conclusion-type note | "Branch is suspect" |
| ABANDONED | Branch was left without resolution | "Branch findings should be discarded" |

**Visibility labels ≠ correctness labels.** A STALE branch may contain perfectly accurate observations; the label describes engagement, not truth.

---

## 6. Stale authority governance (load-bearing)

> **Staleness degrades interpretive authority, not historical existence.**

A stale branch's annotations, conclusions, and severity labels **remain in the audit lineage and remain replay-visible**. What changes is the **operator-tier interpretive weight** that current consumers should assign to them.

### 6.1 Stale-authority table

| Source of staleness | Surface treatment | Forbidden treatment |
|---|---|---|
| `updated_at_ms` older than operator-defined window | Surface a stale flag in operator UI | Auto-delete annotations |
| Branch open with no recent activity | Surface "no activity since `ts`" | Auto-archive without operator action |
| FROZEN snapshot revision aged beyond N days from any subsequent recapture | Surface revision-age | Hide prior revisions |
| Operator-priority key with no recent acknowledgement | Surface acknowledgement-age | Auto-resolve the priority |
| Annotations referencing pruned upstream evidence | `is_pruned=True` flag per [lip-multi-operator §7](lip-multi-operator.md) | Drop the annotation |

**Forbidden:** an "operator reliability" or "operator staleness" score. Staleness is a property of *the branch's last activity*, not of the operator.

### 6.2 What staleness MUST NOT trigger

- Auto-delete.
- Auto-resolve.
- Auto-merge with another branch.
- Auto-supersession.
- Re-attribution to another operator.
- Forced convergence to a "canonical" branch.

---

## 7. Supersession discipline (load-bearing)

> **Supersession is a workflow state, not a truth judgment.**

A branch superseded by another is **not** thereby falsified. The supersession marks that a successor exists; the predecessor remains in audit lineage as a legitimate prior observation.

### 7.1 Branch supersession semantics

| Statement | Meaning | What it does NOT mean |
|---|---|---|
| Branch A superseded by Branch B | A successor case exists; the operator chose to consolidate continuation in B | "A was wrong" |
| Branch A deprecated | Operator marked A as no-longer-active for governance reasons | "A's evidence was invalid" |
| Branch A historical | A was archived; lineage preserved | "A's observations are disproven" |
| Branch A abandoned | A was left open without resolution | "A's hypothesis was rejected" |

**Vocabulary discipline:**

- *superseded* ≠ *false*
- *deprecated* ≠ *invalid*
- *historical* ≠ *wrong*
- *abandoned* ≠ *disproven*

The replacement vocabulary list in §11.1 enforces this at the prose tier.

### 7.2 Existing supersession mechanisms

| Mechanism | Where |
|---|---|
| FROZEN-revision supersession | `investigation_replay_snapshots.is_active` flag flip on new revision; prior revisions preserved ([models.py:712-714](../shared/kazus_db/models.py#L712)) |
| Operator-priority key supersession | `OperatorPriorityHistory.is_active` flag pattern ([models.py:536](../shared/kazus_db/models.py#L536)) |
| Note correction via follow-up note | Append-only; no edit/delete on prior note ([models.py:651-657](../shared/kazus_db/models.py#L651)) |
| Status transition (e.g., MONITORING → INVESTIGATING) | `investigation_events` row preserves both states |

---

## 8. Contamination governance (load-bearing)

> **Replay visibility may alter later operator interpretation. This does not alter runtime history.**

### 8.1 Contamination-risk matrix

| Surface | Contamination risk | Existing mitigation | Future mitigation (NOT IMPLEMENTED) |
|---|---|---|---|
| Escalation labels (severity field) | Operator B sees critical → triages adjacent cases higher | Append-only audit; `as_of` filtering reveals original framing | Hidden-review mode |
| Highlighted branches in UI | Operator B sees a highlighted "primary" branch → anchors interpretation | Surface revision-history per [lip-multi-operator §5](lip-multi-operator.md) | Blinded review mode |
| Operator comments / notes | Operator B reads A's hypothesis → confirmation bias | `author_id` attribution; replay filtering | Annotation visibility scope |
| Prior conclusions | Operator B sees `confirmed_structural` → frames new evidence similarly | Append-only; reopen flow preserves history | Post-review reveal workflow |
| Propagated annotations | Annotations replicated across linked cases shape downstream framing | `evidence_type` typed link; no auto-propagation of free-text | Link visibility scoping |
| Review outcomes | A reviewer's prior verdict shapes a subsequent reviewer | Audit trail preserves both | Blinded subsequent review |

### 8.2 Contamination mitigation table

| Mitigation | Status | Class |
|---|---|---|
| Append-only lineage | **Implemented** ([lip-multi-operator §11](lip-multi-operator.md)) | — |
| Replay `as_of` filtering (client-side discipline) | **Partial** ([lip-multi-operator §12](lip-multi-operator.md)) | Endpoint hardening = Class B |
| Hidden-review / blinded-review runtime mode | **NOT IMPLEMENTED** | Class B + Class E |
| Delayed-reveal workflow (reviewer commits before seeing prior annotations) | **NOT IMPLEMENTED** | Class B + Class E |
| Visibility scoping on notes (`visibility = 'local' / 'shared' / 'review_only'`) | **NOT IMPLEMENTED** | Class B + Class E |
| Replay-mode separation (separate read mode where annotations are hidden) | **NOT IMPLEMENTED** | Class B |
| Audit-visible contamination markers (e.g., flag indicating "this review saw prior conclusion X") | **NOT IMPLEMENTED** | Class B + Class E |

**Discipline (load-bearing).** Contamination is a property of *interpretation surfaces*, not runtime history. The runtime stores attributively; the surface tier is where contamination occurs. Mitigations are **governance surfaces**, NEVER "AI scoring", "operator quality", or "reliability scoring".

### 8.3 Three replay modes (proposed vocabulary; only one implemented)

| Mode | Definition | Status |
|---|---|---|
| **PRE-ANNOTATION replay** | Replay with `as_of` set before any operator annotations were written | Achievable client-side via `created_at_ms ≤ as_of`; not endpoint-enforced |
| **POST-ANNOTATION replay** | Replay including all annotations up to `as_of` | Today's default behavior |
| **BLINDED replay review** | Replay with annotations hidden regardless of `as_of` (for fresh-eyes review) | NOT IMPLEMENTED |

Until BLINDED mode is implemented, fresh-eyes review is an operator-discipline matter (open a new browser session and avoid scrolling to the notes); it is not a platform feature.

---

## 9. Replay-time visibility (load-bearing)

> **Replay reconstructs historical visibility, not canonical interpretation.**

Restates [lip-multi-operator §12](lip-multi-operator.md) invariant in the branching context.

### 9.1 Multi-branch replay matrix

| Replay query | Returns | Does NOT return |
|---|---|---|
| `as_of = T` for a single case | Notes / events / evidence visible at T for that case | Sibling-branch state |
| `as_of = T` across the case family (proposed; not implemented) | All branches existing at T with their T-time state | "Which branch was canonical at T" |
| `as_of = T` with branch_id (proposed; not implemented) | A specific branch's T-time state | Other branches' content |

### 9.2 Multi-branch replay invariant

The system **may** surface multiple unresolved branches simultaneously and **must not** force selection of a canonical branch. A consumer that flattens "unresolved" to a single chosen branch is performing operator-tier interpretation; it is not a platform output.

**Forbidden replay behaviors:**

- Auto-select "the primary branch" at replay time.
- Hide non-canonical branches from the default replay view.
- Compute a "preferred interpretation" across branches.
- Suppress branches whose `confidence` is lower (no such field exists; if added, would not warrant suppression).

---

## 10. Forbidden semantics

The following vocabulary is forbidden across all platform documentation, code comments, operator UI, alerts, exports, and replay overlays.

| Banned phrasing | Reason |
|---|---|
| "canonical investigation" | Implies a single authoritative branch; rejected per §4 |
| "final truth branch" | Truth claim outside the platform's mandate |
| "authoritative interpretation" | Same |
| "resolved market truth" | Ontology violation per [lip-ontology-boundaries §7](lip-ontology-boundaries.md) |
| "operator convergence" | Implies the platform computes / requires convergence |
| "analyst consensus" | Same |
| "collective interpretation" | Same; rejected per [lip-multi-operator §6](lip-multi-operator.md) |
| "preferred narrative" | Implies platform-side narrative selection |
| "winning interpretation" | Implies adjudication |
| "truth merge" | Implies merge-to-truth semantics |
| "the correct branch" | Same |
| "merge consensus" | Same |
| "primary investigation" (as a standing claim) | Permitted only as a per-operator UI sort key; never as a platform-side selection |
| "canonical replay" | Replay reconstructs visibility; "canonical" implies adjudication |
| "the team's final position" | Implies aggregated team verdict; the platform stores per-operator attribution |
| "the senior branch" | Implies operator hierarchy |

### 10.1 Approved replacement patterns

- "case A and case B share `origin_fingerprint` X; both are ACTIVE"
- "branch A was superseded by branch B per operator X's recapture at `ts`"
- "branches A and B coexist with different `note_type = conclusion` values"
- "replay `as_of = T` shows N visible branches"
- "branch A is STALE (`updated_at_ms` older than threshold); audit lineage preserved"
- "branch A archived; evidence rows retained"

---

## 11. Governance & maturity

| Aspect | Status |
|---|---|
| **This document** | Class A (documentation-only) per [lip-governance §2](lip-governance.md). Authorized during Operational Observation Period |
| **Adding `branch_id` / `parent_branch_id` / `fork_reason` / `fork_ts` / `superseded_by` / `abandoned_ts`** | Class B + Class E. NOT AUTHORIZED during Observation Period |
| **Adding `branch_state` enum** | Class B + Class E. NOT AUTHORIZED |
| **Adding hidden-review / blinded replay runtime mode** | Class B + Class E (inherited from [lip-multi-operator §9](lip-multi-operator.md)) |
| **Adding `visibility` scope on notes** | Class B + Class E (inherited from [lip-multi-operator §2](lip-multi-operator.md)) |
| **Auto-selecting / auto-merging branches at replay time** | Permanently forbidden by §4, §9.2 |
| **"Branch reliability score" / "operator quality score" / "branch correctness"** | Permanently forbidden by §6.1, §10 + [lip-governance §3 row 9](lip-governance.md) |
| **Maturity stage** | Observational. Existing branching surfaces (`origin_kind = reopen`, `origin_fingerprint`, `revision` on snapshots, supersession via `is_active`) are operational under this contract. Promotion to Operator-visible (formally) gated on actual multi-operator usage |

### 11.1 Critical invariants (cross-cutting)

| Invariant | Source / restated |
|---|---|
| **Branching preserves epistemic separation, not alternate market realities.** | §1.2 + cross-cutting ontology invariant |
| **Forking an investigation does not imply prior investigation invalidity.** | §3 |
| **Parallel branches may remain unresolved indefinitely.** | §4 |
| **Supersession is a workflow state, not a truth judgment.** | §7 |
| **Replay reconstructs historical visibility, not canonical interpretation.** | §9 |
| **Contamination affects interpretation surfaces, not runtime history.** | §8 |
| **Parallel unresolved branches are legitimate outcomes.** | §4 |
| **Staleness degrades interpretive authority, not historical existence.** | §6 |
| **The system preserves investigation lineage. It does not resolve epistemic disagreement automatically.** | §9.2 |
| **Replay visibility may alter later operator interpretation. This does not alter runtime history.** | §8 |

---

## 12. What this document is not

- Not a new runtime layer.
- Not a collaborative platform spec.
- Not a CRDT design.
- Not a distributed-systems essay.
- Not a merge-conflict resolution algorithm.
- Not an analyst ranking engine.
- Not a "branch quality" or "branch correctness" scoring system.
- Not a philosophy of disagreement.
- Not an ontology of "alternate market realities".
- Not authorization to add new schema fields during the Operational Observation Period.
- Not a claim that today's deployment exhibits multi-branch usage — today is single-user per [lip-multi-operator §1](lip-multi-operator.md).

It is a Class A documentation hardening pass that pre-commits the platform's branching, supersession, staleness, and contamination surfaces to attribution-without-authority semantics, rejects consensus / convergence / canonical-truth framings, and makes the existing implicit branching infrastructure (`origin_fingerprint`, `revision`, `reopen` lifecycle, supersession-via-active-flag) operate under explicit governance contracts.
