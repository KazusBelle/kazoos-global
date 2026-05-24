# Stability & Operator Reality Pass

**Review date:** 2026-05-24
**Scope:** kazus-global as a production-grade operator platform after Phases 15–19 + Integrity Repair Pass. Cognitive load, signal hierarchy, UI hierarchy, long-session behavior, trust semantics, recovery behavior.
**Method:** walk the live surfaces the way an operator would over an 8-hour shift, name every place where the system imposes more cognitive cost than it returns in clarity, and prescribe one of seven dispositions per finding.
**Severity:** HIGH / MEDIUM / LOW, same rubric as `2026-05-24-operational-review.md`.

---

## 1. Operator cognitive load

### 1.1 DISC page is now a 16-panel everything-dashboard — HIGH
[frontend/src/components/Discovery.tsx](frontend/src/components/Discovery.tsx) hosts: `OperatorPrioritiesPanel`, `SanityBanner`, `AdaptationStatePanel`, `CrisisGenesisPanel`, `NarrativeCausalityPanel`, `CausalPropagationPanel`, `StructuralDependenciesPanel`, `MarketStateTransitionsPanel`, `PatternDiscoveryPanel`, `CrisisArchetypesPanel`, `HiddenRegimesPanel`, `PropagationPanel`, `EvolutionaryPanel`, `MemoryAbstractionPanel`, `IntelligenceForecastPanel`, `AdaptationPanel` — 16 panels, all visible on first load. The page has organically grown into the system's catch-all.

**Why this hurts:** the operator's eye treats the first two panels as the "real" surface and the bottom 14 as wallpaper. The information they ARE supposed to act on (Operator Queue + Sanity) drowns in scroll. The Phase 17 design property "operator workflow above the stable core" is structurally violated by the page layout.

**Disposition:** **simplify** + **hide by default**. The page should land with Operator Queue + Sanity + Adaptation Banner + Narrative Headline visible; everything else collapsed under "diagnostic drill-down" with a single click to expand. None of the lower panels are operator decisions — they are diagnostic context.

### 1.2 Sidebar has 12 top-level pages — MEDIUM
OTE · TDA · LIQ · Research · Operations · Strategy · Meta · Coordination · Memory · Discovery · Investigations · Server. Functional but the operator-relevant set is 3–4 (OTE, LIQ, Discovery, Investigations). Research / Operations / Strategy / Meta / Coordination are analyst surfaces from earlier phases that an operator running the live screening flow does not need on the same nav rail.

**Disposition:** **simplify** by sectioning the sidebar visually (operator-tier vs analyst-tier). Don't remove anything — these surfaces have audit value and at least one of them is the source of the workflow for some users — but stop putting them at the same prominence as DISC.

### 1.3 INV drawer has 7 tabs and two of them are timelines — see operational-review §1.1 — MEDIUM (carried forward)
Still unresolved. Rename pending.

### 1.4 Operator Queue surface appears in two places — MEDIUM
The queue panel on DISC and the case list on INV both invite the operator to "do something next". Cognitively the operator has to mental-model "is this priority I see here the same as the case I have open?" The Integrity Pass added retention-safe evidence linking but did not bridge the queue ↔ investigation lifecycles (review §2.1, still open).

**Disposition:** **merge** the action concept. INV-drawer "evidence" tab already surfaces the linked operator_priority; bidirectional state (case open → queue row tagged "under investigation") is what closes the loop.

### 1.5 Auto-draft is invisible — LOW
PRE_CASCADE has fired zero times in production. The auto-draft path is a feature the operator never sees and may forget exists. They may also one day be surprised by a case they didn't create.

**Disposition:** **keep as-is** but document explicitly that auto-drafts are extremely rare and may appear without warning. Surface a one-liner in INV header: "Auto-drafts open here on PRE_CASCADE genesis verdicts. None yet."

---

## 2. Signal hierarchy & vocabulary collisions

The system exposes at least nine scales operating on the same surfaces. Most have honest semantics in isolation; collisions happen at the operator's attention layer.

