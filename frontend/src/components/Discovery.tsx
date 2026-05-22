/**
 * Phase-14 Discovery dashboard.
 *
 * Autonomous pattern discovery + crisis archetypes + hidden regimes +
 * symbol-level propagation + evolutionary trends + memory abstraction +
 * intelligence forecasting + adaptation recommendations.
 *
 * Read-only over the data the previous phases already accumulate.
 * Recommendations on this page are descriptive — nothing auto-applies.
 */

import { useEffect, useState } from "react";
import {
  getAdaptationRecommendations,
  getCrisisArchetypes,
  getEvolutionaryBehavior,
  getHiddenRegimes,
  getIntelligenceForecast,
  getMemoryAbstraction,
  getPatternDiscovery,
  getPropagation,
  type AdaptationOut,
  type CrisisArchetypes,
  type EvolutionaryBehavior,
  type HiddenRegimes,
  type IntelligenceForecast,
  type MemoryAbstraction,
  type PatternDiscovery,
  type Propagation,
} from "../lib/api";

const REFRESH_MS = 60_000;

export function Discovery() {
  return (
    <div className="space-y-4">
      <div className="flex items-baseline gap-3">
        <div className="text-accent text-xl font-bold tracking-[0.3em]">DISC</div>
        <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
          autonomous discovery · archetypes · hidden regimes · propagation
        </div>
      </div>

      <PatternDiscoveryPanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <CrisisArchetypesPanel />
        <HiddenRegimesPanel />
      </div>
      <PropagationPanel />
      <EvolutionaryPanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MemoryAbstractionPanel />
        <IntelligenceForecastPanel />
      </div>
      <AdaptationPanel />
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

function TertileChip({ v }: { v: "low" | "mid" | "high" | "na" }) {
  const colors: Record<typeof v, string> = {
    low: "rgba(82, 185, 122, 0.95)",
    mid: "rgba(140, 170, 235, 0.95)",
    high: "rgba(214, 105, 105, 0.95)",
    na: "rgba(90, 95, 107, 0.85)",
  };
  const color = colors[v];
  return (
    <span
      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
      style={{
        color,
        borderColor: color.replace(/0\.95\)$/, "0.5)"),
        background: color.replace(/0\.95\)$/, "0.10)"),
      }}
    >
      {v}
    </span>
  );
}

function fmtTs(ts: number): string {
  return new Date(ts).toISOString().replace("T", " ").slice(0, 16);
}

// ── Pattern discovery ───────────────────────────────────────────────────

