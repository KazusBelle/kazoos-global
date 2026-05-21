import { useEffect, useMemo, useRef, useState } from "react";
import {
  getLiquidityMetricSeries,
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

function displayName(symbol: string): string {
  let s = symbol.replace(/USDT$/, "");
  if (s.startsWith("1000")) s = s.slice(4);
  return s;
}

export function LiquidityChartModal({
  symbol,
  orderedSymbols,
  onSwitchSymbol,
  onClose,
}: Props) {
  const modalCardRef = useRef<HTMLDivElement>(null);
  const [metrics, setMetrics] = useState<LiqMetricMeta[]>([]);
  const [activeMetric, setActiveMetric] = useState<string | null>(null);
  const [windowChoice, setWindowChoice] = useState<WindowChoice>("24h");
  const [series, setSeries] = useState<LiqMetricSeries | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const idx = orderedSymbols.indexOf(symbol);
  const prevSymbol = idx > 0 ? orderedSymbols[idx - 1] : null;
  const nextSymbol =
    idx >= 0 && idx < orderedSymbols.length - 1 ? orderedSymbols[idx + 1] : null;
  const currentLabel = displayName(symbol);
  const nearPrevLabel = idx > 0 ? displayName(orderedSymbols[idx - 1]) : "";
  const farPrevLabel = idx > 1 ? displayName(orderedSymbols[idx - 2]) : "";
  const nearNextLabel =
    idx >= 0 && idx < orderedSymbols.length - 1 ? displayName(orderedSymbols[idx + 1]) : "";
  const farNextLabel =
    idx >= 0 && idx < orderedSymbols.length - 2 ? displayName(orderedSymbols[idx + 2]) : "";

  useEffect(() => {
    let cancelled = false;
    listLiquidityMetrics()
      .then((list) => {
        if (cancelled) return;
        setMetrics(list);
        if (list.length > 0 && activeMetric == null) {
          setActiveMetric(list[0].name);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err?.message ?? "failed to load metrics");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!activeMetric) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getLiquidityMetricSeries(symbol, activeMetric, windowChoice)
      .then((res) => {
        if (cancelled) return;
        setSeries(res);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err?.message ?? "failed to load series");
        setSeries(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, activeMetric, windowChoice]);

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

  const pricePoints = useMemo(
    () =>
      (series?.samples ?? [])
        .filter((s) => s.price != null)
        .map((s) => ({ ts: s.ts, value: s.price })),
    [series],
  );
  const metricPoints = useMemo(
    () =>
      (series?.samples ?? []).map((s) => ({ ts: s.ts, value: s.value })),
    [series],
  );

  const modalBg = "#18181b";
  const modalBorder = "#3f3f46";
  const subText = "#71717a";
  const modalText = "#f4f4f5";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm kz-modal-enter">
      <div
        ref={modalCardRef}
        className="border kz-modal-pop rounded-2xl p-4 w-[min(1080px,94vw)] max-h-[94vh] overflow-y-auto"
        style={{ background: modalBg, borderColor: modalBorder }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Coin carousel — mirrors OTE modal */}
        <div className="kz-modal-header">
          <div className="coinNav5" key={symbol}>
            <span className="sideCoin sideCoin-far truncate" title={farPrevLabel}>
              {farPrevLabel}
            </span>
            <button
              type="button"
              onClick={() => prevSymbol && onSwitchSymbol(prevSymbol)}
              disabled={!prevSymbol}
              className="kz-nav sideCoin sideCoin-near kz-coin-side h-6 w-[90px] justify-self-center truncate disabled:pointer-events-none disabled:opacity-0"
              title={nearPrevLabel ? `← ${nearPrevLabel}` : ""}
            >
              {nearPrevLabel}
            </button>
            <button
              type="button"
              onClick={() => prevSymbol && onSwitchSymbol(prevSymbol)}
              disabled={!prevSymbol}
              className="kz-nav arrow kz-coin-arrow h-7 w-[28px] justify-self-center disabled:pointer-events-none disabled:opacity-0"
              aria-label="Previous coin"
            >
              ‹
            </button>
            <span className="currentCoin kz-coin-active h-[36px] w-[130px] justify-self-center truncate">
              {currentLabel}
            </span>
            <button
              type="button"
              onClick={() => nextSymbol && onSwitchSymbol(nextSymbol)}
              disabled={!nextSymbol}
              className="kz-nav arrow kz-coin-arrow h-7 w-[28px] justify-self-center disabled:pointer-events-none disabled:opacity-0"
              aria-label="Next coin"
            >
              ›
            </button>
            <button
              type="button"
              onClick={() => nextSymbol && onSwitchSymbol(nextSymbol)}
              disabled={!nextSymbol}
              className="kz-nav sideCoin sideCoin-near kz-coin-side h-6 w-[90px] justify-self-center truncate disabled:pointer-events-none disabled:opacity-0"
              title={nearNextLabel ? `${nearNextLabel} →` : ""}
            >
              {nearNextLabel}
            </button>
            <span className="sideCoin sideCoin-far truncate" title={farNextLabel}>
              {farNextLabel}
            </span>
          </div>
        </div>

        {/* Toolbar: metric tabs left, window switcher + close right */}
        <div className="kz-unified-toolbar">
          <div className="flex gap-1">
            {metrics.map((m) => (
              <button
                key={m.name}
                onClick={() => setActiveMetric(m.name)}
                className={`kz-tab h-8 px-3 inline-flex items-center rounded-md text-[11px] uppercase tracking-[0.22em] ${
                  activeMetric === m.name ? "kz-tab-active" : ""
                }`}
                style={{
                  color: activeMetric === m.name ? modalText : subText,
                  background: activeMetric === m.name ? "rgba(63,63,70,0.55)" : "transparent",
                }}
              >
                {m.label}
              </button>
            ))}
          </div>

          <div className="kz-toolbar-actions">
            {WINDOWS.map((w) => (
              <button
                key={w}
                onClick={() => setWindowChoice(w)}
                className="kz-btn h-8 px-3 inline-flex items-center rounded-md border text-[11px] tracking-[0.22em] uppercase"
                style={{
                  borderColor: modalBorder,
                  color: windowChoice === w ? modalText : subText,
                  background: windowChoice === w ? "rgba(63,63,70,0.55)" : "transparent",
                }}
              >
                {w}
              </button>
            ))}
            <button
              onClick={onClose}
              className="kz-btn h-8 w-8 inline-flex items-center justify-center rounded-md border text-[11px]"
              style={{ borderColor: modalBorder, color: subText, background: "transparent" }}
              title="Close"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Chart */}
        <div>
          {error && (
            <div
              className="rounded-xl border border-border bg-bg/60 px-4 py-3 text-xs"
              style={{ color: "rgba(214, 139, 139, 0.9)" }}
            >
              {error}
            </div>
          )}
          {!error && loading && series == null && (
            <div className="rounded-xl border border-border bg-bg/60 px-4 py-8 text-center text-xs text-muted">
              Loading…
            </div>
          )}
          {!error && series && (
            <StackedLineChart
              price={pricePoints}
              metric={metricPoints}
              metricLabel={series.label}
            />
          )}
        </div>
      </div>
    </div>
  );
}
