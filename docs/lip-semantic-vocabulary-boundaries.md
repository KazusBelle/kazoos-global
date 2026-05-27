# Semantic Vocabulary Boundaries — canonical companion

**Companion to:** [`docs/2026-05-23-architecture-freeze.md`](2026-05-23-architecture-freeze.md) §15 (phrase-compression reference, authoritative source for the cross-stack vocabulary discipline), [`docs/lip-governance.md`](lip-governance.md), [`docs/lip-ontology-boundaries.md`](lip-ontology-boundaries.md), all per-layer companions.

**Status: Class A documentation hardening pass.** Adds no code, no states, no emit fields. Codifies vocabulary-discipline contracts already partially documented in freeze §15 (phrase compression) and per-layer banned-vocabulary tables ([lip-execution-validation §15](lip-execution-validation.md), [lip-regime-engine §9 / §18](lip-regime-engine.md), [lip-causal-propagation §10](lip-causal-propagation.md), [lip-venue-quality §14](lip-venue-quality.md), [lip-multi-operator §13](lip-multi-operator.md), [lip-epistemic-branching §10](lip-epistemic-branching.md), [lip-ontology-boundaries §12.1](lip-ontology-boundaries.md)). Per [lip-governance §2](lip-governance.md), authorized during Operational Observation Period.

**Boundary statement (load-bearing).** Documentation, code comments, operator UI, alerts, exports, and replay overlays must use vocabulary whose every modifier and noun has a measurable referent in code, persistence, replay, or audit trail. Vocabulary that adds semantic authority without operational meaning is forbidden. This document enumerates the test, the modifier-risk taxonomy, the allowed replacements, and the enforcement contract.

---

## 1. Semantic overclaim — definition

A phrase is **semantically overclaiming** if it satisfies all of:

1. It modifies or qualifies a platform output, layer, metric, or process.
2. It evokes authority, depth, quality, intelligence, sophistication, trust, or reach.
3. **None** of the following questions can be answered against the phrase:

| Test question | If unanswerable → overclaiming |
|---|---|
| What is measured by it? | Yes |
| What is validated by it? | Yes |
| What is emitted by it? | Yes |
| What is persisted by it? | Yes |
| What is replayed by it? | Yes |
| What is degraded by it? | Yes |
| What is suppressed by it? | Yes |
| What is unknowable by it? | Yes |
| What is bounded by it? | Yes |
| What is versioned by it? | Yes |
| What is NOT implemented by it? | Yes |

If a phrase passes one or more tests with a concrete code / table / formula referent, it is **operational**. Otherwise it is **semantic inflation** and must be removed or rewritten per §13.

---

## 2. Operational-language invariant (load-bearing)

> **If a phrase adds authority without adding operational meaning, remove or rewrite it.**

Restated:

> **Semantic density is not a substitute for measurable specificity.**

A document, a UI string, a commit message, or a PR description that piles modifiers without referents is **less informative**, not more. Each modifier the reader sees without a measurable hook trains the reader to discount modifiers in general — including the modifiers that DO have hooks. Vocabulary discipline preserves the signal-bearing modifiers.

---

## 3. Modifier risk taxonomy

Modifiers fall into five risk tiers. Tier 4–5 require explicit per-use justification or removal.

| Tier | Class | Examples | Default treatment |
|---|---|---|---|
| **Tier 1** | Operationally pinned | `MEASURED`, `EXHAUSTED`, `DROPPED`, `DIRECTIONAL`, `CONFIRMED`, `EXPLORATORY` (code enum values); `append-only`, `forward-only`, `replay-bounded`, `refusal-first` | Allowed without justification |
| **Tier 2** | Bounded technical | `deterministic` (when paired with the specific function), `idempotent` (when verified), `monotonic` (when bounded by code), `bit-exact` (when test-covered) | Allowed when the binding is explicit nearby |
| **Tier 3** | Conditional | `bounded`, `versioned`, `audited`, `attributable` | Allowed when the contract is named (e.g., "audited per [lip-governance §10](lip-governance.md)") |
| **Tier 4** | Authority-implying | `sophisticated`, `advanced`, `mature` (outside the maturity-classes scale), `comprehensive`, `powerful`, `intelligent`, `robust`, `deep`, `nuanced`, `rich`, `layered`, `high-fidelity`, `high-quality`, `meaningful`, `realistic` | **Remove or replace** per §13 |
| **Tier 5** | Prestige / overclaim | `institutional-grade`, `forensic-grade`, `production-grade`, `world-class`, `elite`, `state-of-the-art`, `enterprise-grade`, `industry-standard`, `next-generation` | **Remove**. Freeze §15 already documents replacements for the first three |

