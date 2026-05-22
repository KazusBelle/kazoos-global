import { useEffect, useMemo, useRef, useState } from "react";
import {
  getCrossExchange,
  getLiquidityMetricSeries,
  heartbeatLiquidityActive,
  listLiquidityMetrics,
  type CrossExResponse,
  type LiqMetricMeta,
  type LiqMetricSeries,
} from "../lib/api";
import { StackedLineChart, type ChartMarker } from "./StackedLineChart";

type Props = {
  symbol: string;
  orderedSymbols: string[];
  onSwitchSymbol: (symbol: string) => void;
  onClose: () => void;
};

type WindowChoice = "1h" | "24h" | "7d" | "30d";
const WINDOWS: WindowChoice[] = ["1h", "24h", "7d", "30d"];

const HEARTBEAT_MS = 30_000;
const LIVE_POLL_MS = 1500;
const REALTIME_METRICS = new Set(["obi_rt", "credible_depth", "liq_stress"]);

// Window → TradingView interval code. TV expects bare numbers for
// intraday minutes and the letter "D" / "W" for day/week.
const TV_INTERVAL: Record<WindowChoice, string> = {
  "1h": "1",
  "24h": "15",
  "7d": "60",
  "30d": "240",
};

function displayName(symbol: string): string {
  let s = symbol.replace(/USDT$/, "");
  if (s.startsWith("1000")) s = s.slice(4);
  return s;
}

function tradingViewUrl(symbol: string, interval: string): string {
  // Binance USDT-M perpetual symbols on TradingView are `BINANCE:<symbol>.P`.
  // style=2 = line chart, dark theme, all toolbars hidden so it reads as
  // a clean price strip rather than a full trading widget.
  const tvSymbol = encodeURIComponent(`BINANCE:${symbol}.P`);
  const params = new URLSearchParams({
    symbol: tvSymbol,
    interval,
    theme: "dark",
    style: "2",
    hidesidetoolbar: "1",
    hidetoptoolbar: "1",
    withdateranges: "0",
    hide_legend: "1",
    hide_volume: "1",
    locale: "en",
    timezone: "Etc/UTC",
  });
  // URLSearchParams encodes the colon — fine for TV.
  return `https://s.tradingview.com/widgetembed/?${params.toString()}`;
}

