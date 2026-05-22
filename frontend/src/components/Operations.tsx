/**
 * Phase-9 Operations dashboard.
 *
 * This is the "what's working now / what's broken now" view. Research
 * (Phase 7/8) is the exploratory surface; Operations is the at-a-glance
 * decision-support surface — narrative, systemic stress, signal
 * reliability, edge persistence, transition forecasts.
 *
 * Important: this page is descriptive only. There are NO trading
 * recommendations here — explicitly out of scope per the spec.
 */

import { useEffect, useState } from "react";
import {
  getEdgePersistence,
  getMarketNarrative,
  getRiskState,
  getSignalReliability,
  getTransitionForecast,
  type EdgePersistenceOut,
  type MarketNarrative,
  type ReliabilityKind,
  type ReliabilityOut,
  type RiskState,
  type TransitionForecastOut,
} from "../lib/api";

const NARRATIVE_REFRESH_MS = 30_000;

export function Operations() {
  return (
    <div className="space-y-4">
      <div className="flex items-baseline gap-3">
        <div className="text-accent text-xl font-bold tracking-[0.3em]">OPS</div>
        <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
          operational intelligence · descriptive only
        </div>
      </div>

      <NarrativeCard />
      <RiskStatePanel />
      <SignalReliabilityPanel />
      <EdgePersistencePanel />
      <TransitionForecastPanel />
    </div>
  );
}

// ── Shared panel shell (mirrors Research style) ───────────────────────────

function Panel({
  title,
  subtitle,
  toolbar,
  children,
}: {
  title: string;
  subtitle?: string;
  toolbar?: React.ReactNode;
  children: React.ReactNode;
}) {
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

// ── Market Narrative ─────────────────────────────────────────────────────

function NarrativeCard() {
  const [data, setData] = useState<MarketNarrative | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await getMarketNarrative();
        if (!cancelled) setData(d);
      } catch (e: any) {
        if (!cancelled) setError(e?.message ?? "failed");
      }
    };
    load();
    const id = window.setInterval(load, NARRATIVE_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const levelColor =
    data?.level === "SEVERE" ? "rgba(214, 75, 75, 0.95)"
      : data?.level === "ELEVATED" ? "rgba(214, 105, 105, 0.95)"
        : data?.level === "WATCH" ? "rgba(227, 180, 87, 0.95)"
          : "rgba(82, 185, 122, 0.95)";

  return (
    <section
      className="rounded-2xl border p-4 space-y-2"
      style={{
        borderColor: levelColor.replace(/0\.95\)$/, "0.45)"),
        background: levelColor.replace(/0\.95\)$/, "0.06)"),
      }}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <>
          <div className="flex items-baseline gap-3 flex-wrap">
            <span className="text-[10px] uppercase tracking-[0.2em] text-muted">market narrative</span>
            <span
              className="text-[10px] uppercase tracking-[0.18em] rounded border px-2 py-0.5"
              style={{
                color: levelColor,
                borderColor: levelColor.replace(/0\.95\)$/, "0.5)"),
                background: levelColor.replace(/0\.95\)$/, "0.08)"),
              }}
            >
              {data.level} · {data.score.toFixed(0)}
            </span>
          </div>
          <div className="text-zinc-100 text-base">{data.headline}</div>
          <ul className="text-zinc-300 text-sm space-y-0.5">
            {data.bullets.map((b, i) => (
              <li key={i}>
                <span className="text-muted">▸</span> {b}
              </li>
            ))}
          </ul>
          {data.alert_summary && (
            <div className="text-[11px] text-muted">{data.alert_summary}</div>
          )}
          {data.regime_summary && (
            <div className="text-[11px] text-muted">{data.regime_summary}</div>
          )}
          {data.historical_context && (
            <div className="text-[11px] text-zinc-400 italic border-l-2 pl-3 mt-2" style={{ borderColor: levelColor }}>
              {data.historical_context}
            </div>
          )}
        </>
      )}
    </section>
  );
}

// ── Risk-State Panel ─────────────────────────────────────────────────────

