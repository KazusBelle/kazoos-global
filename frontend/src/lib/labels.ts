/**
 * Attention & Trust Simplification Pass — presentation-layer labels.
 *
 * Pure UI helpers. API contracts (verdict/role/kind enums) are
 * unchanged. This file softens deterministic-sounding labels into
 * probabilistic phrasing so the operator-visible wording matches the
 * actual confidence the system has.
 *
 * Rule of thumb when extending:
 *   - prefer "candidate", "observed", "pattern", "appears as"
 *   - avoid "is", "has", "shows", action-imperative wording
 *   - never introduce a new score or confidence number here
 */

// ── Crisis genesis verdicts ──────────────────────────────────────────

export type GenesisVerdictLabelKind = "headline" | "wording" | "next_action";

const GENESIS_VERDICT_HEADLINE: Record<string, string> = {
  CALM:             "calm",
  EARLY_DISTORTION: "early distortion (observed)",
  ELEVATED_RISK:    "elevated risk conditions",
  PRE_CASCADE:      "pre-cascade conditions present",
  INSUFFICIENT:     "insufficient data",
};

const GENESIS_VERDICT_WORDING: Record<string, string> = {
  CALM:             "no precursor signals materially elevated",
  EARLY_DISTORTION: "one or two precursor signals firing — exploratory",
  ELEVATED_RISK:    "multiple precursor signals firing — watch for confirmation",
  PRE_CASCADE:      "most precursor signals firing — interpret cautiously",
  INSUFFICIENT:     "the engine is silent because data is missing, not because the market is calm",
};

const GENESIS_VERDICT_NEXT_ACTION: Record<string, string> = {
  CALM:             "no operator action required.",
  EARLY_DISTORTION: "treat as exploratory — could resolve or escalate.",
  ELEVATED_RISK:    "monitor for the next hot probe to confirm.",
  PRE_CASCADE:      "investigate; do not treat as a prediction.",
  INSUFFICIENT:     "treat this surface as unavailable, not 'all clear'.",
};

export function genesisVerdictLabel(v: string, kind: GenesisVerdictLabelKind = "headline"): string {
  const map =
    kind === "headline" ? GENESIS_VERDICT_HEADLINE :
    kind === "wording"  ? GENESIS_VERDICT_WORDING :
                          GENESIS_VERDICT_NEXT_ACTION;
  return map[v] ?? v.toLowerCase().replace(/_/g, " ");
}

// ── Causal-propagation verdicts ──────────────────────────────────────

const CAUSAL_VERDICT_SOFT: Record<string, string> = {
  DIRECTIONAL:     "directional pattern (lead-lag)",
  COMMON_DRIVEN:   "common-driver candidate",
  AMBIGUOUS:       "ambiguous (no asymmetry)",
  UNDER_EVIDENCED: "under-evidenced",
  COINCIDENCE:     "coincidence (rejected)",
  EXPLORATORY:     "exploratory (low data)",
};

export function causalVerdictLabel(v: string): string {
  return CAUSAL_VERDICT_SOFT[v] ?? v.toLowerCase().replace(/_/g, " ");
}

// ── Causal-role / influence-hierarchy labels ─────────────────────────

const CAUSAL_ROLE_SOFT: Record<string, string> = {
  LEADER:          "appears as leader (candidate)",
  AMPLIFIER:       "appears in chains",
  FOLLOWER:        "appears as follower (candidate)",
  INSTABILITY_HUB: "unstable activity (candidate)",
  ISOLATED:        "isolated",
};

export function causalRoleLabel(r: string): string {
  return CAUSAL_ROLE_SOFT[r] ?? r.toLowerCase();
}

// ── Severity-domain wording (review §2.1) ────────────────────────────
//
// `severity` is overloaded across three independent domains. The API
// keeps the bare `severity` field everywhere; the UI presents it with
// a domain-disambiguating word so the operator can't conflate them.

export function alertSeverityLabel(sev: string): string {
  // LIQ-engine alert: keep the canonical bare word.
  return sev.toLowerCase();
}

export function sanitySeverityLabel(sev: string): string {
  // Sanity finding: prefix so it reads as engine-self-check.
  return `integrity ${sev.toLowerCase()}`;
}

export function investigationSeverityLabel(sev: string): string {
  // Operator-chosen case severity is really a priority — say so.
  const map: Record<string, string> = {
    critical: "high priority",
    warn:     "medium priority",
    info:     "low priority",
  };
  return map[sev.toLowerCase()] ?? sev.toLowerCase();
}

// ── Operator-priority lifecycle labels (review §4.1) ─────────────────
//
// "Persistent CRITICAL becomes wallpaper". The lifecycle label is what
// we use to differentiate visual treatment: chronic/persistent items
// get muted styling, NEW/WORSENING items get sharp styling. This map
// returns ONLY presentation hints; numeric thresholds stay in the
// research layer.

export type LifecycleAttention = "fresh" | "escalating" | "stable" | "calming" | "resolved";

export function lifecycleAttention(lifecycle: string | null | undefined): LifecycleAttention {
  switch ((lifecycle ?? "").toUpperCase()) {
    case "NEW":         return "fresh";
    case "WORSENING":   return "escalating";
    case "STABILIZING": return "calming";
    case "RESOLVED":    return "resolved";
    case "PERSISTENT":
    default:            return "stable";
  }
}

// Same idea for sanity trend (NEW / WORSENING / STABILIZING / RECURRING / CHRONIC / TRANSIENT).
export function sanityTrendAttention(trend: string | null | undefined): LifecycleAttention {
  switch ((trend ?? "").toUpperCase()) {
    case "NEW":         return "fresh";
    case "WORSENING":   return "escalating";
    case "STABILIZING": return "calming";
    case "TRANSIENT":   return "calming";
    case "CHRONIC":
    case "RECURRING":
    default:            return "stable";
  }
}