**Per freeze §15 (lines 1509-1514):**
- `institutional-grade` → "replay-consistent measurement pipeline with append-only sample history"
- `forensic-grade` → "append-only deterministic as-of replay against retained history tables"
- `production-grade` → "depended-on for daily operation; covered by `/admin/runtime-health`"
- `world-class / elite / advanced / high-end` → *delete*

This companion extends the freeze table with Tier-4 modifiers below.

---

## 4. Allowed vs forbidden adjectives (Tier 4 extension)

| Forbidden adjective | Default replacement / removal |
|---|---|
| sophisticated | (remove) OR "with N gates / N refusal paths / N-step precedence" |
| advanced (as adjective) | (remove) OR cite the specific algorithmic property |
| mature (outside §9 maturity stages) | (remove) OR cite the maturity stage explicitly |
| intelligent | (remove) — implies inference; replace with the specific computation |
| robust | (remove) OR "tolerates X failure mode; cite the test" |
| comprehensive | (remove) OR list what is covered |
| powerful | (remove) — pure prestige term |
| deep (when applied to observability / understanding) | (remove) OR cite the specific surface depth (e.g., "top-20 depth20 frames") |
| nuanced | (remove) — substitute concrete distinction |
| rich (context / data) | (remove) — cite the specific fields |
| layered | (remove) OR list the layers |
| high-fidelity | (remove) OR cite the specific resolution / cadence |
| high-quality | (remove) OR cite the maturity class per [lip-governance §5](lip-governance.md) |
| meaningful | (remove) — implies semantic judgment; cite the threshold |
| realistic | (remove) — implies truth claim; cite the bounded measurement |
| reliable (without bounded measurement) | (remove) OR cite the bound |
| accurate (without a comparator) | (remove) OR cite the empirical comparator |

When an adjective passes only with a follow-up clause, the discipline is: **inline the clause, drop the adjective**.

Example:
- Before: "robust forward-only pipeline that tolerates upstream failures"
- After: "forward-only pipeline; on upstream failure the cursor advances without retry per [exec-impact.py:201](../shared/kazus_logic/liquidity/realtime/exec_impact.py#L201)"

---

## 5. Implied-understanding vocabulary

These words anthropomorphize the platform or imply inference the platform does not perform. **Forbidden across all surfaces.**

| Banned | Why | Replacement |
|---|---|---|
| understands | implies cognition the platform does not have | "classifies" / "labels" / "aggregates" |
| interprets | implies inference; out of mandate | "applies threshold ladder to" / "emits enum from" |
| recognizes | same | "matches input to" |
| sees | anthropomorphic | "reads from" / "subscribes to" |
| identifies true X | truth claim | "emits label X under thresholds" |
| detects intent | intent inference forbidden per [lip-governance §3 row 9](lip-governance.md) | (rejected — never permitted) |
| discovers structure | classification-not-discovery violation per [lip-ontology-boundaries §3](lip-ontology-boundaries.md) | "emits classification" |
| reconstructs causality | causation overclaim per [lip-causal-propagation §3](lip-causal-propagation.md) | "emits edges where lagged adjacency survived refusal gates" |
| meaningful signal | semantic judgment without contract | "value crossed configured threshold" |
| hidden stress | unobservable claim per [lip-execution-validation §10](lip-execution-validation.md) | (rejected) |
| latent pressure | same | (rejected) |
| sees through noise | anthropomorphic + truth claim | (rejected) |

---

## 6. Implied-truth vocabulary

Restates [lip-ontology-boundaries §12.1](lip-ontology-boundaries.md) for cross-stack consistency.

| Banned | Replacement |
|---|---|
| real liquidity | "visible top-20 walkable depth" |
| true market state | "observed configuration class" |
| actual structure | "emit at configured thresholds" |
| genuine signal | "value crossed threshold `θ = C`" |
| market reality | "observed market state" |
| authoritative replay | "replay reconstructs persisted emit at version tuple V" |
| reliable interpretation | (rejected — interpretation is operator-tier) |

---

## 7. Quality-claim discipline (load-bearing)

> **Quality claims without measurable referents are forbidden.**

A "quality" word is operational only if it cites a specific maturity class, validation result, or empirical bound.

