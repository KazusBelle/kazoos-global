# Governance Consistency Audit — cross-surface verification companion

**Companion to:** [`docs/lip-governance.md`](lip-governance.md), [`docs/lip-semantic-vocabulary-boundaries.md`](lip-semantic-vocabulary-boundaries.md), [`docs/lip-ontology-boundaries.md`](lip-ontology-boundaries.md), [`docs/lip-execution-validation.md`](lip-execution-validation.md), [`docs/lip-causal-propagation.md`](lip-causal-propagation.md), [`docs/lip-regime-engine.md`](lip-regime-engine.md), [`docs/lip-multi-operator.md`](lip-multi-operator.md).

**Status: Class A audit-only companion.** Verifies cross-surface compliance with the discipline already documented in the companions above. **No code changes. No new contract content.** Surfaces audited: API endpoint paths, UI strings, TypeScript / Python code comments, DB schema enum values, alert text. Findings are classified by [lip-governance §2](lip-governance.md) Class so the operator can choose what (if anything) to remediate.

**Cross-cutting invariant audited:** *the platform emits bounded observational classifications under current instrumentation constraints; it does not establish authoritative market ontology, does not provide execution guidance, does not score operators or venues, and does not anthropomorphize itself.*

---

## 1. Audit scope

| Surface | Method |
|---|---|
| API endpoint paths | grep `@router\.(get\|post\|patch\|put\|delete)\(` against banned terms in `backend/app/api/` |
| UI strings (visible labels) | grep string literals matching banned vocabulary in `frontend/src/**/*.{ts,tsx}` |
| Python code comments | grep `^\s*#` lines for T4/T5 modifiers in `shared/kazus_logic/**/*.py` |
| TypeScript code comments / docstrings | same in `frontend/src/**/*.{ts,tsx}` |
| DB schema enum values | grep documented enum values in `shared/kazus_db/models.py` |
| Alert / annotation kind enums | grep `AnnotationKind` / `note_type` / similar in API + UI + models |

---

## 2. Banned-vocabulary inventory (consolidated from existing companions)

For audit purposes, the consolidated banned set across the six referenced companions:

| Category | Banned terms |
|---|---|
| **Predictive / directional** | signal (as standing claim), target, expected move, pump, prediction, forecast (as standing claim), bullish, bearish, smart money |
| **Ontology / truth** | real liquidity, true market state, honest book, market reality, truth engine |
| **Intent inference** | manipulation (as verdict), manipulation score, hidden intent, smart money intent |
| **Quality / authority modifiers (T4/T5)** | sophisticated, advanced (adj), institutional-grade, forensic-grade, world-class, powerful, intelligent, robust (without bound), mature (outside maturity stages), comprehensive |
| **Trader vocabulary** | pressure (as standing noun), spring, coil, pump setup |
| **Operator surfaces** | operator quality score, trust score, reputation, canonical interpretation, consensus reading |

---

## 3. Compliance verdict table

| Surface | Result |
|---|---|
| **API endpoint paths** | ⚠️ **2 FINDINGS** (Class B, deferred) |
| **UI visible labels** | ⚠️ **3 FINDINGS** in `AnnotationKind` enum (Class B+E, deferred) |
| **UI predictive language** | ✅ PASS — 1 hit in negation context (anti-prediction tooltip) |
| **Python code comments** | ✅ PASS — 0 T4/T5 hits in `shared/kazus_logic/` |
| **TypeScript code comments** | ⚠️ **3 FINDINGS** (Class A, safe to remediate inline) |
| **DB schema enum values** | ⚠️ **3 FINDINGS** (same as UI enum — Class B+E) |
| **Investigation `note_type` enum** | ✅ PASS — operational vocabulary (`note / hypothesis / conclusion / false_positive / needs_monitoring / confirmed_structural / coincidence / comment`) |
| **Operator-priority enums** | ✅ PASS — workflow-state values per Phase 17 |
| **Replay surfaces** | ✅ PASS — replay vocabulary already disciplined per `lip-execution-validation §21`, `lip-multi-operator §12`, `lip-epistemic-branching §9` |
| **Operator scoring / venue trust scoring** | ✅ PASS — none implemented; permanently forbidden per `lip-multi-operator §10`, `lip-venue-quality §13`, `lip-epistemic-branching §11` |
| **Autonomous adaptation** | ✅ PASS — `adaptation_state` modifiers are bounded scalars over named formulas, operator-visible, not autonomously updated |

