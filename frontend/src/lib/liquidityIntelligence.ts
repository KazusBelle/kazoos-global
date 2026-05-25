/**
 * Phase-5 Signal & Validation Layer.
 *
 * The LIQ scanner used to derive every flag, regime label and threshold
 * inline from a soup of magic numbers. That worked while every signal was
 * advisory; now those signals feed an alert engine, so they need to be:
 *
 *   - adaptive (thresholds scale with the visible cohort, not fixed bps);
 *   - hierarchical (one dominant regime, not five contradictory labels);
 *   - regime-weighted (intelligence score emphasises whatever matters
 *     most given the current regime — fragility in chop, positioning in
 *     trend);
 *   - debounced + confidence-gated (alerts don't fire on a single noisy
 *     tick; confidence falls when WS data is stale or signals disagree).
 *
 * Everything in this module is pure and client-side — no DB writes — so
 * we can iterate the rules without a migration. The Liquidity component
 * does the React plumbing on top.
 */

// ── Types ────────────────────────────────────────────────────────────────

export type MetricSample = { value: number | null; ts: number };
export type MetricsMap = Record<string, MetricSample>;

// "UNKNOWN" is the honest first-render state — used when we have not yet
// received any metric samples for the symbol. It is NOT a synonym for
// HEALTHY_TREND: HEALTHY_TREND means "we measured and nothing is wrong",
// UNKNOWN means "we have not measured yet". Treating these as the same
// produced an actively misleading initial paint where every row looked
// confidently green for ~500ms before snapshot data arrived.
export type Regime =
  | "UNKNOWN"
  | "HEALTHY_TREND"
  | "THIN_LIQUIDITY"
  | "SPOOF_PRONE"
  | "LIQUIDATION_CASCADE"
  | "CROWDED_LONGS"
  | "CROWDED_SHORTS"
  | "UNSTABLE_MARKET";

export const REGIME_COLORS: Record<Regime, string> = {
  UNKNOWN: "rgba(140, 140, 150, 0.95)",
  HEALTHY_TREND: "rgba(82, 185, 122, 0.95)",
  THIN_LIQUIDITY: "rgba(214, 139, 105, 0.95)",
  SPOOF_PRONE: "rgba(214, 105, 105, 0.95)",
  LIQUIDATION_CASCADE: "rgba(214, 75, 75, 0.95)",
  CROWDED_LONGS: "rgba(214, 139, 105, 0.95)",
  CROWDED_SHORTS: "rgba(140, 170, 235, 0.95)",
  UNSTABLE_MARKET: "rgba(227, 180, 87, 0.95)",
};

// Hierarchy: the higher the rank, the more dominant the regime. Used both
// for picking the single winner among multiple candidates and for sorting
// rows by "anomaly priority" (worst-first). UNKNOWN is intentionally
// below HEALTHY_TREND so rows without data sort last by priority.
export const REGIME_RANK: Record<Regime, number> = {
  LIQUIDATION_CASCADE: 100,
  UNSTABLE_MARKET: 80,
  SPOOF_PRONE: 70,
  THIN_LIQUIDITY: 60,
  CROWDED_LONGS: 50,
  CROWDED_SHORTS: 50,
  HEALTHY_TREND: 0,
  UNKNOWN: -1,
};

export type Flag = { label: string; color: string; title: string };

export type Severity = "info" | "warn" | "critical";

export const SEVERITY_COLOR: Record<Severity, string> = {
  info: "rgba(140, 170, 235, 0.95)",
  warn: "rgba(227, 180, 87, 0.95)",
  critical: "rgba(214, 75, 75, 0.95)",
};

export type AlertEvent = {
  id: string;             // `${symbol}:${kind}:${startedAt}` — stable across ticks
  symbol: string;
  kind: AlertKind;
  trigger: string;        // human-readable trigger summary
  regime: Regime;
  severity: Severity;
  confidence: number;     // 0..100
  priority: number;       // 0..100 composite — high = act first
  rank: number;           // 1-based rank within currently-active alerts
  startedAt: number;      // epoch ms when the condition first persisted
  lastSeenAt: number;     // epoch ms updated each tick while the condition holds
};

// Severity → base score; multiplied by regime rank, confidence, and a
// "co-occurrence bonus" so several alerts on the same symbol pile up.
const SEVERITY_BASE: Record<Severity, number> = {
  info: 20,
  warn: 50,
  critical: 80,
};

/**
 * Composite alert priority (0..100). The Phase-5 timeline treated alerts
 * as equally important; Phase 6 makes "liquidation cascade in low
 * confidence + 3 supporting alerts" stand out from "isolated spread
 * blip".
 */
export function alertPriority(
  kind: AlertKind,
  severity: Severity,
  regime: Regime,
  confidence: number,
  coOccurringKinds: number,   // how many OTHER active alerts on the same symbol
): number {
  const base = SEVERITY_BASE[severity];
  // Regime contribution: cascade/unstable lift priority, healthy_trend
  // damps it (an isolated spread blip in a healthy market is mostly noise).
  const regimeContrib = REGIME_RANK[regime] * 0.15;
  // Co-occurrence: each extra concurrent alert adds 6 priority points, capped.
  const cluster = Math.min(24, coOccurringKinds * 6);
  // Confidence shapes the score symmetrically: HIGH = +10, LOW = −15.
  const conf = confidence >= 75 ? 10 : confidence >= 50 ? 0 : -15;
  // A handful of alert kinds are inherently more "real" — liq cascade,
  // resiliency failure — bump those.
  const kindBonus =
    kind === "LIQ_CASCADE" || kind === "RESILIENCY_FAILURE" || kind === "DEPTH_COLLAPSE"
      ? 8
      : kind === "FRAGILITY_SPIKE" || kind === "REGIME_TRANSITION"
        ? 4
        : 0;
  return Math.max(0, Math.min(100, base + regimeContrib + cluster + conf + kindBonus));
}

