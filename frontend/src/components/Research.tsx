/**
 * Phase-7 Research dashboard.
 *
 * This page consumes the analytics endpoints in /liquidity/research/* —
 * it does not compute anything heavy on its own. Each section is a
 * lazy-loaded panel so the user can pick what they want to look at
 * without paying for the rest.
 *
 * Panels:
 *   - Signal Quality   — per-kind precision / false-positive over alert history
 *   - Regime Stats     — regime counts + top transitions
 *   - Calibration      — current adaptive thresholds (mirrors the
 *                        same compute the scanner does, so the user
 *                        can sanity-check what the engine is using)
 *   - Drift            — bucketed cohort percentiles for one metric over time
 *   - Similarity       — top-K historical matches for a chosen symbol
 *   - Venue Quality    — Binance vs Bybit divergence aggregation
 *   - Annotations      — recent labelled events
 */

import { useEffect, useMemo, useState } from "react";
import {
  deleteAnnotation,
  getDriftSeries,
  getEdgeRanking,
  getInteractionMatrix,
  getMetaState,
  getRegimeOutcomes,
  getRegimeStats,
  getSignalStats,
  getSimilarity,
  getVenueLeadership,
  getVenueQuality,
  listAnnotations,
  type Annotation,
  type DriftSeries,
  type EdgeRanking,
  type InteractionMatrix,
  type MetaState,
  type RegimeOutcomes,
  type RegimeStatsResponse,
  type SignalStatsResponse,
  type SimilarityResponse,
  type VenueLeadership,
  type VenueQualityResponse,
} from "../lib/api";

const RANGE_OPTIONS = [
  { label: "24H", ms: 24 * 3600 * 1000 },
  { label: "7D", ms: 7 * 24 * 3600 * 1000 },
  { label: "30D", ms: 30 * 24 * 3600 * 1000 },
] as const;

type RangeMs = (typeof RANGE_OPTIONS)[number]["ms"];