| label | range | scope | what it actually measures |
|---|---|---|---|
| **severity** (alert) | critical / warn / info | per alert | LIQ-engine alert severity |
| **severity** (sanity finding) | critical / warn / info | per finding | sanity_audit severity score band |
| **severity** (investigation) | critical / warn / info | per case | operator-chosen case severity |
| **escalation** | NORMAL / WATCH / IMPORTANT / CRITICAL | per priority_key | operator-queue score band |
| **priority_score** | 0–100 | per priority_key | severity_raw × confidence × recency × source_weight |
| **confidence** (causal edge) | 0–1 | per edge | causal-propagation per-edge composite |
| **confidence** (pattern) | 0–100 | per pattern | discovery layer's `pattern_confidence` |
| **confidence** (genesis probe) | 0–1 | per probe | contributing_probes / 7 |
| **novelty_score** | 0–100 | per anomaly | 100 = first time seen; 0 = exact recurrence |
| **integrity_score** | 0–100 | per propagation_graph | weighted by symmetry / coverage / weak share |
| **similarity_score** | 0–100 | per case-vs-case | sum of weighted reasons |
| **data_quality** | HIGH / PARTIAL / INSUFFICIENT / PRUNED | per surface | retention/sample-count gate |
| **scarcity_factor** | 0.15 / 0.40 / 0.75 / 1.00 | per surface | numeric mirror of data_quality |
| **modifier** | 0.5–1.5 | per modifier | adaptation cap on downstream behavior |
| **genesis_score** | 0–100 | global | composite of contributing probes |

### 2.1 "severity" overloaded across three independent domains — HIGH
The word `severity` carries three meanings the operator cannot distinguish from context alone. An alert with severity=critical means "the LIQ engine thinks something is structurally wrong with this symbol". A sanity finding with severity=critical means "the engine itself may be misbehaving". An investigation with severity=critical means "the operator chose to mark this case important". Three different worlds, same label.

**Disposition:** **simplify by renaming at presentation** — investigation severity should read "case_priority" in the UI (the field stays `severity` in the API for compatibility); sanity findings should read "integrity finding"; alert severity is the established meaning and should keep the bare word. No backend change required.

### 2.2 escalation (4-band) and severity (3-band) double-encode the same operator urgency — MEDIUM
Operator-queue rows show BOTH `priority_score` (0-100), `escalation` (NORMAL/WATCH/IMPORTANT/CRITICAL), AND the underlying finding's severity. A CRITICAL escalation on a sanity finding with severity=warn happens often (severity is the input, escalation is a composite). Operator has to decide which number to trust.

**Disposition:** **simplify** in the UI by showing escalation as the primary label and suppressing the raw finding severity in the queue row. The underlying decomposition is already exposed in the tooltip — enough.

### 2.3 confidence (0–1) and confidence (0–100) coexist — MEDIUM
Causal edges use 0–1, discovery patterns use 0–100. An operator scanning both panels has to mentally convert. The `pattern_confidence=42` and `causal_confidence=0.42` mean different things; they look identical to a tired eye.

**Disposition:** **simplify** by uniformly displaying 0–100 in UI even where the API exposes 0–1. Already done for some surfaces; standardize.

### 2.4 data_quality + scarcity_factor are the same thing twice — LOW
`SCARCITY_FACTOR = {INSUFFICIENT: 0.15, LOW: 0.40, MEDIUM: 0.75, HIGH: 1.00}` is a numeric mirror of `data_quality`. The operator does not need to see both; the numeric value lives inside the function and the qualitative label is what gets surfaced. This is already the right split. Keep.

**Disposition:** **keep exactly as-is**.

### 2.5 modifier ranges (0.5–1.5) read as "score" — LOW
Adaptation modifiers like `narrative_confidence_modifier=0.78` look like a score on the same scale the operator just read confidence on. They are actually a coefficient: 1.0 = no change, 0.5 = halve, 1.5 = boost. Operators may read 0.78 as "78% confident".

**Disposition:** **simplify** — display modifiers as relative deltas in the UI ("×0.78 — narrative confidence dampened by 22%"). Same number, less misreadable.

---

## 3. UI hierarchy