function RiskStatePanel() {
  const [data, setData] = useState<RiskState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await getRiskState();
        if (!cancelled) setData(d);
      } catch (e: any) {
        if (!cancelled) setError(e?.message ?? "failed");
      }
    };
    load();
    const id = window.setInterval(load, NARRATIVE_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  return (
    <Panel title="Systemic Stress" subtitle="composite of cohort risk drivers">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.n_symbols === 0 && <div className="text-xs text-muted">No recent samples in the universe.</div>}
      {data && data.n_symbols > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Headline gauge */}
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted">overall</div>
            <div
              className="text-5xl font-bold tracking-tight mt-1"
              style={{ color: stressColor(data.systemic_stress_level) }}
            >
              {data.risk_state_score.toFixed(0)}
            </div>
            <div
              className="text-[11px] uppercase tracking-[0.2em]"
              style={{ color: stressColor(data.systemic_stress_level) }}
            >
              {data.systemic_stress_level}
            </div>
            <div className="text-[10px] text-muted mt-1">{data.n_symbols} symbols in cohort</div>
          </div>

          {/* Driver bars */}
          <div className="lg:col-span-1">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">drivers</div>
            <div className="space-y-1.5">
              {Object.entries(data.drivers)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 8)
                .map(([k, v]) => (
                  <div key={k} className="flex items-center gap-2 text-[10px] font-mono">
                    <span className="text-muted w-32 truncate">{k}</span>
                    <div className="flex-1 h-1.5 rounded bg-bg/60 overflow-hidden">
                      <div
                        className="h-full"
                        style={{
                          width: `${Math.max(0, Math.min(100, v * 100))}%`,
                          background: v >= 0.6 ? "rgba(214, 75, 75, 0.9)" : v >= 0.4 ? "rgba(227, 180, 87, 0.9)" : "rgba(82, 185, 122, 0.85)",
                        }}
                      />
                    </div>
                    <span className="text-zinc-300 w-10 text-right">{(v * 100).toFixed(0)}</span>
                  </div>
                ))}
            </div>
          </div>

          {/* Instability rank */}
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">instability rank</div>
            <ul className="space-y-1 font-mono text-[10px] max-h-[200px] overflow-auto pr-1">
              {data.instability_rank.map((r, i) => (
                <li key={r.symbol} className="flex items-center gap-2 border-t border-border/40 py-1">
                  <span className="text-muted w-5 text-right">{i + 1}</span>
                  <span className="text-zinc-200 flex-1">{r.symbol}</span>
                  <span style={{ color: r.stress > 0.6 ? "rgba(214, 75, 75, 0.95)" : r.stress > 0.4 ? "rgba(227, 180, 87, 0.95)" : "rgba(82, 185, 122, 0.95)" }}>
                    {(r.stress * 100).toFixed(0)}
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

function stressColor(level: RiskState["systemic_stress_level"]): string {
  switch (level) {
    case "SEVERE": return "rgba(214, 75, 75, 0.95)";
    case "ELEVATED": return "rgba(214, 105, 105, 0.95)";
    case "WATCH": return "rgba(227, 180, 87, 0.95)";
    default: return "rgba(82, 185, 122, 0.95)";
  }
}

// ── Signal Reliability ───────────────────────────────────────────────────

const RELIABILITY_STATE_COLOR: Record<string, string> = {
  STRONG: "rgba(82, 185, 122, 0.95)",
  STABLE: "rgba(140, 170, 235, 0.95)",
  WEAK: "rgba(227, 180, 87, 0.95)",
  DEGRADED: "rgba(214, 75, 75, 0.95)",
};

function SignalReliabilityPanel() {
  const [data, setData] = useState<ReliabilityOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSignalReliability()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Signal Reliability" subtitle="composite score · STRONG / STABLE / WEAK / DEGRADED">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.kinds.length === 0 && (
        <div className="text-xs text-muted">No resolved alerts in the last 30 days — fire some alerts to populate.</div>
      )}
      {data && data.kinds.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">RANK</th>
              <th className="text-left py-2">KIND</th>
              <th className="text-right py-2">SCORE</th>
              <th className="text-left py-2">STATE</th>
              <th className="text-right py-2">ACCURACY</th>
              <th className="text-right py-2">STABILITY</th>
              <th className="text-right py-2">REGIME-CONS</th>
              <th className="text-right py-2">SIZE</th>
              <th className="text-right py-2">N</th>
            </tr>
          </thead>
          <tbody>
            {data.kinds.map((k) => (
              <ReliabilityRow key={k.kind} k={k} />
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

function ReliabilityRow({ k }: { k: ReliabilityKind }) {
  const stateColor = RELIABILITY_STATE_COLOR[k.state] ?? "rgba(140, 170, 235, 0.95)";
  return (
    <tr className="border-t border-border/40">
      <td className="py-1.5 text-muted">{k.rank}</td>
      <td className="py-1.5 text-zinc-200">{k.kind.replace(/_/g, " ")}</td>
      <td className="py-1.5 text-right text-zinc-200 font-semibold">{k.reliability_score.toFixed(0)}</td>
      <td className="py-1.5">
        <span
          className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em]"
          style={{
            color: stateColor,
            borderColor: stateColor.replace(/0\.95\)$/, "0.5)"),
            background: stateColor.replace(/0\.95\)$/, "0.10)"),
          }}
        >
          {k.state}
        </span>
      </td>
      <td className="py-1.5 text-right text-muted">{(k.accuracy * 100).toFixed(0)}%</td>
      <td className="py-1.5 text-right text-muted">{k.components.stability.toFixed(0)}</td>
      <td className="py-1.5 text-right text-muted">{k.components.regime_consistency.toFixed(0)}</td>
      <td className="py-1.5 text-right text-muted">{k.components.sample_size.toFixed(0)}</td>
      <td className="py-1.5 text-right text-muted">{k.resolved}</td>
    </tr>
  );
}

// ── Edge Persistence ─────────────────────────────────────────────────────

function EdgePersistencePanel() {
  const [data, setData] = useState<EdgePersistenceOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [windowDays, setWindowDays] = useState(7);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getEdgePersistence({ window_days: windowDays })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [windowDays]);

  return (
    <Panel
      title="Edge Persistence"
      subtitle="rolling precision · degradation slope · half-life"
      toolbar={
        <div className="flex items-center gap-1">
          {[1, 3, 7, 14].map((d) => (
            <button
              key={d}
              onClick={() => setWindowDays(d)}
              className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.14em] ${
                windowDays === d
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border text-muted hover:text-zinc-200"
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
      {data && data.kinds.length === 0 && <div className="text-xs text-muted">No resolved alerts yet.</div>}
      {data && data.kinds.length > 0 && (
        <div className="space-y-4">
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                <th className="text-left py-2">KIND</th>
                <th className="text-right py-2">LATEST PREC</th>
                <th className="text-right py-2">SLOPE/DAY</th>
                <th className="text-right py-2">HALF-LIFE</th>
                <th className="text-left py-2 pl-4">SERIES</th>
              </tr>
            </thead>
            <tbody>
              {data.kinds.map((k) => (
                <EdgePersistenceRow key={k.kind} k={k} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function EdgePersistenceRow({ k }: { k: EdgePersistenceOut["kinds"][number] }) {
  const slope = k.slope_per_day;
  const slopeColor =
    slope == null
      ? "text-muted"
      : slope < -0.01
        ? "text-[#d68b8b]"
        : slope > 0.01
          ? "text-[#52b97a]"
          : "text-zinc-300";
  return (
    <tr className="border-t border-border/40">
      <td className="py-1.5 text-zinc-200">{k.kind.replace(/_/g, " ")}</td>
      <td className="py-1.5 text-right text-zinc-200">
        {k.latest_precision != null ? `${(k.latest_precision * 100).toFixed(0)}%` : "—"}
      </td>
      <td className={`py-1.5 text-right ${slopeColor}`}>
        {slope != null ? `${slope > 0 ? "+" : ""}${(slope * 100).toFixed(2)}pp` : "—"}
      </td>
      <td className="py-1.5 text-right text-muted">
        {k.half_life_days != null ? `${k.half_life_days.toFixed(1)}d` : "—"}
      </td>
      <td className="py-1.5 pl-4">
        <PrecisionSparkline series={k.series} />
      </td>
    </tr>
  );
}

function PrecisionSparkline({ series }: { series: EdgePersistenceOut["kinds"][number]["series"] }) {
  const w = 180;
  const h = 24;
  const pts = series.filter((s) => s.precision != null) as Array<typeof series[number] & { precision: number }>;
  if (pts.length < 2) return <span className="text-muted text-[10px]">insufficient data</span>;
  const minP = Math.min(...pts.map((p) => p.precision));
  const maxP = Math.max(...pts.map((p) => p.precision));
  const span = Math.max(0.01, maxP - minP);
  const minT = pts[0].bucket_ts;
  const maxT = pts[pts.length - 1].bucket_ts;
  const spanT = Math.max(1, maxT - minT);
  const xy = pts.map((p) => {
    const x = ((p.bucket_ts - minT) / spanT) * (w - 4) + 2;
    const y = h - 2 - ((p.precision - minP) / span) * (h - 4);
    return [x, y] as const;
  });
  const d = `M${xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L")}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width={w} height={h}>
      <path d={d} fill="none" stroke="rgba(227, 208, 45, 0.85)" strokeWidth={1.4} strokeLinejoin="round" />
      <circle cx={xy[xy.length - 1][0]} cy={xy[xy.length - 1][1]} r={2} fill="rgba(227, 208, 45, 0.95)" />
    </svg>
  );
}

// ── Transition Forecast ──────────────────────────────────────────────────

function TransitionForecastPanel() {
  const [data, setData] = useState<TransitionForecastOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getTransitionForecast()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Transition Forecast" subtitle="next-state probabilities · expected latency · collapse vs stabilization">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.regimes.length === 0 && <div className="text-xs text-muted">No regime samples in the last 30 days.</div>}
      {data && data.regimes.length > 0 && (
        <div className="space-y-3">
          {data.regimes.map((r) => {
            const collapseColor =
              r.collapse_prob >= 0.4 ? "rgba(214, 75, 75, 0.95)"
                : r.collapse_prob >= 0.2 ? "rgba(227, 180, 87, 0.95)"
                  : "rgba(82, 185, 122, 0.95)";
            const stabColor =
              r.stabilization_prob >= 0.4 ? "rgba(82, 185, 122, 0.95)"
                : r.stabilization_prob >= 0.2 ? "rgba(140, 170, 235, 0.95)"
                  : "rgba(214, 105, 105, 0.95)";
            const topNext = Object.entries(r.next_probs)
              .sort((a, b) => b[1] - a[1])
              .slice(0, 4);
            return (
              <div key={r.regime} className="border-t border-border/40 pt-3">
                <div className="flex items-baseline justify-between mb-2 flex-wrap gap-2">
                  <div className="flex items-baseline gap-2">
                    <span className="text-zinc-100 font-semibold">{r.regime}</span>
                    <span className="text-[10px] uppercase tracking-[0.18em] text-muted">
                      {r.count} samples · {r.out_transitions} transitions
                    </span>
                  </div>
                  <div className="flex items-center gap-3 text-[10px] uppercase tracking-[0.18em]">
                    <span style={{ color: stabColor }}>STAB {(r.stabilization_prob * 100).toFixed(0)}%</span>
                    <span style={{ color: collapseColor }}>COLLAPSE {(r.collapse_prob * 100).toFixed(0)}%</span>
                    <span className="text-muted">VOL+ {(r.volatility_expansion_prob * 100).toFixed(0)}%</span>
                    <span className="text-muted">
                      lat {r.expected_latency_ms != null ? fmtDuration(r.expected_latency_ms) : "—"}
                    </span>
                  </div>
                </div>
                <div className="flex flex-wrap gap-2 text-[10px] font-mono">
                  {topNext.map(([target, p]) => (
                    <div key={target} className="flex items-center gap-1.5">
                      <span className="text-muted">{target.replace(/_/g, " ")}</span>
                      <div className="w-32 h-1.5 rounded bg-bg/60 overflow-hidden">
                        <div
                          className="h-full"
                          style={{
                            width: `${(p * 100).toFixed(0)}%`,
                            background: "rgba(227, 208, 45, 0.7)",
                          }}
                        />
                      </div>
                      <span className="text-zinc-300 w-9 text-right">{(p * 100).toFixed(0)}%</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

function fmtDuration(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(0)}s`;
  if (ms < 3600_000) return `${(ms / 60_000).toFixed(0)}m`;
  if (ms < 86_400_000) return `${(ms / 3600_000).toFixed(1)}h`;
  return `${(ms / 86_400_000).toFixed(1)}d`;
}