export type AlertKind =
  | "SPREAD_EXPLOSION"
  | "DEPTH_COLLAPSE"
  | "LIQ_CASCADE"
  | "RESILIENCY_FAILURE"
  | "OI_SURGE"
  | "FUNDING_EXTREME"
  | "FRAGILITY_SPIKE"
  | "REGIME_TRANSITION";

export type CohortStats = {
  atrLiquidityP90: number | null;
  credDepthP90: number | null;
  credDepthP10: number | null;
  spreadP10: number | null;
  spreadP95: number | null;
  liqStressP90: number | null;
  fragilityP90: number | null;
};

export type AdaptiveThresholds = {
  spreadExplosion: number;   // fraction (e.g. 0.0008 = 8 bps)
  liqSpikeUsd: number;       // absolute USD over rolling window
  obiFlipMag: number;        // |OBI| considered significant
  depthCollapseRatio: number; // current depth / cohort p90 below which "collapsed"
  fundingExtremeBps: number;  // |funding| in bps that counts as extreme
  fundingZExtreme: number;    // |z| that counts as extreme
  oiSurgePct: number;         // |Δ%| 1h that counts as a surge
  fragilitySpike: number;     // fragility_score that counts as spike
};

// "UNKNOWN" mirrors the Regime UNKNOWN state — the symbol has no metric
// samples yet, so we should not display a confident score derived from
// "no penalties because no signals". Score is kept at 0 in this state
// so existing numeric consumers don't crash; UI must check `state` and
// render the absence rather than the zero.
export type ConfidenceState = "UNKNOWN" | "LOW" | "MEDIUM" | "HIGH";
export type Confidence = { score: number; state: ConfidenceState; reasons: string[] };

export type IntelResult = {
  score: number;
  quality: number;
  regime: Regime;
  flags: Flag[];
  confidence: Confidence;
  anomalyPriority: number;
  // Active alerts produced this tick — newest snapshot wins; the engine
  // de-dupes across ticks using AlertEvent.id.
  alertKinds: AlertKind[];
};

/* ── Explainability types ────────────────────────────────────────────── */

// One component that contributed to the intelligence score. Bundled with
// the breakdown so the UI can render positive/negative-impact panels.
export type IntelContribution = {
  key: string;            // metric key, e.g. "spread"
  label: string;          // human label, e.g. "Spread"
  score: number;          // 0..100 component score
  weight: number;         // regime-adjusted weight
  contribution: number;   // score × weight (raw)
  delta: number;          // contribution − (mean contribution) → +/− indicator
};

export type IntelBreakdown = {
  score: number;
  quality: number;
  contributions: IntelContribution[];
  positive: IntelContribution[];   // top contributors (best deltas)
  negative: IntelContribution[];   // worst contributors (most negative deltas)
};

// Reasons that a regime was selected — used by the audit panel.
export type RegimeExplanation = {
  regime: Regime;
  candidates: Regime[];   // all candidates that fired, sorted by rank
  drivers: string[];      // human descriptions of the triggers ("fragility ≥ 60", "LIQ-STRESS flag")
  confidence: number;     // 0..100 — copy of the symbol's Confidence.score for convenience
};

// ── Helpers ──────────────────────────────────────────────────────────────

export function getMetric(metrics: MetricsMap, key: string): number | null {
  const v = metrics[key]?.value;
  return v == null || !Number.isFinite(v) ? null : v;
}

export function percentile(sorted: number[], p: number): number | null {
  if (sorted.length === 0) return null;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor(p * (sorted.length - 1))));
  return sorted[idx];
}

// ── Adaptive thresholds ──────────────────────────────────────────────────
// Each threshold is the MAX of:
//   * a cohort-relative value (so thin universes don't false-positive
//     against absolute floors that were tuned for BTC-sized books);
//   * an absolute floor so we don't false-positive in flat conditions
//     either (a cohort of perfectly calm coins shouldn't lower the bar
//     to noise).
// Regime tightens/relaxes thresholds — e.g. in LIQUIDATION_CASCADE we
// raise spread/liq thresholds so we don't re-fire on the same event.

export function computeAdaptiveThresholds(
  cohort: CohortStats,
  regime: Regime,
  metrics: MetricsMap,
): AdaptiveThresholds {
  // ATR proxy from atr_liquidity isn't an ATR per se — but symbols with
  // higher ATR-liquidity typically have a tighter intrinsic spread, so we
  // use the cohort p95 spread as the practical "wide" baseline and scale
  // by regime. When fragility is high the bar tightens further so the
  // next explosion fires sooner.
  const fragility = getMetric(metrics, "fragility_score") ?? 0;
  const fragilityMul = fragility >= 60 ? 0.8 : 1.0;

  const inCascade = regime === "LIQUIDATION_CASCADE";
  const inUnstable = regime === "UNSTABLE_MARKET";

  const spreadExplosionBase = Math.max(
    cohort.spreadP95 ?? 0.0008,   // cohort-relative
    0.0008,                        // hard floor: 8 bps
  );
  const spreadExplosion = spreadExplosionBase * (inCascade ? 1.5 : fragilityMul);

  const liqSpikeBase = Math.max(
    cohort.liqStressP90 ?? 50_000,
    50_000,
  );
  const liqSpikeUsd = liqSpikeBase * (inCascade ? 2.0 : 1.0);

  // OBI flip magnitude: tighter in trends (so positioning shifts pop),
  // looser in unstable markets (where noise dominates).
  const obiFlipMag = inUnstable ? 0.4 : regime === "HEALTHY_TREND" ? 0.25 : 0.3;

  // Depth collapse: relative to the cohort median (not p90, which is just
  // "BTC-sized"). We don't have p50 stored; approximate with √(p10·p90).
  const cdMid =
    cohort.credDepthP10 != null && cohort.credDepthP90 != null && cohort.credDepthP90 > 0
      ? Math.sqrt(cohort.credDepthP10 * cohort.credDepthP90)
      : null;
  const depthCollapseRatio = cdMid != null && cohort.credDepthP90 != null && cohort.credDepthP90 > 0
    ? Math.max(0.15, cdMid / cohort.credDepthP90 * 0.5)
    : 0.3;

  const fundingExtremeBps = inCascade ? 12 : 8;
  const fundingZExtreme = inUnstable ? 2.5 : 2.0;
  const oiSurgePct = inUnstable ? 8 : 5;
  const fragilitySpike = cohort.fragilityP90 != null
    ? Math.max(60, cohort.fragilityP90)
    : 60;

  return {
    spreadExplosion,
    liqSpikeUsd,
    obiFlipMag,
    depthCollapseRatio,
    fundingExtremeBps,
    fundingZExtreme,
    oiSurgePct,
    fragilitySpike,
  };
}

