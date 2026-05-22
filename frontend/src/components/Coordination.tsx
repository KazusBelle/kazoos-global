/**
 * Phase-12 Coordination dashboard.
 *
 * Synthesis of OPS / STRAT / META into one coordinated market view +
 * cross-layer conflict resolution, alert suppression diagnostics,
 * crisis clusters, narrative evolution, multi-horizon alignment.
 *
 * The page is "the engine's overall opinion at this moment" — the
 * single most condensed surface in the app. Descriptive only.
 */

import { useEffect, useState } from "react";
import {
  getAlertSuppression,
  getConflicts,
  getCrisisClusters,
  getMultiHorizon,
  getNarrativeEvolution,
  getSynthesis,
  triggerAutoAnomalyScan,
  type AutoAnomalyOut,
  type Conflicts,
  type CrisisClusters,
  type MultiHorizon,
  type NarrativeEvolution,
  type Suppression,
  type Synthesis,
} from "../lib/api";

const REFRESH_MS = 30_000;

export function Coordination() {
  return (
    <div className="space-y-4">
      <div className="flex items-baseline gap-3">
        <div className="text-accent text-xl font-bold tracking-[0.3em]">COORD</div>
        <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
          synthesis · conflicts · multi-horizon · crisis clusters
        </div>
      </div>

      <SynthesisCard />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <ConflictsPanel />
        <MultiHorizonPanel />
      </div>
      <NarrativeEvolutionPanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <SuppressionPanel />
        <AutoAnomalyPanel />
      </div>
      <CrisisClustersPanel />
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

// ── Synthesis headline ───────────────────────────────────────────────────

const COORD_STATE_COLOR: Record<Synthesis["coordinated_state"], string> = {
  STABLE_COORDINATED_MARKET: "rgba(82, 185, 122, 0.95)",
  EARLY_STRUCTURAL_STRESS: "rgba(140, 170, 235, 0.95)",
  STRUCTURAL_MARKET_DETERIORATION: "rgba(214, 139, 105, 0.95)",
  FRAGMENTING_LIQUIDITY_ENVIRONMENT: "rgba(227, 180, 87, 0.95)",
  ESCALATING_SYSTEMIC_INSTABILITY: "rgba(214, 105, 105, 0.95)",
  ACTIVE_CASCADE_PROPAGATION: "rgba(214, 75, 75, 0.95)",
};

function SynthesisCard() {
  const [data, setData] = useState<Synthesis | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await getSynthesis();
        if (!cancelled) setData(d);
      } catch (e: any) {
        if (!cancelled) setError(e?.message ?? "failed");
      }
    };
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  const color = data ? COORD_STATE_COLOR[data.coordinated_state] : "rgba(140, 170, 235, 0.95)";

  return (
    <section
      className="rounded-2xl border p-4 space-y-3"
      style={{ borderColor: color.replace(/0\.95\)$/, "0.45)"), background: color.replace(/0\.95\)$/, "0.06)") }}
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <>
          <div className="flex items-baseline gap-3 flex-wrap">
            <span className="text-[10px] uppercase tracking-[0.2em] text-muted">coordinated state</span>
            <span className="text-zinc-100 text-xl font-bold tracking-[0.16em]" style={{ color }}>
              {data.coordinated_state.replace(/_/g, " ")}
            </span>
            <span className="text-[10px] uppercase tracking-[0.18em] text-muted">
              synthesized stress <span className="text-zinc-200">{data.synthesized_stress.toFixed(0)}</span>
              {" · "}cross-layer agreement <span className="text-zinc-200">{data.cross_layer_agreement.toFixed(0)}%</span>
            </span>
          </div>

          <div className="space-y-1.5">
            {data.layers.map((l) => (
              <div key={l.name} className="flex items-center gap-2 text-[10px] font-mono">
                <span className="text-muted w-44 truncate">{l.name.replace(/_/g, " ")}</span>
                <div className="flex-1 h-1.5 rounded bg-bg/60 overflow-hidden">
                  <div
                    className="h-full"
                    style={{
                      width: `${Math.max(0, Math.min(100, l.score))}%`,
                      background: l.score >= 65 ? "rgba(214, 75, 75, 0.9)" : l.score >= 45 ? "rgba(227, 180, 87, 0.9)" : "rgba(82, 185, 122, 0.9)",
                    }}
                  />
                </div>
                <span className="text-zinc-300 w-10 text-right">{l.score.toFixed(0)}</span>
                <span
                  className="text-[9px] w-12 text-right"
                  style={{ color: l.delta_from_mean > 15 ? "rgba(214, 105, 105, 0.95)" : l.delta_from_mean < -15 ? "rgba(140, 170, 235, 0.95)" : "rgba(140, 140, 140, 0.7)" }}
                >
                  {l.delta_from_mean > 0 ? "+" : ""}{l.delta_from_mean.toFixed(0)}
                </span>
              </div>
            ))}
          </div>

          <div className="grid grid-cols-2 md:grid-cols-3 gap-2 text-[10px] font-mono pt-2 border-t border-border/40">
            <div className="flex justify-between"><span className="text-muted">stress</span><span className="text-zinc-200">{data.components.stress_level}</span></div>
            <div className="flex justify-between"><span className="text-muted">shift warning</span><span className="text-zinc-200">{data.components.shift_warning}</span></div>
            <div className="flex justify-between"><span className="text-muted">structural break</span><span className="text-zinc-200">{data.components.structural_break_score.toFixed(0)}</span></div>
            <div className="flex justify-between"><span className="text-muted">meta confidence</span><span className="text-zinc-200">{data.components.meta_confidence_state}</span></div>
            <div className="flex justify-between"><span className="text-muted">intel health</span><span className="text-zinc-200">{data.components.intelligence_health_state}</span></div>
            <div className="flex justify-between"><span className="text-muted">strategic state</span><span className="text-zinc-200">{data.components.strategic_state.replace(/_/g, " ")}</span></div>
          </div>
        </>
      )}
    </section>
  );
}

