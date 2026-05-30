# System Governance & Change-Control Contract

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md), [`docs/lip-metric-registry.md`](lip-metric-registry.md), [`docs/lip-epistemic-boundaries.md`](lip-epistemic-boundaries.md), [`docs/lip-validation-and-calibration.md`](lip-validation-and-calibration.md), [`docs/lip-execution-validation.md`](lip-execution-validation.md).

**Boundary statement (load-bearing).** This document does not describe what the platform *is*. It constrains what the platform is **allowed to become**. Changes outside the contracts below are governance violations regardless of code-review approval, regardless of intent, and regardless of whether the change "works".

The platform is allowed to become more measurable, more replayable, more falsifiable, more observable. It is **not** allowed to become more interpretive, more inferential, more autonomous, less replay-consistent, less versioned, or less falsifiable. §11 restates this as a single invariant.

---

## 1. Scope of governance

**Binding scope.** This contract binds:

- All code under [`shared/kazus_logic/`](../shared/kazus_logic/) that emits, transforms, or persists liquidity-platform measurements.
- All migrations, table additions, or schema changes that touch persisted measurement state.
- All threshold constants referenced from the metric registry [Part A](lip-metric-registry.md) and Part B.
- All operator-facing surfaces that expose platform output (alerts, dashboards, exports).
- All replay-affecting code paths (Layer 12, Phase 19).
- All documentation that defines semantics for the above.

**Out of scope.** Internal refactors that do not change emitted values, log lines, test fixtures, dev tooling, and CI configuration. These remain ordinary engineering changes.

**Precedence.** Where this document conflicts with a layer-specific companion, this document wins for governance questions and the layer companion wins for measurement semantics. Where this document conflicts with the freeze, the freeze wins for architecture and this document wins for change-control.

---

## 2. System change classification

Every change to in-scope code or documentation falls into exactly one class. Class is determined by the change's *effect*, not by the author's *intent*.

| Class | Definition | Examples | Replay impact | Validation required | Allowed during Observation Period | Calibration reset | Version bump | Replay invalidation | Operator disclosure |
|---|---|---|---|---|---|---|---|---|---|
| **A — Documentation-only** | No code change; doc/comment/memory update. No constant change | This document. Companion edits | None | None | Yes | No | Doc revision footer | None | None |
| **B — Measurement-layer change** | Code change that alters how an emitted value is computed, even if the value distribution is unchanged | Rewrite of `_walk`; change to `_find_pre_snapshot` semantics | Per-burst output may shift | Golden-vector regression; before/after distribution check on `liquidity_samples` | Only if Class A-equivalent in emit (no distribution shift); else NO | Yes — affected anchors revert to L0 per [lip-execution-validation §23](lip-execution-validation.md) | `schema_version` bump on affected output | Pre-change samples flagged as previous `schema_version` | Required if operator-visible metric affected |
| **C — Calibration change** | Threshold constant value change. No code-path change | `EXPECTED_FLOOR_BPS: 0.5 → 0.7` | Historical samples not directly comparable across boundary | §22 acceptance contract row for the threshold ([lip-execution-validation.md](lip-execution-validation.md) §22) | NO — calibration changes gated on observation period exit | Yes — affected threshold returns to its qualifying maturity per §5 | `calibration_version` bump | Pre-change samples flagged as previous `calibration_version` | Required, with the §22 acceptance record link |
| **D — Runtime-behavior change** | Code change that alters when an emit occurs, the order of emits, or the suppression conditions | Reordering precedence checks; changing pruning cadence | Per-event sequence shift possible | Determinism re-verification; emit count regression test | NO except for performance fixes that prove emit-equivalence | Conditional on whether emit values shift | `runtime_generation` bump (see §8) | Pre-change samples flagged as previous `runtime_generation` | Required |
| **E — Replay-affecting change** | Code or schema change that alters the ability of Layer 12 to reproduce historical state from persisted inputs | New persisted field; column type change; retention policy change; encoding change | Direct — replay is the affected surface | Replay reproducibility re-test; before/after read-back parity on representative window | NO except for additive, append-only fields with no behavior dependence | No (replay is structural, not calibrational) | `schema_version` bump | Affected window flagged in replay catalog | Required |
| **F — Observability→decision leakage** | Any change that turns an observation into an automated action, ranking, recommendation, or other consumer of platform output beyond diagnostic display | Adding an alert that triggers a trade action; auto-promoting a symbol; composite score used for routing | Behavioral — output is no longer purely observational | All of §6 firewall requirements (currently unsatisfiable during Observation Period) | NO | (irrelevant — change is forbidden during Observation Period regardless) | (n/a) | (n/a) | (n/a — the change itself is rejected) |
| **G — Forbidden change** | See §3 | (see §3) | (rejected at review) | (rejected at review) | NO and forever | (n/a) | (n/a) | (n/a) | (n/a) |