| Quality claim | When allowed | When forbidden |
|---|---|---|
| "high-confidence X" | When `X` is a code enum value with a defined threshold (e.g., `confidence label = HIGH ≥ 0.70` per [lip-metric-registry §B.4](lip-metric-registry.md)) | When `X` is a free-form claim with no threshold citation |
| "validated X" | When `X` has passed a specific [lip-execution-validation §22](lip-execution-validation.md)-style acceptance contract | When `X` has not been measured |
| "calibrated X" | When `X` has progressed past L0 per [lip-execution-validation §23](lip-execution-validation.md) | When `X` is still at L0 (which is every threshold today) |
| "audited X" | When `X` is in an append-only event log per [lip-governance §10](lip-governance.md) | When the audit-trail path is unclear |
| "deterministic X" | When `X`'s output is a function of declared inputs with no hidden state | When `X` includes non-deterministic elements (model output, random sampling, wall-clock dependency) |
| "reproducible X" | When `X` is re-derivable from persisted state at the named version tuple | When `X` depends on pruned data or unstamped versioning |

If a quality word fails the "when allowed" condition, **remove it or downgrade it** to the specific operational property.

---

## 8. Confidence-language discipline (load-bearing)

> **Confidence vocabulary must map to explicit gating or degradation logic.**

| Phrasing | Bound required |
|---|---|
| "confidence X" | Must cite the formula producing X (e.g., per [lip-causal-propagation §11](lip-causal-propagation.md) `confidence = persistence_factor × ... × scarcity_factor`) |
| "high / medium / low confidence" | Must cite the enum thresholds (e.g., `HIGH ≥ 0.70 · MEDIUM ≥ 0.45 · LOW otherwise`) |
| "degraded confidence" | Must cite the degradation mechanism (multiplicative factor, refusal gate, exploratory flag) |
| "confidence demotion" | Must cite the demotion factor (e.g., `reversal_factor = 0.25` per [lip-regime-engine §11](lip-regime-engine.md)) |
| "we are confident that" | (forbidden — anthropomorphic) |
| "confidence level" | Must be either an enum (HIGH/MEDIUM/LOW) or a 0..1 scalar with a named source |

**Forbidden:** "confidence" used as a prestige adjective without a formula, an enum, or a citation.

---

## 9. Replay-language discipline (load-bearing)

> **Replay wording must describe reconstruction scope, not historical truth.**

Already load-bearing in [lip-ontology-boundaries §6](lip-ontology-boundaries.md). Restated for vocabulary discipline:

| Banned replay phrasing | Approved |
|---|---|
| "replay reconstructs what really happened" | "replay re-derives the layer's emit for `[since, now]` from persisted rows" |
| "historical truth" | "persisted emit history + audit-trail entries" |
| "exact market reconstruction" | "replay-bounded sequence reconstruction" (per [lip-causal-propagation §1.7](lip-causal-propagation.md)) |
| "authoritative replay" | "deterministic replay under version tuple `(schema_version, calibration_version)`" |
| "the replay shows the market" | "replay shows the emit at `as_of = T`" |
| "ground-truth replay" | (rejected — no ground truth claim) |
| "comprehensive replay" | "replay over `[since, now]` against retained tables" |
| "what the market did" | "what the platform recorded and what classifiers had emitted" |

---

## 10. Causality-language discipline (load-bearing)

Restates [lip-causal-propagation §10](lip-causal-propagation.md) for vocabulary discipline:

| Banned | Approved |
|---|---|
| "X caused Y" | "Y emission followed X emission within the configured `[5s, 30min]` window" |
| "X drove Y" | "edge X → Y emitted; HIGH confidence per formula citation" |
| "X led the market" | "X's out-dense node classification (legacy `LEADER` enum) at this window" |
| "transmission chain" | "replay-bounded sequence reconstruction" |
| "propagation source" | "first observed edge participant in the window" |
| "narrative causality" | "event chain reconstruction" (legacy function name `narrative_causality()` reframed per [lip-causal-propagation §7.4](lip-causal-propagation.md)) |
| "causal engine" | "propagation / event-chain reconstruction stack" |

---

## 11. Governance-language discipline

