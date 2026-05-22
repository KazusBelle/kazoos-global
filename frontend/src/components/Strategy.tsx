/**
 * Phase-10 Strategy dashboard.
 *
 * One layer above Operations: the strategic-state classifier, structural
 * break detector, regime-shift early warning, edge survival, meta-
 * confidence, long-horizon market evolution. Same UX language as
 * Operations / Research — no recommendations, only descriptive intel.
 */

import { useEffect, useMemo, useState } from "react";
import {
  getAdaptiveReliability,
  getEdgeSurvival,
  getMarketEvolution,
  getMetaConfidence,
  getRegimeShiftWarning,
  getStrategicState,
  getStructuralBreaks,
  type AdaptiveReliabilityOut,
  type EdgeSurvivalOut,
  type MarketEvolution,
  type MetaConfidence,
  type RegimeShiftWarning,
  type StrategicState,
  type StructuralBreakOut,
} from "../lib/api";

const REFRESH_MS = 30_000;

export function Strategy() {
  return (
    <div className="space-y-4">
      <div className="flex items-baseline gap-3">
        <div className="text-accent text-xl font-bold tracking-[0.3em]">STRAT</div>
        <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
          strategic intelligence · structural shifts · adaptation
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <StrategicStateCard />
        <RegimeShiftCard />
      </div>
      <StructuralBreaksPanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MetaConfidencePanel />
        <AdaptiveReliabilityPanel />
      </div>
      <EdgeSurvivalPanel />
      <MarketEvolutionPanel />
    </div>
  );
}

function Panel({
  title, subtitle, toolbar, children,
}: { title: string; subtitle?: string; toolbar?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-border bg-panel">
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-2.5">
        <div className="flex items-baseline gap-3">
          <span className="text-[11px] uppercase tracking-[0.22em] text-zinc-100">{title}</span>
          {subtitle && <span className="text-[10px] uppercase tracking-[0.2em] text-muted">{subtitle}</span>}
        </div>
        {toolbar}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

const STRATEGIC_STATE_COLOR: Record<StrategicState["state"], string> = {
  STABLE_INSTITUTIONAL_FLOW: "rgba(82, 185, 122, 0.95)",
  FRAGILE_SPECULATIVE_MARKET: "rgba(227, 180, 87, 0.95)",
  TRANSITIONAL_UNSTABLE: "rgba(214, 139, 105, 0.95)",
  LIQUIDITY_DETERIORATION_PHASE: "rgba(214, 105, 105, 0.95)",
  CASCADE_RISK_ENVIRONMENT: "rgba(214, 75, 75, 0.95)",
};

const TRUST_COLOR: Record<StrategicState["trustworthiness"], string> = {
  TRUSTWORTHY: "rgba(82, 185, 122, 0.95)",
  GUARDED: "rgba(227, 180, 87, 0.95)",
  UNRELIABLE: "rgba(214, 105, 105, 0.95)",
  UNKNOWN: "rgba(140, 170, 235, 0.95)",
};

// ── Strategic State headline card ────────────────────────────────────────

function StrategicStateCard() {
  const [data, setData] = useState<StrategicState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await getStrategicState();
        if (!cancelled) setData(d);
      } catch (e: any) {
        if (!cancelled) setError(e?.message ?? "failed");
      }
    };
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  const color = data ? STRATEGIC_STATE_COLOR[data.state] : "rgba(140, 170, 235, 0.95)";
  const trustColor = data ? TRUST_COLOR[data.trustworthiness] : "rgba(140, 170, 235, 0.95)";

  return (
    <Panel title="Strategic State" subtitle="aggregate market intelligence classification">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-3">
          <div
            className="rounded-lg border px-3 py-3"
            style={{ borderColor: color.replace(/0\.95\)$/, "0.45)"), background: color.replace(/0\.95\)$/, "0.08)") }}
          >
            <div className="flex items-baseline gap-2 flex-wrap">
              <span className="text-[10px] uppercase tracking-[0.2em] text-muted">classified</span>
              <span className="text-zinc-100 text-lg font-bold tracking-[0.15em]" style={{ color }}>
                {data.state.replace(/_/g, " ")}
              </span>
            </div>
            <div className="text-[10px] uppercase tracking-[0.15em] mt-1" style={{ color: trustColor }}>
              meta-confidence: {data.trustworthiness}
            </div>
          </div>
          <ul className="text-[11px] text-zinc-300 space-y-0.5">
            {data.rationale.map((r, i) => (
              <li key={i}>
                <span className="text-muted">▸</span> {r}
              </li>
            ))}
          </ul>
          <div className="grid grid-cols-2 gap-2 text-[10px] font-mono pt-2 border-t border-border/40">
            <div className="flex justify-between"><span className="text-muted">stress</span><span className="text-zinc-200">{data.inputs.stress_level} · {data.inputs.stress_score.toFixed(0)}</span></div>
            <div className="flex justify-between"><span className="text-muted">shift warn</span><span className="text-zinc-200">{data.inputs.shift_warning}</span></div>
            <div className="flex justify-between"><span className="text-muted">break score</span><span className="text-zinc-200">{data.inputs.structural_break_score.toFixed(0)}</span></div>
            <div className="flex justify-between"><span className="text-muted">dominant regime</span><span className="text-zinc-200">{data.inputs.dominant_regime}</span></div>
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Regime Shift Early Warning ───────────────────────────────────────────