**Determination rule.** If a change matches multiple classes, the **most restrictive** class applies (later letters more restrictive than earlier). A PR description must declare the class. A missing or wrong class is itself a governance violation.

---

## 3. Forbidden changes (Class G — explicit list)

Each item is forbidden absolutely. The "WHY / WHAT BREAKS / INVARIANT VIOLATED" columns make rejection auditable.

| Forbidden change | Why forbidden | What breaks | Invariant violated |
|---|---|---|---|
| **Hidden ML weighting** of any emit (model output blended into a metric without explicit `model_id`, `version`, and operator-visible disclosure) | Emit value becomes non-falsifiable from inputs alone | Replay reproducibility, calibration governance, allowed-claims contract | "deterministic measurement" — §11 invariant |
| **Silent threshold mutation** (changing `CREDIBLE_BAND_PCT`, `EXPECTED_FLOOR_BPS`, etc. without `calibration_version` bump and disclosure) | Cross-window aggregations silently mix constants | Class C contract; [lip-validation-and-calibration §5](lip-validation-and-calibration.md) | "calibration-version dependency" — load-bearing |
| **Non-versioned calibration updates** | Historical comparability collapses; analyses become unreproducible | All §22 acceptance contracts; replay determinism | §7 versioning contract |
| **Retroactive replay mutation** (changing the result of replaying a window without changing inputs) | Replay ceases to be a function; becomes an evolving narrative | Class E contract; Phase 19 freeze | §4 replay stability |
| **Silent backfill** (writing rows with `ts` predating activation, or interpolating gaps) | Forward-only invariant violated | [lip-execution-validation §9](lip-execution-validation.md); freeze §11 epistemic boundaries | "forward-only" — load-bearing |
| **Hidden score blending** (composite emitted without §6 declaration) | Operator sees a number with no traceable derivation | §6 composite creation contract | "no soft composites" |
| **Autonomous ranking promotion** (a diagnostic ranking becoming an input to automatic prioritization, alerting precedence, or routing) | Diagnostic → action firewall breached | §5 firewall | "platform output is diagnostic-only" |
| **Execution recommendations** (any output of the form "trade X", "size Y", "enter/exit/avoid", explicit or implicit) | Platform exits its declared mandate | §5 firewall; freeze §0.1 "what the platform does not do" | "No output produced by the platform constitutes execution advice" |
| **Inferred intent scoring** (any field claiming to score actor intent, strategy, or motive) | Out of epistemic mandate | [lip-epistemic-boundaries §2 non-inference list](lip-epistemic-boundaries.md) | "no participant intent" — load-bearing |
| **Hidden confidence modifiers** (multipliers, gating factors, or weightings on emits without explicit field on the output and §6 declaration) | Emit no longer linearly tied to documented inputs | §6 composite contract | "deterministic measurement" |
| **Semantic relabeling without versioning** (a published metric name continues to be emitted but its definition changes) | Downstream analyses silently misinterpret | Class B + §7 | "no semantic mutation under stable name" |
| **LLM-generated narratives in operator surfaces** | Non-deterministic, non-replayable, non-auditable | Replay invariant; [Operational Observation Period](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_operational_observation_period.md) inventory | "deterministic replay" |
| **Auto-trading / auto-resolve / auto-action logic** anywhere downstream of platform output | Action firewall breach by construction | §5 firewall | "platform output is diagnostic-only" |
| **Propagation causality semantics expansion** beyond [lip-epistemic-boundaries §3](lip-epistemic-boundaries.md) | Epistemic ceiling violation | epistemic-boundaries §3 invariant | "no causal inference beyond simultaneity rule" |