// ── Conflict resolution ─────────────────────────────────────────────────

function ConflictsPanel() {
  const [data, setData] = useState<Conflicts | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getConflicts()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Cross-Layer Conflicts" subtitle="dominant layer · suppressed layers · disagreements">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-3">
          <div className="flex items-baseline gap-3 flex-wrap">
            <span className="text-3xl font-bold" style={{ color: data.conflict_score >= 50 ? "rgba(214, 105, 105, 0.95)" : data.conflict_score >= 25 ? "rgba(227, 180, 87, 0.95)" : "rgba(82, 185, 122, 0.95)" }}>
              {data.conflict_score.toFixed(0)}
            </span>
            <span className="text-[10px] uppercase tracking-[0.2em] text-muted">conflict score</span>
          </div>
          <div className="text-[10px] font-mono space-y-0.5">
            <div className="flex justify-between"><span className="text-muted">dominant layer</span><span className="text-zinc-200">{data.dominant_layer.replace(/_/g, " ")}</span></div>
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-muted">suppressed</span>
              <span className="text-zinc-300 text-right">{data.suppressed_layers.length === 0 ? "—" : data.suppressed_layers.map((s) => s.replace(/_/g, " ")).join(", ")}</span>
            </div>
          </div>
          {data.conflicts.length === 0 ? (
            <div className="text-xs text-muted">No active conflicts.</div>
          ) : (
            <ul className="space-y-2">
              {data.conflicts.map((c, i) => (
                <li key={i} className="rounded border border-border/60 bg-bg/40 px-3 py-2 text-[11px]">
                  <div className="text-zinc-100">{c.description}</div>
                  <div className="text-[9px] uppercase tracking-[0.14em] text-muted mt-1">
                    {c.kind.replace(/_/g, " ")}
                    {c.dominant_horizon ? ` · dominant horizon: ${c.dominant_horizon}` : ""}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Panel>
  );
}

// ── Multi-horizon alignment ─────────────────────────────────────────────

const MULTI_STATE_COLOR: Record<MultiHorizon["structural_alignment_state"], string> = {
  ALIGNED: "rgba(82, 185, 122, 0.95)",
  DIVERGENT: "rgba(227, 180, 87, 0.95)",
  FRAGMENTED: "rgba(214, 105, 105, 0.95)",
  INSUFFICIENT_DATA: "rgba(140, 140, 140, 0.7)",
};

function MultiHorizonPanel() {
  const [data, setData] = useState<MultiHorizon | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMultiHorizon()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Multi-Horizon Alignment" subtitle="short · medium · long instability scores">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-3">
          <div className="flex items-baseline gap-3 flex-wrap">
            <span className="text-[10px] uppercase tracking-[0.2em] text-muted">alignment</span>
            <span className="text-3xl font-bold" style={{ color: MULTI_STATE_COLOR[data.structural_alignment_state] }}>
              {data.horizon_alignment_score != null ? data.horizon_alignment_score.toFixed(0) : "—"}
            </span>
            <span className="text-[10px] uppercase tracking-[0.18em]" style={{ color: MULTI_STATE_COLOR[data.structural_alignment_state] }}>
              {data.structural_alignment_state}
            </span>
            <span className="text-[10px] text-muted">dominant: {data.dominant_horizon ?? "—"}</span>
          </div>
          <div className="space-y-1.5">
            {(["short", "medium", "long"] as const).map((h) => {
              const v = data.scores[h];
              return (
                <div key={h} className="flex items-center gap-2 text-[10px] font-mono">
                  <span className="text-muted w-20 uppercase tracking-[0.14em]">{h}</span>
                  <div className="flex-1 h-1.5 rounded bg-bg/60 overflow-hidden">
                    <div
                      className="h-full"
                      style={{
                        width: `${v != null ? Math.max(0, Math.min(100, v)) : 0}%`,
                        background: v != null && v >= 60 ? "rgba(214, 75, 75, 0.9)" : v != null && v >= 35 ? "rgba(227, 180, 87, 0.9)" : "rgba(82, 185, 122, 0.85)",
                      }}
                    />
                  </div>
                  <span className="text-zinc-300 w-10 text-right">{v != null ? v.toFixed(0) : "—"}</span>
                </div>
              );
            })}
          </div>
          <div className="text-[10px] font-mono pt-2 border-t border-border/40 space-y-0.5">
            <div className="flex justify-between"><span className="text-muted">short vs medium</span><span className="text-zinc-200">{data.horizon_conflict_map.short_vs_medium?.toFixed(0) ?? "—"}</span></div>
            <div className="flex justify-between"><span className="text-muted">short vs long</span><span className="text-zinc-200">{data.horizon_conflict_map.short_vs_long?.toFixed(0) ?? "—"}</span></div>
            <div className="flex justify-between"><span className="text-muted">medium vs long</span><span className="text-zinc-200">{data.horizon_conflict_map.medium_vs_long?.toFixed(0) ?? "—"}</span></div>
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Narrative evolution ─────────────────────────────────────────────────

function NarrativeEvolutionPanel() {
  const [data, setData] = useState<NarrativeEvolution | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getNarrativeEvolution()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Narrative Evolution" subtitle="how the market changed across 1h · 24h · 7d">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">short term (last hour vs prior day)</div>
            {data.short_term_bullets.length === 0 ? (
              <div className="text-xs text-muted">No material short-term moves.</div>
            ) : (
              <ul className="space-y-1 text-[11px]">
                {data.short_term_bullets.map((b, i) => <li key={i}><span className="text-muted">▸</span> {b}</li>)}
              </ul>
            )}
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">long term (last day vs prior week)</div>
            {data.long_term_bullets.length === 0 ? (
              <div className="text-xs text-muted">No material long-term moves.</div>
            ) : (
              <ul className="space-y-1 text-[11px]">
                {data.long_term_bullets.map((b, i) => <li key={i}><span className="text-muted">▸</span> {b}</li>)}
              </ul>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Alert suppression ───────────────────────────────────────────────────

function SuppressionPanel() {
  const [data, setData] = useState<Suppression | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getAlertSuppression()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Alert Suppression" subtitle="redundancy diagnostics over the last hour">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-3 text-[10px] font-mono">
            <div className="flex flex-col">
              <span className="text-muted uppercase tracking-[0.14em]">total alerts</span>
              <span className="text-zinc-100 text-lg">{data.total_alerts}</span>
            </div>
            <div className="flex flex-col">
              <span className="text-muted uppercase tracking-[0.14em]">unique clusters</span>
              <span className="text-zinc-100 text-lg">{data.unique_clusters}</span>
            </div>
            <div className="flex flex-col">
              <span className="text-muted uppercase tracking-[0.14em]">compression ratio</span>
              <span className="text-zinc-100 text-lg">{(data.alert_compression_ratio * 100).toFixed(0)}%</span>
            </div>
          </div>
          {data.redundant_clusters.length === 0 ? (
            <div className="text-xs text-muted">No redundant clusters in window.</div>
          ) : (
            <table className="w-full text-sm font-mono">
              <thead>
                <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                  <th className="text-left py-2">#</th>
                  <th className="text-left py-2">SYMBOL</th>
                  <th className="text-left py-2">KIND</th>
                  <th className="text-right py-2">N</th>
                  <th className="text-left py-2">SEV</th>
                  <th className="text-right py-2">REDUND</th>
                </tr>
              </thead>
              <tbody>
                {data.redundant_clusters.map((c) => (
                  <tr key={c.cluster_id} className="border-t border-border/40">
                    <td className="py-1 text-muted">{c.cluster_id}</td>
                    <td className="py-1 text-zinc-200">{c.symbol}</td>
                    <td className="py-1 text-zinc-300">{c.kind.replace(/_/g, " ")}</td>
                    <td className="py-1 text-right text-zinc-200">{c.count}</td>
                    <td className="py-1 text-[10px] uppercase tracking-[0.14em]" style={{
                      color: c.max_severity === "critical" ? "rgba(214, 75, 75, 0.95)" : c.max_severity === "warn" ? "rgba(227, 180, 87, 0.95)" : "rgba(140, 170, 235, 0.95)",
                    }}>{c.max_severity}</td>
                    <td className="py-1 text-right text-muted">{c.redundancy_score.toFixed(0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </Panel>
  );
}

// ── Auto-anomaly scan ───────────────────────────────────────────────────

function AutoAnomalyPanel() {
  const [data, setData] = useState<AutoAnomalyOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  async function trigger() {
    setRunning(true);
    setError(null);
    try {
      const d = await triggerAutoAnomalyScan();
      setData(d);
    } catch (e: any) {
      setError(e?.message ?? "failed");
    } finally {
      setRunning(false);
    }
  }

  return (
    <Panel
      title="Auto-Anomaly Scan"
      subtitle="worker runs this every 5 min · trigger here for on-demand check"
      toolbar={
        <button
          onClick={trigger}
          disabled={running}
          className="h-7 px-3 rounded-md border border-accent/60 text-[10px] uppercase tracking-[0.18em] text-accent hover:bg-accent/10 disabled:opacity-40"
        >
          {running ? "…" : "scan now"}
        </button>
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!data && !error && <div className="text-xs text-muted">Click "scan now" to run a fresh scan.</div>}
      {data && (
        <div className="space-y-3">
          {data.inserted.length > 0 ? (
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">recorded {data.inserted.length}</div>
              <ul className="space-y-1 text-[11px] font-mono">
                {data.inserted.map((r) => (
                  <li key={r.id} className="flex items-center gap-2">
                    <span className="text-zinc-200">{r.kind.replace(/_/g, " ")}</span>
                    <span className="text-[9px] uppercase" style={{
                      color: r.severity === "critical" ? "rgba(214, 75, 75, 0.95)" : "rgba(227, 180, 87, 0.95)",
                    }}>{r.severity}</span>
                    <span className="text-muted">novelty {r.novelty_score.toFixed(0)} · recur {r.recurrence_count}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="text-[11px] text-muted">No new anomalies in this scan.</div>
          )}
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-1">decisions</div>
            <ul className="space-y-0.5 text-[10px] font-mono">
              {data.decisions.map((d, i) => (
                <li key={i} className="flex items-center gap-2">
                  <span className="text-muted w-32">{d.kind}</span>
                  <span className="text-zinc-300 w-24">{d.action}</span>
                  {d.score != null && <span className="text-muted">score {d.score.toFixed(0)}</span>}
                  {d.state && <span className="text-muted">state {d.state}</span>}
                  {d.next_eligible_in_ms != null && (
                    <span className="text-muted">eligible in {(d.next_eligible_in_ms / 60_000).toFixed(0)}m</span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Crisis clusters ─────────────────────────────────────────────────────

function CrisisClustersPanel() {
  const [data, setData] = useState<CrisisClusters | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getCrisisClusters()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Crisis Clusters" subtitle={data ? `${data.anomaly_count} anomalies grouped` : "agglomerative on anomaly memory"}>
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.clusters.length === 0 && (
        <div className="text-xs text-muted">No anomalies recorded yet. The worker populates anomaly memory every 5 minutes when thresholds trip.</div>
      )}
      {data && data.clusters.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">CL</th>
              <th className="text-right py-2">SIZE</th>
              <th className="text-left py-2">DOMINANT</th>
              <th className="text-right py-2">FREQ/D</th>
              <th className="text-right py-2">AVG NOV</th>
              <th className="text-left py-2">KINDS</th>
            </tr>
          </thead>
          <tbody>
            {data.clusters.map((c) => (
              <tr key={c.cluster_id} className="border-t border-border/40">
                <td className="py-1.5 text-muted">{c.cluster_id}</td>
                <td className="py-1.5 text-right text-zinc-200">{c.size}</td>
                <td className="py-1.5 text-zinc-200">{c.dominant_kind.replace(/_/g, " ")}</td>
                <td className="py-1.5 text-right text-muted">{c.frequency_per_day.toFixed(2)}</td>
                <td className="py-1.5 text-right text-muted">{c.avg_novelty.toFixed(0)}</td>
                <td className="py-1.5 text-[10px] text-zinc-300">
                  {Object.entries(c.kinds).map(([k, n]) => `${k.replace(/_/g, " ")}=${n}`).join(" · ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