// ── Flag computation (adaptive) ──────────────────────────────────────────

export function computeFlags(
  metrics: MetricsMap,
  cohort: CohortStats,
  thresholds: AdaptiveThresholds,
): Flag[] {
  const flags: Flag[] = [];
  const cd = getMetric(metrics, "credible_depth");
  const ls = getMetric(metrics, "liq_stress");
  const obi = getMetric(metrics, "obi");
  const spread = getMetric(metrics, "spread");
  const fund = getMetric(metrics, "funding");
  const fundZ = getMetric(metrics, "funding_z");
  const oiD1h = getMetric(metrics, "oi_delta_1h");
  const oiD5m = getMetric(metrics, "oi_delta_5m");
  const fragility = getMetric(metrics, "fragility_score");

  if (cd != null && cohort.credDepthP10 != null && cd <= cohort.credDepthP10) {
    flags.push({
      label: "THIN",
      color: "rgba(214, 139, 105, 0.95)",
      title: "Credible depth in bottom 10% of cohort — thin book, slippage risk",
    });
  }

  if (
    spread != null &&
    spread > thresholds.spreadExplosion &&
    cd != null &&
    cohort.credDepthP10 != null &&
    cd <= cohort.credDepthP10
  ) {
    flags.push({
      label: "SPOOF-RISK",
      color: "rgba(214, 105, 105, 0.95)",
      title: `Wide spread (>${(thresholds.spreadExplosion * 10000).toFixed(1)}bps) + thin credible depth`,
    });
  }

  if (ls != null && ls >= thresholds.liqSpikeUsd) {
    flags.push({
      label: "LIQ-STRESS",
      color: "rgba(214, 105, 105, 0.95)",
      title: `Liquidation volume $${ls.toFixed(0)} ≥ adaptive threshold $${thresholds.liqSpikeUsd.toFixed(0)}`,
    });
  }

  if (
    (oiD5m != null && Math.abs(oiD5m) > thresholds.oiSurgePct / 5) ||
    (oiD1h != null && Math.abs(oiD1h) > thresholds.oiSurgePct)
  ) {
    const dir = (oiD1h ?? oiD5m ?? 0) > 0 ? "+" : "−";
    flags.push({
      label: `OI-SURGE${dir}`,
      color: dir === "+" ? "rgba(82, 185, 122, 0.95)" : "rgba(214, 139, 105, 0.95)",
      title: `OI surge: Δ5m=${oiD5m?.toFixed(2)}% Δ1h=${oiD1h?.toFixed(2)}% (thr ${thresholds.oiSurgePct}%)`,
    });
  }

  if (
    (fundZ != null && Math.abs(fundZ) > thresholds.fundingZExtreme) ||
    (fund != null && Math.abs(fund * 10_000) > thresholds.fundingExtremeBps)
  ) {
    flags.push({
      label: "FUNDING-EXTREME",
      color: "rgba(227, 180, 87, 0.95)",
      title: `Funding ${(fund ? fund * 10000 : 0).toFixed(2)}bps · z=${fundZ?.toFixed(2) ?? "—"}`,
    });
  }

  if (fragility != null && fragility >= thresholds.fragilitySpike) {
    flags.push({
      label: "FRAGILITY",
      color: "rgba(227, 180, 87, 0.95)",
      title: `Fragility ${fragility.toFixed(0)} ≥ threshold ${thresholds.fragilitySpike.toFixed(0)}`,
    });
  }

  // Directional pressure — adaptive via OBI flip magnitude.
  if (
    (fund != null && fund > 0.0001 && obi != null && obi > thresholds.obiFlipMag) ||
    (fundZ != null && fundZ > thresholds.fundingZExtreme && oiD1h != null && oiD1h > 1)
  ) {
    flags.push({
      label: "PRESSURE-LONG",
      color: "rgba(214, 139, 105, 0.95)",
      title: "Long-side crowding",
    });
  }
  if (
    (fund != null && fund < -0.0001 && obi != null && obi < -thresholds.obiFlipMag) ||
    (fundZ != null && fundZ < -thresholds.fundingZExtreme && oiD1h != null && oiD1h > 1)
  ) {
    flags.push({
      label: "PRESSURE-SHORT",
      color: "rgba(140, 170, 235, 0.95)",
      title: "Short-side crowding",
    });
  }

  return flags;
}

// ── Regime hierarchy ─────────────────────────────────────────────────────

/**
 * Sister to detectRegime — returns not just the chosen regime, but the
 * conditions that brought each candidate forward. Powers the audit panel.
 */