Rejection of a Class G change is **not** a code-review decision — it is a governance fact. A reviewer who approves a Class G change is acting outside their authority; the PR is invalid regardless of merge state.

---

## 4. Replay stability contract

**1. Purpose.** Define what may and may not change about a replay's output across time, code revisions, and calibration changes.

**2. Scope.** Every emit consumed by Layer 12 (Replay Reconstruction Engine, Phase 19) and every persisted row in `liquidity_samples`, `liquidity_alert_history`, `frozen_snapshots`, and any future replay-input table.

**3. What is allowed to vary across replays of the same window:**

- Annotations attached at the operator tier (notes, tags, investigation links) — these live outside the replay-input tables.
- Display labels in UI surfaces — strictly presentation.
- Overlay rendering choices (colors, axis ticks, accordion default-open state) — presentation.
- `calibration_version` *visibility* (which version label is shown alongside a sample) — informative; does not change the sample value.
- Documentation text in companion docs.

**4. What is NOT allowed to vary across replays of the same window:**

- Values of persisted samples written under a given `calibration_version` and `schema_version`.
- Emitted enum states (e.g., `MEASURED`, `EXHAUSTED`, `DROPPED` per [lip-execution-validation §4](lip-execution-validation.md); refusal verdicts per [lip-epistemic-boundaries §3.3](lip-epistemic-boundaries.md)).
- Historical outputs under the same `(schema_version, calibration_version, runtime_generation)` tuple.
- Event boundaries — burst start/end timestamps, recovery event ts, alert ts.
- Refusal/abstain verdicts and their reason codes.

**5. Runtime implications.** A replay engine implementation that produces different output for the same `(window, version_tuple)` inputs is broken, not "improved". Determinism is the contract; performance is a separate axis.

**6. Replay drift classification.**

| Drift class | Meaning | Permitted handling |
|---|---|---|
| **Acceptable drift** | Variation only in §4(3) Allowed list | Continue without action |
| **Versioned drift** | Variation in §4(4) but accompanied by a `schema_version` or `calibration_version` change that explains it | Read with the version-stamped row; do not aggregate across version boundary without explicit operator decision |
| **Invalidating drift** | Variation in §4(4) without a corresponding version change | Governance violation. Stop, write incident, identify the silent mutation, version it retroactively or revert |

**7. Validation implications.** Replay reproducibility is in the [lip-execution-validation §22](lip-execution-validation.md) governance contract as "bit-exact match on golden vectors". Extending to full-window replay requires per-window golden snapshots, which are not currently captured.

**8. Failure modes.** (a) `liquidity_samples` overwrite — currently prevented by append-only discipline; any migration that introduces UPDATEs is a Class E change. (b) Frame loss during ingestion creating non-deterministic windows — already documented as a coverage gap, surfaces as DROPPED outcomes in exec_impact and as gaps in resiliency events. (c) Pruning before replay — `book_history` ring (≤60 snapshots) means real-time per-burst replay is structurally unavailable past the ring window (see [lip-execution-validation §20-§21](lip-execution-validation.md)).

**9. Versioning requirements.** Every replay-input row MUST eventually carry `(schema_version, calibration_version)`. Currently NOT IMPLEMENTED — see §8. Until implemented, the entire history is implicitly version `(v0, c0)` and any threshold or schema change creates an undetectable boundary, which is a known governance debt tracked here and in [lip-validation-and-calibration §5](lip-validation-and-calibration.md).

---

## 5. Calibration governance — threshold lifecycle

Every threshold constant in [lip-metric-registry Part A](lip-metric-registry.md) and Part B occupies exactly one class below. Class transitions require the §22 governance contract from [lip-execution-validation.md](lip-execution-validation.md) (or its equivalent for non-exec metrics).