---

## 4. Findings — detailed

### 4.1 API endpoint paths (Class B — API contract change)

| Path | File:line | Issue | Severity |
|---|---|---|---|
| `GET /research/signal-stats` | [liquidity.py:973](../backend/app/api/liquidity.py#L973) | Uses "signal" — banned as standing claim per [lip-semantic-vocabulary §8](lip-semantic-vocabulary-boundaries.md) when not tied to an explicit enum threshold. The endpoint computes stats over emitted-alert outcomes, so the operational referent is "alert", not "signal" | Deferred Class B |
| `GET /research/signal-reliability` | [liquidity.py:1728](../backend/app/api/liquidity.py#L1728) | Same. The endpoint computes precision-recall-style metrics over emitted alerts | Deferred Class B |

**Context.** Both endpoints compute meta-validation over `LiquidityAlertHistory`. The word "signal" here was internally used in the historical sense of "platform-emitted alert", not in the predictive trading sense. The surface label is nevertheless the banned word.

**Class B because:** renaming touches the URL surface (all callers — UI, notebooks, external observers), TypeScript type names, and any cached request signatures.

**Proposed rename candidates (not authorizing):**
- `signal-stats` → `alert-stats` or `emit-stats`
- `signal-reliability` → `alert-reliability` or `emit-reliability`

**Deferred per** [lip-governance §2](lip-governance.md): Class B (semantic relabeling that touches API surface). NOT AUTHORIZED during Operational Observation Period. Same disposition as the `/research/narrative-causality` rename candidate ([lip-causal-propagation §7.4](lip-causal-propagation.md)).

### 4.2 `AnnotationKind` enum values (Class B + Class E — schema + UI + historical rows)

Location: [`shared/kazus_db/models.py:294-295`](../shared/kazus_db/models.py#L294) (documented enum), [`frontend/src/lib/api.ts:577`](../frontend/src/lib/api.ts#L577) (TypeScript type), [`frontend/src/components/Liquidity.tsx:1500-1508`](../frontend/src/components/Liquidity.tsx#L1500) (UI labels).

| Enum value | UI label | Issue |
|---|---|---|
| `useful_signal` | "USEFUL" | "signal" as standing claim (banned) |
| `false_signal` | "FALSE" | Same |
| `manipulation` | "MANIP" | Direct manipulation-verdict surface — banned per [lip-execution-validation §15](lip-execution-validation.md), [lip-ontology-boundaries §12.1](lip-ontology-boundaries.md), [lip-multi-operator §13](lip-multi-operator.md). Even as an *operator-applied attribution label*, the platform surfaces and persists the term in its first-class enum |

**Context.** `AnnotationKind` is the operator-applied label set for retrospective annotations on emitted alerts. Per [lip-multi-operator §3](lip-multi-operator.md): operator attribution ≠ truth attribution. An operator labeling something as "manipulation" is the operator's interpretation, not the platform's claim. Nevertheless, the **vocabulary the platform offers** for that interpretation shapes operator language and persists into operator-visible surfaces.

**Class B+E because:**
1. UI label change (B — semantic surface).
2. DB-stored enum value change (B — emit shape).
3. Historical rows persist current values — a rename creates a discontinuity unless migrated (E — persistence) or unless the platform accepts old+new values during a transitional window.

**Proposed rename candidates (not authorizing, drawn from the user's "allowed" list and existing companion vocabulary):**

| Current | Candidate |
|---|---|
| `useful_signal` | `useful_alert` / `confirmed_alert` |
| `false_signal` | `false_positive` (consistent with existing `investigation_notes.note_type` enum value!) |
| `manipulation` | `structural_irregularity` / `structural_anomaly` |
| `liquidation_event` | (keep — `liquidation_event` is operationally named; describes a tape event, not interpretive) |
| `spoof_behavior` | (borderline — "behavior" is interpretive; could be `near_touch_flicker_below_persistence_floor`. Today's vocabulary is acceptable as it points to a microstructure pattern, not an actor claim) |
| `interesting_setup` | "setup" carries trader connotation but is annotator-tier; deferred |
| `other` | keep |

**Deferred per** [lip-governance §2](lip-governance.md): Class B + Class E. NOT AUTHORIZED during Operational Observation Period.

### 4.3 TypeScript module docstrings / comments (Class A — comment-only fix)

| Location | Current text | Disposition |
|---|---|---|
| [liquidityIntelligence.ts:2](../frontend/src/lib/liquidityIntelligence.ts#L2) | `* Phase-5 Signal & Validation Layer.` | Replace "Signal & Validation" → "Alert & Validation" (or "Emit & Validation"). Class A — internal docstring |
| [liquidityIntelligence.ts:5](../frontend/src/lib/liquidityIntelligence.ts#L5) | `* inline from a soup of magic numbers. That worked while every signal was` | Same — replace "signal" with "alert" or "emit". Class A |
| [liquidityIntelligence.ts:787](../frontend/src/lib/liquidityIntelligence.ts#L787) | `// one signal is shouting; treat with skepticism.` | Class A. Suggested rewrite: `// one component crossed threshold; cross-check before promoting.` |

**Class A because:** internal code comments, no behavior change, no API change, no DB change, no UI label change. Safe to remediate in any subsequent commit.

**Per [lip-semantic-vocabulary-boundaries §15](lip-semantic-vocabulary-boundaries.md):** "Code comment T4-T5 modifiers in code comments are equivalent to operator UI: replace with `code:line` citation or remove." Same discipline applies to anthropomorphic / trader-vocabulary terms in comments.

### 4.4 Predictive vocabulary check — single hit in negation context

[`frontend/src/lib/labels.ts:39`](../frontend/src/lib/labels.ts#L39):

```typescript
PRE_CASCADE: "investigate; do not treat as a prediction.",
```

✅ **PASS.** This is an explicit anti-predictive disclaimer for the `PRE_CASCADE` verdict tooltip. The word "prediction" appears here as the **disclaimed framing**, not as the platform's claim.

### 4.5 Confidence theater check

✅ **PASS.** Confidence values across the platform map to explicit formulas per [lip-semantic-vocabulary §8](lip-semantic-vocabulary-boundaries.md) — none is a UI ornament:

- Propagation edge confidence: 5-factor weighted formula per [lip-metric-registry §B.4](lip-metric-registry.md), HIGH/MEDIUM/LOW thresholds at 0.70 / 0.45.
- Causal verdict confidence: 6-factor multiplicative blend per §B.5.
- Transition verdict confidence: 4-factor multiplicative blend per [lip-regime-engine §11](lip-regime-engine.md).
- Crisis genesis composite: 7 named probes with INSUFFICIENT-removes-self discipline.

No UI element displays a "confidence" number without a derivation in code that the audit could trace.

### 4.6 "UI silence" check

✅ **PASS.** Per audit:
- `INSUFFICIENT` / `LOW` data quality → `exploratory = True` tag preserved through to render.
- Refusal verdicts (EXPLORATORY / UNDER_EVIDENCED / COMMON_DRIVEN / COINCIDENCE) emitted explicitly.
- `book_exhausted = True` flag preserved.
- `is_pruned = True` flag on retention-pruned evidence per investigation timeline.
- `current_state = None` returned when no snapshots exist; no fabricated default.

The UI rendering of these absence-states is the operator UI's responsibility; the data layer makes silence first-class.

### 4.7 Anti-creep checks

| Anti-creep policy | Verdict |
|---|---|
| No hidden predictive layers | ✅ Forecasts are bounded OLS with explicit `slope_capped` / `extrapolation_capped` / `horizon_decay` / `cap_factor` discounts ([freeze line 1115](2026-05-23-architecture-freeze.md)). No layer publishes a directional trade signal |
| No operator scoring | ✅ Permanently forbidden per [lip-multi-operator §10](lip-multi-operator.md). Audit: no `operator_score` / `trust_score` / `reputation` field exists in any DB table |
| No venue trust scoring | ✅ Permanently forbidden per [lip-venue-quality §13](lip-venue-quality.md). `crossex` exists as read-only diagnostic, not wired into any composite ([lip-liquidity-quality §6.2](lip-liquidity-quality.md) confirms audit findings) |
| No autonomous adaptation | ✅ `adaptation_state` modifiers are bounded scalars over named formulas over observable inputs, operator-visible, never updated by unobserved process (per [lip-execution-validation §16](lip-execution-validation.md), regime engine §11 — no learned weights anywhere) |

---

## 5. Findings summary by class

| Class | Count | Items | Authorization status during Observation Period |
|---|---|---|---|
| **A** (comment / docstring) | 3 | `liquidityIntelligence.ts` lines 2, 5, 787 | Authorized — operator's choice |
| **B** (API surface) | 2 | `/research/signal-stats` rename, `/research/signal-reliability` rename | **NOT AUTHORIZED** today |
| **B + E** (schema + UI + historical migration) | 3 | `useful_signal` / `false_signal` / `manipulation` annotation kinds | **NOT AUTHORIZED** today |

**Total drift incidents: 8.** Of these:
- **3 are safe Class A** comment fixes that the operator may choose to remediate in any commit.
- **5 are deferred Class B / B+E** that require Observation Period exit + governance event with audit-trail entry per [lip-governance §10](lip-governance.md).

**8 incidents out of an audited surface of 8 distinct surface categories × ~1500 banned-pattern test sites × multi-file scope** represents a low drift rate. The platform's vocabulary discipline is mostly holding; the residual surfaces (annotation kinds, signal-prefixed endpoints) are legacy choices that pre-date the documentation hardening passes.

---

## 6. Recommended disposition

| Action | Class | When |
|---|---|---|
| Fix 3 comment / docstring lines in `liquidityIntelligence.ts` | A | Any time; operator's decision |
| Defer `signal-stats` / `signal-reliability` endpoint rename | B | Bundle with `/research/narrative-causality` rename when Observation Period concludes — single API-migration window |
| Defer `AnnotationKind` enum rename | B+E | Same migration window; requires historical-row strategy (forward-compat read of both old + new values, OR explicit migration) |
| Do NOT add operator-scoring / venue-trust-scoring / autonomous-adaptation surfaces | — | Permanently forbidden regardless of period |

**The audit concludes with no urgent remediation required.** Drift is documented; remediation is operator-scheduled within governance Class A / B / B+E boundaries.

---

## 7. Cross-surface consistency invariant

> **Vocabulary discipline holds across all surfaces — docs, UI, API, alerts, replay, code comments, investigation surfaces — under the same banned/approved tables documented in the per-layer companions. Drift in any one surface is a defect; tolerance for drift erodes the discipline.**

This audit verifies the invariant **as of this commit**. It does not authorize tolerance for future drift. Subsequent audits should grep the same surfaces against the same banned-vocabulary inventory and report regressions as governance events per [lip-governance §10](lip-governance.md).

---

## 8. What this document is not

- Not a new contract.
- Not an ontology expansion.
- Not authorization to rename anything.
- Not a code rewrite.
- Not a UI redesign.
- Not a performance audit.
- Not a security audit.
- Not a license audit.
- Not a recommendation to enable autonomous behaviors.

It is a single cross-surface compliance verification: the platform's banned-vocabulary discipline (already documented across seven companions) is **held** in 6 of 6 audited categories, with 8 residual drift incidents (3 Class A safe-fixable, 5 Class B/B+E deferred) enumerated, classified, and dispositioned.