export function explainRegime(
  metrics: MetricsMap,
  flags: Flag[],
  thresholds: AdaptiveThresholds,
  confidence: number,
): RegimeExplanation {
  const labels = new Set(flags.map((f) => f.label));
  const liqStress = getMetric(metrics, "liq_stress") ?? 0;
  const fragility = getMetric(metrics, "fragility_score");
  const cd = getMetric(metrics, "credible_depth");
  const spread = getMetric(metrics, "spread");

  const candidates: { regime: Regime; driver: string }[] = [];

  if (labels.has("LIQ-STRESS") && liqStress > 100_000) {
    candidates.push({
      regime: "LIQUIDATION_CASCADE",
      driver: `Liquidations $${liqStress.toFixed(0)} > $100k & LIQ-STRESS flag`,
    });
  }
  if (labels.has("FRAGILITY") || (fragility != null && fragility >= thresholds.fragilitySpike)) {
    candidates.push({
      regime: "UNSTABLE_MARKET",
      driver: `Fragility ${fragility?.toFixed(0) ?? "?"} ≥ ${thresholds.fragilitySpike.toFixed(0)}`,
    });
  }
  if (labels.has("SPOOF-RISK")) {
    candidates.push({
      regime: "SPOOF_PRONE",
      driver: `Spread ${((spread ?? 0) * 10000).toFixed(1)}bps > threshold & THIN depth`,
    });
  }
  if (labels.has("THIN")) {
    candidates.push({
      regime: "THIN_LIQUIDITY",
      driver: `Credible depth ${cd?.toFixed(0) ?? "?"} below cohort P10`,
    });
  }
  if (labels.has("PRESSURE-LONG")) {
    candidates.push({
      regime: "CROWDED_LONGS",
      driver: "Funding + OBI + OI Δ all bullish-crowded",
    });
  }
  if (labels.has("PRESSURE-SHORT")) {
    candidates.push({
      regime: "CROWDED_SHORTS",
      driver: "Funding + OBI + OI Δ all bearish-crowded",
    });
  }

  candidates.sort((a, b) => REGIME_RANK[b.regime] - REGIME_RANK[a.regime]);
  const winner: Regime = candidates[0]?.regime ?? "HEALTHY_TREND";
  const drivers = candidates.length === 0
    ? ["No regime triggers fired — falling back to HEALTHY_TREND"]
    : candidates.map((c) => `${c.regime}: ${c.driver}`);

  return {
    regime: winner,
    candidates: candidates.map((c) => c.regime),
    drivers,
    confidence,
  };
}

// True iff at least one metric in the map carries a non-null value.
// Used by detectRegime / computeConfidence to distinguish "no data yet"
// from "data measured, nothing wrong". Without this distinction the
// downstream logic returns HEALTHY_TREND / 92 for empty inputs, which
// is the right answer to the wrong question.
function _hasAnyMetricValue(metrics: MetricsMap): boolean {
  for (const k in metrics) {
    if (metrics[k] && metrics[k].value != null) return true;
  }
  return false;
}


export function detectRegime(metrics: MetricsMap, flags: Flag[]): Regime {
  if (!_hasAnyMetricValue(metrics)) return "UNKNOWN";
  const labels = new Set(flags.map((f) => f.label));
  const liqStress = getMetric(metrics, "liq_stress") ?? 0;
  const fragility = getMetric(metrics, "fragility_score");

  // Candidates are scored against REGIME_RANK so a single dominant label
  // wins even when several flags fire — no more "THIN + PRESSURE_LONG +
  // SPOOF + FRAGILITY all claiming to be the regime".
  const candidates: Regime[] = [];
  if (labels.has("LIQ-STRESS") && liqStress > 100_000) candidates.push("LIQUIDATION_CASCADE");
  if (labels.has("FRAGILITY") || (fragility != null && fragility >= 60)) candidates.push("UNSTABLE_MARKET");
  if (labels.has("SPOOF-RISK")) candidates.push("SPOOF_PRONE");
  if (labels.has("THIN")) candidates.push("THIN_LIQUIDITY");
  if (labels.has("PRESSURE-LONG")) candidates.push("CROWDED_LONGS");
  if (labels.has("PRESSURE-SHORT")) candidates.push("CROWDED_SHORTS");

  if (candidates.length === 0) return "HEALTHY_TREND";
  return candidates.sort((a, b) => REGIME_RANK[b] - REGIME_RANK[a])[0];
}

// ── Intelligence score with regime-aware weights ─────────────────────────