| Class | Where allowed | Affects runtime? | Affects replay? | Operator-visible? | Allowed in composites? | Allowed in alerting? | Empirical validation? |
|---|---|---|---|---|---|---|---|
| **Implementation constant** | Source code only | Yes (it IS the runtime) | Yes (defines the emit) | No (do not surface unvalidated values as "anchors") | No | No | Not required (but anchor is L0 per [lip-execution-validation §23](lip-execution-validation.md)) |
| **Observation-period anchor** | Source code + companion doc §12-equivalent | Yes | Yes | Yes — visible in documentation, not in operator dashboard alert text | No | No | Required to advance |
| **Calibration candidate** | Source code + governance contract entry with proposed value and acceptance criteria | No (candidate; runtime still uses observation-period anchor until promoted) | No (same) | Yes — in governance doc, not in product | No | No | Required to advance |
| **Validated operational threshold** | Source code, companion docs, operator dashboards | Yes | Yes | Yes | Yes — with explicit §6 declaration | Yes — with explicit acceptance record | Already passed |
| **Deprecated threshold** | Source code only (transitional); marked for removal | Yes only behind a compatibility flag for replay of historical windows | Yes — only for windows whose `calibration_version` predates deprecation | No (do not display) | No | No | Already passed (historical) |
| **Forbidden threshold** | Nowhere; pre-rejected by §3 (e.g., any threshold gating a Class F emit) | Never | Never | Never | Never | Never | Never |