export function LiquidityChartModal({
  symbol,
  orderedSymbols,
  onSwitchSymbol,
  onClose,
}: Props) {
  const [metrics, setMetrics] = useState<LiqMetricMeta[]>([]);
  const [windowChoice, setWindowChoice] = useState<WindowChoice>("24h");

  const idx = orderedSymbols.indexOf(symbol);
  const prevSymbol = idx > 0 ? orderedSymbols[idx - 1] : null;
  const nextSymbol =
    idx >= 0 && idx < orderedSymbols.length - 1 ? orderedSymbols[idx + 1] : null;

  // Metric registry — one chart per registered metric, in registry order.
  useEffect(() => {
    let cancelled = false;
    listLiquidityMetrics()
      .then((list) => {
        if (!cancelled) setMetrics(list);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Esc / arrows
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowLeft" && prevSymbol) {
        e.preventDefault();
        onSwitchSymbol(prevSymbol);
      } else if (e.key === "ArrowRight" && nextSymbol) {
        e.preventDefault();
        onSwitchSymbol(nextSymbol);
      } else if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [prevSymbol, nextSymbol, onSwitchSymbol, onClose]);

  // Heartbeat → keep WS subscription alive for this symbol.
  useEffect(() => {
    let cancelled = false;
    const beat = () => {
      heartbeatLiquidityActive(symbol).catch(() => {});
    };
    beat();
    const id = window.setInterval(() => {
      if (!cancelled) beat();
    }, HEARTBEAT_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [symbol]);

  return (
    <div className="fixed inset-0 z-50 bg-bg overflow-y-auto">
      {/* Header bar — coin nav left, window switcher center, close right */}
      <div className="sticky top-0 z-10 bg-bg/95 backdrop-blur border-b border-border px-6 py-3 flex items-center justify-between">
        <CoinCarousel
          symbol={symbol}
          orderedSymbols={orderedSymbols}
          onSwitchSymbol={onSwitchSymbol}
        />

        <div className="flex items-center gap-1">
          {WINDOWS.map((w) => (
            <button
              key={w}
              onClick={() => setWindowChoice(w)}
              className={`h-8 px-3 rounded-md border text-[11px] uppercase tracking-[0.22em] transition-colors ${
                windowChoice === w
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border text-muted hover:text-zinc-200 hover:border-accent/50"
              }`}
            >
              {w}
            </button>
          ))}
        </div>

        <button
          onClick={onClose}
          className="kz-btn h-8 w-8 inline-flex items-center justify-center rounded-md border border-border text-muted hover:text-zinc-200"
          title="Close (Esc)"
        >
          ✕
        </button>
      </div>

      <div className="px-6 py-6 space-y-6 max-w-[1280px] mx-auto">
        {/* ── Price (TradingView) ── */}
        <section>
          <div className="flex items-baseline gap-3 mb-3">
            <span className="text-[11px] uppercase tracking-[0.3em] text-muted">Price</span>
            <span className="text-zinc-300 text-sm">{displayName(symbol)} · Binance Futures</span>
          </div>
          <div
            className="rounded-xl overflow-hidden border border-border bg-bg/40"
            style={{
              boxShadow: "0 0 60px rgba(227, 208, 45, 0.10), 0 0 8px rgba(227, 208, 45, 0.05)",
            }}
          >
            <iframe
              key={`${symbol}-${windowChoice}`}
              src={tradingViewUrl(symbol, TV_INTERVAL[windowChoice])}
              title={`${symbol} price`}
              className="w-full"
              style={{ height: 360, border: 0, display: "block" }}
              allowTransparency
              scrolling="no"
              frameBorder={0}
            />
          </div>
        </section>

        {/* ── Cross-exchange validation ── */}
        <CrossExchangeSection symbol={symbol} />

        {/* ── Liquidity metrics ── */}
        <section>
          <div className="text-[11px] uppercase tracking-[0.3em] text-muted mb-3">
            Liquidity metrics
          </div>
          <div className="space-y-4">
            {metrics.length === 0 && (
              <div className="rounded-xl border border-border bg-bg/60 px-4 py-8 text-center text-xs text-muted">
                Loading metric registry…
              </div>
            )}
            {metrics.map((m) => (
              <MetricChartSection
                key={m.name}
                symbol={symbol}
                metric={m}
                window={windowChoice}
              />
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

// ── Coin carousel ──────────────────────────────────────────────────────────

function CoinCarousel({
  symbol,
  orderedSymbols,
  onSwitchSymbol,
}: {
  symbol: string;
  orderedSymbols: string[];
  onSwitchSymbol: (s: string) => void;
}) {
  const idx = orderedSymbols.indexOf(symbol);
  const prevSymbol = idx > 0 ? orderedSymbols[idx - 1] : null;
  const nextSymbol =
    idx >= 0 && idx < orderedSymbols.length - 1 ? orderedSymbols[idx + 1] : null;
  const nearPrev = idx > 0 ? displayName(orderedSymbols[idx - 1]) : "";
  const nearNext =
    idx >= 0 && idx < orderedSymbols.length - 1 ? displayName(orderedSymbols[idx + 1]) : "";
  return (
    <div className="flex items-center gap-3 min-w-0">
      <button
        type="button"
        onClick={() => prevSymbol && onSwitchSymbol(prevSymbol)}
        disabled={!prevSymbol}
        className="h-7 px-2 rounded-md border border-border text-muted hover:text-zinc-200 disabled:opacity-30 disabled:pointer-events-none"
        title={nearPrev ? `← ${nearPrev}` : ""}
      >
        ‹
      </button>
      <div className="text-zinc-100 font-bold tracking-[0.18em] text-lg">
        {displayName(symbol)}
      </div>
      <button
        type="button"
        onClick={() => nextSymbol && onSwitchSymbol(nextSymbol)}
        disabled={!nextSymbol}
        className="h-7 px-2 rounded-md border border-border text-muted hover:text-zinc-200 disabled:opacity-30 disabled:pointer-events-none"
        title={nearNext ? `${nearNext} →` : ""}
      >
        ›
      </button>
    </div>
  );
}

// ── Per-metric chart ───────────────────────────────────────────────────────
// Each metric manages its own fetch + (for WS metrics) incremental live
// poll. Isolating state per metric means a slow / failing one doesn't
// stall the others, and adding a new metric is just one more <MetricChartSection>.

function MetricChartSection({
  symbol,
  metric,
  window: windowChoice,
}: {
  symbol: string;
  metric: LiqMetricMeta;
  window: WindowChoice;
}) {
  const [series, setSeries] = useState<LiqMetricSeries | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const isRealtime = REALTIME_METRICS.has(metric.name);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getLiquidityMetricSeries(symbol, metric.name, windowChoice)
      .then((res) => {
        if (!cancelled) setSeries(res);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err?.message ?? "failed to load");
          setSeries(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, metric.name, windowChoice]);

  const seriesRef = useRef<LiqMetricSeries | null>(null);
  useEffect(() => {
    seriesRef.current = series;
  }, [series]);

  useEffect(() => {
    if (!isRealtime) return;
    const id = window.setInterval(async () => {
      const cur = seriesRef.current;
      const latest = cur?.samples?.length ? cur.samples[cur.samples.length - 1].ts : undefined;
      try {
        const incoming = await getLiquidityMetricSeries(symbol, metric.name, windowChoice, latest);
        if (!incoming.samples?.length) return;
        setSeries((prev) =>
          prev && prev.metric === incoming.metric
            ? { ...prev, samples: [...prev.samples, ...incoming.samples] }
            : incoming,
        );
      } catch {
        // transient
      }
    }, LIVE_POLL_MS);
    return () => window.clearInterval(id);
  }, [symbol, metric.name, windowChoice, isRealtime]);

  const metricPoints = useMemo(
    () => (series?.samples ?? []).map((s) => ({ ts: s.ts, value: s.value })),
    [series],
  );

  // Event markers derived from the loaded series itself — different rule
  // per metric: spikes for liq_stress, sign flips for OBI, threshold breaks
  // for spread / funding / oi_delta. Keeps the chart self-explanatory
  // without a new backend endpoint.
  const markers = useMemo<ChartMarker[]>(
    () => detectMarkers(metric.name, metricPoints),
    [metric.name, metricPoints],
  );

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-baseline gap-3">
          <span className="text-zinc-200 text-sm uppercase tracking-[0.2em]">{metric.label}</span>
          {isRealtime && (
            <span className="text-[9px] uppercase tracking-[0.18em] text-accent">live</span>
          )}
        </div>
      </div>
      {error && (
        <div
          className="rounded-xl border border-border bg-bg/60 px-4 py-3 text-xs"
          style={{ color: "rgba(214, 139, 139, 0.9)" }}
        >
          {error}
        </div>
      )}
      {!error && loading && series == null && (
        <div className="rounded-xl border border-border bg-bg/60 px-4 py-6 text-center text-xs text-muted">
          Loading…
        </div>
      )}
      {!error && series && (
        <StackedLineChart
          price={[]}
          metric={metricPoints}
          metricLabel={metric.label}
          height={200}
          markers={markers}
        />
      )}
    </div>
  );
}

// ── Cross-exchange validation strip ───────────────────────────────────────
// Compares the current symbol's Binance state against Bybit. Big
// divergences in mid-price / funding / OI are the classic anti-
// manipulation tell — a Binance-only spread blow-up is much more
// suspect than one mirrored on Bybit.

function CrossExchangeSection({ symbol }: { symbol: string }) {
  const [data, setData] = useState<CrossExResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    getCrossExchange(symbol)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e?.message ?? "failed"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [symbol]);

  return (
    <section>
      <div className="flex items-baseline gap-3 mb-3">
        <span className="text-[11px] uppercase tracking-[0.3em] text-muted">cross-exchange</span>
        <span className="text-zinc-300 text-sm">{data ? `${data.snapshots.length} venues · vs ${data.reference}` : ""}</span>
      </div>
      <div className="rounded-xl border border-border bg-bg/40 px-4 py-3">
        {loading && <div className="text-xs text-muted">Loading…</div>}
        {error && <div className="text-xs" style={{ color: "rgba(214, 139, 139, 0.9)" }}>{error}</div>}
        {data && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-[11px] font-mono">
            <div>
              <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-2">Snapshots</div>
              <table className="w-full">
                <thead>
                  <tr className="text-[9px] uppercase tracking-[0.14em] text-muted">
                    <th className="text-left py-1">EX</th>
                    <th className="text-right py-1">MID</th>
                    <th className="text-right py-1">SPREAD</th>
                    <th className="text-right py-1">FUND</th>
                    <th className="text-right py-1">OI</th>
                  </tr>
                </thead>
                <tbody>
                  {data.snapshots.map((s) => (
                    <tr key={s.exchange} className="border-t border-border/40">
                      <td className="py-1 uppercase">{s.exchange}</td>
                      <td className="py-1 text-right">{s.mid_price?.toFixed(s.mid_price && s.mid_price < 1 ? 6 : 3) ?? "—"}</td>
                      <td className="py-1 text-right">{s.spread_fraction != null ? `${(s.spread_fraction * 10_000).toFixed(1)}bps` : "—"}</td>
                      <td className="py-1 text-right">{s.funding_rate != null ? `${(s.funding_rate * 10_000).toFixed(2)}bps` : "—"}</td>
                      <td className="py-1 text-right">{s.open_interest_usd != null ? `$${formatBig(s.open_interest_usd)}` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div>
              <div className="text-[9px] uppercase tracking-[0.18em] text-muted mb-2">
                Divergences (vs {data.reference})
              </div>
              {data.divergences.length === 0 && (
                <div className="text-muted text-[10px]">no other venues responded</div>
              )}
              {data.divergences.map((d) => (
                <div key={d.exchange} className="space-y-1">
                  <div className="text-[10px] uppercase tracking-[0.12em] text-zinc-300">{d.exchange}</div>
                  <DivergenceRow label="Mid" value={d.mid_price_diff_pct} unit="%" warnAbs={0.15} />
                  <DivergenceRow label="Spread" value={d.spread_diff_pct} unit="%" warnAbs={50} />
                  <DivergenceRow label="Funding" value={d.funding_diff != null ? d.funding_diff * 10_000 : null} unit="bps" warnAbs={1} />
                  <DivergenceRow label="OI" value={d.oi_diff_pct} unit="%" warnAbs={10} />
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function DivergenceRow({ label, value, unit, warnAbs }: { label: string; value: number | null; unit: string; warnAbs: number }) {
  if (value == null || !Number.isFinite(value)) {
    return (
      <div className="flex items-center justify-between text-[10px]">
        <span className="text-muted">{label}</span>
        <span className="text-muted">—</span>
      </div>
    );
  }
  const warn = Math.abs(value) >= warnAbs;
  const color = warn
    ? Math.abs(value) >= warnAbs * 2
      ? "rgba(214, 75, 75, 0.95)"
      : "rgba(227, 180, 87, 0.95)"
    : "rgba(82, 185, 122, 0.9)";
  const sign = value > 0 ? "+" : "";
  return (
    <div className="flex items-center justify-between text-[10px]">
      <span className="text-muted">{label}</span>
      <span style={{ color }}>{sign}{value.toFixed(2)}{unit}</span>
    </div>
  );
}

function formatBig(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  return n.toFixed(2);
}

// ── Event-marker detectors ────────────────────────────────────────────────
// Per-metric: each picks out the moments that matter in that series so the
// chart reads as a timeline of events, not just a wiggly line. Pure
// client-side — no DB schema, no extra calls.

function detectMarkers(
  name: string,
  pts: { ts: number; value: number | null }[],
): ChartMarker[] {
  const filled = pts.filter((p) => p.value != null && Number.isFinite(p.value as number)) as
    { ts: number; value: number }[];
  if (filled.length < 3) return [];

  if (name === "liq_stress") {
    // Spikes: values in the top 5% AND ≥ 3× median, with at least 60s
    // between markers so a long burst doesn't smear into a row of dots.
    const sortedV = filled.map((p) => p.value).sort((a, b) => a - b);
    const p95 = sortedV[Math.floor(sortedV.length * 0.95)];
    const median = sortedV[Math.floor(sortedV.length * 0.5)] || 0;
    const thresh = Math.max(p95, median * 3, 1);
    const out: ChartMarker[] = [];
    let lastTs = 0;
    for (const p of filled) {
      if (p.value >= thresh && p.ts - lastTs > 60_000) {
        out.push({
          ts: p.ts,
          color: "rgba(214, 105, 105, 0.95)",
          label: `Liquidation spike: $${p.value.toFixed(0)}`,
          kind: "up",
        });
        lastTs = p.ts;
      }
    }
    return out;
  }

  if (name === "obi" || name === "obi_rt") {
    // Sign flips that cross at least ±0.1 — ignore micro-jitter near 0.
    const out: ChartMarker[] = [];
    let prevSign: number | null = null;
    let lastTs = 0;
    for (const p of filled) {
      const sign = p.value > 0.1 ? 1 : p.value < -0.1 ? -1 : 0;
      if (sign !== 0 && prevSign !== null && sign !== prevSign && p.ts - lastTs > 30_000) {
        out.push({
          ts: p.ts,
          color: sign > 0 ? "rgba(82, 185, 122, 0.95)" : "rgba(214, 105, 105, 0.95)",
          label: sign > 0 ? "OBI flip → bid-heavy" : "OBI flip → ask-heavy",
          kind: sign > 0 ? "up" : "down",
        });
        lastTs = p.ts;
      }
      if (sign !== 0) prevSign = sign;
    }
    return out;
  }

  if (name === "spread") {
    // Explosions: spread > p95 of the window AND > 5bps absolute.
    const sortedV = filled.map((p) => p.value).sort((a, b) => a - b);
    const p95 = sortedV[Math.floor(sortedV.length * 0.95)];
    const thresh = Math.max(p95, 0.0005);
    const out: ChartMarker[] = [];
    let lastTs = 0;
    for (const p of filled) {
      if (p.value >= thresh && p.ts - lastTs > 120_000) {
        out.push({
          ts: p.ts,
          color: "rgba(227, 180, 87, 0.95)",
          label: `Spread explosion: ${(p.value * 10000).toFixed(1)}bps`,
          kind: "up",
        });
        lastTs = p.ts;
      }
    }
    return out;
  }

  if (name === "funding" || name === "funding_z") {
    // Threshold crossings of |2σ| (z) or |5bps| (raw).
    const isZ = name === "funding_z";
    const abnormal = isZ ? 2 : 0.0005;
    const out: ChartMarker[] = [];
    let prevBucket: -1 | 0 | 1 | null = null;
    for (const p of filled) {
      const bucket = p.value > abnormal ? 1 : p.value < -abnormal ? -1 : 0;
      if (bucket !== 0 && bucket !== prevBucket) {
        out.push({
          ts: p.ts,
          color: bucket > 0 ? "rgba(214, 139, 105, 0.95)" : "rgba(140, 170, 235, 0.95)",
          label: bucket > 0 ? `Funding extreme → long-crowded (${p.value.toFixed(2)})` : `Funding extreme → short-crowded (${p.value.toFixed(2)})`,
          kind: bucket > 0 ? "up" : "down",
        });
      }
      prevBucket = bucket;
    }
    return out;
  }

  if (name.startsWith("oi_delta")) {
    // Surges: |Δ| > 1% (5m series) or > 5% (1h/24h series).
    const isShort = name === "oi_delta_5m";
    const thresh = isShort ? 1 : 5;
    const out: ChartMarker[] = [];
    let lastTs = 0;
    for (const p of filled) {
      if (Math.abs(p.value) >= thresh && p.ts - lastTs > 300_000) {
        out.push({
          ts: p.ts,
          color: p.value > 0 ? "rgba(82, 185, 122, 0.95)" : "rgba(214, 105, 105, 0.95)",
          label: `OI ${p.value > 0 ? "expansion" : "unwind"}: ${p.value.toFixed(2)}%`,
          kind: p.value > 0 ? "up" : "down",
        });
        lastTs = p.ts;
      }
    }
    return out;
  }

  return [];
}