### 3.1 DISC top doesn't tell the operator what to look at first — HIGH
Operator Queue is correctly first, but Sanity, Adaptation, Genesis, Narrative, Causal, Structural, Transitions, Discovery, etc. follow in declared order with equal visual weight. There is no "everything below this line is diagnostic, not action".

**Disposition:** **simplify** by inserting an explicit visual separator after Adaptation Banner with a header "diagnostic context — do not act on these alone". Costs nothing, anchors operator attention.

### 3.2 Several panels are show-and-forget — MEDIUM
`EvolutionaryPanel`, `MemoryAbstractionPanel`, `IntelligenceForecastPanel`, `CrisisArchetypesPanel`, `HiddenRegimesPanel` are all rendered on every DISC load but operators interact with them less than monthly. Each polls its endpoint on the 60s cadence.

**Disposition:** **hide by default** — collapsed behind "research drill-down" accordion. Lazy-load on expand. Removes ~5 polled endpoints from the steady-state load.

### 3.3 INV-drawer `tree` and `replay/propagation` overlap visually — MEDIUM
Both render symbol-relationship views. Tree shows propagation/causal/structural edges (rationale-tagged); replay propagation shows alert-count bars + static lead-lag edges. Two angles on related but distinct data; operator may treat them as duplicates.

**Disposition:** **keep both** but rename the propagation block within replay to "alert wave timeline" so the operator does not read it as "causal tree".

### 3.4 `cursor snapshot` vs `frozen snapshot` toggle in replay is a power-user control — LOW
Most operators will use the live-at-cursor mode 95% of the time. The frozen toggle is forensic-only.

**Disposition:** **keep as-is** (the toggle is small and labeled). Possibly **hide by default** behind a "compare to frozen" link.

---

## 4. Long-session behavior (the 6–8 hour shift)

### 4.1 Persistent CRITICAL becomes wallpaper — HIGH
Sanity is currently and durably CRITICAL because of the `propagation_loop` finding (84 symmetric pairs). The arch freeze §7 notes this is the feedback loop working as designed. But the consequence is: every DISC load shows a red sanity banner that never changes. After the second hour, the operator stops seeing it.

This is severity desensitization. The system gives a CRITICAL signal that is structurally not actionable; eventually the operator filters all CRITICAL banners cognitively.

**Disposition:** **simplify** the sanity banner to differentiate "chronic" findings (RECURRING / CHRONIC trend, already labeled in the API) from "new" findings. Chronic-CRITICAL gets a dim sustained-red treatment; new-CRITICAL gets a strobe-once attention treatment. Operator's attention budget is finite; spend it on novelty.

### 4.2 Operator-queue digest 24h-window updates fractionally — MEDIUM
Sitting 8 hours staring at the same `digest?window_hours=24` panel watches `new=3 worsened=2 stabilized=1` shift by ±1 every few minutes. The semantic "what materially changed" loses meaning over a long session because the window is rolling and the operator IS the window.

**Disposition:** **simplify** by adding a "since I last looked at this" sub-window driven by client-side timestamp of the last operator click on the digest. No new endpoint required.

### 4.3 60s polling on every panel creates a constant refresh hum — LOW
Every panel has `setInterval(refresh, 60_000)`. 16 DISC panels = ~16 fetches per minute on DISC alone. None of them are doing anything most of the time. Operator sees nothing change but the network tab is hammering.

**Disposition:** **simplify** by using a single shared interval-clock per panel-group (already implicit in some cases). Lower the cadence for the cold panels to 300s.

### 4.4 Investigation tab counters grow without an obvious cap — LOW
INV list has no pagination indicator until 100+ cases. Long-running operator with many resolved cases sees the list grow until the rendered table is sluggish.

**Disposition:** **keep as-is** for now (status=active filter already culls); add pagination later when total > 200.

### 4.5 Replay scrubber's play loop is open-loop — LOW
A 12h case window at speed=1 plays for 12 minutes of wall time. Operator presses play, walks away. There is no auto-pause on cursor-at-end (there IS a clamp, but no audio/visual notification).

**Disposition:** **keep as-is**. Forensic surface; this is operator's deliberate action.

---

## 5. Trust semantics — places the system sounds more sure than it should