**Current state.** Every threshold in the system today is either Implementation constant or Observation-period anchor. No threshold is currently Validated operational. The "validated operational" class is reachable only after Operational Observation Period exit + completion of [lip-execution-validation §22](lip-execution-validation.md) (or equivalent for the threshold's owning layer).

---

## 6. Observation → action firewall

**1. Purpose.** Prevent any platform output from becoming an input to automated action, ranking-as-action, or recommendation. Formalize the boundary the system already operationally observes but does not contractually enforce.

**2. Scope.** Every emitted metric, every alert, every replay overlay, every operator-facing surface that consumes any of the above.

**3. What is allowed:**

- Diagnostics — surfacing measurements for operator inspection.
- Replay overlays — annotating historical windows.
- Operator investigation — manual workflows on Phase 18 surfaces.
- Ranking within diagnostics — sorting symbols by a measured quantity (e.g., highest `fragility_score`) for operator attention, **provided** the ranking is visible, the sort key is named, and the ranking does not gate other actions.
- Degradation tagging — marking samples as PRUNED / INSUFFICIENT / EXHAUSTED / UNDER_EVIDENCED.

**4. What is NOT allowed:**

- Trade recommendations of any form (explicit "buy X", implicit "X is preferred", or score → trade mapping documented anywhere).
- Autonomous execution — any code path where a platform output crosses into an order-management or routing layer.
- Auto-ranking of "best trades" or "best opportunities" — even diagnostic ranking is disallowed when the framing implies trading desirability.
- Hidden signal generation — emits not enumerated in the metric registry.
- Execution routing decisions informed by platform output without an out-of-platform human decision in between.
- Position sizing logic anywhere downstream of platform output.
- Autonomous prioritization of assets for trading.

**5. The "no execution advice" clause (load-bearing).**

> No output produced by the Liquidity Intelligence Platform constitutes execution advice. This statement holds across all surfaces, all replay outputs, all exports, all alert formats, and all future extensions. A consumer of platform output who infers an action from it does so outside the platform's mandate; the platform neither endorses nor recognizes that inference.

**6. Runtime implications.** Any new code path that reads a platform emit and writes to a non-diagnostic destination requires explicit governance review with a §11 invariant check. Adding a webhook, a queue write, or a cross-service emit are each cases that trigger this review.

**7. Replay implications.** Replay outputs are subject to the same firewall. A replay surface that surfaces a "would-have-traded" annotation is a Class F change.

**8. Failure modes.** Firewall is enforced socially today (operator discipline, code review, this document) not technically. There is no runtime check that prevents a platform emit from being consumed by an order router. Technical enforcement (e.g., explicit topic separation, output ACLs) is a Class A documentation TODO; not implemented.

**9. Versioning requirements.** N/A — the firewall is a categorical contract, not a versioned threshold.

---

## 7. Composite creation contract

**1. Purpose.** Prevent "soft composites" — derived emits whose construction is implicit, partially documented, or untraced.

**2. Scope.** Any new emit that is a function of two or more existing emits, or a function of one emit plus an external input.

**3. Declaration requirements.** Every new composite MUST declare, in the metric registry or companion doc, all of:

- **Upstream metrics** — exhaustive list with stable names and their owning layers.
- **Weights** — numeric, named constants. No "tuned" or "learned" weights without §3 forbidden-changes check (an ML-learned weight is a hidden ML weighting per §3 row 1).
- **Thresholds** — every cutoff applied within the composite construction, named.
- **Normalization** — exact formula or "none" if values are raw.
- **Stale behavior** — what happens when one input is missing or pruned; default MUST be "composite suppressed", not "treat as zero".
- **Replay behavior** — composite must be replayable from persisted upstream values. If any upstream is not persisted, composite is NOT replayable and this fact MUST be documented.
- **Failure behavior** — exhaustive list of input states that cause composite suppression.
- **Calibration status** — maturity class per §5 for each threshold/weight.
- **Blind spots** — same discipline as [lip-execution-validation §10](lip-execution-validation.md).
- **Validation state** — measured vs pending per [lip-execution-validation §13](lip-execution-validation.md) equivalent for this composite.

**4. What is forbidden:**

- Composites whose weights are derived from rolling fits, optimization, or any non-constant procedure (Class G — hidden ML weighting).
- Composites that emit a value when any upstream is missing.
- Composites that have no named entry in [lip-metric-registry.md](lip-metric-registry.md).
- "Score" emissions that lack the above declarations.

**5. Runtime implications.** A new composite is at minimum a Class B change. If it adds a new persisted column, it is also a Class E change. The more restrictive class applies.

**6. Replay implications.** If any upstream of the composite is not in the replay-input persistence tier, the composite is non-replayable and §4 invalidating drift is structurally possible. In that case, the composite MUST NOT be emitted to operator surfaces.

**7. Validation implications.** A composite cannot be at a maturity higher than its lowest-maturity input.

**8. Failure modes.** (a) Soft composite emerges via incremental commits each below the disclosure bar — countered by §2's "most restrictive class applies" rule. (b) Composite migrates to a different formula under the same name — Class B + §3 forbidden row 11 (semantic relabeling).

**9. Versioning requirements.** Composite is part of `schema_version`. Weight changes are `calibration_version` bumps.

---

## 8. Layer versioning contract

**1. Purpose.** Make every emit traceable to the exact code revision and calibration set that produced it.

**2. Scope.** All persisted emit rows, all replay-input tables, all alert records, all frozen snapshots.

**3. Required version fields on every persisted emit.**

| Field | Meaning | Bumped by |
|---|---|---|
| `schema_version` | Identifies the structure and computation of the emit. Bumped by Class B and Class E changes | Any change to `_walk`, `_measure`, persisted schema, or emit-shape contract |
| `calibration_version` | Identifies the set of threshold constants live when the emit was produced | Any Class C change (any threshold value change) |
| `runtime_generation` | Identifies the process generation (incremented per worker restart) — useful for tracing emit batches to a specific runtime instance | Worker restart |
| `observation_period_state` | One of: `OBSERVATION` (current, since 2026-05-25), `VALIDATED_OPERATIONAL`, `MIGRATION_PAUSED` | Operator-controlled state machine |

**4. What is allowed:**

- Emits without these fields **only** for windows preceding the implementation of versioning. Those windows are implicitly `(v0, c0)` and cannot be aggregated with post-versioning windows without explicit operator awareness.

**5. What is forbidden:**

- Mutating any of the four fields on a persisted row after write.
- Emitting a value under a `calibration_version` that has not been published in the governance audit trail (§9).
- Replaying a window using a different `calibration_version` than the row carries and presenting the result as equivalent.

**6. Runtime implications.** Versioning fields must be set at emit time, by the layer that owns the emit, with values pulled from a single source-of-truth registry (currently NOT IMPLEMENTED).

**7. Replay implications.** Replay engine MUST read the version tuple of every persisted row and refuse to aggregate across boundaries silently. Refusal verdict: `VERSION_BOUNDARY_CROSSED`.

**8. Failure modes.** (a) Version fields not yet implemented — current state. Until implemented, every Class B/C change creates undetectable replay drift. Tracked as governance debt; the highest-priority technical follow-up to this document. (b) Version field set incorrectly (e.g., to a constant) — Class B change reverts to undetectable state.

**9. Versioning requirements.** This document is itself revisioned via Class A footer (date + brief delta). The versioning **contract** itself is a Class A artifact; the versioning **implementation** is Class E (it changes the schema of every persisted table).

---

## 9. Operational maturity gates

Maturity progression for a layer (not for individual thresholds — those are in §5).

| Stage | Required to enter |
|---|---|
| **Experimental** | (default; any new layer starts here) |
| **Observational** | (a) Sample coverage ≥ 30 days continuous emit across the active symbol set without DROPPED-rate excursion; (b) determinism re-verified post any Class B change; (c) blind-spot inventory documented (equivalent to [lip-execution-validation §10](lip-execution-validation.md)); (d) at least one operator review with no blockers raised |
| **Operator-visible** | (a) Observational stage held for ≥ 30 days; (b) refusal-path coverage — every documented failure mode has at least one occurrence in the emit history (proves the suppression paths fire); (c) operator review with explicit sign-off to surface the metric on a dashboard |
| **Validated-operational** | (a) Operator-visible held for ≥ 90 days; (b) calibration evidence per §22 acceptance contract for every threshold the layer depends on; (c) replay-stability check passed on representative windows; (d) external review (peer engineer + operator + one other) signed off in writing |

**Demotion.** A layer at any stage can be demoted to the previous stage (or removed) at any time by an operator-initiated incident. Demotion bypasses the time gates; promotion does not.

**Current state.** Every L1+ layer in the platform is at **Observational**. None are Validated-operational. The Operational Observation Period is the prerequisite for any progression.

---

## 10. Governance audit trail

**1. Purpose.** Make governance-affecting changes visible, attributable, and replay-coherent.

**2. Scope.** Every Class C, D, E change; every layer promotion or demotion; every composite addition; every suppression-rule mutation; every forbidden-list addition.

**3. Required for each governance event:**

- **Versioned** — change carries a unique identifier (commit SHA + governance event ID).
- **Timestamped** — UTC timestamp of the change.
- **Attributable** — author + reviewer named.
- **Replay-visible** — emits produced before vs after the change are distinguishable via §8 version fields.
- **Audit-recorded** — entry in a governance changelog (currently this document's footer; promoted to a dedicated `lip-governance-changelog.md` once the changelog exceeds 30 entries).

**4. What is forbidden:**

- Class C, D, or E change without an audit trail entry.
- Threshold change committed under a Class A label.
- Backdated audit entries.

**5. Runtime implications.** Until §8 versioning is implemented, audit trail is the **only** mechanism distinguishing pre- and post-change windows. Discipline on audit completeness is correspondingly load-bearing.

**6. Replay implications.** Replay reading a row whose write-time is between two audit entries must resolve the version tuple from the audit log (until §8 versioning is in-row).

**7. Validation implications.** A validation that crosses an audit-trail event without acknowledging it is invalid by construction.

**8. Failure modes.** (a) Audit entry omitted — governance violation; remediation is a retroactive entry plus a postmortem on the omission. (b) Audit entry inaccurate — equivalent to Class G semantic relabeling. (c) Audit changelog growing unbounded — addressed by the 30-entry threshold above.

**9. Versioning requirements.** Audit changelog itself follows append-only discipline; entries are immutable post-write.

---

## 11. Final invariant

The platform is allowed to become:

- more measurable,
- more replayable,
- more falsifiable,
- more observable.

The platform is **NOT** allowed to become:

- more interpretive,
- more inferential,
- more autonomous,
- less replay-consistent,
- less versioned,
- less falsifiable.

This invariant is the parent of every contract in §§1–10. Any proposed change passes governance if and only if it strictly contracts the "allowed" axis without expanding any "not allowed" axis. A change that simultaneously increases measurability and increases autonomy is a Class F or Class G change; the autonomy increase wins.

**Enforcement of the invariant** is the union of: §2 classification + §3 forbidden list + §5 calibration class + §6 firewall + §7 composite contract + §8 versioning + §9 maturity gates + §10 audit trail. Any future hardening pass extends one of these surfaces; none of them are decorative.

---

## 12. Relationship to other documents

| Document | Relationship |
|---|---|
| [2026-05-23-architecture-freeze.md](2026-05-23-architecture-freeze.md) | Architecture description; this document constrains how the architecture is allowed to evolve. Where they conflict on change-control, this document wins |
| [lip-metric-registry.md](lip-metric-registry.md) | Inventory of emits; every new emit added to it must pass §2 + §3 + §7 |
| [lip-epistemic-boundaries.md](lip-epistemic-boundaries.md) | What the platform cannot infer; §3's "inferred intent scoring" and "propagation expansion" forbidden rows pin to it |
| [lip-validation-and-calibration.md](lip-validation-and-calibration.md) | What "validated" means; §5 of the present doc maps every threshold to a class consistent with the calibration-version dependency declared in §5 of that doc |
| [lip-execution-validation.md](lip-execution-validation.md) | Layer-specific contract for exec_impact; §22/§23/§26 there are the template for non-exec layer governance (calibration acceptance, maturity levels, allowed-claims contract) |
| [Operational Observation Period memory](../.claude/projects/-home-deploy-workspace-kazus-global/memory/project_operational_observation_period.md) | Defines the current period; this document's §3 forbidden list and §9 maturity gates reference the period's allowed/disallowed inventory |

---

## 13. What this document is not

- Not a feature spec.
- Not an architecture description.
- Not a redesign.
- Not an AI integration plan.
- Not philosophy.

It is a contract. Sections §2 through §10 are operative; §1, §11, §12, §13 are framing. A proposed change is evaluated against §2 (classification) and §3 (forbidden list) first; everything else follows from the class.

---

## 14. Governance changelog (audit trail, §10)

Append-only. Entries are immutable post-write (§10.9). Promoted to a dedicated `lip-governance-changelog.md` once this exceeds 30 entries.

| Event ID | UTC date | Class | Change | Observation-Period basis | Author / reviewer | Replay-visible distinction |
|---|---|---|---|---|---|---|
| `2026-05-29-06` | 2026-05-29 | B + E | **Resiliency hardening (PHASE 4A).** New append-only table `liquidity_resiliency` + `realtime/resiliency.py`: burst-synchronized per-episode recovery of `credible_depth` toward the **pre-burst baseline** (Option A; `t0 = burst_end_ts + SETTLE_MS`), reusing the **shared** `iter_settled_bursts` boundaries (no second grouping). Adds replay-deterministic append-only persistence + explicit refusal states `{MEASURED·UNKNOWN·INSUFFICIENT·DROPPED}` (frozen) to the existing resiliency primitive. **Classified by audit as Class A hardening/completion**, NOT a new primitive (recovery_time/refill already existed). `resiliency_score` / `intelligence.py` **unchanged** (verified). OUT OF SCOPE & not built: `resiliency_ratio`, spread-normalization, any new score/verdict. | Operator-authorized after architecture audit (hardening, exempt from the new-observation-report gate). Class E carve-out: additive append-only table via `create_all`; no behaviour dependence — existing emits/`liquidity_samples`/3A/3B untouched. | Nikita Oliinyk / pending review | New table; episode reproducible from credible_depth series + shared burst boundaries (replay-deterministic). `RECOVERY_FRACTION=0.80`, `RECOVERY_MAX_AGE_MS=90000`, `RES_PRE_STALENESS_MS=3000`, `RES_GAP_MS=5000` interim (Class C). |
| `2026-05-29-04` | 2026-05-29 | B + E | **Runtime Health Telemetry (WS_RELIABILITY_001).** New append-only table `liquidity_runtime_health` + a heartbeat coroutine `_health_loop` + in-memory stage probes. Localizes the runtime failure boundary via a **pure deterministic classifier** over persisted numerics → `failure_boundary ∈ {HEALTHY · FEED_NETWORK_SILENCE · CONSUMER_STALL · PERSISTENCE_BOTTLENECK · SCHEDULER_STARVATION · DOWNSTREAM_OF_INGEST_SUCCESS}` (frozen). Diagnostic-only; asserts only instrument-provable boundaries (queue-backlog explicitly non-instrumentable; no Binance-vs-network sub-attribution; SCHEDULER_STARVATION never names the blocking call). **Revised authorization (same day):** PERSISTENCE_BOTTLENECK requires flush activity to explain the **majority** of loop lag (`flush_contribution ≥ PERSISTENCE_LAG_FRACTION=0.5 × loop_lag`) — flush *occurrence* alone is insufficient; otherwise SCHEDULER_STARVATION. Contract: [lip-runtime-health.md](lip-runtime-health.md). | Permitted Observation-Period category *operational tooling / telemetry for measuring frictions*. Class E carve-out: additive append-only table via `create_all`; **no behaviour dependence** — only reads progress, appends a separate table; zero change to metric computation / state taxonomies / Phase 2·3A·3B / `liquidity_samples`. Heartbeat excluded from run()'s FIRST_COMPLETED set so a diagnostic failure cannot disrupt ingestion. Operator-authorized (WS_RELIABILITY_001). Diagnostic-only — NO corrective action. | Nikita Oliinyk / pending review | New table; classifier reproducible from each row (replay-deterministic). Thresholds Class C. Finer labels require separate review. |
| `2026-05-29-03` | 2026-05-29 | B + E | **Execution Validation per-burst records (PHASE 3B).** New append-only table `liquidity_exec_validation` — one row per settled burst with `execution_validation_state ∈ {MEASURED·EXHAUSTED·INSUFFICIENT·DROPPED·UNKNOWN}` (frozen set; NO new states e.g. CONTAMINATED without separate review) + expected/realized/divergence_bps + divergence_label (POSITIVE/NEGATIVE, sign only) + exhaustion_state. Reuses existing measurement math: `_measure` refactored to a single shared `evaluate_burst` core (behaviour-preserving — 14 exec_impact tests unchanged & green) consumed by both rolling-median path and the 3B records. Same shared burst boundaries (`iter_settled_bursts`) — no second grouping. Splits the old coarse silent DROPPED into INSUFFICIENT/DROPPED/UNKNOWN by proximate cause; refusal-first explicit. Contract: [lip-execution-validation §4](lip-execution-validation.md). | Class E carve-out (additive append-only table via `create_all`; no behaviour dependence — rolling medians + ExecEvent path unchanged; nothing downstream consumes the table). Operator-authorized (PHASE 3B). Completion of an already-implemented primitive, not capability expansion. | Nikita Oliinyk / pending review | New table — pre-change windows lack it; rolling-median emit unchanged so no version bump there. Constants inherited (Class C). |
| `2026-05-29-02` | 2026-05-29 | B + E | **Burst Detection (PHASE 3A).** Added standalone burst records to a new append-only table `liquidity_bursts` (`burst_start_ts/end_ts/duration_ms/trade_count/notional/side` + UNKNOWN/INSUFFICIENT/DROPPED refusal markers). Burst boundaries factored into one shared primitive `burst.iter_settled_bursts`, now consumed by both burst records and `exec_impact` (behaviour-preserving refactor; exec_impact's 14 tests unchanged & green). On `@trade` sensor (aggTrade unavailable on this perimeter). Contract: [lip-burst-detection.md](lip-burst-detection.md). | Class E carve-out (additive, append-only table via `create_all`; no migration tool; no behaviour dependence — nothing downstream consumes the table; exec_impact emit unchanged). Operator-authorized (PHASE 3A). Completion of an already-declared/implemented primitive (§4 Execution Validation), not capability expansion. | Nikita Oliinyk / pending review | New table — pre-change windows simply lack it; no existing-output version bump. BURST_GAP_MS/SETTLE_MS/BURST_WARMUP_MS interim (Class C). |
| `2026-05-29-01` | 2026-05-29 | B + E | Added `persistence_quality` realtime emit — a measurement-quality self-assessment for Credible Depth (freshness · coverage · continuity over the depth20 frame sequence; `[0,1]`, `None` for UNKNOWN/INSUFFICIENT). Contract: [lip-metric-registry §A.1b](lip-metric-registry.md#a1b-persistence-quality-persistence_quality) + [lip-credible-depth-persistence.md](lip-credible-depth-persistence.md). | Permitted under the **Class E carve-out for additive, append-only fields with no behaviour dependence**: changes no existing emit's value/distribution/timing/order/suppression; nothing downstream consumes it (no composite, no §6 firewall surface); no schema migration (new metric name, new rows in key/value `liquidity_samples`). Operator-authorized (PHASE 2B). Not a new intelligence layer / spoof-manipulation detector / venue-quality / trust score; increases measurability without increasing inference or autonomy (§11). | Nikita Oliinyk / pending review | New metric name — pre-change windows simply lack the row; no existing-output version bump needed. Interim constants are a pending Class C calibration item. |