const WEIGHTS_BY_REGIME: Record<Regime, Record<string, number>> = {
  // UNKNOWN never reaches the score computation (intelligenceScore
  // returns null when no metrics are present), but the Record<Regime, …>
  // type requires every regime key. Copying HEALTHY_TREND weights here
  // is harmless and avoids a separate special case.
  UNKNOWN: {
    spread: 1.0, credible_depth: 1.2, atr_liquidity: 0.8,
    obi: 0.5, liq_stress: 0.6, funding_z: 0.7, oi_delta_1h: 0.7,
    resiliency_score: 0.6, impact_inv: 0.4, fragility_inv: 0.4,
  },
  HEALTHY_TREND: {
    spread: 1.0, credible_depth: 1.2, atr_liquidity: 0.8,
    obi: 0.5, liq_stress: 0.6, funding_z: 0.7, oi_delta_1h: 0.7,
    resiliency_score: 0.6, impact_inv: 0.4, fragility_inv: 0.4,
  },
  THIN_LIQUIDITY: {
    spread: 1.2, credible_depth: 1.6, atr_liquidity: 0.6,
    obi: 0.4, liq_stress: 0.8, funding_z: 0.4, oi_delta_1h: 0.3,
    resiliency_score: 1.0, impact_inv: 0.8, fragility_inv: 0.8,
  },
  SPOOF_PRONE: {
    spread: 1.4, credible_depth: 1.6, atr_liquidity: 0.4,
    obi: 0.3, liq_stress: 0.6, funding_z: 0.3, oi_delta_1h: 0.3,
    resiliency_score: 1.2, impact_inv: 1.0, fragility_inv: 0.8,
  },
  LIQUIDATION_CASCADE: {
    spread: 1.0, credible_depth: 1.4, atr_liquidity: 0.4,
    obi: 0.4, liq_stress: 1.6, funding_z: 0.6, oi_delta_1h: 0.6,
    resiliency_score: 1.4, impact_inv: 1.0, fragility_inv: 1.2,
  },
  CROWDED_LONGS: {
    spread: 0.8, credible_depth: 1.0, atr_liquidity: 0.8,
    obi: 0.6, liq_stress: 0.6, funding_z: 1.2, oi_delta_1h: 1.2,
    resiliency_score: 0.6, impact_inv: 0.4, fragility_inv: 0.4,
  },
  CROWDED_SHORTS: {
    spread: 0.8, credible_depth: 1.0, atr_liquidity: 0.8,
    obi: 0.6, liq_stress: 0.6, funding_z: 1.2, oi_delta_1h: 1.2,
    resiliency_score: 0.6, impact_inv: 0.4, fragility_inv: 0.4,
  },
  UNSTABLE_MARKET: {
    spread: 1.0, credible_depth: 1.2, atr_liquidity: 0.4,
    obi: 0.4, liq_stress: 1.0, funding_z: 0.6, oi_delta_1h: 0.4,
    resiliency_score: 1.4, impact_inv: 1.0, fragility_inv: 1.4,
  },
};

const COMPONENT_LABELS: Record<string, string> = {
  spread: "Spread",
  credible_depth: "Credible Depth",
  atr_liquidity: "ATR Liquidity",
  obi: "OBI",
  liq_stress: "Liquidation Stress",
  funding_z: "Funding Z",
  oi_delta_1h: "OI Δ 1H",
  resiliency_score: "Resiliency",
  impact_inv: "Impact (inv)",
  fragility_inv: "Fragility (inv)",
};

/**
 * Compute the score AND a per-component breakdown the UI can render as
 * positive/negative contributors. The "delta" is contribution minus the
 * mean contribution — a contribution that lifts the score relative to its
 * peers shows as positive, one that drags it down as negative. That's a
 * more useful framing than absolute contribution because every component
 * is positive-coded.
 */
export function intelligenceBreakdown(
  metrics: MetricsMap,
  cohort: CohortStats,
  regime: Regime,
): IntelBreakdown | null {
  const w = WEIGHTS_BY_REGIME[regime];
  const raw: { key: string; score: number; weight: number }[] = [];

  const push = (score: number | null, key: keyof typeof w) => {
    if (score == null || !Number.isFinite(score)) return;
    const weight = w[key as string] ?? 0;
    if (weight === 0) return;
    raw.push({ key: String(key), score: Math.max(0, Math.min(100, score)), weight });
  };

  const spread = getMetric(metrics, "spread");
  if (spread != null && cohort.spreadP10 != null) {
    push(100 * (cohort.spreadP10 / Math.max(spread, 1e-9)), "spread");
  }
  const credDepth = getMetric(metrics, "credible_depth");
  if (credDepth != null && cohort.credDepthP90 != null && cohort.credDepthP90 > 0) {
    push((credDepth / cohort.credDepthP90) * 100, "credible_depth");
  }
  const atrLiq = getMetric(metrics, "atr_liquidity");
  if (atrLiq != null && cohort.atrLiquidityP90 != null && cohort.atrLiquidityP90 > 0) {
    push((atrLiq / cohort.atrLiquidityP90) * 100, "atr_liquidity");
  }
  const obi = getMetric(metrics, "obi");
  if (obi != null) push(100 * (1 - Math.min(1, Math.abs(obi) / 0.6)), "obi");
  const liqStress = getMetric(metrics, "liq_stress");
  if (liqStress != null && cohort.liqStressP90 != null) {
    const ratio = cohort.liqStressP90 > 0 ? liqStress / cohort.liqStressP90 : 0;
    push(100 - 100 * Math.min(1, ratio), "liq_stress");
  }
  const fundZ = getMetric(metrics, "funding_z");
  if (fundZ != null) push(100 - (Math.abs(fundZ) / 3) * 100, "funding_z");
  const oiDelta = getMetric(metrics, "oi_delta_1h");
  if (oiDelta != null) push(100 - Math.max(0, Math.abs(oiDelta) - 5) * 10, "oi_delta_1h");
  const resil = getMetric(metrics, "resiliency_score");
  if (resil != null) push(resil, "resiliency_score");
  const impact = getMetric(metrics, "impact_score");
  if (impact != null) push(100 - impact, "impact_inv");
  const fragility = getMetric(metrics, "fragility_score");
  if (fragility != null) push(100 - fragility, "fragility_inv");

  if (raw.length === 0) return null;

  const totalW = raw.reduce((s, p) => s + p.weight, 0);
  const sw = raw.reduce((s, p) => s + p.score * p.weight, 0);
  const score = sw / totalW;
  const sortedScores = raw.map((p) => p.score).sort((a, b) => a - b);
  const worstFew = sortedScores.slice(0, Math.max(1, Math.floor(sortedScores.length / 3)));
  const quality = worstFew.reduce((s, v) => s + v, 0) / worstFew.length;

  // contribution = score×weight; delta vs the per-component mean tells
  // which signals pull the score up vs drag it down.
  const meanContribution = sw / raw.length;
  const contributions: IntelContribution[] = raw
    .map((p) => {
      const c = p.score * p.weight;
      return {
        key: p.key,
        label: COMPONENT_LABELS[p.key] ?? p.key,
        score: p.score,
        weight: p.weight,
        contribution: c,
        delta: c - meanContribution,
      };
    })
    .sort((a, b) => b.delta - a.delta);

  const positive = contributions.filter((c) => c.delta > 0).slice(0, 5);
  const negative = [...contributions].filter((c) => c.delta < 0).sort((a, b) => a.delta - b.delta).slice(0, 5);

  return { score, quality, contributions, positive, negative };
}

