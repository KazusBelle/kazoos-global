/**
 * Phase-13 Memory dashboard.
 *
 * Long-horizon market memory: chronicle of how things evolved, market
 * cycle phases, intelligence evolution timeline, anomaly genealogy
 * (memory graph), crisis evolution tree, regime ancestry, edge lineage.
 *
 * Read-only over `liquidity_anomaly_edges` + `liquidity_intelligence_
 * history`. The worker keeps both alive; this page just renders.
 */

import { useEffect, useMemo, useState } from "react";
import {
  getAnomalyLineage,
  getCrisisEvolutionTree,
  getEdgeLineage,
  getIntelligenceHistory,
  getMarketCycle,
  getMemoryGraph,
  getNarrativeChronicle,
  getRegimeAncestry,
  type AnomalyLineage,
  type CrisisEvolution,
  type EdgeLineage,
  type IntelligenceHistory,
  type MarketCycle,
  type MemoryGraph,
  type NarrativeChronicle,
  type RegimeAncestry,
} from "../lib/api";

const REFRESH_MS = 60_000;

export function MemoryPage() {
  return (
    <div className="space-y-4">
      <div className="flex items-baseline gap-3">
        <div className="text-accent text-xl font-bold tracking-[0.3em]">MEM</div>
        <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
          long-horizon memory · genealogy · evolution · cycles
        </div>
      </div>

      <ChroniclePanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MarketCyclePanel />
        <CrisisEvolutionPanel />
      </div>
      <IntelligenceHistoryPanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <RegimeAncestryPanel />
        <EdgeLineagePanel />
      </div>
      <MemoryGraphPanel />
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

function fmtTs(ts: number): string {
  return new Date(ts).toISOString().replace("T", " ").slice(0, 16);
}

function fmtDuration(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(0)}s`;
  if (ms < 3600_000) return `${(ms / 60_000).toFixed(0)}m`;
  if (ms < 86_400_000) return `${(ms / 3600_000).toFixed(1)}h`;
  return `${(ms / 86_400_000).toFixed(1)}d`;
}

// ── Chronicle ─────────────────────────────────────────────────────────────

function ChroniclePanel() {
  const [data, setData] = useState<NarrativeChronicle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(21);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await getNarrativeChronicle(days);
        if (!cancelled) setData(d);
      } catch (e: any) {
        if (!cancelled) setError(e?.message ?? "failed");
      }
    };
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [days]);

  return (
    <Panel
      title="Market Chronicle"
      subtitle={data ? `${data.anomaly_count} anomalies recorded` : "multi-week story"}
      toolbar={
        <div className="flex items-center gap-1">
          {[7, 21, 60].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.14em] ${
                days === d ? "border-accent text-accent bg-accent/10" : "border-border text-muted hover:text-zinc-200"
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
        <div className="space-y-2">
          <div className="text-zinc-100">{data.summary}</div>
          <ul className="text-[11px] text-zinc-300 space-y-0.5">
            {data.highlights.map((h, i) => (
              <li key={i}><span className="text-muted">▸</span> {h}</li>
            ))}
          </ul>
          {data.first_state && data.last_state && (
            <div className="text-[10px] uppercase tracking-[0.16em] text-muted pt-2 border-t border-border/40">
              {data.first_state.replace(/_/g, " ")} → {data.last_state.replace(/_/g, " ")}
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

// ── Market cycle ─────────────────────────────────────────────────────────

const CYCLE_PHASE_COLOR: Record<string, string> = {
  STABLE_LIQUIDITY: "rgba(82, 185, 122, 0.95)",
  SPECULATIVE_EXPANSION: "rgba(140, 170, 235, 0.95)",
  INSTABILITY_PROPAGATION: "rgba(227, 180, 87, 0.95)",
  CASCADE_PHASE: "rgba(214, 75, 75, 0.95)",
};

function MarketCyclePanel() {
  const [data, setData] = useState<MarketCycle | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMarketCycle(60)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Market Cycle" subtitle="phase decomposition over 60d">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-3">
          {data.current_phase && (
            <div className="flex items-baseline gap-3">
              <span className="text-[10px] uppercase tracking-[0.2em] text-muted">current phase</span>
              <span className="text-zinc-100 font-bold tracking-[0.18em]" style={{ color: CYCLE_PHASE_COLOR[data.current_phase] ?? "rgba(140, 170, 235, 0.95)" }}>
                {data.current_phase.replace(/_/g, " ")}
              </span>
            </div>
          )}
          {data.runs.length === 0 ? (
            <div className="text-xs text-muted">No history snapshots yet — worker writes one every 5 minutes.</div>
          ) : (
            <div className="space-y-1.5">
              <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-1">recent phase runs</div>
              {data.runs.slice(-8).reverse().map((r, i) => (
                <div key={i} className="flex items-center gap-2 text-[10px] font-mono">
                  <span
                    className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em]"
                    style={{
                      color: CYCLE_PHASE_COLOR[r.phase] ?? "rgba(140, 170, 235, 0.95)",
                      borderColor: (CYCLE_PHASE_COLOR[r.phase] ?? "rgba(140, 170, 235, 0.95)").replace(/0\.95\)$/, "0.5)"),
                      background: (CYCLE_PHASE_COLOR[r.phase] ?? "rgba(140, 170, 235, 0.95)").replace(/0\.95\)$/, "0.10)"),
                    }}
                  >
                    {r.phase.replace(/_/g, " ")}
                  </span>
                  <span className="text-muted">{fmtTs(r.start_ts)} → {fmtTs(r.end_ts)}</span>
                  <span className="text-zinc-300">{fmtDuration(r.duration_ms)}{r.open ? " (open)" : ""}</span>
                </div>
              ))}
            </div>
          )}
          {data.transition_matrix.length > 0 && (
            <div className="pt-2 border-t border-border/40">
              <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-1">top transitions</div>
              <ul className="space-y-0.5 text-[10px] font-mono">
                {data.transition_matrix.slice(0, 5).map((t, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <span className="text-zinc-200">{t.from_phase.replace(/_/g, " ")}</span>
                    <span className="text-muted">→</span>
                    <span className="text-zinc-200">{t.to_phase.replace(/_/g, " ")}</span>
                    <span className="text-muted ml-auto">{(t.probability * 100).toFixed(0)}% · {t.count}×</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

// ── Crisis evolution tree ────────────────────────────────────────────────

function CrisisEvolutionPanel() {
  const [data, setData] = useState<CrisisEvolution | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getCrisisEvolutionTree(30)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Crisis Evolution Tree" subtitle="escalation vs stabilization probabilities per state">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.states.length === 0 && (
        <div className="text-xs text-muted">Not enough snapshots yet. Cycle history takes a few hours to accumulate.</div>
      )}
      {data && data.states.length > 0 && (
        <div className="space-y-3">
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                <th className="text-left py-2">STATE</th>
                <th className="text-right py-2">N</th>
                <th className="text-right py-2">ESCAL</th>
                <th className="text-right py-2">STAB</th>
                <th className="text-right py-2">PERSIST</th>
              </tr>
            </thead>
            <tbody>
              {data.states.map((s) => (
                <tr key={s.state} className="border-t border-border/40">
                  <td className="py-1.5 text-zinc-200">{s.state.replace(/_/g, " ")}</td>
                  <td className="py-1.5 text-right text-muted">{s.total_transitions}</td>
                  <td className="py-1.5 text-right text-[#d68b8b]">{(s.escalation_prob * 100).toFixed(0)}%</td>
                  <td className="py-1.5 text-right text-[#52b97a]">{(s.stabilization_prob * 100).toFixed(0)}%</td>
                  <td className="py-1.5 text-right text-muted">{s.persist_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {data.tree.length > 0 && (
            <div>
              <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-1">top branches</div>
              <ul className="space-y-0.5 text-[10px] font-mono">
                {data.tree.slice(0, 8).map((t, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <span className="text-zinc-200">{t.from_state.replace(/_/g, " ")}</span>
                    <span className="text-muted">→</span>
                    <span className="text-zinc-200">{t.to_state.replace(/_/g, " ")}</span>
                    <span className="text-muted ml-auto">{t.count}×</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

// ── Intelligence history timeline ────────────────────────────────────────

function IntelligenceHistoryPanel() {
  const [data, setData] = useState<IntelligenceHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(14);

  useEffect(() => {
    let cancelled = false;
    const since = Date.now() - days * 24 * 3600_000;
    getIntelligenceHistory(since, 1000)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [days]);

  return (
    <Panel
      title="Intelligence Evolution"
      subtitle="how the engine's own state moved over time"
      toolbar={
        <div className="flex items-center gap-1">
          {[3, 7, 14, 30].map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.14em] ${
                days === d ? "border-accent text-accent bg-accent/10" : "border-border text-muted hover:text-zinc-200"
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
      {data && data.series.length === 0 && (
        <div className="text-xs text-muted">No snapshots in window — worker writes every 5 minutes.</div>
      )}
      {data && data.series.length > 0 && <IntelEvolutionChart series={data.series} />}
    </Panel>
  );
}

function IntelEvolutionChart({ series }: { series: IntelligenceHistory["series"] }) {
  const width = 880;
  const height = 220;
  const padX = 56;
  const padY = 18;
  if (series.length < 2) return <div className="text-xs text-muted">insufficient series</div>;
  const xs = series.map((s) => s.ts_ms);
  const minT = Math.min(...xs);
  const maxT = Math.max(...xs);
  const spanT = Math.max(1, maxT - minT);
  const sx = (t: number) => padX + ((t - minT) / spanT) * (width - 2 * padX);
  const sy = (v: number) => height - padY - (v / 100) * (height - 2 * padY);
  const trace = (key: keyof IntelligenceHistory["series"][number], color: string) => {
    const pts = series
      .map((s) => ({ t: s.ts_ms, v: s[key] as number | null }))
      .filter((p): p is { t: number; v: number } => p.v != null && Number.isFinite(p.v));
    if (pts.length < 2) return null;
    const d = `M${pts.map((p) => `${sx(p.t).toFixed(1)},${sy(p.v).toFixed(1)}`).join(" L")}`;
    return <path d={d} fill="none" stroke={color} strokeWidth={1.4} strokeLinejoin="round" />;
  };
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto block">
      {[0, 25, 50, 75, 100].map((g) => (
        <g key={g}>
          <line x1={padX} x2={width - padX} y1={sy(g)} y2={sy(g)} stroke="#1a1c22" strokeWidth={1} />
          <text x={padX - 6} y={sy(g) + 3} textAnchor="end" fontSize="9" fill="#5a5f6b">{g}</text>
        </g>
      ))}
      {trace("synthesized_stress", "rgba(214, 105, 105, 0.9)")}
      {trace("structural_break_score", "rgba(227, 180, 87, 0.9)")}
      {trace("meta_intelligence_health", "rgba(82, 185, 122, 0.9)")}
      {trace("cross_layer_agreement", "rgba(140, 170, 235, 0.9)")}
      {trace("risk_state_score", "rgba(214, 139, 105, 0.9)")}
      <g fontSize="9" fill="#5a5f6b">
        <rect x={padX} y={4} width={10} height={2} fill="rgba(214, 105, 105, 0.9)" />
        <text x={padX + 14} y={9}>stress</text>
        <rect x={padX + 70} y={4} width={10} height={2} fill="rgba(227, 180, 87, 0.9)" />
        <text x={padX + 84} y={9}>break</text>
        <rect x={padX + 140} y={4} width={10} height={2} fill="rgba(82, 185, 122, 0.9)" />
        <text x={padX + 154} y={9}>health</text>
        <rect x={padX + 210} y={4} width={10} height={2} fill="rgba(140, 170, 235, 0.9)" />
        <text x={padX + 224} y={9}>agreement</text>
        <rect x={padX + 300} y={4} width={10} height={2} fill="rgba(214, 139, 105, 0.9)" />
        <text x={padX + 314} y={9}>risk</text>
      </g>
    </svg>
  );
}

// ── Regime ancestry ──────────────────────────────────────────────────────

function RegimeAncestryPanel() {
  const [data, setData] = useState<RegimeAncestry | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getRegimeAncestry(30)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Regime Ancestry" subtitle="parent → child regime lineage from per-symbol alert history">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.nodes.length === 0 && <div className="text-xs text-muted">No regime data.</div>}
      {data && data.nodes.length > 0 && (
        <div className="space-y-3">
          {data.dominant_lineage.length > 1 && (
            <div className="text-[11px] font-mono">
              <span className="text-[10px] uppercase tracking-[0.18em] text-muted">dominant lineage: </span>
              {data.dominant_lineage.map((s, i) => (
                <span key={i}>
                  <span className="text-zinc-200">{s.replace(/_/g, " ")}</span>
                  {i < data.dominant_lineage.length - 1 && <span className="text-muted"> → </span>}
                </span>
              ))}
            </div>
          )}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-1">regimes</div>
              <ul className="space-y-0.5 text-[10px] font-mono">
                {data.nodes.slice(0, 8).map((n) => (
                  <li key={n.regime} className="flex items-center justify-between">
                    <span className="text-zinc-200">{n.regime.replace(/_/g, " ")}</span>
                    <span className="text-muted">{n.count}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-1">top edges</div>
              <ul className="space-y-0.5 text-[10px] font-mono">
                {data.edges.slice(0, 8).map((e, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <span className="text-zinc-200">{e.parent_regime.replace(/_/g, " ")}</span>
                    <span className="text-muted">→</span>
                    <span className="text-zinc-200">{e.child_regime.replace(/_/g, " ")}</span>
                    <span className="text-muted ml-auto">{e.weight}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Edge lineage ─────────────────────────────────────────────────────────

const EDGE_KINDS = [
  "SPREAD_EXPLOSION", "DEPTH_COLLAPSE", "LIQ_CASCADE", "RESILIENCY_FAILURE",
  "OI_SURGE", "FUNDING_EXTREME", "FRAGILITY_SPIKE", "REGIME_TRANSITION",
];

const EDGE_PHASE_COLOR: Record<string, string> = {
  STRENGTHENING: "rgba(82, 185, 122, 0.95)",
  DEGRADATION: "rgba(214, 105, 105, 0.95)",
  INVERTED: "rgba(214, 75, 75, 0.95)",
  STEADY: "rgba(140, 170, 235, 0.95)",
};

function EdgeLineagePanel() {
  const [kind, setKind] = useState("SPREAD_EXPLOSION");
  const [data, setData] = useState<EdgeLineage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getEdgeLineage(kind, { lookback_days: 60, bucket_days: 7 })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [kind]);

  return (
    <Panel
      title="Edge Lineage"
      subtitle="per-kind precision lifecycle"
      toolbar={
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          className="h-7 px-2 text-[10px] uppercase tracking-[0.15em] rounded-md border border-border bg-bg/60 text-zinc-200"
        >
          {EDGE_KINDS.map((k) => (
            <option key={k} value={k}>{k.replace(/_/g, " ")}</option>
          ))}
        </select>
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.series.length === 0 && <div className="text-xs text-muted">No data for this kind.</div>}
      {data && data.series.length > 0 && (
        <div className="space-y-3">
          {data.origin_ts && (
            <div className="text-[10px] uppercase tracking-[0.16em] text-muted">
              origin: {fmtTs(data.origin_ts)}
            </div>
          )}
          <EdgeLineageSparkline series={data.series} />
          {data.phases.length > 0 && (
            <div>
              <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-1">phases</div>
              <ul className="space-y-0.5 text-[10px] font-mono">
                {data.phases.slice(-6).reverse().map((p, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <span
                      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em]"
                      style={{
                        color: EDGE_PHASE_COLOR[p.phase] ?? "rgba(140, 170, 235, 0.95)",
                        borderColor: (EDGE_PHASE_COLOR[p.phase] ?? "rgba(140, 170, 235, 0.95)").replace(/0\.95\)$/, "0.5)"),
                        background: (EDGE_PHASE_COLOR[p.phase] ?? "rgba(140, 170, 235, 0.95)").replace(/0\.95\)$/, "0.10)"),
                      }}
                    >
                      {p.phase}
                    </span>
                    <span className="text-muted">{fmtTs(p.start_ts)} → {fmtTs(p.end_ts)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

function EdgeLineageSparkline({ series }: { series: EdgeLineage["series"] }) {
  const w = 600; const h = 80;
  const pts = series.filter((p) => p.precision != null) as Array<typeof series[number] & { precision: number }>;
  if (pts.length < 2) return <div className="text-[10px] text-muted">insufficient data</div>;
  const xs = pts.map((p) => p.bucket_ts);
  const minT = Math.min(...xs); const maxT = Math.max(...xs);
  const spanT = Math.max(1, maxT - minT);
  const sx = (t: number) => ((t - minT) / spanT) * (w - 8) + 4;
  const sy = (v: number) => h - 4 - v * (h - 8);
  const d = `M${pts.map((p) => `${sx(p.bucket_ts).toFixed(1)},${sy(p.precision).toFixed(1)}`).join(" L")}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h}>
      <line x1={0} x2={w} y1={sy(0.5)} y2={sy(0.5)} stroke="#1a1c22" strokeDasharray="2 4" />
      <path d={d} fill="none" stroke="rgba(227, 208, 45, 0.85)" strokeWidth={1.6} strokeLinejoin="round" />
      <text x={2} y={sy(0.5) - 2} fontSize="9" fill="#5a5f6b">0.5 threshold</text>
    </svg>
  );
}