function PatternDiscoveryPanel() {
  const [data, setData] = useState<PatternDiscovery | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [minSupport, setMinSupport] = useState(8);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getPatternDiscovery({ min_support: minSupport })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [minSupport]);

  return (
    <Panel
      title="Pattern Discovery"
      subtitle={data ? `base rate ${(data.base_rate * 100).toFixed(2)}% · ${data.total_buckets} buckets · ${data.patterns.length} patterns` : "recurring metric combos → downstream alert rates"}
      toolbar={
        <div className="flex items-center gap-1">
          {[5, 8, 15, 30].map((s) => (
            <button
              key={s}
              onClick={() => setMinSupport(s)}
              className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.14em] ${
                minSupport === s ? "border-accent text-accent bg-accent/10" : "border-border text-muted hover:text-zinc-200"
              }`}
            >
              n≥{s}
            </button>
          ))}
        </div>
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.patterns.length === 0 && (
        <div className="text-xs text-muted">No patterns at this support level. Try a lower threshold.</div>
      )}
      {data && data.patterns.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                <th className="text-left py-2">ID</th>
                {data.metrics.map((m) => (
                  <th key={m} className="text-left py-2 px-1">{m.replace(/_score/g, "").replace(/_/g, " ").slice(0, 6)}</th>
                ))}
                <th className="text-right py-2">N</th>
                <th className="text-right py-2">RATE</th>
                <th className="text-right py-2">LIFT</th>
                <th className="text-right py-2">NOV</th>
                <th className="text-left py-2">DOM KIND</th>
              </tr>
            </thead>
            <tbody>
              {data.patterns.map((p) => {
                const liftColor =
                  p.lift == null ? "text-muted"
                    : p.lift >= 2 ? "text-[#52b97a]"
                      : p.lift >= 1.3 ? "text-[#e3b457]"
                        : p.lift < 0.7 ? "text-[#d68b8b]" : "text-zinc-300";
                return (
                  <tr key={p.discovered_pattern_id} className="border-t border-border/40">
                    <td className="py-1 text-muted text-[10px]">{p.discovered_pattern_id}</td>
                    {data.metrics.map((m) => (
                      <td key={m} className="py-1 px-1"><TertileChip v={p.signature[m] as "low" | "mid" | "high" | "na"} /></td>
                    ))}
                    <td className="py-1 text-right text-zinc-200">{p.support}</td>
                    <td className="py-1 text-right text-zinc-200">{(p.outcome_rate * 100).toFixed(0)}%</td>
                    <td className={`py-1 text-right ${liftColor}`}>{p.lift != null ? `${p.lift.toFixed(2)}×` : "—"}</td>
                    <td className="py-1 text-right text-muted">{p.novelty_score.toFixed(0)}</td>
                    <td className="py-1 text-[10px] text-zinc-300">{p.dominant_alert_kind ? `${p.dominant_alert_kind.replace(/_/g, " ")} (${p.dominant_alert_count})` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

// ── Crisis archetypes ───────────────────────────────────────────────────

const ARCHETYPE_COLOR: Record<string, string> = {
  slow_deterioration: "rgba(227, 180, 87, 0.95)",
  liquidity_evaporation: "rgba(214, 139, 105, 0.95)",
  instability_propagation: "rgba(214, 105, 105, 0.95)",
  explosive_cascade: "rgba(214, 75, 75, 0.95)",
  venue_fragmentation: "rgba(140, 170, 235, 0.95)",
  speculative_overheating: "rgba(214, 139, 105, 0.95)",
  recovery_exhaustion: "rgba(140, 170, 235, 0.95)",
  isolated_outlier: "rgba(140, 140, 140, 0.85)",
};

function CrisisArchetypesPanel() {
  const [data, setData] = useState<CrisisArchetypes | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getCrisisArchetypes()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel
      title="Crisis Archetypes"
      subtitle={data ? `${data.archetypes.length} clusters · ${data.anomaly_count} anomalies` : "auto-labelled archetype clusters"}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.archetypes.length === 0 && (
        <div className="text-xs text-muted">No archetypes yet — populate anomaly memory first.</div>
      )}
      {data && data.archetypes.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">ID</th>
              <th className="text-left py-2">LABEL</th>
              <th className="text-right py-2">SIZE</th>
              <th className="text-right py-2">FREQ/D</th>
              <th className="text-right py-2">SEV</th>
              <th className="text-right py-2">RECOV</th>
              <th className="text-left py-2">DOMINANT</th>
            </tr>
          </thead>
          <tbody>
            {data.archetypes.map((a) => {
              const color = ARCHETYPE_COLOR[a.archetype_label] ?? "rgba(140, 170, 235, 0.95)";
              return (
                <tr key={a.archetype_id} className="border-t border-border/40">
                  <td className="py-1.5 text-muted">{a.archetype_id}</td>
                  <td className="py-1.5">
                    <span
                      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
                      style={{
                        color,
                        borderColor: color.replace(/0\.95\)$/, "0.5)"),
                        background: color.replace(/0\.95\)$/, "0.10)"),
                      }}
                    >
                      {a.archetype_label.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="py-1.5 text-right text-zinc-200">{a.size}</td>
                  <td className="py-1.5 text-right text-muted">{a.frequency_per_day.toFixed(2)}</td>
                  <td className="py-1.5 text-right text-muted">{(a.structural_severity * 100).toFixed(0)}</td>
                  <td className="py-1.5 text-right text-muted">{(a.recovery_probability * 100).toFixed(0)}%</td>
                  <td className="py-1.5 text-[10px] text-zinc-300">{a.dominant_kind.replace(/_/g, " ")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

// ── Hidden regimes ──────────────────────────────────────────────────────

function HiddenRegimesPanel() {
  const [data, setData] = useState<HiddenRegimes | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getHiddenRegimes()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel
      title="Hidden Regimes"
      subtitle={data ? `${data.clusters.length} clusters · ${data.snapshot_count} snapshots` : "clusters in engine-state space"}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.clusters.length === 0 && (
        <div className="text-xs text-muted">Not enough intelligence history yet — worker snapshots every 5 min.</div>
      )}
      {data && data.clusters.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">CL</th>
              <th className="text-left py-2">LABEL HINT</th>
              <th className="text-left py-2">DOMINANT STATE</th>
              <th className="text-right py-2">SIZE</th>
              <th className="text-right py-2">EMERGENT</th>
              <th className="text-right py-2">STABILITY</th>
            </tr>
          </thead>
          <tbody>
            {data.clusters.map((c) => {
              const emergentColor = c.is_emergent ? "rgba(227, 180, 87, 0.95)" : "rgba(140, 170, 235, 0.95)";
              return (
                <tr key={c.cluster_id} className="border-t border-border/40">
                  <td className="py-1.5 text-muted">{c.cluster_id}</td>
                  <td className="py-1.5">
                    <span
                      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
                      style={{
                        color: emergentColor,
                        borderColor: emergentColor.replace(/0\.95\)$/, "0.5)"),
                        background: emergentColor.replace(/0\.95\)$/, "0.10)"),
                      }}
                    >
                      {c.label_hint.replace(/_/g, " ")}
                    </span>
                  </td>
                  <td className="py-1.5 text-[10px] text-zinc-300">{c.dominant_coordinated_state?.replace(/_/g, " ") ?? "—"}</td>
                  <td className="py-1.5 text-right text-zinc-200">{c.size}</td>
                  <td className="py-1.5 text-right" style={{ color: emergentColor }}>{c.emergent_regime_score.toFixed(0)}</td>
                  <td className="py-1.5 text-right text-muted">{c.regime_stability.toFixed(0)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

// ── Propagation graph ───────────────────────────────────────────────────

function PropagationPanel() {
  const [data, setData] = useState<Propagation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getPropagation()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel
      title="Structural Propagation"
      subtitle={data ? `${data.total_alerts} alerts · contagion ${data.systemic_contagion_score.toFixed(0)}` : "symbol→symbol lead-lag"}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.edges.length === 0 && (
        <div className="text-xs text-muted">No propagation edges yet — needs alert history with multiple symbols.</div>
      )}
      {data && data.edges.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">top edges</div>
            <table className="w-full text-sm font-mono">
              <thead>
                <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                  <th className="text-left py-2">A → B</th>
                  <th className="text-right py-2">N</th>
                  <th className="text-right py-2">LEAD</th>
                </tr>
              </thead>
              <tbody>
                {data.edges.slice(0, 12).map((e, i) => (
                  <tr key={i} className="border-t border-border/40">
                    <td className="py-1 text-[10px]">
                      <span className="text-zinc-200">{e.from_symbol}</span>
                      <span className="text-muted"> → </span>
                      <span className="text-zinc-200">{e.to_symbol}</span>
                    </td>
                    <td className="py-1 text-right text-zinc-200">{e.count}</td>
                    <td className="py-1 text-right text-muted">{e.avg_lead_s.toFixed(0)}s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">leaders</div>
            <table className="w-full text-sm font-mono">
              <thead>
                <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                  <th className="text-left py-2">SYMBOL</th>
                  <th className="text-right py-2">OUT</th>
                  <th className="text-right py-2">IN</th>
                  <th className="text-right py-2">NET</th>
                </tr>
              </thead>
              <tbody>
                {data.nodes.slice(0, 12).map((n) => (
                  <tr key={n.symbol} className="border-t border-border/40">
                    <td className="py-1 text-zinc-200">{n.symbol}</td>
                    <td className="py-1 text-right text-muted">{n.out_count}</td>
                    <td className="py-1 text-right text-muted">{n.in_count}</td>
                    <td className={`py-1 text-right ${n.net_lead > 0 ? "text-[#d68b8b]" : n.net_lead < 0 ? "text-[#52b97a]" : "text-zinc-300"}`}>
                      {n.net_lead > 0 ? "+" : ""}{n.net_lead}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {data?.average_propagation_velocity_s != null && (
        <div className="text-[10px] uppercase tracking-[0.16em] text-muted pt-3 border-t border-border/40 mt-3">
          avg propagation velocity: <span className="text-zinc-300">{data.average_propagation_velocity_s.toFixed(0)}s</span>
        </div>
      )}
    </Panel>
  );
}

// ── Evolutionary behavior ───────────────────────────────────────────────

const EVOLUTIONARY_STATE_COLOR: Record<string, string> = {
  STABLE_MATURATION: "rgba(82, 185, 122, 0.95)",
  SLOW_MUTATION: "rgba(140, 170, 235, 0.95)",
  INSTABILITY_GROWTH: "rgba(227, 180, 87, 0.95)",
  DETERIORATION: "rgba(214, 75, 75, 0.95)",
};

function EvolutionaryPanel() {
  const [data, setData] = useState<EvolutionaryBehavior | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEvolutionaryBehavior()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Evolutionary Behavior" subtitle="long-horizon structural shift">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted">evolutionary state</div>
            <div className="text-3xl font-bold mt-1" style={{ color: EVOLUTIONARY_STATE_COLOR[data.evolutionary_state] ?? "#5a5f6b" }}>
              {data.evolutionary_state.replace(/_/g, " ")}
            </div>
            <div className="text-[10px] text-muted mt-2 space-y-0.5">
              <div>shift rate <span className="text-zinc-300">{data.behavioral_shift_rate.toFixed(1)}</span></div>
              <div>instability accel <span className="text-zinc-300">{data.instability_acceleration.toFixed(1)}</span></div>
              <div>maturity score <span className="text-zinc-300">{data.structural_maturity_score.toFixed(0)}</span></div>
              <div>bad directions <span className="text-zinc-300">{data.bad_directions}</span></div>
            </div>
          </div>
          <div className="lg:col-span-2">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">metric trend slopes / day</div>
            <ul className="space-y-1 font-mono text-[10px]">
              {data.metric_trends.slice(0, 12).map((t) => {
                const slope = t.slope_per_day;
                const sign = slope == null ? "—" : (slope > 0 ? "+" : "");
                const slopeColor = slope == null ? "text-muted" : slope > 0 ? "text-[#e3b457]" : "text-[#52b97a]";
                return (
                  <li key={t.metric} className="flex items-center justify-between">
                    <span className="text-muted">{t.metric}</span>
                    <span className={slopeColor}>{slope != null ? `${sign}${slope.toExponential(2)}` : "—"}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Memory abstraction ──────────────────────────────────────────────────

function MemoryAbstractionPanel() {
  const [data, setData] = useState<MemoryAbstraction | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMemoryAbstraction()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel
      title="Memory Abstraction"
      subtitle={data ? `density ${data.memory_density_score.toFixed(0)}% · ${data.total_anomalies} anomalies` : "compressed archetype view"}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.abstractions.length === 0 && (
        <div className="text-xs text-muted">Memory is empty.</div>
      )}
      {data && data.abstractions.length > 0 && (
        <div className="space-y-1.5">
          {data.abstractions.map((a) => {
            const color = ARCHETYPE_COLOR[a.label] ?? "rgba(140, 170, 235, 0.95)";
            return (
              <div key={a.archetype_id} className="flex items-center gap-2 text-[10px] font-mono">
                <span className="text-muted w-12">{a.archetype_id}</span>
                <span
                  className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
                  style={{
                    color, borderColor: color.replace(/0\.95\)$/, "0.5)"),
                    background: color.replace(/0\.95\)$/, "0.10)"),
                  }}
                >
                  {a.label.replace(/_/g, " ")}
                </span>
                <div className="flex-1 h-1.5 rounded bg-bg/60 overflow-hidden">
                  <div className="h-full" style={{ width: `${(a.share_of_memory * 100).toFixed(0)}%`, background: color }} />
                </div>
                <span className="text-zinc-300 w-12 text-right">{(a.share_of_memory * 100).toFixed(0)}%</span>
                <span className="text-muted w-12 text-right">n={a.members}</span>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

// ── Intelligence forecast ───────────────────────────────────────────────

const TRAJECTORY_COLOR: Record<string, string> = {
  ESCALATING: "rgba(214, 75, 75, 0.95)",
  DEESCALATING: "rgba(82, 185, 122, 0.95)",
  STEADY: "rgba(140, 170, 235, 0.95)",
  DRIFTING: "rgba(227, 180, 87, 0.95)",
  UNKNOWN: "rgba(140, 140, 140, 0.85)",
};

function IntelligenceForecastPanel() {
  const [data, setData] = useState<IntelligenceForecast | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(7);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getIntelligenceForecast(days)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [days]);

  return (
    <Panel
      title="Intelligence Forecast"
      subtitle="OLS extrapolation of engine state (not price)"
      toolbar={
        <div className="flex items-center gap-1">
          {[3, 7, 14].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.14em] ${
                days === d ? "border-accent text-accent bg-accent/10" : "border-border text-muted hover:text-zinc-200"
              }`}
            >
              +{d}d
            </button>
          ))}
        </div>
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.forecasts.length === 0 && (
        <div className="text-xs text-muted">Need ≥5 intelligence snapshots — accumulating.</div>
      )}
      {data && data.forecasts.length > 0 && (
        <div className="space-y-3">
          {data.trajectory && (
            <div className="flex items-baseline gap-3">
              <span className="text-[10px] uppercase tracking-[0.2em] text-muted">trajectory</span>
              <span className="text-2xl font-bold" style={{ color: TRAJECTORY_COLOR[data.trajectory] ?? "#5a5f6b" }}>
                {data.trajectory}
              </span>
              <span className="text-[10px] text-muted">{data.snapshot_count} snapshots</span>
            </div>
          )}
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                <th className="text-left py-2">METRIC</th>
                <th className="text-right py-2">NOW</th>
                <th className="text-right py-2">+{data.horizon_days}D</th>
                <th className="text-right py-2">SLOPE/D</th>
                <th className="text-right py-2">CONF</th>
              </tr>
            </thead>
            <tbody>
              {data.forecasts.map((f) => (
                <tr key={f.metric} className="border-t border-border/40">
                  <td className="py-1.5 text-zinc-200">{f.metric.replace(/_/g, " ")}</td>
                  <td className="py-1.5 text-right text-muted">{f.current.toFixed(1)}</td>
                  <td className="py-1.5 text-right text-zinc-200 font-semibold">{f.forecast_value.toFixed(1)}</td>
                  <td className={`py-1.5 text-right ${f.slope_per_day > 0.1 ? "text-[#e3b457]" : f.slope_per_day < -0.1 ? "text-[#52b97a]" : "text-muted"}`}>
                    {f.slope_per_day > 0 ? "+" : ""}{f.slope_per_day.toFixed(2)}
                  </td>
                  <td className="py-1.5 text-right text-muted">{f.confidence.toFixed(0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

// ── Adaptation recommendations ──────────────────────────────────────────

const ACTION_COLOR: Record<string, string> = {
  STRENGTHEN: "rgba(82, 185, 122, 0.95)",
  STRENGTHEN_EDGE: "rgba(82, 185, 122, 0.95)",
  WEAKEN: "rgba(214, 105, 105, 0.95)",
  TIGHTEN_THRESHOLD: "rgba(227, 180, 87, 0.95)",
  LOOSEN_THRESHOLD: "rgba(140, 170, 235, 0.95)",
  REWEIGHT_UNSTABLE: "rgba(214, 139, 105, 0.95)",
};

function AdaptationPanel() {
  const [data, setData] = useState<AdaptationOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await getAdaptationRecommendations();
        if (!cancelled) setData(d);
      } catch (e: any) {
        if (!cancelled) setError(e?.message ?? "failed");
      }
    };
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  return (
    <Panel
      title="Adaptation Recommendations"
      subtitle={data ? `adaptation score ${data.adaptation_score.toFixed(0)} · ${data.recommendations.length} suggestions` : "what to up-weight / tighten — read-only"}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.recommendations.length === 0 && (
        <div className="text-xs text-muted">Engine has no actionable suggestions right now — calibration is balanced.</div>
      )}
      {data && data.recommendations.length > 0 && (
        <ul className="space-y-2">
          {data.recommendations.map((r, i) => {
            const color = ACTION_COLOR[r.action] ?? "rgba(140, 170, 235, 0.95)";
            return (
              <li key={i} className="rounded border border-border/60 bg-bg/40 px-3 py-2">
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  <span
                    className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em]"
                    style={{
                      color,
                      borderColor: color.replace(/0\.95\)$/, "0.5)"),
                      background: color.replace(/0\.95\)$/, "0.10)"),
                    }}
                  >
                    {r.action.replace(/_/g, " ")}
                  </span>
                  <span className="font-mono text-zinc-200">{r.target.replace(/_/g, " ")}</span>
                  <span className="text-[9px] text-muted ml-auto">
                    Δ {r.importance_shift > 0 ? "+" : ""}{r.importance_shift.toFixed(2)}
                  </span>
                </div>
                <div className="text-[11px] text-zinc-300">{r.rationale}</div>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
