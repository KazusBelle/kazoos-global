/**
 * Phase-11 Meta dashboard.
 *
 * Self-calibration, anomaly memory, edge mutation, regime compression
 * and meta-intelligence health. The page is the engine looking at
 * itself: what should be retuned, what's mutating, what's recurring.
 *
 * Recommendations are read-only — nothing here writes to thresholds or
 * weights automatically. The user reads the suggestion and decides.
 */

import { useEffect, useState } from "react";
import {
  getAdaptiveMetricWeights,
  getEdgeMutation,
  getMetaHealth,
  getRegimeCompression,
  getStateEmbedding,
  getThresholdCalibration,
  listAnomalyMemory,
  type AdaptiveWeights,
  type AnomalyMemoryItem,
  type EdgeMutationOut,
  type MetaHealth,
  type RegimeCompression,
  type StateEmbedding,
  type ThresholdCalibration,
} from "../lib/api";

const REFRESH_MS = 30_000;

export function Meta() {
  return (
    <div className="space-y-4">
      <div className="flex items-baseline gap-3">
        <div className="text-accent text-xl font-bold tracking-[0.3em]">META</div>
        <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
          self-calibration · anomaly memory · mutation · meta-health
        </div>
      </div>

      <MetaHealthCard />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <ThresholdCalibrationPanel />
        <AdaptiveWeightsPanel />
      </div>
      <EdgeMutationPanel />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <RegimeCompressionPanel />
        <StateEmbeddingPanel />
      </div>
      <AnomalyMemoryPanel />
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

// ── Meta health headline ─────────────────────────────────────────────────

const META_STATE_COLOR: Record<MetaHealth["state"], string> = {
  HEALTHY: "rgba(82, 185, 122, 0.95)",
  DRIFTING: "rgba(140, 170, 235, 0.95)",
  DEGRADING: "rgba(227, 180, 87, 0.95)",
  CRITICAL: "rgba(214, 75, 75, 0.95)",
};

function MetaHealthCard() {
  const [data, setData] = useState<MetaHealth | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await getMetaHealth();
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
    <Panel title="Meta-Intelligence Health" subtitle="how well is the engine itself functioning">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted">health</div>
            <div className="text-5xl font-bold mt-1" style={{ color: META_STATE_COLOR[data.state] }}>
              {data.meta_intelligence_health.toFixed(0)}
            </div>
            <div className="text-[11px] uppercase tracking-[0.2em]" style={{ color: META_STATE_COLOR[data.state] }}>
              {data.state}
            </div>
            <div className="text-[10px] text-muted mt-2 space-y-0.5">
              <div>self-consistency {data.self_consistency_score.toFixed(0)}</div>
              <div>adaptation quality {data.adaptation_quality.toFixed(0)}</div>
            </div>
          </div>
          <div className="lg:col-span-2">
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">components</div>
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
            <div className="text-[10px] text-muted mt-3 space-y-0.5">
              <div>alert saturation ratio (24h vs 30d): <span className="text-zinc-300">{data.alert_saturation_ratio.toFixed(2)}×</span></div>
              <div>distinct regimes / day: <span className="text-zinc-300">{data.avg_distinct_regimes_per_day.toFixed(2)}</span></div>
              <div>edge mutation magnitude: <span className="text-zinc-300">{data.mutation_magnitude_sum.toFixed(3)}</span></div>
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Threshold calibration recommendations ────────────────────────────────

function ThresholdCalibrationPanel() {
  const [data, setData] = useState<ThresholdCalibration | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getThresholdCalibration()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Threshold Calibration" subtitle="self-tuning recommendations — read-only">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.kinds.length === 0 && <div className="text-xs text-muted">No data.</div>}
      {data && data.kinds.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">KIND</th>
              <th className="text-right py-2">PREC</th>
              <th className="text-right py-2">/DAY</th>
              <th className="text-left py-2">ACTION</th>
              <th className="text-right py-2">×</th>
              <th className="text-right py-2">CONF</th>
            </tr>
          </thead>
          <tbody>
            {data.kinds.map((k) => {
              const actionColor =
                k.action === "TIGHTEN" ? "rgba(214, 105, 105, 0.95)"
                  : k.action === "LOOSEN" ? "rgba(227, 180, 87, 0.95)"
                    : "rgba(140, 170, 235, 0.95)";
              return (
                <tr key={k.kind} className="border-t border-border/40" title={k.rationale.join(" · ")}>
                  <td className="py-1.5 text-zinc-200">{k.kind.replace(/_/g, " ")}</td>
                  <td className="py-1.5 text-right text-muted">
                    {k.precision != null ? `${(k.precision * 100).toFixed(0)}%` : "—"}
                  </td>
                  <td className="py-1.5 text-right text-muted">{k.per_day.toFixed(1)}</td>
                  <td className="py-1.5">
                    <span
                      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em]"
                      style={{
                        color: actionColor,
                        borderColor: actionColor.replace(/0\.95\)$/, "0.5)"),
                        background: actionColor.replace(/0\.95\)$/, "0.10)"),
                      }}
                    >
                      {k.action}
                    </span>
                  </td>
                  <td className="py-1.5 text-right text-zinc-200">{k.adjustment_multiplier.toFixed(2)}×</td>
                  <td className="py-1.5 text-right text-muted">{k.calibration_confidence.toFixed(0)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

// ── Adaptive weights ─────────────────────────────────────────────────────

function AdaptiveWeightsPanel() {
  const [data, setData] = useState<AdaptiveWeights | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getAdaptiveMetricWeights()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Adaptive Metric Weights" subtitle="relevance score · auto-tuned from follow-through alerts">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.weights.length === 0 && <div className="text-xs text-muted">No data.</div>}
      {data && data.weights.length > 0 && (
        <div className="space-y-1.5">
          {data.weights.map((w) => {
            const weightColor = w.weight > 1.15 ? "rgba(82, 185, 122, 0.95)" : w.weight < 0.85 ? "rgba(214, 105, 105, 0.95)" : "rgba(140, 170, 235, 0.95)";
            return (
              <div key={w.metric} className="flex items-center gap-2 text-[10px] font-mono">
                <span className="text-muted w-32 truncate">{w.metric}</span>
                <div className="flex-1 h-1.5 rounded bg-bg/60 overflow-hidden">
                  <div
                    className="h-full"
                    style={{
                      width: `${Math.max(0, Math.min(100, w.relevance_score)).toFixed(0)}%`,
                      background: weightColor,
                    }}
                  />
                </div>
                <span className="w-16 text-right" style={{ color: weightColor }}>×{w.weight.toFixed(2)}</span>
                <span className="text-muted w-10 text-right text-[9px]">n={w.samples}</span>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

// ── Edge mutation ────────────────────────────────────────────────────────

const MUTATION_COLOR: Record<string, string> = {
  STRENGTHENING: "rgba(82, 185, 122, 0.95)",
  WEAKENING: "rgba(214, 139, 105, 0.95)",
  INVERTED: "rgba(214, 75, 75, 0.95)",
  NEUTRAL: "rgba(140, 170, 235, 0.95)",
};

function EdgeMutationPanel() {
  const [data, setData] = useState<EdgeMutationOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [windowDays, setWindowDays] = useState(7);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    getEdgeMutation({ window_days: windowDays })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [windowDays]);

  return (
    <Panel
      title="Edge Mutation"
      subtitle="precision delta recent-window vs prior-window"
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
      {data && data.kinds.length === 0 && <div className="text-xs text-muted">No resolved data on both sides.</div>}
      {data && data.kinds.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">KIND</th>
              <th className="text-right py-2">PRIOR</th>
              <th className="text-right py-2">RECENT</th>
              <th className="text-right py-2">Δ</th>
              <th className="text-right py-2">VEL/DAY</th>
              <th className="text-left py-2">DIRECTION</th>
            </tr>
          </thead>
          <tbody>
            {data.kinds.map((k) => {
              const color = MUTATION_COLOR[k.mutation_direction];
              return (
                <tr key={k.kind} className="border-t border-border/40">
                  <td className="py-1.5 text-zinc-200">{k.kind.replace(/_/g, " ")}</td>
                  <td className="py-1.5 text-right text-muted">
                    {k.prior_precision != null ? `${(k.prior_precision * 100).toFixed(0)}%` : "—"}
                  </td>
                  <td className="py-1.5 text-right text-zinc-200">
                    {k.recent_precision != null ? `${(k.recent_precision * 100).toFixed(0)}%` : "—"}
                  </td>
                  <td className="py-1.5 text-right" style={{ color }}>
                    {k.delta != null ? `${k.delta > 0 ? "+" : ""}${(k.delta * 100).toFixed(1)}pp` : "—"}
                  </td>
                  <td className="py-1.5 text-right text-muted">
                    {k.mutation_velocity_per_day != null ? `${(k.mutation_velocity_per_day * 100).toFixed(2)}pp` : "—"}
                  </td>
                  <td className="py-1.5">
                    <span
                      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em]"
                      style={{
                        color,
                        borderColor: color.replace(/0\.95\)$/, "0.5)"),
                        background: color.replace(/0\.95\)$/, "0.10)"),
                      }}
                    >
                      {k.mutation_direction}{k.inverted ? "" : ""}
                    </span>
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

// ── Regime compression ───────────────────────────────────────────────────

function RegimeCompressionPanel() {
  const [data, setData] = useState<RegimeCompression | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getRegimeCompression()
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel title="Regime Compression" subtitle="cosine similarity over alert-kind profiles">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.regimes.length === 0 && <div className="text-xs text-muted">Not enough regime data.</div>}
      {data && data.regimes.length > 0 && (
        <div className="space-y-3">
          <div className="overflow-x-auto">
            <table className="font-mono text-[10px]">
              <thead>
                <tr>
                  <th className="p-1"></th>
                  {data.regimes.map((m) => (
                    <th key={m} className="p-1 text-muted whitespace-nowrap" style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}>{m}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.regimes.map((a) => (
                  <tr key={a}>
                    <td className="p-1 text-muted text-right whitespace-nowrap">{a}</td>
                    {data.regimes.map((b) => {
                      const cell = data.matrix.find((c) => (c.a === a && c.b === b) || (c.a === b && c.b === a));
                      const cos = cell?.cosine ?? 0;
                      const bg = `rgba(82, 185, 122, ${Math.min(0.9, cos * 0.85 + 0.05)})`;
                      return (
                        <td
                          key={b}
                          className="border border-bg text-center align-middle"
                          style={{ width: 36, height: 36, background: bg, color: cos > 0.5 ? "#0d0e11" : "#d1d5db" }}
                          title={`${a} × ${b} = cos ${cos.toFixed(2)}`}
                        >
                          {cos.toFixed(2)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.merge_candidates.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">merge candidates (cos ≥ 0.85)</div>
              <ul className="space-y-1 text-[11px] font-mono">
                {data.merge_candidates.map((m, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <span className="text-zinc-200">{m.a}</span>
                    <span className="text-muted">∼</span>
                    <span className="text-zinc-200">{m.b}</span>
                    <span className="text-[#52b97a]">{(m.cosine * 100).toFixed(0)}%</span>
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

// ── State embedding ──────────────────────────────────────────────────────

function StateEmbeddingPanel() {
  const [data, setData] = useState<StateEmbedding | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await getStateEmbedding();
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
    <Panel title="Universe State Fingerprint" subtitle="compact embedding · used for anomaly recurrence">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="space-y-2">
          <div className="text-[10px] text-muted">snapshot: {new Date(data.ts_ms).toISOString().replace("T", " ").slice(0, 19)} UTC</div>
          <div className="grid grid-cols-2 gap-2 text-[10px] font-mono">
            {data.metrics.map((m) => {
              const v = data.fingerprint[m];
              return (
                <div key={m} className="flex justify-between border-t border-border/40 pt-1">
                  <span className="text-muted">{m}</span>
                  <span className="text-zinc-200">
                    {v == null
                      ? "—"
                      : Math.abs(v) < 0.01
                        ? v.toExponential(2)
                        : v.toFixed(3)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Anomaly memory explorer ──────────────────────────────────────────────

const ANOMALY_KIND_COLOR: Record<string, string> = {
  structural_break: "rgba(227, 180, 87, 0.95)",
  regime_collapse: "rgba(214, 75, 75, 0.95)",
  venue_divergence: "rgba(140, 170, 235, 0.95)",
  pre_cascade: "rgba(214, 105, 105, 0.95)",
  edge_inversion: "rgba(214, 139, 105, 0.95)",
  regime_emergence: "rgba(82, 185, 122, 0.95)",
};

function AnomalyMemoryPanel() {
  const [data, setData] = useState<{ items: AnomalyMemoryItem[]; counts_by_kind: Record<string, number> } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listAnomalyMemory({ limit: 100 })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, []);

  return (
    <Panel
      title="Anomaly Memory"
      subtitle="persistent record of structural anomalies · novelty + recurrence"
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.items.length === 0 && (
        <div className="text-xs text-muted">
          No anomalies recorded yet. The engine populates this as structural breaks and cascade events fire.
        </div>
      )}
      {data && data.items.length > 0 && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2 text-[10px] uppercase tracking-[0.16em]">
            {Object.entries(data.counts_by_kind).map(([k, c]) => {
              const color = ANOMALY_KIND_COLOR[k] ?? "rgba(140, 170, 235, 0.95)";
              return (
                <span
                  key={k}
                  className="rounded-sm border px-1.5 py-0.5"
                  style={{
                    color, borderColor: color.replace(/0\.95\)$/, "0.5)"),
                    background: color.replace(/0\.95\)$/, "0.10)"),
                  }}
                >
                  {k.replace(/_/g, " ")} <span className="text-muted">{c}</span>
                </span>
              );
            })}
          </div>
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                <th className="text-left py-2">WHEN</th>
                <th className="text-left py-2">KIND</th>
                <th className="text-right py-2">NOVELTY</th>
                <th className="text-right py-2">RECUR</th>
                <th className="text-left py-2">FINGERPRINT</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((a) => {
                const color = ANOMALY_KIND_COLOR[a.kind] ?? "rgba(140, 170, 235, 0.95)";
                const noveltyColor = a.novelty_score >= 75 ? "rgba(214, 75, 75, 0.95)" : a.novelty_score >= 40 ? "rgba(227, 180, 87, 0.95)" : "rgba(82, 185, 122, 0.95)";
                const fpShort = Object.entries(a.fingerprint)
                  .slice(0, 4)
                  .map(([k, v]) => `${k}=${typeof v === "number" ? (Math.abs(v) < 0.01 ? v.toExponential(1) : v.toFixed(2)) : v}`)
                  .join(" · ");
                return (
                  <tr key={a.id} className="border-t border-border/40">
                    <td className="py-1.5 text-muted whitespace-nowrap">{new Date(a.occurred_at_ms).toISOString().replace("T", " ").slice(0, 16)}</td>
                    <td className="py-1.5">
                      <span
                        className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
                        style={{
                          color, borderColor: color.replace(/0\.95\)$/, "0.5)"),
                          background: color.replace(/0\.95\)$/, "0.10)"),
                        }}
                      >
                        {a.kind.replace(/_/g, " ")}
                      </span>
                    </td>
                    <td className="py-1.5 text-right font-semibold" style={{ color: noveltyColor }}>
                      {a.novelty_score.toFixed(0)}
                    </td>
                    <td className="py-1.5 text-right text-muted">{a.recurrence_count}</td>
                    <td className="py-1.5 text-[10px] text-zinc-300 truncate max-w-[400px]">{fpShort}</td>
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