// ── Memory graph ─────────────────────────────────────────────────────────

const ANOMALY_NODE_COLOR: Record<string, string> = {
  structural_break: "rgba(227, 180, 87, 0.95)",
  regime_collapse: "rgba(214, 75, 75, 0.95)",
  venue_divergence: "rgba(140, 170, 235, 0.95)",
  pre_cascade: "rgba(214, 105, 105, 0.95)",
  edge_inversion: "rgba(214, 139, 105, 0.95)",
  regime_emergence: "rgba(82, 185, 122, 0.95)",
};

const EDGE_KIND_COLOR: Record<string, string> = {
  caused_by: "rgba(214, 105, 105, 0.7)",
  evolved_into: "rgba(227, 180, 87, 0.7)",
  historically_similar: "rgba(140, 170, 235, 0.5)",
  preceded: "rgba(140, 140, 140, 0.6)",
  destabilized: "rgba(214, 75, 75, 0.7)",
  stabilized: "rgba(82, 185, 122, 0.7)",
};

function MemoryGraphPanel() {
  const [data, setData] = useState<MemoryGraph | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [lineage, setLineage] = useState<AnomalyLineage | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMemoryGraph(80)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (selected == null) { setLineage(null); return; }
    let cancelled = false;
    getAnomalyLineage(selected, 3)
      .then((d) => { if (!cancelled) setLineage(d); })
      .catch(() => { if (!cancelled) setLineage(null); });
    return () => { cancelled = true; };
  }, [selected]);

  const nodePositions = useMemo(() => {
    if (!data) return new Map<number, { x: number; y: number }>();
    // Simple deterministic layout: time on x, severity/kind on y rows.
    const sorted = [...data.nodes].sort((a, b) => a.occurred_at_ms - b.occurred_at_ms);
    if (sorted.length === 0) return new Map();
    const w = 880; const h = 320;
    const minT = sorted[0].occurred_at_ms;
    const maxT = sorted[sorted.length - 1].occurred_at_ms;
    const spanT = Math.max(1, maxT - minT);
    const kindRows = Array.from(new Set(sorted.map((n) => n.kind)));
    const rowH = h / Math.max(1, kindRows.length);
    const map = new Map<number, { x: number; y: number }>();
    for (const n of sorted) {
      const x = 40 + ((n.occurred_at_ms - minT) / spanT) * (w - 80);
      const y = 20 + kindRows.indexOf(n.kind) * rowH + rowH / 2;
      map.set(n.id, { x, y });
    }
    return map;
  }, [data]);

  return (
    <Panel
      title="Memory Graph"
      subtitle={data ? `${data.nodes.length} anomalies · ${data.edges.length} edges` : "anomaly genealogy"}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.nodes.length === 0 && (
        <div className="text-xs text-muted">Anomaly memory is empty. The worker writes here every 5 min when thresholds trip.</div>
      )}
      {data && data.nodes.length > 0 && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2 text-[10px] uppercase tracking-[0.16em]">
            {Object.entries(data.edge_counts_by_kind).map(([k, c]) => (
              <span key={k} className="text-muted">
                <span style={{ color: EDGE_KIND_COLOR[k] ?? "rgba(140, 170, 235, 0.9)" }}>{k.replace(/_/g, " ")}</span>{" "}<span className="text-zinc-300">{c}</span>
              </span>
            ))}
          </div>
          <div className="overflow-x-auto">
            <svg viewBox="0 0 880 360" width="100%" height="360" className="block">
              {data.edges.map((e, i) => {
                const a = nodePositions.get(e.from_id);
                const b = nodePositions.get(e.to_id);
                if (!a || !b) return null;
                return (
                  <line
                    key={i}
                    x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                    stroke={EDGE_KIND_COLOR[e.kind] ?? "rgba(140, 140, 140, 0.5)"}
                    strokeWidth={Math.min(2.5, 0.6 + e.weight * 1.4)}
                  />
                );
              })}
              {data.nodes.map((n) => {
                const p = nodePositions.get(n.id);
                if (!p) return null;
                const color = ANOMALY_NODE_COLOR[n.kind] ?? "rgba(140, 170, 235, 0.95)";
                const r = 5 + Math.min(6, n.novelty_score / 25);
                return (
                  <g
                    key={n.id}
                    onClick={() => setSelected((cur) => cur === n.id ? null : n.id)}
                    style={{ cursor: "pointer" }}
                  >
                    <title>{`${n.kind.replace(/_/g, " ")} · ${fmtTs(n.occurred_at_ms)} · novelty ${n.novelty_score.toFixed(0)}`}</title>
                    <circle
                      cx={p.x} cy={p.y} r={r}
                      fill={color}
                      stroke={selected === n.id ? "#fff" : "#0d0e11"}
                      strokeWidth={selected === n.id ? 2 : 0.7}
                    />
                  </g>
                );
              })}
            </svg>
          </div>
          {selected != null && lineage?.root && (
            <div className="rounded border border-border/60 bg-bg/40 p-3 text-[11px] font-mono">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-zinc-100 uppercase tracking-[0.14em]">{lineage.root.kind.replace(/_/g, " ")}</span>
                <span className="text-muted">{fmtTs(lineage.root.occurred_at_ms)}</span>
                <span className="text-muted ml-auto">depth {lineage.lineage_depth} · neighborhood {lineage.neighborhood_size}</span>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-1">parents</div>
                  {lineage.parents.length === 0 ? (
                    <div className="text-muted text-[10px]">—</div>
                  ) : lineage.parents.flat().map((n) => (
                    <div key={n.id} className="text-[10px] text-zinc-200">
                      {n.kind.replace(/_/g, " ")} · {fmtTs(n.occurred_at_ms)} · novelty {n.novelty_score.toFixed(0)}
                    </div>
                  ))}
                </div>
                <div>
                  <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-1">descendants</div>
                  {lineage.descendants.length === 0 ? (
                    <div className="text-muted text-[10px]">—</div>
                  ) : lineage.descendants.flat().map((n) => (
                    <div key={n.id} className="text-[10px] text-zinc-200">
                      {n.kind.replace(/_/g, " ")} · {fmtTs(n.occurred_at_ms)} · novelty {n.novelty_score.toFixed(0)}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