export function intelligenceScore(
  metrics: MetricsMap,
  cohort: CohortStats,
  regime: Regime,
): { score: number; quality: number } | null {
  const w = WEIGHTS_BY_REGIME[regime];
  const parts: { score: number; weight: number }[] = [];

  const push = (score: number | null, key: keyof typeof w) => {
    if (score == null || !Number.isFinite(score)) return;
    const weight = w[key as string] ?? 0;
    if (weight === 0) return;
    parts.push({ score: Math.max(0, Math.min(100, score)), weight });
  };

  const spread = getMetric(metrics, "spread");
  if (spread != null && cohort.spreadP10 != null) {
    push(100 * (cohort.spreadP10 / Math.max(spread, 1e-9)), "spread");
  }
  const credDepth = getMetric(metrics, "credible_depth");
  if (credDepth != null && cohort.credDepthP90 != null && cohort.credDepthP90 > 0) {
    push((credDepth / cohort.credDepthP90) * 100, "credible_depth");
  }
  const atrLiq = getMetric(metrics, "atr_liquidity");
  if (atrLiq != null && cohort.atrLiquidityP90 != null && cohort.atrLiquidityP90 > 0) {
    push((atrLiq / cohort.atrLiquidityP90) * 100, "atr_liquidity");
  }
  const obi = getMetric(metrics, "obi");
  if (obi != null) push(100 * (1 - Math.min(1, Math.abs(obi) / 0.6)), "obi");
  const liqStress = getMetric(metrics, "liq_stress");
  if (liqStress != null && cohort.liqStressP90 != null) {
    const ratio = cohort.liqStressP90 > 0 ? liqStress / cohort.liqStressP90 : 0;
    push(100 - 100 * Math.min(1, ratio), "liq_stress");
  }
  const fundZ = getMetric(metrics, "funding_z");
  if (fundZ != null) push(100 - (Math.abs(fundZ) / 3) * 100, "funding_z");
  const oiDelta = getMetric(metrics, "oi_delta_1h");
  if (oiDelta != null) push(100 - Math.max(0, Math.abs(oiDelta) - 5) * 10, "oi_delta_1h");

  const resil = getMetric(metrics, "resiliency_score");
  if (resil != null) push(resil, "resiliency_score");
  const impact = getMetric(metrics, "impact_score");
  if (impact != null) push(100 - impact, "impact_inv");
  const fragility = getMetric(metrics, "fragility_score");
  if (fragility != null) push(100 - fragility, "fragility_inv");

  if (parts.length === 0) return null;
  const totalW = parts.reduce((s, p) => s + p.weight, 0);
  const sw = parts.reduce((s, p) => s + p.score * p.weight, 0);
  const score = sw / totalW;
  const sortedScores = parts.map((p) => p.score).sort((a, b) => a - b);
  const worstFew = sortedScores.slice(0, Math.max(1, Math.floor(sortedScores.length / 3)));
  const quality = worstFew.reduce((s, v) => s + v, 0) / worstFew.length;
  return { score, quality };
}

// ── Confidence engine ────────────────────────────────────────────────────