| Banned | Approved |
|---|---|
| "governance-compliant" | "passes [lip-governance §N](lip-governance.md) Class A/B/C/E declaration" |
| "fully audited" | "audit-trail entries exist per [lip-governance §10](lip-governance.md) for X mutations" |
| "production-ready" | (rejected per freeze §15) OR "depended-on for daily operation per `/admin/runtime-health`" |
| "battle-tested" | (rejected — prestige term) OR cite operational metrics |
| "best-in-class" | (rejected — prestige + comparative without comparator) |
| "industry standard" | (rejected — implies external authority not cited) |
| "fully versioned" | "carries `schema_version` + `calibration_version` per [lip-governance §8](lip-governance.md)" — and today this is **NOT IMPLEMENTED** platform-wide, so the claim is currently invalid |
| "complete coverage" | "covers X% of Y, measured per Z" with explicit numbers; otherwise (rejected) |

---

## 12. "Institutional-grade" audit (cross-stack)

Per freeze §15 line 1509: `institutional-grade` → "replay-consistent measurement pipeline with append-only sample history".

**This document affirms the freeze §15 disposition and extends it:**

| Phrase containing "institutional" | Disposition |
|---|---|
| "institutional-grade observability" | rejected — replace with "replay-consistent measurement pipeline with append-only sample history" (freeze §15) |
| "institutional execution" | rejected per [lip-execution-validation §15](lip-execution-validation.md) banned vocabulary table |
| "institutional flow" | rejected — not measurable from platform inputs (hidden / off-tape / OTC; per [lip-execution-validation §10](lip-execution-validation.md)) |
| "institutional intent" | rejected — intent inference forbidden per [lip-governance §3 row 9](lip-governance.md) |
| "institutional workflow" | rejected — substitute "multi-operator workflow under attribution + append-only audit" (per [lip-multi-operator.md](lip-multi-operator.md)) |
| "institutional consensus" | rejected per [lip-multi-operator §13](lip-multi-operator.md) (no consensus computation) |
| "institutional review" | (rejected) OR "operator review filed per `note_type = conclusion`" |
| "institutional-quality" | rejected — pure prestige modifier; remove |

**The word "institutional" is permitted only when modifying a real external referent** (e.g., "institutional crypto venue API" naming a specific protocol surface, not characterizing the platform).

---

## 13. Rewrite patterns

For every residual semantic phrase encountered in PR review, doc edit, or operator UI:

| Step | Action |
|---|---|
| 1 | Read the phrase aloud. Identify the modifier(s). |
| 2 | For each modifier, run §1 test questions. If none answerable → suspect. |
| 3 | If suspect: either (a) inline the operational referent and drop the modifier, or (b) remove the entire phrase. |
| 4 | Never substitute one vague modifier for another. |
| 5 | Cite the resulting operational referent (`code:line` / `companion §N` / `enum value` / `code threshold`). |
| 6 | If no operational referent can be cited, the original claim was unsupported — remove. |

### 13.1 Worked examples

| Original | Diagnosis | Rewrite / Removal |
|---|---|---|
| "sophisticated regime classifier" | "sophisticated" is Tier 4. No operational referent. | "regime-transition classifier with deterministic verdict precedence REVERSED > FLICKER > ACCELERATING > PERSISTENT per [lip-regime-engine §3.3](lip-regime-engine.md)" |
| "deep observability" | "deep" is Tier 4. No referent. | "depth20 frames + trade tape + per-symbol metric stream + alert + intelligence_history per freeze §1" |
| "robust replay" | "robust" is Tier 4. | "replay is deterministic for `(window, schema_version, calibration_version)` tuple per [lip-governance §4](lip-governance.md); calibration-version stamping NOT IMPLEMENTED today" |
| "intelligent stress detection" | "intelligent" is forbidden (anthropomorphic). | "seven-probe distributed-stress composite per freeze §14" |
| "comprehensive blind-spot inventory" | "comprehensive" is Tier 4. | "blind-spot inventory at [lip-execution-validation §10](lip-execution-validation.md), [§24](lip-execution-validation.md), [lip-epistemic-boundaries §2](lip-epistemic-boundaries.md), [§5](lip-epistemic-boundaries.md)" |
| "high-quality propagation edge" | "high-quality" is Tier 4 without referent. | "edge with `confidence label = HIGH` (`confidence_score ≥ 0.70`) per [lip-metric-registry §B.4](lip-metric-registry.md)" |
| "meaningful regime transition" | "meaningful" is Tier 4. | "transition with verdict ∈ {ACCELERATING, PERSISTENT}" |
| "realistic execution model" | "realistic" implies truth claim. | "execution validation comparing book-walk-predicted visible impact vs realized mid move at SETTLE_MS = 500 ms" |
| "trustworthy venue" | banned per [lip-venue-quality §14](lip-venue-quality.md). | "venue with OBSERVABLE state per §6 in the current window" |
| "production-grade audit trail" | per freeze §15 disposition. | "audit trail depended-on for daily operation per `/admin/runtime-health`" |