export function Research() {
  const [rangeMs, setRangeMs] = useState<RangeMs>(7 * 24 * 3600 * 1000);
  const sinceMs = useMemo(() => Date.now() - rangeMs, [rangeMs]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex items-baseline gap-3">
          <div className="text-accent text-xl font-bold tracking-[0.3em]">RESEARCH</div>
          <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
            calibration · validation · similarity
          </div>
        </div>
        <div className="flex items-center gap-1">
          {RANGE_OPTIONS.map((r) => (
            <button
              key={r.label}
              onClick={() => setRangeMs(r.ms)}
              className={`h-8 px-3 rounded-md border text-[11px] uppercase tracking-[0.22em] transition-colors ${
                rangeMs === r.ms
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border text-muted hover:text-zinc-200 hover:border-accent/50"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      <SignalQualityPanel sinceMs={sinceMs} />
      <EdgeRankingPanel sinceMs={sinceMs} />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <InteractionMatrixPanel sinceMs={sinceMs} />
        <RegimeOutcomesPanel sinceMs={sinceMs} />
      </div>
      <RegimeStatsPanel sinceMs={sinceMs} />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <DriftPanel sinceMs={sinceMs} />
        <VenueQualityPanel sinceMs={sinceMs} />
      </div>
      <VenueLeadershipPanel sinceMs={sinceMs} />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <SimilarityPanel />
        <MetaStatePanel />
      </div>
      <AnnotationsPanel sinceMs={sinceMs} />
    </div>
  );
}

// ── Generic panel shell ──────────────────────────────────────────────────

function Panel({
  title,
  subtitle,
  children,
  toolbar,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  toolbar?: React.ReactNode;
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

// ── Signal Quality ────────────────────────────────────────────────────────

function SignalQualityPanel({ sinceMs }: { sinceMs: number }) {
  const [data, setData] = useState<SignalStatsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getSignalStats({ since_ms: sinceMs })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [sinceMs]);

  return (
    <Panel title="Signal Quality" subtitle="per alert kind">
      {loading && <div className="text-xs text-muted">Loading…</div>}
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {data && data.kinds.length === 0 && (
        <div className="text-xs text-muted">No alerts persisted in this window yet.</div>
      )}
      {data && data.kinds.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">KIND</th>
              <th className="text-right py-2">TOTAL</th>
              <th className="text-right py-2">RESOLVED</th>
              <th className="text-right py-2">FOLLOW-THRU</th>
              <th className="text-right py-2">NOISE</th>
              <th className="text-right py-2">PRECISION</th>
              <th className="text-right py-2">AVG PRI</th>
              <th className="text-right py-2">AVG CONF</th>
            </tr>
          </thead>
          <tbody>
            {data.kinds.map((k) => {
              const precision = k.precision;
              const color =
                precision == null
                  ? "text-muted"
                  : precision >= 0.6
                    ? "text-[#52b97a]"
                    : precision >= 0.3
                      ? "text-[#e3b457]"
                      : "text-[#d68b8b]";
              return (
                <tr key={k.kind} className="border-t border-border/40">
                  <td className="py-1.5 text-zinc-200">{k.kind.replace(/_/g, " ")}</td>
                  <td className="py-1.5 text-right text-zinc-200">{k.total}</td>
                  <td className="py-1.5 text-right text-muted">{k.resolved}</td>
                  <td className="py-1.5 text-right text-[#52b97a]">{k.followed_through}</td>
                  <td className="py-1.5 text-right text-[#d68b8b]">{k.noise}</td>
                  <td className={`py-1.5 text-right ${color}`}>
                    {precision != null ? `${(precision * 100).toFixed(0)}%` : "—"}
                  </td>
                  <td className="py-1.5 text-right text-muted">
                    {k.avg_priority != null ? k.avg_priority.toFixed(0) : "—"}
                  </td>
                  <td className="py-1.5 text-right text-muted">
                    {k.avg_confidence != null ? k.avg_confidence.toFixed(0) : "—"}
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

// ── Regime Stats ─────────────────────────────────────────────────────────

function RegimeStatsPanel({ sinceMs }: { sinceMs: number }) {
  const [data, setData] = useState<RegimeStatsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    getRegimeStats(sinceMs)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [sinceMs]);

  return (
    <Panel title="Regime Statistics" subtitle="counts + transitions (from alert history)">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">Regime counts</div>
            {Object.keys(data.regime_counts).length === 0 ? (
              <div className="text-xs text-muted">No data.</div>
            ) : (
              <ul className="space-y-1 font-mono text-[12px]">
                {Object.entries(data.regime_counts)
                  .sort((a, b) => b[1] - a[1])
                  .map(([regime, count]) => (
                    <li key={regime} className="flex items-center justify-between">
                      <span className="text-zinc-200">{regime}</span>
                      <span className="text-muted">{count}</span>
                    </li>
                  ))}
              </ul>
            )}
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted mb-2">Top transitions</div>
            {data.top_transitions.length === 0 ? (
              <div className="text-xs text-muted">No transitions observed yet.</div>
            ) : (
              <ul className="space-y-1 font-mono text-[12px]">
                {data.top_transitions.map((t, i) => (
                  <li key={i} className="flex items-center justify-between">
                    <span className="text-zinc-200">
                      {t.from_regime} <span className="text-muted">→</span> {t.to_regime}
                    </span>
                    <span className="text-muted">{t.count}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}

// ── Drift ────────────────────────────────────────────────────────────────

const DRIFT_METRICS = [
  "spread", "credible_depth", "obi", "atr_liquidity",
  "liq_stress", "funding_z", "oi_delta_1h",
  "resiliency_score", "impact_score", "fragility_score",
];

function DriftPanel({ sinceMs }: { sinceMs: number }) {
  const [metric, setMetric] = useState("spread");
  const [data, setData] = useState<DriftSeries | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getDriftSeries(metric, 60, sinceMs)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [metric, sinceMs]);

  return (
    <Panel
      title="Drift Monitor"
      subtitle="cohort P10 / P50 / P90 per metric"
      toolbar={
        <select
          value={metric}
          onChange={(e) => setMetric(e.target.value)}
          className="h-7 px-2 text-[10px] uppercase tracking-[0.15em] rounded-md border border-border bg-bg/60 text-zinc-200"
        >
          {DRIFT_METRICS.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
      }
    >
      {loading && <div className="text-xs text-muted">Loading…</div>}
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {data && data.points.length === 0 && <div className="text-xs text-muted">No samples in window.</div>}
      {data && data.points.length > 0 && <DriftChart series={data} />}
    </Panel>
  );
}

function DriftChart({ series }: { series: DriftSeries }) {
  const width = 720;
  const height = 220;
  const padX = 48;
  const padY = 20;
  const xs = series.points.map((p) => p.bucket_ts);
  const allVals = series.points.flatMap((p) => [p.p10, p.p50, p.p90].filter((v): v is number => v != null));
  if (allVals.length === 0) return <div className="text-xs text-muted">No data.</div>;
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...allVals);
  const maxY = Math.max(...allVals);
  const sx = (x: number) => padX + ((x - minX) / Math.max(1, maxX - minX)) * (width - 2 * padX);
  const sy = (y: number) => height - padY - ((y - minY) / Math.max(1e-9, maxY - minY)) * (height - 2 * padY);

  const pathOf = (key: "p10" | "p50" | "p90") => {
    const pts = series.points
      .filter((p) => p[key] != null)
      .map((p) => `${sx(p.bucket_ts).toFixed(1)},${sy(p[key] as number).toFixed(1)}`);
    return pts.length > 0 ? `M${pts.join(" L")}` : "";
  };

  const fmt = (n: number) => {
    const abs = Math.abs(n);
    if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
    if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
    if (abs >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
    if (abs >= 1) return n.toFixed(2);
    return n.toPrecision(3);
  };

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto block">
      {[0, 0.5, 1].map((t, i) => {
        const y = padY + t * (height - 2 * padY);
        const v = maxY - t * (maxY - minY);
        return (
          <g key={i}>
            <line x1={padX} x2={width - padX} y1={y} y2={y} stroke="#1a1c22" strokeWidth={1} />
            <text x={padX - 6} y={y + 3} textAnchor="end" fontSize="9" fill="#5a5f6b">{fmt(v)}</text>
          </g>
        );
      })}
      <path d={pathOf("p10")} fill="none" stroke="rgba(214, 105, 105, 0.85)" strokeWidth={1.4} />
      <path d={pathOf("p50")} fill="none" stroke="rgba(227, 208, 45, 0.95)" strokeWidth={1.6} />
      <path d={pathOf("p90")} fill="none" stroke="rgba(82, 185, 122, 0.85)" strokeWidth={1.4} />
      <g>
        <rect x={padX} y={4} width={10} height={2} fill="rgba(214, 105, 105, 0.85)" />
        <text x={padX + 14} y={9} fontSize="9" fill="#5a5f6b">P10</text>
        <rect x={padX + 50} y={4} width={10} height={2} fill="rgba(227, 208, 45, 0.95)" />
        <text x={padX + 64} y={9} fontSize="9" fill="#5a5f6b">P50</text>
        <rect x={padX + 100} y={4} width={10} height={2} fill="rgba(82, 185, 122, 0.85)" />
        <text x={padX + 114} y={9} fontSize="9" fill="#5a5f6b">P90</text>
      </g>
    </svg>
  );
}

// ── Similarity ───────────────────────────────────────────────────────────

function SimilarityPanel() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [pending, setPending] = useState(symbol);
  const [data, setData] = useState<SimilarityResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  function search() {
    setSymbol(pending.trim().toUpperCase());
  }

  useEffect(() => {
    if (!symbol) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getSimilarity(symbol, { top_k: 10, sample_minutes: 15, lookback_days: 14 })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [symbol]);

  return (
    <Panel
      title="Similarity Matching"
      subtitle="closest historical states (L2 in normalized metric space)"
      toolbar={
        <div className="flex items-center gap-1">
          <input
            value={pending}
            onChange={(e) => setPending(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && search()}
            className="h-7 px-2 text-[10px] uppercase tracking-[0.15em] rounded-md border border-border bg-bg/60 text-zinc-200"
            placeholder="SYMBOL"
          />
          <button
            onClick={search}
            className="h-7 px-3 rounded-md border border-accent/60 text-[10px] uppercase tracking-[0.18em] text-accent hover:bg-accent/10"
          >
            FIND
          </button>
        </div>
      }
    >
      {loading && <div className="text-xs text-muted">Loading…</div>}
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {data && data.matches.length === 0 && <div className="text-xs text-muted">No historical match.</div>}
      {data && data.matches.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">WHEN</th>
              <th className="text-right py-2">DIST</th>
              <th className="text-right py-2">SPREAD</th>
              <th className="text-right py-2">CRED DEPTH</th>
              <th className="text-right py-2">OBI</th>
              <th className="text-right py-2">FRAG</th>
              <th className="text-right py-2">RESIL</th>
              <th className="text-right py-2">FUND-Z</th>
            </tr>
          </thead>
          <tbody>
            {data.matches.map((m) => (
              <tr key={m.ts} className="border-t border-border/40">
                <td className="py-1.5 text-zinc-200">{new Date(m.ts).toISOString().replace("T", " ").slice(0, 16)}</td>
                <td className="py-1.5 text-right text-zinc-200">{m.distance.toFixed(2)}</td>
                <td className="py-1.5 text-right text-muted">{m.metrics.spread != null ? (m.metrics.spread * 10_000).toFixed(1) + "bps" : "—"}</td>
                <td className="py-1.5 text-right text-muted">{m.metrics.credible_depth != null ? `$${formatBig(m.metrics.credible_depth)}` : "—"}</td>
                <td className="py-1.5 text-right text-muted">{m.metrics.obi?.toFixed(2) ?? "—"}</td>
                <td className="py-1.5 text-right text-muted">{m.metrics.fragility_score?.toFixed(0) ?? "—"}</td>
                <td className="py-1.5 text-right text-muted">{m.metrics.resiliency_score?.toFixed(0) ?? "—"}</td>
                <td className="py-1.5 text-right text-muted">{m.metrics.funding_z?.toFixed(2) ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

// ── Venue Quality ────────────────────────────────────────────────────────

function VenueQualityPanel({ sinceMs }: { sinceMs: number }) {
  const [data, setData] = useState<VenueQualityResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    getVenueQuality(sinceMs)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [sinceMs]);

  return (
    <Panel title="Venue Quality" subtitle="Binance reference · Bybit divergence">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.venues.length === 0 && (
        <div className="text-xs text-muted">No crossex samples yet — open a chart detail modal to populate the history.</div>
      )}
      {data && data.venues.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">EX</th>
              <th className="text-right py-2">SAMPLES</th>
              <th className="text-right py-2">AVG SPREAD</th>
              <th className="text-right py-2">AVG OI</th>
              <th className="text-right py-2">MED FUNDING</th>
              <th className="text-right py-2">MID DIVERGE</th>
              <th className="text-right py-2">FUND DIVERGE</th>
            </tr>
          </thead>
          <tbody>
            {data.venues.map((v) => (
              <tr key={v.exchange} className="border-t border-border/40">
                <td className="py-1.5 uppercase text-zinc-200">{v.exchange}</td>
                <td className="py-1.5 text-right text-muted">{v.samples}</td>
                <td className="py-1.5 text-right text-zinc-200">{v.avg_spread_bps != null ? `${v.avg_spread_bps.toFixed(1)}bps` : "—"}</td>
                <td className="py-1.5 text-right text-zinc-200">{v.avg_oi_usd != null ? `$${formatBig(v.avg_oi_usd)}` : "—"}</td>
                <td className="py-1.5 text-right text-muted">{v.median_funding_bps != null ? `${v.median_funding_bps.toFixed(2)}bps` : "—"}</td>
                <td className="py-1.5 text-right text-muted">{v.avg_mid_divergence_pct != null ? `${v.avg_mid_divergence_pct.toFixed(3)}%` : "—"}</td>
                <td className="py-1.5 text-right text-muted">{v.avg_funding_divergence_bps != null ? `${v.avg_funding_divergence_bps.toFixed(2)}bps` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

// ── Annotations ──────────────────────────────────────────────────────────

const ANNOTATION_COLORS: Record<string, string> = {
  useful_signal: "rgba(82, 185, 122, 0.95)",
  false_signal: "rgba(214, 105, 105, 0.95)",
  manipulation: "rgba(214, 75, 75, 0.95)",
  interesting_setup: "rgba(227, 180, 87, 0.95)",
  liquidation_event: "rgba(214, 105, 105, 0.95)",
  spoof_behavior: "rgba(214, 139, 105, 0.95)",
  other: "rgba(140, 170, 235, 0.95)",
};

function AnnotationsPanel({ sinceMs }: { sinceMs: number }) {
  const [rows, setRows] = useState<Annotation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function reload() {
    setError(null);
    try {
      const list = await listAnnotations({ since_ms: sinceMs, limit: 200 });
      setRows(list);
    } catch (e: any) {
      setError(e?.message ?? "failed");
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sinceMs]);

  return (
    <Panel
      title="Annotations"
      subtitle="labelled research events"
      toolbar={
        <button
          onClick={reload}
          className="h-7 px-3 rounded-md border border-border text-[10px] uppercase tracking-[0.18em] text-muted hover:text-zinc-200"
        >
          Refresh
        </button>
      }
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && rows == null && <div className="text-xs text-muted">Loading…</div>}
      {rows && rows.length === 0 && (
        <div className="text-xs text-muted">
          No annotations yet — enter replay mode, pick a row, and tag a moment.
        </div>
      )}
      {rows && rows.length > 0 && (
        <ul className="space-y-1 font-mono text-[11px] max-h-96 overflow-auto">
          {rows.map((a) => {
            const color = ANNOTATION_COLORS[a.kind] ?? "rgba(140, 170, 235, 0.95)";
            return (
              <li key={a.id} className="flex items-center gap-2 border-t border-border/40 py-1">
                <span className="text-muted whitespace-nowrap">
                  {new Date(a.ts_ms).toISOString().replace("T", " ").slice(0, 16)}
                </span>
                <span className="font-semibold text-zinc-200 w-24">{a.symbol}</span>
                <span
                  className="rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
                  style={{
                    color,
                    borderColor: color.replace(/0\.95\)$/, "0.5)"),
                    background: color.replace(/0\.95\)$/, "0.10)"),
                  }}
                >
                  {a.kind.replace(/_/g, " ")}
                </span>
                <span className="text-zinc-300 flex-1 truncate">{a.note ?? ""}</span>
                <button
                  onClick={async () => {
                    try {
                      await deleteAnnotation(a.id);
                      reload();
                    } catch {
                      // ignore
                    }
                  }}
                  className="text-muted hover:text-[#d68b8b] text-xs"
                  title="Delete"
                >
                  ✕
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// Phase-8 panels: edge ranking, interaction heatmap, regime outcomes,
// venue leadership, meta-state rarity.
// ══════════════════════════════════════════════════════════════════════════

// ── Edge ranking ─────────────────────────────────────────────────────────

const EDGE_ALERT_KINDS = [
  "", "SPREAD_EXPLOSION", "DEPTH_COLLAPSE", "LIQ_CASCADE",
  "RESILIENCY_FAILURE", "OI_SURGE", "FUNDING_EXTREME",
  "FRAGILITY_SPIKE", "REGIME_TRANSITION",
];

function EdgeRankingPanel({ sinceMs }: { sinceMs: number }) {
  const [alertKind, setAlertKind] = useState("");
  const [data, setData] = useState<EdgeRanking | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getEdgeRanking({ since_ms: sinceMs, alert_kind: alertKind || undefined })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [sinceMs, alertKind]);

  return (
    <Panel
      title="Edge Ranking"
      subtitle={data ? `base rate ${(data.base_rate * 100).toFixed(2)}% · ${data.total_buckets} buckets · ${(data.outcome_window_ms / 60_000).toFixed(0)}m outcome window` : "tertile combos → downstream alert lift"}
      toolbar={
        <select
          value={alertKind}
          onChange={(e) => setAlertKind(e.target.value)}
          className="h-7 px-2 text-[10px] uppercase tracking-[0.15em] rounded-md border border-border bg-bg/60 text-zinc-200"
        >
          <option value="">ANY ALERT</option>
          {EDGE_ALERT_KINDS.filter((k) => k).map((k) => (
            <option key={k} value={k}>{k}</option>
          ))}
        </select>
      }
    >
      {loading && <div className="text-xs text-muted">Loading…</div>}
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {data && data.combos.length === 0 && <div className="text-xs text-muted">No combos with enough support yet.</div>}
      {data && data.combos.length > 0 && (
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
              <th className="text-left py-2">RESILIENCY</th>
              <th className="text-left py-2">FRAGILITY</th>
              <th className="text-left py-2">FUND-Z</th>
              <th className="text-right py-2">N</th>
              <th className="text-right py-2">OUTCOMES</th>
              <th className="text-right py-2">RATE</th>
              <th className="text-right py-2">LIFT</th>
            </tr>
          </thead>
          <tbody>
            {data.combos.slice(0, 27).map((c, i) => {
              const liftColor =
                c.lift == null
                  ? "text-muted"
                  : c.lift >= 2
                    ? "text-[#52b97a]"
                    : c.lift >= 1.3
                      ? "text-[#e3b457]"
                      : c.lift < 0.7
                        ? "text-[#d68b8b]"
                        : "text-zinc-300";
              return (
                <tr key={i} className="border-t border-border/40">
                  <td className="py-1.5"><TertileChip v={c.resiliency} /></td>
                  <td className="py-1.5"><TertileChip v={c.fragility} /></td>
                  <td className="py-1.5"><TertileChip v={c.funding_z} /></td>
                  <td className="py-1.5 text-right text-muted">{c.total}</td>
                  <td className="py-1.5 text-right text-zinc-200">{c.outcomes}</td>
                  <td className="py-1.5 text-right text-zinc-200">{(c.rate * 100).toFixed(1)}%</td>
                  <td className={`py-1.5 text-right ${liftColor}`}>
                    {c.lift != null ? `${c.lift.toFixed(2)}×` : "—"}
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

function TertileChip({ v }: { v: "low" | "mid" | "high" }) {
  const colors = {
    low: "rgba(82, 185, 122, 0.95)",
    mid: "rgba(140, 170, 235, 0.95)",
    high: "rgba(214, 105, 105, 0.95)",
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

// ── Interaction matrix (heatmap) ─────────────────────────────────────────

function InteractionMatrixPanel({ sinceMs }: { sinceMs: number }) {
  const [data, setData] = useState<InteractionMatrix | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    getInteractionMatrix(sinceMs)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [sinceMs]);

  return (
    <Panel title="Signal Interaction" subtitle="Pearson correlation across metrics">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && <InteractionHeatmap matrix={data} />}
    </Panel>
  );
}

function InteractionHeatmap({ matrix }: { matrix: InteractionMatrix }) {
  const metrics = matrix.metrics;
  const cellById = new Map<string, { r: number | null; n: number }>();
  for (const c of matrix.cells) cellById.set(`${c.a}::${c.b}`, { r: c.r, n: c.n });
  const cellSize = 40;
  return (
    <div className="overflow-x-auto">
      <table className="font-mono text-[10px]">
        <thead>
          <tr>
            <th className="p-1"></th>
            {metrics.map((m) => (
              <th
                key={m}
                className="p-1 text-muted whitespace-nowrap"
                style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
              >
                {m}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {metrics.map((a) => (
            <tr key={a}>
              <td className="p-1 text-muted text-right whitespace-nowrap">{a}</td>
              {metrics.map((b) => {
                const c = cellById.get(`${a}::${b}`);
                const r = c?.r ?? null;
                const bg =
                  r == null
                    ? "rgba(60, 60, 70, 0.20)"
                    : r > 0
                      ? `rgba(82, 185, 122, ${Math.min(0.9, Math.abs(r) * 0.9 + 0.1)})`
                      : `rgba(214, 105, 105, ${Math.min(0.9, Math.abs(r) * 0.9 + 0.1)})`;
                return (
                  <td
                    key={b}
                    className="border border-bg text-center align-middle"
                    style={{
                      width: cellSize, height: cellSize, background: bg,
                      color: r != null && Math.abs(r) > 0.5 ? "#0d0e11" : "#d1d5db",
                    }}
                    title={`${a} vs ${b}: r=${r?.toFixed(2) ?? "—"} (n=${c?.n ?? 0})`}
                  >
                    {r != null ? r.toFixed(2) : "—"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Regime outcomes ──────────────────────────────────────────────────────

function RegimeOutcomesPanel({ sinceMs }: { sinceMs: number }) {
  const [data, setData] = useState<RegimeOutcomes | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    getRegimeOutcomes(sinceMs)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [sinceMs]);

  return (
    <Panel title="Regime Outcomes" subtitle="durations · transition probs · collapse %">
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.regimes.length === 0 && <div className="text-xs text-muted">No regime samples in window.</div>}
      {data && data.regimes.length > 0 && (
        <div className="space-y-3">
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                <th className="text-left py-2">REGIME</th>
                <th className="text-right py-2">COUNT</th>
                <th className="text-right py-2">AVG DUR</th>
                <th className="text-right py-2">COLLAPSE %</th>
                <th className="text-left py-2">TOP NEXT</th>
              </tr>
            </thead>
            <tbody>
              {data.regimes.map((r) => {
                const top = Object.entries(r.transitions)
                  .sort((a, b) => b[1] - a[1])
                  .slice(0, 3);
                const collapseColor =
                  r.collapse_prob >= 0.4 ? "text-[#d68b8b]"
                    : r.collapse_prob >= 0.2 ? "text-[#e3b457]"
                      : "text-zinc-300";
                return (
                  <tr key={r.regime} className="border-t border-border/40">
                    <td className="py-1.5 text-zinc-200">{r.regime}</td>
                    <td className="py-1.5 text-right text-zinc-200">{r.count}</td>
                    <td className="py-1.5 text-right text-muted">
                      {r.avg_duration_ms != null ? fmtDuration(r.avg_duration_ms) : "—"}
                    </td>
                    <td className={`py-1.5 text-right ${collapseColor}`}>
                      {(r.collapse_prob * 100).toFixed(0)}%
                    </td>
                    <td className="py-1.5 text-[10px]">
                      {top.length === 0 ? <span className="text-muted">—</span> : top.map(([t, p]) => (
                        <span key={t} className="text-muted mr-2">
                          {t.replace(/_/g, " ")} <span className="text-zinc-300">{(p * 100).toFixed(0)}%</span>
                        </span>
                      ))}
                    </td>
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

function fmtDuration(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(0)}s`;
  if (ms < 3600_000) return `${(ms / 60_000).toFixed(1)}m`;
  if (ms < 86_400_000) return `${(ms / 3600_000).toFixed(1)}h`;
  return `${(ms / 86_400_000).toFixed(1)}d`;
}

// ── Venue leadership ─────────────────────────────────────────────────────

function VenueLeadershipPanel({ sinceMs }: { sinceMs: number }) {
  const [data, setData] = useState<VenueLeadership | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    getVenueLeadership(sinceMs)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); });
    return () => { cancelled = true; };
  }, [sinceMs]);

  return (
    <Panel
      title="Venue Leadership"
      subtitle="cross-venue lag · who moves first"
    >
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {!error && !data && <div className="text-xs text-muted">Loading…</div>}
      {data && data.metrics.length === 0 && (
        <div className="text-xs text-muted">
          Not enough overlap between Binance and other venues yet — open the detail modal on several pinned symbols to populate crossex history.
        </div>
      )}
      {data && data.metrics.length > 0 && (
        <div className="space-y-3">
          <div className="text-[10px] text-muted">
            Pairs analyzed: {data.pair_count} · max lag examined: {data.max_lag_s}s · positive lag = OTHER venue led Binance
          </div>
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-[10px] uppercase tracking-[0.18em] text-muted border-b border-border/60">
                <th className="text-left py-2">METRIC</th>
                <th className="text-left py-2">EX</th>
                <th className="text-right py-2">PAIRS</th>
                <th className="text-right py-2">MEAN LAG</th>
                <th className="text-right py-2">LED BNB</th>
                <th className="text-right py-2">LAGGED BNB</th>
              </tr>
            </thead>
            <tbody>
              {data.metrics.flatMap((m) =>
                m.venues.map((v) => (
                  <tr key={`${m.metric}:${v.exchange}`} className="border-t border-border/40">
                    <td className="py-1.5 text-zinc-200">{m.metric}</td>
                    <td className="py-1.5 uppercase text-zinc-200">{v.exchange}</td>
                    <td className="py-1.5 text-right text-muted">{v.samples}</td>
                    <td className={`py-1.5 text-right ${v.mean_lag_s > 1 ? "text-[#d68b8b]" : v.mean_lag_s < -1 ? "text-[#52b97a]" : "text-muted"}`}>
                      {v.mean_lag_s.toFixed(1)}s
                    </td>
                    <td className="py-1.5 text-right text-muted">{(v.share_led_binance * 100).toFixed(0)}%</td>
                    <td className="py-1.5 text-right text-muted">{(v.share_lagged_binance * 100).toFixed(0)}%</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

// ── Meta-state rarity ────────────────────────────────────────────────────

function MetaStatePanel() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [pending, setPending] = useState(symbol);
  const [data, setData] = useState<MetaState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  function search() {
    setSymbol(pending.trim().toUpperCase());
  }

  useEffect(() => {
    if (!symbol) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getMetaState(symbol, 30)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [symbol]);

  const rarity = data?.rarity_pct ?? null;
  const rarityColor =
    rarity == null
      ? "#5a5f6b"
      : rarity >= 75
        ? "rgba(214, 75, 75, 0.95)"
        : rarity >= 50
          ? "rgba(227, 180, 87, 0.95)"
          : "rgba(82, 185, 122, 0.95)";

  return (
    <Panel
      title="Meta-State Rarity"
      subtitle="how unusual is the symbol's current state vs 30d history"
      toolbar={
        <div className="flex items-center gap-1">
          <input
            value={pending}
            onChange={(e) => setPending(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && search()}
            className="h-7 px-2 text-[10px] uppercase tracking-[0.15em] rounded-md border border-border bg-bg/60 text-zinc-200"
            placeholder="SYMBOL"
          />
          <button
            onClick={search}
            className="h-7 px-3 rounded-md border border-accent/60 text-[10px] uppercase tracking-[0.18em] text-accent hover:bg-accent/10"
          >
            CHECK
          </button>
        </div>
      }
    >
      {loading && <div className="text-xs text-muted">Loading…</div>}
      {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
      {data && data.rarity_pct == null && (
        <div className="text-xs text-muted">Not enough history for {data.symbol}.</div>
      )}
      {data && data.rarity_pct != null && (
        <div className="space-y-3">
          <div className="flex items-baseline gap-3">
            <span className="text-zinc-100 font-bold text-3xl" style={{ color: rarityColor }}>
              {data.rarity_pct.toFixed(0)}%
            </span>
            <span className="text-[10px] uppercase tracking-[0.2em] text-muted">
              rarity (100 = unprecedented · 0 = typical)
            </span>
          </div>
          <div className="text-[10px] text-muted">
            {data.similar_count}/{data.n} historical fingerprints within distance {data.threshold?.toFixed(2)} of the current state.
          </div>
          {data.reference && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[10px] font-mono">
              {Object.entries(data.reference).map(([m, v]) => (
                <div key={m} className="flex justify-between border-t border-border/40 pt-1">
                  <span className="text-muted">{m}</span>
                  <span className="text-zinc-200">
                    {typeof v === "number"
                      ? Math.abs(v) < 0.01
                        ? v.toExponential(2)
                        : v.toFixed(2)
                      : "—"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

function formatBig(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1e12) return `${(n / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  return n.toFixed(2);
}