export function computeConfidence(
  metrics: MetricsMap,
  args: {
    isSubscribed: boolean;
    wsConnected: boolean;
    lastWsFrameMs: number | null;
    snapshotAgeMs: number | null;
    flags: Flag[];
    regime: Regime;
    intel: { score: number; quality: number } | null;
    prevRegime?: Regime;
  },
): Confidence {
  // First render gate: if we have no metric samples, every penalty
  // below short-circuits to "no problem", yielding score≈92 HIGH —
  // an actively misleading "all good" badge. UNKNOWN says that
  // honestly. score stays 0 (not visible in the UI for UNKNOWN).
  if (!_hasAnyMetricValue(metrics)) {
    return { score: 0, state: "UNKNOWN", reasons: ["awaiting metrics"] };
  }

  let score = 100;
  const reasons: string[] = [];

  // 1) Data freshness — biggest hit when WS data is missing or stale.
  if (!args.wsConnected) {
    score -= 25;
    reasons.push("ws down");
  } else if (args.isSubscribed && args.lastWsFrameMs != null && args.lastWsFrameMs > 8_000) {
    score -= 20;
    reasons.push(`ws stale ${(args.lastWsFrameMs / 1000).toFixed(1)}s`);
  }
  if (args.snapshotAgeMs != null && args.snapshotAgeMs > 15_000) {
    score -= 10;
    reasons.push("snapshot stale");
  }
  if (!args.isSubscribed) {
    // Non-pinned/non-active symbol → relying on REST-only data → less
    // certainty about microstructure signals.
    score -= 8;
    reasons.push("REST-only");
  }

  // 2) Metric agreement — sign agreement across OBI / funding / OI Δ
  // confirms positioning calls; disagreement lowers confidence.
  const obi = getMetric(metrics, "obi");
  const fund = getMetric(metrics, "funding");
  const oiD = getMetric(metrics, "oi_delta_1h");
  if (obi != null && fund != null) {
    const obiSign = obi > 0.1 ? 1 : obi < -0.1 ? -1 : 0;
    const fundSign = fund > 0.0001 ? 1 : fund < -0.0001 ? -1 : 0;
    if (obiSign !== 0 && fundSign !== 0 && obiSign !== fundSign) {
      score -= 10;
      reasons.push("OBI/funding disagree");
    }
  }
  if (oiD != null && obi != null && Math.abs(oiD) > 3) {
    const oiSign = oiD > 0 ? 1 : -1;
    const obiSign = obi > 0.1 ? 1 : obi < -0.1 ? -1 : 0;
    if (obiSign !== 0 && obiSign !== oiSign) {
      score -= 6;
      reasons.push("OBI/OI disagree");
    }
  }

  // 3) Anomaly clustering — the more flags concur, the more confident
  // the regime call.
  if (args.flags.length >= 3) {
    score += 6;
    reasons.push("flags cluster");
  } else if (args.flags.length === 0 && args.regime !== "HEALTHY_TREND") {
    // Regime claimed but no supporting flags — suspicious.
    score -= 8;
    reasons.push("regime w/o flags");
  }

  // 4) Score consistency — quality lagging score by a wide margin means
  // one signal is shouting; treat with skepticism.
  if (args.intel) {
    const gap = args.intel.score - args.intel.quality;
    if (gap > 30) {
      score -= 6;
      reasons.push(`score/quality gap ${gap.toFixed(0)}`);
    }
  }

  // 5) Regime stability — flipping vs previous tick is allowed once but
  // jitters drop confidence. We only know the immediate-previous regime
  // here; the caller may pass it via prevRegime.
  if (args.prevRegime && args.prevRegime !== args.regime && args.regime !== "HEALTHY_TREND") {
    score -= 4;
    reasons.push(`transitioned from ${args.prevRegime}`);
  }

  score = Math.max(0, Math.min(100, score));
  const state: ConfidenceState = score >= 75 ? "HIGH" : score >= 50 ? "MEDIUM" : "LOW";
  return { score, state, reasons };
}

// ── Alert engine (per-tick proposals) ────────────────────────────────────

export type AlertProposal = {
  kind: AlertKind;
  trigger: string;
  severity: Severity;
};

/**
 * Produce candidate alerts for one tick — the caller's stateful engine
 * applies persistence, debounce and cooldown. Returning proposals here
 * keeps this module pure; the React hook owns the timing rules.
 */
export function proposeAlerts(
  metrics: MetricsMap,
  flags: Flag[],
  regime: Regime,
  thresholds: AdaptiveThresholds,
  prevRegime: Regime | undefined,
): AlertProposal[] {
  const out: AlertProposal[] = [];
  const labels = new Set(flags.map((f) => f.label));
  const spread = getMetric(metrics, "spread");
  const cd = getMetric(metrics, "credible_depth");
  const ls = getMetric(metrics, "liq_stress");
  const fragility = getMetric(metrics, "fragility_score");
  const resil = getMetric(metrics, "resiliency_score");
  const fundZ = getMetric(metrics, "funding_z");
  const fund = getMetric(metrics, "funding");
  const oiD1h = getMetric(metrics, "oi_delta_1h");

  if (spread != null && spread > thresholds.spreadExplosion * 1.5) {
    out.push({
      kind: "SPREAD_EXPLOSION",
      trigger: `Spread ${(spread * 10000).toFixed(1)}bps > ${(thresholds.spreadExplosion * 10000 * 1.5).toFixed(1)}bps`,
      severity: spread > thresholds.spreadExplosion * 3 ? "critical" : "warn",
    });
  }

  if (cd != null && cd > 0 && fragility != null && fragility >= thresholds.fragilitySpike) {
    out.push({
      kind: "FRAGILITY_SPIKE",
      trigger: `Fragility ${fragility.toFixed(0)} ≥ ${thresholds.fragilitySpike.toFixed(0)}`,
      severity: fragility >= 80 ? "critical" : "warn",
    });
  }

  if (labels.has("THIN") && cd != null && spread != null && spread > thresholds.spreadExplosion) {
    out.push({
      kind: "DEPTH_COLLAPSE",
      trigger: `Thin depth + spread ${(spread * 10000).toFixed(1)}bps`,
      severity: "warn",
    });
  }

  if (ls != null && ls >= thresholds.liqSpikeUsd && regime === "LIQUIDATION_CASCADE") {
    out.push({
      kind: "LIQ_CASCADE",
      trigger: `Liquidations $${ls.toFixed(0)} over rolling 60s`,
      severity: ls >= thresholds.liqSpikeUsd * 5 ? "critical" : "warn",
    });
  }

  if (resil != null && resil < 25) {
    out.push({
      kind: "RESILIENCY_FAILURE",
      trigger: `Resiliency ${resil.toFixed(0)} < 25 — book not refilling`,
      severity: resil < 10 ? "critical" : "warn",
    });
  }

  if (
    (oiD1h != null && Math.abs(oiD1h) > thresholds.oiSurgePct * 1.5)
  ) {
    out.push({
      kind: "OI_SURGE",
      trigger: `OI Δ1h ${oiD1h.toFixed(2)}%`,
      severity: Math.abs(oiD1h) > thresholds.oiSurgePct * 3 ? "critical" : "warn",
    });
  }

  if (
    (fundZ != null && Math.abs(fundZ) > thresholds.fundingZExtreme * 1.25) ||
    (fund != null && Math.abs(fund * 10_000) > thresholds.fundingExtremeBps * 1.25)
  ) {
    out.push({
      kind: "FUNDING_EXTREME",
      trigger: `Funding ${(fund ? fund * 10000 : 0).toFixed(2)}bps · z=${fundZ?.toFixed(2) ?? "—"}`,
      severity: Math.abs(fundZ ?? 0) > 3 ? "critical" : "warn",
    });
  }

  if (prevRegime && prevRegime !== regime && REGIME_RANK[regime] > REGIME_RANK[prevRegime] + 20) {
    out.push({
      kind: "REGIME_TRANSITION",
      trigger: `${prevRegime} → ${regime}`,
      severity: REGIME_RANK[regime] >= 80 ? "critical" : "info",
    });
  }

  return out;
}