---

## 14. "Remove entirely" table — phrases with no operational contract

| Phrase | Disposition |
|---|---|
| "deep observability" | remove |
| "rich market context" | remove |
| "sophisticated view" | remove |
| "layered intelligence" | remove |
| "semantic insight" | remove |
| "strategic visibility" | remove |
| "comprehensive market awareness" | remove |
| "mature replay" | remove (use maturity stage explicitly if relevant) |
| "powerful refusal logic" | remove (cite the refusal-first invariant) |
| "intelligent filtering" | remove |
| "smart detection" | remove |
| "robust intelligence" | remove |
| "high-confidence structure" | remove (substitute the specific code enum + threshold citation) |
| "execution realism" | remove (substitute per [lip-execution-validation §15](lip-execution-validation.md) replacement: "match between book-walk prediction and realized mid move") |
| "market understanding" | remove (anthropomorphic) |
| "behavioral signal" | remove (semantic; substitute the specific metric) |
| "structural clarity" | remove |
| "forensic-grade visibility" | per freeze §15 → "append-only deterministic as-of replay against retained history tables" |
| "next-level observability" | remove |
| "advanced market interpretation" | remove (also violates [lip-ontology-boundaries §5](lip-ontology-boundaries.md)) |
| "systemic market behavior" | rejected (ontology claim; substitute "synchronized observable deterioration across N symbols" per [lip-ontology-boundaries §8](lip-ontology-boundaries.md)) |

---

## 15. Cross-stack enforcement contract

| Surface | Enforcement |
|---|---|
| PR description | Reviewer scans for Tier 4 / Tier 5 modifiers; flags before approval |
| Doc edit | Author runs the §1 test against every modifier; cites operational referent or removes |
| Commit message | Same as PR description |
| Operator UI | New label requires §13.1 worked-example mapping in the relevant companion's banned/approved table before deploy |
| Code comment | Tier 4–5 modifiers in code comments are equivalent to operator UI: replace with `code:line` citation or remove |
| Alert format | Banned modifiers in alert text violate [lip-governance §3 row 11](lip-governance.md) (semantic relabeling) |
| External communication / external report | Same as alert format; the public-facing surface is the strictest |
| Memory entries | Same discipline; semantic inflation in `[[memory-name]]` entries propagates through cross-references and is corrosive |

**This document does not introduce a runtime check.** Enforcement is human review per [lip-governance §10](lip-governance.md) audit trail. A future Class B candidate (NOT AUTHORIZED today): a static-analysis pre-commit hook that flags Tier 4–5 modifiers in docs.

---

## 16. Critical invariants (cross-cutting)

| Invariant | Source / restated |
|---|---|
| **If a phrase adds authority without adding operational meaning, remove or rewrite it.** | §2 |
| **Confidence vocabulary must map to explicit gating or degradation logic.** | §8 |
| **Quality claims without measurable referents are forbidden.** | §7 |
| **Replay wording must describe reconstruction scope, not historical truth.** | §9 + [lip-ontology-boundaries §6](lip-ontology-boundaries.md) |
| **Observability wording must describe instrumentation scope, not market understanding.** | [lip-ontology-boundaries §1](lip-ontology-boundaries.md) extended |
| **Semantic density is not a substitute for measurable specificity.** | §2 |
| **Never substitute one vague modifier for another.** | §13 step 4 |
| **The word "institutional" modifies external referents only, never the platform itself.** | §12 |

---

## 17. What this document is not

- Not a style guide for "good prose".
- Not a literary critique surface.
- Not a brand-voice spec.
- Not a marketing-language guideline.
- Not a runtime checker (today).
- Not philosophy about language.
- Not a comprehensive linguistics treatise.
- Not authorization to add or remove vocabulary from per-layer companions without their own update.

It is a Class A documentation hardening pass that codifies the cross-stack discipline against semantic-authority inflation. Per-layer companions retain their own banned-vocabulary tables; this document is the canonical *meta*-table, the test rubric, and the rewrite playbook. Future per-layer hardening passes inherit §1 test and §13 rewrite patterns by default.