### 5.1 verdict labels read like deterministic claims — HIGH
`DIRECTIONAL`, `COMMON_DRIVEN`, `PRE_CASCADE`, `dominant_driver`, `INFLUENCE_HUB`, `AMPLIFIER`, `synchronized stress group` — these labels carry assertive grammar. An operator reading "PRE_CASCADE — verdict CRITICAL" naturally hears "the system has detected a pre-cascade". The actual semantics are "the 7-probe composite scored ≥75 with ≥3 contributing probes, with explicit scarcity caps". That is honest in the API; it is not honest in the operator-facing word "verdict".

**Disposition:** **simplify** by relabeling at the UI layer (API stays — API contract is frozen). "PRE_CASCADE" becomes "pre-cascade conditions present"; "DIRECTIONAL" becomes "directional pattern (lead-lag asymmetry)"; "dominant_driver" becomes "candidate driver"; "AMPLIFIER" becomes "appears in chains". The probabilistic phrasing is the system's value proposition; the labels currently undercut it.

### 5.2 `causal_*` edge confidence on COINCIDENCE / EXPLORATORY verdicts — see operational-review §3.1 — MEDIUM (carried forward)
Still unresolved. A `COINCIDENCE` edge with `confidence=0.30` looks like 30% causal. The verdict says otherwise.

### 5.3 Replay propagation "alert wave" implies transmission — HIGH
The propagation playback (per-symbol bars across time buckets) visually telegraphs "BTCUSDT fired, then ETHUSDT, then SOLUSDT". The operator reads this as causation propagating. The data is just "alert starts per bucket per symbol", with a deliberate `rationale_note` in the response disclaiming edge transmission timing — but the visual animation does the persuading the disclaimer can't undo.

**Disposition:** **simplify** by removing the animation. Show the per-frame snapshot at the cursor as a STATIC bar chart — operator can scrub manually but the system does not animate symbols "lighting up in order". The forensic value of seeing the order is preserved (operator scrubs and reads); the false-causation visual cue is removed.

### 5.4 Similarity reasons mix observable matches with weighting language — LOW
"Same origin fingerprint" is observable. "Score 65" is a derived quantity that the operator should not internalize as confidence. The similarity panel risks the operator reading high-score cases as "the system thinks these are the same case".

**Disposition:** **simplify** by labeling the score column "match strength (rule-based)" not "similarity score". And expose `score_contribution` per reason (still open from operational-review §1.6).

### 5.5 Narrative_causality template's confidence wording — LOW
The narrative section is template-built and contains hedging language ("appears", "may indicate"). Audit-OK. The "what we don't know" section is always present — good. No change needed.

**Disposition:** **keep exactly as-is**. This is one of the system's strongest design properties.

---

## 6. Recovery behavior

### 6.1 Stale TTL caches do not surface staleness — MEDIUM
`causal_propagation` (300s TTL), `crisis_genesis` (120s TTL), etc. Operator can stare at a 4-minute-old `narrative_headline` thinking it's live. The cache hit is invisible.

**Disposition:** **simplify** by adding a `fetched_at_ms` field to every cached response and a small "cached, age Xs" indicator near each panel's title. The cache decorator already records this; just plumb it.

### 6.2 Replay on retention-pruned history — partial coverage — MEDIUM
The Integrity Pass made the case timeline retention-safe (pruned upstream → snapshot fallback → `is_pruned=true` event). But the replay surface itself (`replay_timeline`, `replay_propagation`) still reads live tables and silently returns empty windows for pre-retention case windows.

**Disposition:** **simplify** by surfacing a top-of-replay-panel notice "case window extends past current retention horizon (35d samples, 90d alerts, 180d anomalies); reconstruction will be incomplete". Honest, no new logic required.

### 6.3 Worker lag — heartbeat exists, operator does not look — LOW
`runtime-health` endpoint surfaces heartbeat-by-proxy + table sizes + cache hits. The data is there. Nothing in DISC visibly says "the worker stopped 10 minutes ago"; operator continues to act on stale data.

**Disposition:** **simplify** by adding one small heartbeat-status chip in DISC top-bar that turns amber/red when any heartbeat is `lagging` / `stale`. One element, one fetch, big trust win.