// Stable id for an alert — same trigger over consecutive ticks produces
// the same id, so the engine can update lastSeenAt instead of duplicating.
export function alertId(symbol: string, kind: AlertKind, bucket: number): string {
  return `${symbol}:${kind}:${bucket}`;
}

/* ── Alert validation ───────────────────────────────────────────────────
 *
 * After an alert promotes to the timeline, we want to know if "something
 * happened" — was there a price move, did spread stay wide, did liq
 * stress follow through? The validator is a pure function over the
 * symbol's metric series within a window after the alert; the UI passes
 * in the already-fetched series to keep this module dependency-free.
 */

export type ValidationOutcome = "followed_through" | "noise" | "pending";

export type AlertValidation = {
  outcome: ValidationOutcome;
  priceMovePct: number | null;
  spreadFollowThrough: boolean;
  liqFollowThrough: boolean;
  resilDeterioration: boolean;
  notes: string[];
};

export type Series = { ts: number; value: number | null; price: number | null }[];

export function validateAlert(
  alert: AlertEvent,
  series: {
    price: Series;          // any metric carrying the price field is fine
    spread: Series;
    liq_stress: Series;
    resiliency_score: Series;
  },
  windowMs = 5 * 60_000,
): AlertValidation {
  const t0 = alert.startedAt;
  const t1 = t0 + windowMs;
  const inWindow = (s: Series) => s.filter((p) => p.ts >= t0 && p.ts <= t1);
  const notes: string[] = [];

  // Price move — biggest swing in the window vs the price at t0.
  const priceSeries = inWindow(series.price).filter((p) => p.price != null);
  let priceMovePct: number | null = null;
  if (priceSeries.length >= 2) {
    const p0 = priceSeries[0].price as number;
    const extremes = priceSeries.map((p) => (p.price as number) - p0);
    const max = Math.max(...extremes);
    const min = Math.min(...extremes);
    const swing = Math.abs(max) > Math.abs(min) ? max : min;
    priceMovePct = (swing / p0) * 100;
  }
  if (priceMovePct != null && Math.abs(priceMovePct) >= 0.4) {
    notes.push(`Price moved ${priceMovePct.toFixed(2)}% in window`);
  }

  // Spread persistence — at least half the post-window samples remain
  // above the pre-alert level (or above 5 bps absolute).
  const spreadSeries = inWindow(series.spread).filter((p) => p.value != null);
  const preSpread = series.spread.find((p) => p.ts <= t0 && p.value != null)?.value ?? null;
  let spreadFollowThrough = false;
  if (spreadSeries.length >= 4) {
    const thresh = Math.max((preSpread ?? 0.0005) * 1.5, 0.0005);
    const above = spreadSeries.filter((p) => (p.value as number) >= thresh).length;
    spreadFollowThrough = above >= spreadSeries.length / 2;
  }
  if (spreadFollowThrough) notes.push("Spread stayed wide");

  // Liq stress follow-through: any sample in window ≥ alert threshold.
  const lsSeries = inWindow(series.liq_stress).filter((p) => p.value != null);
  const lsFollow = lsSeries.some((p) => (p.value as number) >= 50_000);
  if (lsFollow) notes.push("Further liquidations followed");

  // Resiliency deterioration: dropped ≥ 15 points within window from t0.
  const resilSeries = inWindow(series.resiliency_score).filter((p) => p.value != null);
  const preResil = series.resiliency_score.find((p) => p.ts <= t0 && p.value != null)?.value ?? null;
  let resilDeterioration = false;
  if (preResil != null && resilSeries.length > 0) {
    const minPost = Math.min(...resilSeries.map((p) => p.value as number));
    if (preResil - minPost >= 15) {
      resilDeterioration = true;
      notes.push("Resiliency dropped 15+ points");
    }
  }

  const followThrough =
    (priceMovePct != null && Math.abs(priceMovePct) >= 0.4) ||
    spreadFollowThrough ||
    lsFollow ||
    resilDeterioration;

  // Pending if the window hasn't fully elapsed yet — caller distinguishes
  // by checking Date.now() − alert.lastSeenAt.
  const elapsedEnough = Date.now() - t0 >= windowMs;
  const outcome: ValidationOutcome = !elapsedEnough
    ? "pending"
    : followThrough
      ? "followed_through"
      : "noise";

  return {
    outcome,
    priceMovePct,
    spreadFollowThrough,
    liqFollowThrough: lsFollow,
    resilDeterioration,
    notes,
  };
}

export type ValidationStats = {
  total: number;
  resolved: number;
  followedThrough: number;
  noise: number;
  precision: number;      // followedThrough / resolved, 0..1
  pending: number;
};

export function aggregateValidation(outcomes: ValidationOutcome[]): ValidationStats {
  const total = outcomes.length;
  const resolved = outcomes.filter((o) => o !== "pending").length;
  const followedThrough = outcomes.filter((o) => o === "followed_through").length;
  const noise = outcomes.filter((o) => o === "noise").length;
  const pending = outcomes.filter((o) => o === "pending").length;
  const precision = resolved === 0 ? 0 : followedThrough / resolved;
  return { total, resolved, followedThrough, noise, precision, pending };
}