const WARNING_COLOR: Record<RegimeShiftWarning["warning_state"], string> = {
  STABLE: "rgba(82, 185, 122, 0.95)",
  WATCH: "rgba(140, 170, 235, 0.95)",
  ELEVATED_TRANSITION_RISK: "rgba(227, 180, 87, 0.95)",
  PRE_CASCADE: "rgba(214, 75, 75, 0.95)",
  INSUFFICIENT_DATA: "rgba(90, 95, 107, 0.95)",
};

function RegimeShiftCard() {
  const [data, setData] = useState<RegimeShiftWarning | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await getRegimeShiftWarning();
        if (!cancelled) setData(d);
      } catch (e: any) {
        if (!cancelled) setError(e?.message ?? "failed");
      }
    };
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  const color = data ? WARNING_COLOR[data.warning_state] : "rgba(140, 170, 235, 0.95)";
  return (
    <Panel title="Regime Shift Warning" subtitle="acceleration of structural stress · last 60m vs prior 180m">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-3">
          <div className="flex items-baseline gap-3 flex-wrap">
            <span className="text-4xl font-bold" style={{ color }}>{data.regime_shift_probability.toFixed(0)}%</span>
            <span className="text-[11px] uppercase tracking-[0.2em]" style={{ color }}>{data.warning_state.replace(/_/g, " ")}</span>
            <span className="text-[10px] text-muted">accel {data.instability_acceleration.toFixed(0)}</span>
          </div>
          <div className="space-y-1.5">
            {data.signals.length === 0 ? (
              <div className="text-xs text-muted">No acceleration signals.</div>
            ) : data.signals.map((s) => (
              <div key={s.name} className="flex items-center gap-2 text-[10px] font-mono">
                <span className="text-muted w-44 truncate">{s.name.replace(/_/g, " ")}</span>
                <div className="flex-1 h-1.5 rounded bg-bg/60 overflow-hidden">
                  <div
                    className="h-full"
                    style={{
                      width: `${Math.min(100, s.value * 100).toFixed(0)}%`,
                      background: s.value > 0.6 ? "rgba(214, 75, 75, 0.9)" : s.value > 0.3 ? "rgba(227, 180, 87, 0.9)" : "rgba(82, 185, 122, 0.9)",
                    }}
                  />
                </div>
                <span className="text-zinc-300 w-14 text-right">{(s.value * 100).toFixed(0)}%</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Structural Breaks ────────────────────────────────────────────────────

function StructuralBreaksPanel() {
  const [data, setData] = useState<StructuralBreakOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [windowDays, setWindowDays] = useState(7);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getStructuralBreaks(windowDays)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [windowDays]);

  const score = data?.structural_break_score ?? 0;
  const scoreColor = score >= 60 ? "rgba(214, 75, 75, 0.95)" : score >= 35 ? "rgba(227, 180, 87, 0.95)" : "rgba(82, 185, 122, 0.95)";

  return (
    <Panel
      title="Structural Breaks"
      subtitle="cur window vs prior window of same length"
      toolbar={
        <div className="flex items-center gap-1">
          {[3, 7, 14].map((d) => (
            <button
              key={d}
              onClick={() => setWindowDays(d)}
              className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.14em] ${
                windowDays === d ? "border-accent text-accent bg-accent/10" : "border-border text-muted hover:text-zinc-200"
              }`}
            >
              {d}d
            </button>
          ))}
        </div>
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
          <div className="lg:col-span-1">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted">break score</div>
            <div className="text-5xl font-bold mt-1" style={{ color: scoreColor }}>
              {data.structural_break_score.toFixed(0)}
            </div>
            <div className="text-[10px] text-muted mt-1">
              confidence {data.break_confidence.toFixed(0)}%
            </div>
            <div className="mt-3 space-y-1 text-[10px] font-mono">
              <div className="flex justify-between"><span className="text-muted">corr drift</span><span>{data.components.correlation_drift.toFixed(0)}</span></div>
              <div className="flex justify-between"><span className="text-muted">median migration</span><span>{data.components.median_migration.toFixed(0)}</span></div>
              <div className="flex justify-between"><span className="text-muted">regime mix shift</span><span>{data.components.regime_mix_shift.toFixed(0)}</span></div>
            </div>
          </div>

          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">top corr deltas</div>
            <ul className="space-y-1 font-mono text-[10px]">
              {data.affected_correlations.length === 0 && <li className="text-muted">—</li>}
              {data.affected_correlations.map((c, i) => (
                <li key={i} className="flex items-center gap-1.5">
                  <span className="text-muted text-[9px] flex-1 truncate">{c.a} × {c.b}</span>
                  <span className={c.delta > 0 ? "text-[#52b97a]" : "text-[#d68b8b]"}>
                    {c.delta > 0 ? "+" : ""}{c.delta.toFixed(2)}
                  </span>
                </li>
              ))}
            </ul>
          </div>

          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">median migration</div>
            <ul className="space-y-1 font-mono text-[10px]">
              {data.affected_metrics.length === 0 && <li className="text-muted">—</li>}
              {data.affected_metrics.map((m, i) => (
                <li key={i} className="flex items-center gap-1.5">
                  <span className="text-muted text-[9px] flex-1 truncate">{m.metric}</span>
                  <span className={m.pct_delta > 0 ? "text-[#e3b457]" : "text-[#52b97a]"}>
                    {m.pct_delta > 0 ? "+" : ""}{(m.pct_delta * 100).toFixed(0)}%
                  </span>
                </li>
              ))}
            </ul>
          </div>

          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">regime share Δ</div>
            <ul className="space-y-1 font-mono text-[10px]">
              {data.affected_regimes.length === 0 && <li className="text-muted">—</li>}
              {data.affected_regimes.map((r, i) => (
                <li key={i} className="flex items-center gap-1.5">
                  <span className="text-muted text-[9px] flex-1 truncate">{r.regime}</span>
                  <span className={r.delta > 0 ? "text-[#d68b8b]" : "text-[#52b97a]"}>
                    {r.delta > 0 ? "+" : ""}{(r.delta * 100).toFixed(0)}pp
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Meta-Confidence ──────────────────────────────────────────────────────

const META_TRUST_COLOR: Record<MetaConfidence["trustworthiness_state"], string> = {
  TRUSTWORTHY: "rgba(82, 185, 122, 0.95)",
  GUARDED: "rgba(227, 180, 87, 0.95)",
  UNRELIABLE: "rgba(214, 75, 75, 0.95)",
  UNKNOWN: "rgba(140, 170, 235, 0.95)",
};

function MetaConfidencePanel() {
  const [data, setData] = useState<MetaConfidence | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMetaConfidence()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Meta-Confidence" subtitle="how reliable is our confidence">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-3">
          <div className="flex items-baseline gap-3">
            <span className="text-4xl font-bold" style={{ color: META_TRUST_COLOR[data.trustworthiness_state] }}>
              {data.meta_confidence_score.toFixed(0)}
            </span>
            <span className="text-[11px] uppercase tracking-[0.2em]" style={{ color: META_TRUST_COLOR[data.trustworthiness_state] }}>
              {data.trustworthiness_state}
            </span>
            <span className="text-[10px] text-muted">stability {data.confidence_stability.toFixed(0)}</span>
            <span className="text-[10px] text-muted">n={data.n_alerts}</span>
          </div>
          {data.components && (
            <div className="space-y-1.5">
              {Object.entries(data.components).map(([k, v]) => (
                <div key={k} className="flex items-center gap-2 text-[10px] font-mono">
                  <span className="text-muted w-44 truncate">{k.replace(/_/g, " ")}</span>
                  <div className="flex-1 h-1.5 rounded bg-bg/60 overflow-hidden">
                    <div className="h-full" style={{ width: `${v}%`, background: v >= 60 ? "rgba(82, 185, 122, 0.9)" : v >= 40 ? "rgba(227, 180, 87, 0.9)" : "rgba(214, 105, 105, 0.9)" }} />
                  </div>
                  <span className="text-zinc-300 w-10 text-right">{v.toFixed(0)}</span>
                </div>
              ))}
            </div>
          )}
          {data.noise_rate != null && (
            <div className="text-[10px] text-muted">
              recent noise rate {(data.noise_rate * 100).toFixed(0)}% · avg distinct regimes per symbol {data.avg_distinct_regimes?.toFixed(2)}
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

// ── Adaptive Reliability ─────────────────────────────────────────────────

function AdaptiveReliabilityPanel() {
  const [data, setData] = useState<AdaptiveReliabilityOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getAdaptiveReliability()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel
      title="Adaptive Reliability"
      subtitle={data ? `regime-weighted · dominant: ${data.dominant_regime}` : "regime-weighted reliability"}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.kinds.length === 0 && <div className="text-xs text-muted">No data.</div>}
      {data && data.kinds.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">RANK</th>
              <th className="text-left py-2">KIND</th>
              <th className="text-right py-2">BASE</th>
              <th className="text-right py-2">×REGIME</th>
              <th className="text-right py-2">ADJUSTED</th>
            </tr>
          </thead>
          <tbody>
            {data.kinds.map((k) => {
              const liftColor = k.regime_multiplier > 1.05 ? "text-[#52b97a]" : k.regime_multiplier < 0.95 ? "text-[#d68b8b]" : "text-muted";
              return (
                <tr key={k.kind} className="border-t border-border/40">
                  <td className="py-1.5 text-muted">{k.adjusted_rank}</td>
                  <td className="py-1.5 text-zinc-200">{k.kind.replace(/_/g, " ")}</td>
                  <td className="py-1.5 text-right text-muted">{k.reliability_score.toFixed(0)}</td>
                  <td className={`py-1.5 text-right ${liftColor}`}>{k.regime_multiplier.toFixed(2)}×</td>
                  <td className="py-1.5 text-right text-zinc-200 font-semibold">{k.regime_adjusted_reliability.toFixed(0)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

// ── Edge Survival ────────────────────────────────────────────────────────

function EdgeSurvivalPanel() {
  const [data, setData] = useState<EdgeSurvivalOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [threshold, setThreshold] = useState(0.5);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getEdgeSurvival({ threshold })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [threshold]);

  return (
    <Panel
      title="Edge Survival"
      subtitle="KM-style survival per alert kind · threshold = precision floor"
      toolbar={
        <div className="flex items-center gap-1">
          {[0.4, 0.5, 0.6].map((t) => (
            <button
              key={t}
              onClick={() => setThreshold(t)}
              className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.14em] ${
                threshold === t ? "border-accent text-accent bg-accent/10" : "border-border text-muted hover:text-zinc-200"
              }`}
            >
              {(t * 100).toFixed(0)}%
            </button>
          ))}
        </div>
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.kinds.length === 0 && <div className="text-xs text-muted">No survival data.</div>}
      {data && data.kinds.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">KIND</th>
              <th className="text-right py-2">LATEST</th>
              <th className="text-right py-2">DEATHS</th>
              <th className="text-right py-2">REMAIN</th>
              <th className="text-right py-2">ACCEL</th>
              <th className="text-left py-2 pl-4">SURVIVAL</th>
            </tr>
          </thead>
          <tbody>
            {data.kinds.map((k) => {
              const remainColor =
                k.expected_remaining_days == null ? "text-muted"
                  : k.expected_remaining_days < 7 ? "text-[#d68b8b]"
                    : k.expected_remaining_days < 21 ? "text-[#e3b457]"
                      : "text-[#52b97a]";
              const accelColor =
                k.degradation_acceleration == null ? "text-muted"
                  : k.degradation_acceleration < -0.005 ? "text-[#d68b8b]"
                    : k.degradation_acceleration > 0.005 ? "text-[#52b97a]"
                      : "text-zinc-300";
              return (
                <tr key={k.kind} className="border-t border-border/40">
                  <td className="py-1.5 text-zinc-200">{k.kind.replace(/_/g, " ")}</td>
                  <td className="py-1.5 text-right text-zinc-200">
                    {k.latest_precision != null ? `${(k.latest_precision * 100).toFixed(0)}%` : "—"}
                  </td>
                  <td className="py-1.5 text-right text-muted">{k.deaths}</td>
                  <td className={`py-1.5 text-right ${remainColor}`}>
                    {k.expected_remaining_days != null ? `${k.expected_remaining_days.toFixed(1)}d` : "—"}
                  </td>
                  <td className={`py-1.5 text-right ${accelColor}`}>
                    {k.degradation_acceleration != null ? k.degradation_acceleration.toFixed(3) : "—"}
                  </td>
                  <td className="py-1.5 pl-4">
                    <SurvivalSparkline curve={k.survival_curve} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

function SurvivalSparkline({ curve }: { curve: { ts: number; s: number }[] }) {
  const w = 180; const h = 24;
  if (curve.length < 2) return <span className="text-muted text-[10px]">insufficient</span>;
  const minT = curve[0].ts; const maxT = curve[curve.length - 1].ts;
  const spanT = Math.max(1, maxT - minT);
  const xy = curve.map((p) => {
    const x = ((p.ts - minT) / spanT) * (w - 4) + 2;
    const y = h - 2 - p.s * (h - 4);
    return [x, y] as const;
  });
  const d = `M${xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L")}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width={w} height={h}>
      <path d={d} fill="none" stroke="rgba(140, 170, 235, 0.9)" strokeWidth={1.4} strokeLinejoin="round" />
    </svg>
  );
}

// ── Market Evolution ─────────────────────────────────────────────────────

function MarketEvolutionPanel() {
  const [data, setData] = useState<MarketEvolution | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMarketEvolution(60, 7)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  const trendRows = useMemo(() => {
    if (!data) return [];
    return [...data.metric_trends].sort((a, b) => Math.abs(b.slope_per_day ?? 0) - Math.abs(a.slope_per_day ?? 0));
  }, [data]);

  return (
    <Panel title="Market Evolution" subtitle="long-horizon structural trends · weekly medians">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-4">
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                <th className="text-left py-2">METRIC</th>
                <th className="text-right py-2">LATEST</th>
                <th className="text-right py-2">SLOPE/DAY</th>
                <th className="text-left py-2 pl-4">TREND</th>
              </tr>
            </thead>
            <tbody>
              {trendRows.map((m) => {
                const latest = m.series.length > 0 ? m.series[m.series.length - 1].v : null;
                const slope = m.slope_per_day;
                const slopeColor =
                  slope == null ? "text-muted"
                    : slope < 0 ? "text-[#d68b8b]"
                      : slope > 0 ? "text-[#52b97a]"
                        : "text-zinc-300";
                return (
                  <tr key={m.metric} className="border-t border-border/40">
                    <td className="py-1.5 text-zinc-200">{m.metric}</td>
                    <td className="py-1.5 text-right text-zinc-200">
                      {latest != null ? (Math.abs(latest) < 0.01 ? latest.toExponential(2) : latest.toFixed(2)) : "—"}
                    </td>
                    <td className={`py-1.5 text-right ${slopeColor}`}>
                      {slope != null ? slope.toExponential(2) : "—"}
                    </td>
                    <td className="py-1.5 pl-4">
                      <TrendSparkline series={m.series} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {data.regime_entropy_series.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">regime mix entropy (per week)</div>
              <table className="w-full text-sm font-mono">
                <thead>
                  <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                    <th className="text-left py-2">WEEK</th>
                    <th className="text-right py-2">ENTROPY</th>
                    <th className="text-left py-2">DOMINANT</th>
                  </tr>
                </thead>
                <tbody>
                  {data.regime_entropy_series.slice(-8).reverse().map((e) => (
                    <tr key={e.ts} className="border-t border-border/40">
                      <td className="py-1 text-muted">{new Date(e.ts).toISOString().slice(0, 10)}</td>
                      <td className="py-1 text-right text-zinc-200">{e.entropy.toFixed(2)}</td>
                      <td className="py-1 text-zinc-300">{e.dominant_regime}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

function TrendSparkline({ series }: { series: { ts: number; v: number }[] }) {
  const w = 180; const h = 24;
  if (series.length < 2) return <span className="text-muted text-[10px]">—</span>;
  const xs = series.map((p) => p.ts);
  const ys = series.map((p) => p.v);
  const minT = Math.min(...xs); const maxT = Math.max(...xs);
  const minV = Math.min(...ys); const maxV = Math.max(...ys);
  const spanV = Math.max(1e-9, maxV - minV);
  const spanT = Math.max(1, maxT - minT);
  const xy = series.map((p) => {
    const x = ((p.ts - minT) / spanT) * (w - 4) + 2;
    const y = h - 2 - ((p.v - minV) / spanV) * (h - 4);
    return [x, y] as const;
  });
  const d = `M${xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L")}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width={w} height={h}>
      <path d={d} fill="none" stroke="rgba(227, 208, 45, 0.85)" strokeWidth={1.4} strokeLinejoin="round" />
    </svg>
  );
}