### 6.4 Restart behavior is correct — LOW
After Integrity Pass: investigation-capture loop drains PENDING fast; auto-draft loop runs every 5min; intel snapshot every 5min with stagger; investigation_timeline uses snapshot fallback for pruned upstream. The recovery path is materially better than it was a week ago.

**Disposition:** **keep exactly as-is**.

### 6.5 Capture failures surface but the surface is small — LOW
The `capture_status=FAILED` chip on the INV row + retry button (Integrity Pass) is correct but not loud. An operator could not realize a case has no frozen snapshot.

**Disposition:** **keep as-is**, possibly **simplify** by also showing the count of FAILED captures at the top of the INV list as a banner when > 0.

---

## 7. Dispositions summary

The full list of recommendations, sorted by the requested seven dispositions. Numbers in parens reference the section above.

### 7.1 Keep exactly as-is (the system gets these right)
- Append-only investigation notes + lifecycle audit events.
- Append-only frozen snapshot history (Integrity Pass §1).
- Acyclic layer composition; no auto-trading.
- data_quality / scarcity_factor split (§2.4).
- Narrative_causality template wording (§5.5).
- Worker restart behavior after Integrity Pass (§6.4).
- Investigation lifecycle (OPEN → INVESTIGATING → MONITORING → RESOLVED → ARCHIVED) with required `resolution_summary`.
- Operator queue digest 1h/6h/24h structure (semantics are right; only the long-session UX needs nudging).
- Replay scrubber transport controls.
- The "NEVER do" list from the operational review (perimeter).

### 7.2 Simplify (same surface, less cognitive cost)
- DISC: insert "diagnostic context — do not act on these alone" separator after the action-tier (§3.1 / HIGH).
- Operator-queue row: show escalation as primary, suppress raw finding severity in queue row (§2.2).
- Replay propagation: remove animation; show static per-frame bars only (§5.3 / HIGH).
- Verdict labels at UI layer: probabilistic phrasing instead of assertive ("pre-cascade conditions present" vs "PRE_CASCADE — verdict CRITICAL") (§5.1 / HIGH).
- Sanity banner: differentiate chronic CRITICAL from new CRITICAL (§4.1 / HIGH).
- Cached panels: surface `fetched_at_ms` as a small "cached Xs ago" label (§6.1).
- Standardize confidence display to 0–100 across UI even where API exposes 0–1 (§2.3).
- Adaptation modifiers: display as relative deltas, not as 0.78-style scores (§2.5).
- Investigation severity: rename UI label to "case priority" (§2.1 / HIGH).
- Sanity finding severity: rename UI label to "integrity finding" (§2.1 / HIGH).
- Operator-digest: client-tracked "since I last looked" sub-window (§4.2).
- Replay-window-past-retention notice (§6.2).
- Worker-heartbeat chip in DISC top-bar (§6.3).
- FAILED-capture top-banner when > 0 cases affected (§6.5).
- Similarity score column relabeled "match strength (rule-based)" + per-reason contribution (§5.4).

### 7.3 Merge (two surfaces should be one)
- Queue ↔ investigation lifecycle bridge (already noted in operational-review §2.1; concretely: case open → queue row tagged "under investigation"; case resolved → queue row marked acknowledged) (§1.4).
- Two timeline tabs in INV drawer (operational-review §1.1, still open).

### 7.4 Hide by default (collapse / lazy-load)
- DISC panels: EvolutionaryPanel, MemoryAbstractionPanel, IntelligenceForecastPanel, CrisisArchetypesPanel, HiddenRegimesPanel — collapse under "research drill-down" accordion (§3.2 / MEDIUM).
- Replay `cursor snapshot` toggle "compare to frozen" can default-collapse (§3.4).

### 7.5 Remove entirely
- Nothing. Every surface in the system has at least one operator who legitimately uses it; outright removal would lose audit value.

