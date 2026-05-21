import { useEffect, useMemo, useRef, useState } from "react";
import {
  getLiquidityMetricSeries,
  heartbeatLiquidityActive,
  listLiquidityMetrics,
  type LiqMetricMeta,
  type LiqMetricSeries,
} from "../lib/api";
import { StackedLineChart } from "./StackedLineChart";

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
        <StackedLineChart price={[]} metric={metricPoints} metricLabel={metric.label} height={200} />
      )}
    </div>
  );
}