### 7.6 Dangerous if expanded
- **Animated propagation playback.** Already a misleading visual on alert counts; expanding it ("show inferred edges lighting up", "draw causal arrows propagating in time") would manufacture causation that doesn't exist. Cap the animation; never let it grow into "wave visualization".
- **DISC page footprint.** Adding any more panels to DISC pushes it deeper into everything-dashboard. The next intelligence layer should land on its own surface OR explicitly replace a DISC panel.
- **Severity labels** ("CRITICAL" / "PRE_CASCADE" / "DIRECTIONAL"). Reusing these labels for any new layer compounds the desensitization risk. Any new layer should pick fresh vocabulary; reusing the alphabet is debt.
- **Replay window length.** Currently 6h pre + 6h post around anchor (default). Extending to days makes the scrubber strip a flat smear with zero visual resolution; the bucket math degrades; the UX claim "you can scrub the case" breaks down. Cap at ~48h regardless of how long the case has been open.
- **Per-symbol confidence on propagation playback bars.** Counts are counts. Adding a per-bar confidence number invents structure on top of raw data; would re-introduce the category error the rationale_note currently prevents.
- **Auto-resolve of investigations** based on "linked priority resolved + no recent activity" or similar inferred-quiet signals. Crossing this line turns the system from intelligence into autonomous workflow. Resolution is an operator's voice — it stays manual.
- **External notification fan-out** (Slack/Discord/email) without explicit per-case opt-in + audit-logged delivery. Silent perimeter expansion.
- **LLM-rendered narrative_causality.** Replacing the template with generated text would unwind the strongest trust property the system has.
- **Discovery layer feedback into operator queue scoring.** The adaptation modifiers already cap exposure; tightening that loop further (pattern_discovery success rate feeding back as a `pattern_credibility` factor in operator scoring, etc.) creates the silent learned-bias surface the operational review warned about.

### 7.7 Safe for long-term
- Investigation Layer (Phase 18) — small writes, append-only, no recompute path. Storage growth is per-case-bounded.
- Operator Queue persistence (Phase 17) — DB-backed lifecycle, well-tested.
- Replay snapshot append-only history (Integrity Pass §1) — bounded growth (one row per case + one per recapture; recapture is a rare operator-initiated action).
- Retention-safe evidence (Integrity Pass §3) — pure additive at link time.
- Sanity audit (10 checks). Composition is stable; thresholds documented.
- LIQ scanner + Realtime WS engine. Stable for 1+ year.
- Adaptation modifiers (Phase 16) — bounded by ADAPTATION_BOUNDS, acyclic.

---

## 8. Closing posture

The system is past the "rapid layering" phase and has crossed into the territory where **further intelligence is no longer the bottleneck**. The bottleneck is operator attention, which has finite capacity and currently spreads thin across:

- 16 DISC panels (most of which are diagnostic, not actionable)
- 12 sidebar pages
- 7 INV drawer tabs
- 9+ vocabulary scales for "how sure / how bad" (§2)
- Two parallel state machines for the same finding (queue + investigation, still unbridged)
- Persistent visual signals that do not change over an 8h shift

None of the recommendations in §7.2 / §7.3 / §7.4 require new intelligence. They are presentation-layer and signal-hierarchy refinements that strengthen the operator's ability to *trust* the surfaces by reducing how many of them they have to read at once and making the labels match the actual confidence the system has.

The strongest invariants of the system — append-only history, acyclic composition, no auto-trading, explicit scarcity gates, deterministic templates — are intact and should be preserved verbatim. The `Dangerous if expanded` list (§7.6) is the working perimeter; that perimeter is what separates this from a generic AI dashboard and it should not erode for incremental UX wins.

If only one disposition lands from this review, it should be **§4.1** (chronic vs new CRITICAL differentiation): persistent unchanging CRITICAL banners destroy the value of the entire severity vocabulary, and that loss compounds across every other signal the system tries to communicate.

---

## 9. Companion documents

- `docs/2026-05-23-architecture-freeze.md` — system-wide freeze + Integrity Repair Pass section.
- `docs/2026-05-24-operational-review.md` — forensic audit of phases 1–19, HIGH-severity findings now mostly addressed.
- This document — operator-reality / stability lens, presentation-layer focus.

Together: the architecture is documented, the structural risks are documented and largely closed, the operator-experience risks are now documented and not yet closed.
