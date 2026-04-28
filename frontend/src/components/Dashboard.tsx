import { useEffect, useMemo, useRef, useState } from "react";
import {
  addCoin,
  getChart,
  getDashboard,
  movePin,
  removeCoin,
  setCall,
  setToken,
  togglePin,
  type AlertEvent,
  type CallTag,
  type ChartData,
  type DashboardResponse,
  type DashboardRow,
  type Snapshot,
} from "../lib/api";
import { ScreenerTable } from "./ScreenerTable";
import { normalizeSymbol, SymbolSuggestInput } from "./SymbolSuggestInput";
import { TDA } from "./TDA";

const POLL_MS = 15_000;
const DENSITY_KEY = "kazus_density";
const FEED_OPEN_KEY = "kazus_alert_feed_open";
const SIDEBAR_KEY = "kazus_sidebar_open";
const FONTSIZE_KEY = "kazus_fontsize";
const CHART_THEME_KEY = "kazus_chart_theme";
const SWAPPED_KEY = "kazus_tables_swapped";
const PAGE_KEY = "kazus_page";
const CHART_SYMBOL_KEY = "kazus_chart_symbol";
const CHART_TAB_KEY = "kazus_chart_tab";
const CHART_HEIGHT_KEY = "kazus_chart_height";
const GLOBAL_EXPANDED_KEY = "kazus_global_expanded";
const LOCAL_EXPANDED_KEY = "kazus_local_expanded";
const TABLES_EXPANDED_KEY = "kazus_tables_expanded";
const MOTION_KEY = "kazus_motion";

const FONT_SIZES = [9, 10, 11, 12, 13, 14, 16, 18, 20] as const;
type FontSizeValue = (typeof FONT_SIZES)[number];

const CHART_HEIGHT_MIN = 320;
const CHART_HEIGHT_MAX = 560;
const CHART_HEIGHT_DEFAULT = 380;

type Density = "cozy" | "compact";
type ChartTheme = "dark" | "light";
type Page = "ote" | "tda";

function displayName(symbol: string) {
  let s = symbol.replace(/USDT$/, "");
  if (s.startsWith("1000")) s = s.slice(4);
  return s;
}

function zoneLabel(zone?: string) {
  if (zone === "abnormal") return "ABNORMAL";
  return zone ?? "—";
}

// ── Icons ──────────────────────────────────────────────────────────────────

function FibIcon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
      <circle cx="12" cy="12" r="9.5" />
      <path d="M12 12 C16 9 17.5 5.5 14.5 3 C11 0 7 3 7 7 C7 11 10 13.5 13 14.5 C16.5 15.5 19 14 18.5 10.5" />
    </svg>
  );
}

function TDAIcon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <circle cx="4" cy="20" r="2.5" />
      <rect x="9" y="13" width="4.5" height="9" rx="1.5" />
      <rect x="15.5" y="7" width="4.5" height="15" rx="1.5" />
    </svg>
  );
}

// ── Chart Modal (lightweight-charts candlestick + Fibonacci) ────────────────

type ChartInterval = "1d" | "1h" | "15m";

const FVG_ENABLED_KEY = "kazus_fvg_enabled";
const FVG_LIMIT_KEY = "kazus_fvg_limit";
const FVG_DEFAULT_LIMIT = 6;

const CHART_THEMES = {
  dark: {
    bg: "#171717",
    textColor: "#a1a1aa",
    gridColor: "transparent",
    borderColor: "#3f3f46",
    bullColor: "#86898e",
    bullBorder: "#2e2e2e",
    bearColor: "#505757",
    bearBorder: "#2e2e2e",
    wickColor: "#505757",
    loadingBg: "#171717",
  },
  light: {
    bg: "#b2b5be",
    textColor: "#374151",
    gridColor: "transparent",
    borderColor: "#cbd5e1",
    bullColor: "#9598a1",
    bullBorder: "#000000",
    bearColor: "#434651",
    bearBorder: "#000000",
    wickColor: "#000000",
    loadingBg: "#b2b5be",
  },
} as const;

const SVG_NS = "http://www.w3.org/2000/svg";
const RIGHT_PADDING_BARS = 30; // empty bars on the right (initial fallback only — pixel-based logic overrides)
// OTE zone: a fixed-width vertical band right of candles, painted with chart bg
// to physically mask the candle area underneath. Layout from chart-edge of zone:
//   [INNER_LEFT_PAD] [FIB_LINE_W] [FIB_LABEL_GAP_W] [labels ~36] [INNER_RIGHT_PAD]
const FIB_LINE_W = 120;          // fib line segment length in px
const FIB_LABEL_GAP_W = 20;      // gap between line end and label start
const OTE_ZONE_INNER_LEFT_PAD = 8;
const OTE_ZONE_INNER_RIGHT_PAD = 16;
const OTE_LABEL_RESERVE_W = 36;  // reserved width for the longest label "0.618"
const OTE_ZONE_WIDTH =
  OTE_ZONE_INNER_LEFT_PAD + FIB_LINE_W + FIB_LABEL_GAP_W + OTE_LABEL_RESERVE_W + OTE_ZONE_INNER_RIGHT_PAD;
const OTE_TO_CANDLES_GAP_PX = 24; // visible gap between candles' last bar and OTE zone left edge
const FIB_ANCHOR_INSET_PX = 24; // keep 0.0/1.0 visible when their prices are off-screen
const SWING_OFFSCREEN = -9999;
const CANDLE_STROKE_COLOR = "#2e2e2e";
const LIGHT_CANDLE_STROKE_COLOR = "#000000";
const CURRENT_PRICE_COLOR = "#056656";
const MIN_VISIBLE_BARS = 50;
// Limit is left undefined so the backend uses its per-interval default
// (d1=500, h1=900, 15m=600) — matches the worker's compute.py exactly so
// the chart's engine state is identical to the screener table.

const chartDataCache = new Map<string, ChartData>();
const chartDataInFlight = new Map<string, Promise<ChartData>>();

function chartCacheKey(symbol: string, interval: string) {
  return `${symbol.toUpperCase()}|${interval}`;
}

async function getChartCached(symbol: string, interval: string) {
  const key = chartCacheKey(symbol, interval);
  const cached = chartDataCache.get(key);
  if (cached) return cached;

  const inFlight = chartDataInFlight.get(key);
  if (inFlight) return inFlight;

  const req = getChart(symbol, interval)
    .then((data) => {
      chartDataCache.set(key, data);
      chartDataInFlight.delete(key);
      return data;
    })
    .catch((err) => {
      chartDataInFlight.delete(key);
      throw err;
    });

  chartDataInFlight.set(key, req);
  return req;
}

function prefetchChart(symbol: string, interval: string) {
  void getChartCached(symbol, interval).catch(() => {
    // best-effort prefetch
  });
}

function decimalPlaces(value: number): number {
  if (!Number.isFinite(value)) return 0;
  const s = value.toString();
  if (s.includes("e-")) {
    const p = Number(s.split("e-")[1]);
    return Number.isFinite(p) ? Math.min(8, p) : 0;
  }
  const idx = s.indexOf(".");
  if (idx < 0) return 0;
  return Math.min(8, s.length - idx - 1);
}

function inferPricePrecision(
  bars: ReadonlyArray<{ open: number; high: number; low: number; close: number }>
): number {
  let p = 2;
  for (const b of bars) {
    p = Math.max(
      p,
      decimalPlaces(b.open),
      decimalPlaces(b.high),
      decimalPlaces(b.low),
      decimalPlaces(b.close)
    );
  }
  return Math.min(8, Math.max(2, p));
}

// Default visible-bar count when there is no active fib swing to frame.
// Chosen so candles are wide enough not to look "squashed" while still
// giving the user a few weeks/days of context.
const DEFAULT_FALLBACK_VISIBLE_BARS = 120;
const TARGET_CANDLE_HEIGHT_RATIO = 0.70;

type ChartPriceRange = { minValue: number; maxValue: number };

// When there's no fib, frame the most recent N bars instead of fitContent
// (which would show all 500–900 bars at hair-thin widths).
function defaultFallbackRange(
  chart: any,
  candleData: ReadonlyArray<{ time: any; high: number; low: number }>,
) {
  if (candleData.length === 0) {
    chart.timeScale().fitContent();
    return;
  }
  const target = Math.min(candleData.length, DEFAULT_FALLBACK_VISIBLE_BARS);
  const fromIdx = Math.max(0, candleData.length - target);
  try {
    chart.timeScale().setVisibleLogicalRange({
      from: fromIdx,
      to: candleData.length - 1 + RIGHT_PADDING_BARS,
    });
  } catch {
    chart.timeScale().fitContent();
  }
}

// Pick an initial visible time range that puts the active fib swing on screen
// with breathing room on both sides. Run when the modal first opens (no saved
// range) so the OTE zone is immediately framed.
function defaultFitToFib(
  chart: any,
  candleData: ReadonlyArray<{ time: any; high: number; low: number }>,
  data: { fib_high: number | null; fib_low: number | null },
) {
  if (data.fib_high == null || data.fib_low == null || candleData.length === 0) {
    defaultFallbackRange(chart, candleData);
    return;
  }
  // Find the first bar whose high reaches fib_high or low reaches fib_low —
  // that's where the swing begins. Walk from oldest to newest.
  const tol = (data.fib_high - data.fib_low) * 0.001 || 1e-6;
  let swingStart = -1;
  for (let k = 0; k < candleData.length; k++) {
    const b = candleData[k];
    if (
      Math.abs(b.high - data.fib_high) <= tol ||
      Math.abs(b.low - data.fib_low) <= tol
    ) {
      swingStart = k;
      break;
    }
  }
  if (swingStart < 0) {
    defaultFallbackRange(chart, candleData);
    return;
  }
  // Show 25% extra context to the left of the swing and the right edge.
  const span = candleData.length - 1 - swingStart;
  const padLeft = Math.max(8, Math.floor(span * 0.25));
  const fromIdx = Math.max(0, swingStart - padLeft);
  try {
    chart.timeScale().setVisibleLogicalRange({
      from: fromIdx,
      to: candleData.length - 1 + RIGHT_PADDING_BARS,
    });
  } catch {
    defaultFallbackRange(chart, candleData);
  }
}

function padRangeToHeight(
  minValue: number,
  maxValue: number,
  fallbackPrice: number,
): ChartPriceRange | null {
  let low = minValue;
  let high = maxValue;
  if (!Number.isFinite(low) || !Number.isFinite(high)) return null;
  if (high < low) {
    const tmp = high;
    high = low;
    low = tmp;
  }

  let range = high - low;
  if (!Number.isFinite(range) || range <= 0) {
    const basis = Math.max(Math.abs(fallbackPrice), Math.abs(high), 1);
    range = basis * 0.002;
    low -= range / 2;
    high += range / 2;
  }

  const pad = range * ((1 - TARGET_CANDLE_HEIGHT_RATIO) / 2 / TARGET_CANDLE_HEIGHT_RATIO);
  return {
    minValue: low - pad,
    maxValue: high + pad,
  };
}

function getBullishOteZone(
  data: { fib_high: number | null; fib_low: number | null; fib_direction?: string },
): { low: number; high: number } | null {
  if (
    data.fib_direction !== "bullish" ||
    data.fib_high == null ||
    data.fib_low == null ||
    data.fib_high <= data.fib_low
  ) {
    return null;
  }

  const fibRange = data.fib_high - data.fib_low;
  const oteA = data.fib_high - fibRange * 0.618;
  const oteB = data.fib_high - fibRange * 0.786;
  return {
    low: Math.min(oteA, oteB),
    high: Math.max(oteA, oteB),
  };
}

function isOteUsableForCandles(
  candleData: ReadonlyArray<{ high: number; low: number; close: number }>,
  data: { fib_high: number | null; fib_low: number | null; fib_direction?: string },
): boolean {
  const oteZone = getBullishOteZone(data);
  if (oteZone == null || candleData.length === 0) return false;

  const recent = candleData.slice(-DEFAULT_FALLBACK_VISIBLE_BARS);
  let candleLow = Infinity;
  let candleHigh = -Infinity;
  for (const b of recent) {
    candleLow = Math.min(candleLow, b.low);
    candleHigh = Math.max(candleHigh, b.high);
  }
  if (!Number.isFinite(candleLow) || !Number.isFinite(candleHigh)) return false;

  const candleRange = Math.max(candleHigh - candleLow, Math.abs(recent[recent.length - 1].close) * 0.002, 1e-9);
  const tolerance = candleRange * 1.2;
  return oteZone.high >= candleLow - tolerance && oteZone.low <= candleHigh + tolerance;
}

function makeAutoscaleInfoProvider(
  data: { fib_high: number | null; fib_low: number | null; fib_direction?: string },
) {
  return (baseImplementation: () => { priceRange: ChartPriceRange | null; margins?: any } | null) => {
    const base = baseImplementation();
    const baseRange = base?.priceRange;
    if (baseRange == null) return base;

    const candleLow = baseRange.minValue;
    const candleHigh = baseRange.maxValue;
    const candleRange = Math.max(candleHigh - candleLow, Math.abs(candleHigh) * 0.002, 1e-9);
    let minValue = candleLow;
    let maxValue = candleHigh;

    const oteZone = getBullishOteZone(data);
    if (oteZone != null) {
      const tolerance = candleRange * 1.2;
      const nearCandles =
        oteZone.high >= candleLow - tolerance &&
        oteZone.low <= candleHigh + tolerance;

      if (nearCandles) {
        minValue = Math.min(minValue, oteZone.low);
        maxValue = Math.max(maxValue, oteZone.high);
      }
    }

    const padded = padRangeToHeight(minValue, maxValue, (candleHigh + candleLow) / 2);
    return padded == null
      ? base
      : {
          priceRange: padded,
          margins: { above: 0, below: 0 },
        };
  };
}

function CandleChart({
  symbol,
  interval,
  theme,
  chartHeight,
  fvgEnabled,
  fvgLimit,
}: {
  symbol: string;
  interval: ChartInterval;
  theme: ChartTheme;
  chartHeight: number;
  fvgEnabled: boolean;
  fvgLimit: number;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<SVGSVGElement>(null);
  const fibLabelLayerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"loading" | "ok" | "error">("loading");
  const [err, setErr] = useState<string | null>(null);
  const t = CHART_THEMES[theme];

  // Live refs — written by props-sync effects, read by the rAF loop each frame
  // so toggling FVG visibility never forces a chart remount.
  const fvgEnabledRef = useRef(fvgEnabled);
  const fvgLimitRef = useRef(fvgLimit);

  useEffect(() => { fvgEnabledRef.current = fvgEnabled; }, [fvgEnabled]);
  useEffect(() => { fvgLimitRef.current = fvgLimit; }, [fvgLimit]);

  type FvgEl = {
    ts: number;
    end_ts: number;
    top: number;
    bottom: number;
    rect: SVGRectElement;
  };
  const fvgElementsRef = useRef<FvgEl[]>([]);

  useEffect(() => {
    let destroyed = false;
    let chart: any;
    let candles: any;
    let livePriceLine: any;
    let rafId = 0;
    let initialSyncRafIds: number[] = [];
    let initialSyncTimerIds: number[] = [];

    const init = async () => {
      if (destroyed || !containerRef.current) return;
      try {
        setStatus("loading");
        setErr(null);

        const { createChart, ColorType, CandlestickSeries, CrosshairMode } = await import("lightweight-charts");
        const data = await getChartCached(symbol, interval);
        if (destroyed || !containerRef.current) return;

        const candleData = data.bars.slice(0, -1).map((b) => ({
          time: Math.floor(b.ts / 1000) as any,
          open: b.open,
          high: b.high,
          low: b.low,
          close: b.close,
          borderColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : CANDLE_STROKE_COLOR,
          wickColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : CANDLE_STROKE_COLOR,
        }));
        const fibActive =
          data.fib_high != null && data.fib_low != null && data.fib_high > data.fib_low;
        const hasBullishFib = fibActive && data.fib_direction === "bullish";
        const hasInitialOte = isOteUsableForCandles(candleData, data);
        const autoscaleOverride = makeAutoscaleInfoProvider(data);
        const pricePrecision = inferPricePrecision(data.bars);
        const minMove = 10 ** (-pricePrecision);

        chart = createChart(containerRef.current, {
          autoSize: true,
          height: chartHeight,
          layout: {
            background: { type: ColorType.Solid, color: t.bg },
            textColor: t.textColor,
          },
          handleScale: {
            mouseWheel: true,
            pinch: true,
            axisPressedMouseMove: { price: true, time: true },
            axisDoubleClickReset: true,
          },
          handleScroll: {
            mouseWheel: true,
            pressedMouseMove: true,
            horzTouchDrag: true,
            vertTouchDrag: true,
          },
          crosshair: {
            mode: CrosshairMode.Normal, // no "magnet" snap to candle points
            vertLine: {
              color: theme === "dark" ? "rgba(255,255,255,0.2)" : "rgba(32,32,32,0.2)",
              labelBackgroundColor: theme === "dark" ? "rgba(46,46,46,0.2)" : "rgba(110,110,110,0.2)",
            },
            horzLine: {
              color: theme === "dark" ? "rgba(255,255,255,0.2)" : "rgba(32,32,32,0.2)",
              labelBackgroundColor: theme === "dark" ? "rgba(46,46,46,0.2)" : "rgba(110,110,110,0.2)",
            },
          },
          grid: {
            vertLines: { visible: false, color: t.gridColor },
            horzLines: { visible: false, color: t.gridColor },
          },
          rightPriceScale: {
            visible: true,
            borderColor: t.borderColor,
            minimumWidth: 76,
            ticksVisible: true,
            scaleMargins: { top: 0, bottom: 0 },
          },
          timeScale: {
            visible: true,
            borderColor: t.borderColor,
            timeVisible: true,
            secondsVisible: false,
            ticksVisible: true,
            rightOffset: 46,
          },
        });

        candles = chart.addSeries(
          CandlestickSeries,
          {
            ...(theme === "light"
              ? {
                  upColor: "#9598a1",
                  downColor: "#434651",
                }
              : {
                  upColor: t.bullColor,
                  downColor: t.bearColor,
                }),
            borderVisible: true,
            borderUpColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : CANDLE_STROKE_COLOR,
            borderDownColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : CANDLE_STROKE_COLOR,
            wickVisible: true,
            wickUpColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : CANDLE_STROKE_COLOR,
            wickDownColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : CANDLE_STROKE_COLOR,
            // Hide horizontal last-price line, keep native price-scale label.
            priceLineVisible: false,
            lastValueVisible: false,
            priceFormat: {
              type: "price",
              precision: pricePrecision,
              minMove,
            },
            autoscaleInfoProvider: autoscaleOverride,
          }
        );

        // Custom autoscale pads the active visible price range to ~70% height.
        // With scaleMargins at 0/0, the math is not compounded by chart margins.
        try {
          candles.priceScale().applyOptions({
            scaleMargins: { top: 0, bottom: 0 },
            autoScale: true,
          });
        } catch { /* older builds */ }

        candles.setData(candleData);
        if (candleData.length > 0) {
          livePriceLine = candles.createPriceLine({
            price: candleData[candleData.length - 1].close,
            color: CURRENT_PRICE_COLOR,
            lineVisible: false,
            axisLabelVisible: true,
            axisLabelColor: CURRENT_PRICE_COLOR,
            axisLabelTextColor: "#e7fffb",
          });
        }

        const rangeKey = `kazus_chart_range_${symbol}_${interval}`;
        const savedRange = localStorage.getItem(rangeKey);
        const haveFib = hasBullishFib && hasInitialOte && candleData.length > 0;
        if (savedRange) {
          try {
            chart.timeScale().setVisibleRange(JSON.parse(savedRange));
          } catch {
            if (haveFib) defaultFitToFib(chart, candleData, data);
            else defaultFallbackRange(chart, candleData);
          }
        } else if (haveFib) {
          defaultFitToFib(chart, candleData, data);
        } else {
          defaultFallbackRange(chart, candleData);
        }

        // Pixel-based right padding. Empty bars are not enough at low zoom
        // (1 px/bar → 30 bars = 30 px, candles overlap the OTE zone). Compute
        // padding in PIXELS (OTE zone width + visible gap), translate to logical
        // bars, shift the visible range so the last candle sits left of the OTE
        // zone with breathing room. Re-runs on every range change behind a
        // recursion guard so user pan/zoom can't push candles into OTE.
        let isAdjustingPadding = false;
        const enforceRightPadding = () => {
          if (isAdjustingPadding) return;
          try {
            if (!containerRef.current) return;
            let psWidth = 60;
            try { psWidth = chart.priceScale("right").width() || 60; } catch { /* pre-render */ }
            const plotRight = containerRef.current.clientWidth - psWidth;
            if (!Number.isFinite(plotRight) || plotRight <= 0) return;

            const lr = chart.timeScale().getVisibleLogicalRange?.();
            if (!lr) return;
            const from = Number(lr.from);
            const to = Number(lr.to);
            if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) return;

            const span = to - from;
            const barSpacing = plotRight / span;
            if (!Number.isFinite(barSpacing) || barSpacing <= 0) return;

            const desiredPaddingPx = OTE_ZONE_WIDTH + OTE_TO_CANDLES_GAP_PX;
            const desiredPaddingBars = desiredPaddingPx / barSpacing;
            const minTo = (candleData.length - 1) + desiredPaddingBars;

            if (to < minTo - 0.5) {
              isAdjustingPadding = true;
              try {
                chart.timeScale().setVisibleLogicalRange({
                  from: minTo - span,
                  to: minTo,
                });
              } finally {
                isAdjustingPadding = false;
              }
            }
          } catch { /* noop */ }
        };
        enforceRightPadding();

        // X-axis guardrail: keep at least 50 REAL data candles visible inside
        // the plot area (excluding right empty offset zone).
        const enforceMinVisibleBars = () => {
          try {
            if (!containerRef.current) return;
            let psWidth = 60;
            try { psWidth = chart.priceScale("right").width() || 60; } catch { /* pre-render */ }
            const plotRight = containerRef.current.clientWidth - psWidth;
            if (!Number.isFinite(plotRight) || plotRight <= 0) return;

            let visibleDataBars = 0;
            for (const b of candleData) {
              let x: number | null = null;
              try {
                const v = chart.timeScale().timeToCoordinate(b.time);
                x = v == null || !Number.isFinite(Number(v)) ? null : Number(v);
              } catch {
                x = null;
              }
              if (x != null && x >= 0 && x <= plotRight) visibleDataBars++;
            }
            if (visibleDataBars >= MIN_VISIBLE_BARS) return;

            const lr = chart.timeScale().getVisibleLogicalRange?.();
            if (!lr) return;
            const from = Number(lr.from);
            const to = Number(lr.to);
            if (!Number.isFinite(from) || !Number.isFinite(to)) return;

            const deficit = MIN_VISIBLE_BARS - visibleDataBars;
            const pad = Math.max(2, deficit + 2);
            chart.timeScale().setVisibleLogicalRange({
              from: from - pad,
              to,
            });
          } catch {
            // noop
          }
        };
        enforceMinVisibleBars();

        chart.timeScale().subscribeVisibleTimeRangeChange((range: any) => {
          if (range && !destroyed) {
            try { localStorage.setItem(rangeKey, JSON.stringify(range)); } catch { /* full */ }
          }
        });
        chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
          if (!destroyed) {
            enforceMinVisibleBars();
            enforceRightPadding();
          }
        });

        // ── Imperative SVG overlay ────────────────────────────────────────
        const svg = overlayRef.current;
        if (!svg) return;
        while (svg.firstChild) svg.removeChild(svg.firstChild);
        const fibLabelLayer = fibLabelLayerRef.current;
        if (fibLabelLayer) fibLabelLayer.innerHTML = "";

        const isLocal = interval === "1h";
        const isBullish = hasBullishFib && data.fib_high != null && data.fib_low != null;
        const halfColor = isLocal ? "#5c7bd5" : "#752727";

        // ── FVG rectangles — ALL created now, visibility controlled per-frame
        // by the rAF loop reading fvgEnabledRef / fvgLimitRef. This lets the
        // user toggle FVGs without any chart remount.
        const allFvgs = data.fvgs ?? [];
        const newFvgElements: FvgEl[] = [];
        for (const fvg of allFvgs) {
          const fill =
            fvg.kind === "bullish"
              ? "rgba(38, 166, 154, 0.18)"
              : "rgba(117, 39, 39, 0.24)";
          const rect = document.createElementNS(SVG_NS, "rect");
          rect.setAttribute("fill", fill);
          rect.setAttribute("stroke", fill);
          rect.setAttribute("stroke-width", "0.5");
          svg.appendChild(rect);
          newFvgElements.push({
            ts: fvg.ts,
            end_ts: fvg.end_ts,
            top: fvg.top,
            bottom: fvg.bottom,
            rect,
          });
        }
        fvgElementsRef.current = newFvgElements;

        // ── Fib lines — solid, no dashes; labels as plain colored text (no
        // colored rect background) positioned in the price-scale gutter.
        type FibEl = {
          ratio: number;
          color: string;
          label: string;
          anchor: boolean;
          group: SVGGElement;
          line: SVGLineElement;
          text: SVGTextElement;
        };
        const fibElements: FibEl[] = [];
        const fibBlockGroup = document.createElementNS(SVG_NS, "g");
        fibBlockGroup.setAttribute("data-layer", "ote-zone");
        // Solid mask rect: physically separates the OTE zone from the candle
        // plot. Painted with chart bg so candles drawn underneath are hidden.
        // Sized in the rAF loop to match plot height (excluding time axis).
        const oteZoneBg = document.createElementNS(SVG_NS, "rect");
        oteZoneBg.setAttribute("data-ote-bg", "true");
        oteZoneBg.setAttribute("x", "0");
        oteZoneBg.setAttribute("y", "0");
        oteZoneBg.setAttribute("width", String(OTE_ZONE_WIDTH));
        oteZoneBg.setAttribute("fill", t.bg);
        fibBlockGroup.appendChild(oteZoneBg);
        svg.appendChild(fibBlockGroup);

        if (isBullish) {
          const fibConfig = [
            { ratio: 0.0,   color: "#787b86", label: "0.0",   anchor: true },
            { ratio: 0.5,   color: halfColor, label: "0.5",   anchor: false },
            { ratio: 0.618, color: "#056656", label: "0.618", anchor: false },
            { ratio: 0.705, color: "#056656", label: "0.705", anchor: false },
            { ratio: 0.786, color: "#056656", label: "0.786", anchor: false },
            { ratio: 1.0,   color: "#787b86", label: "1.0",   anchor: true },
          ];
          for (const f of fibConfig) {
            const g = document.createElementNS(SVG_NS, "g");
            g.setAttribute("data-fib-ratio", f.label);
            g.setAttribute("data-fib-anchor", f.anchor ? "true" : "false");
            const line = document.createElementNS(SVG_NS, "line");
            line.setAttribute("data-fib-line", f.label);
            line.setAttribute("stroke", f.color);
            line.setAttribute("stroke-width", "1.2");
            line.setAttribute("stroke-linecap", "round");
            line.setAttribute("vector-effect", "non-scaling-stroke");
            const text = document.createElementNS(SVG_NS, "text");
            text.setAttribute("data-fib-text", f.label);
            text.setAttribute("text-anchor", "start");
            text.setAttribute("dominant-baseline", "middle");
            text.setAttribute("font-size", "12");
            text.setAttribute("font-weight", "600");
            text.setAttribute("fill", f.color);
            text.setAttribute("font-family", "monospace");
            text.setAttribute("paint-order", "stroke fill");
            text.setAttribute("stroke", "#0a0a0a");
            text.setAttribute("stroke-width", "2.5");
            text.setAttribute("stroke-linejoin", "round");
            text.setAttribute("opacity", "1");
            text.textContent = f.label;

            g.appendChild(line);
            g.appendChild(text);
            fibBlockGroup.appendChild(g);
            fibElements.push({ ...f, group: g, line, text });
          }
        }

        let placeholder: HTMLDivElement | null = null;
        if (!isBullish && data.fib_direction === "bearish") {
          placeholder = document.createElement("div");
          placeholder.textContent = "downtrend";
          placeholder.style.position = "absolute";
          placeholder.style.left = "0px";
          placeholder.style.top = "0px";
          placeholder.style.transform = "translate(-9999px,-9999px)";
          placeholder.style.width = "120px";
          placeholder.style.textAlign = "center";
          placeholder.style.fontFamily = "monospace";
          placeholder.style.fontSize = "12px";
          placeholder.style.fontWeight = "700";
          placeholder.style.lineHeight = "1";
          placeholder.style.whiteSpace = "nowrap";
          placeholder.style.color = "#71717a";
          placeholder.style.textTransform = "uppercase";
          placeholder.style.letterSpacing = "0.16em";
          placeholder.style.opacity = "0";
          fibLabelLayer?.appendChild(placeholder);
        }

        // Swing markers — HH/HL/LL/LH tags. All swing groups live in a single
        // container appended LAST to the SVG so they always sit on top of the
        // FVG rectangles and the fib lines (otherwise the dark fib/FVG fills
        // can blend with the dark tag fills and visually swallow the labels).
        const swingLayer = document.createElementNS(SVG_NS, "g");
        swingLayer.setAttribute("data-layer", "swings");
        type SwingEl = {
          ts: number;
          price: number;
          label: string;
          isHigh: boolean;
          tag: SVGRectElement;
          tagText: SVGTextElement;
          stem: SVGLineElement;
          dot: SVGCircleElement;
          group: SVGGElement;
        };
        const swingElements: SwingEl[] = [];
        const swingStyles = {
          bullish: { fill: "#2a3d27", stroke: "#7fb678" },
          bearish: { fill: "#752727", stroke: "#d68b8b" },
        } as const;
        for (const sw of data.swings) {
          const isHigh = sw.label === "HH" || sw.label === "LH";
          const bullishTag = sw.label === "HH" || sw.label === "HL";
          const style = bullishTag ? swingStyles.bullish : swingStyles.bearish;
          const g = document.createElementNS(SVG_NS, "g");
          const stem = document.createElementNS(SVG_NS, "line");
          stem.setAttribute("stroke", style.stroke);
          stem.setAttribute("stroke-width", "1");
          stem.setAttribute("opacity", "0.7");
          // Small dot AT the swing price so the point is visible even when
          // the tag rect is offset away from the candle wick.
          const dot = document.createElementNS(SVG_NS, "circle");
          dot.setAttribute("r", "2.5");
          dot.setAttribute("fill", style.stroke);
          dot.setAttribute("stroke", "#0a0a0a");
          dot.setAttribute("stroke-width", "0.6");
          const tag = document.createElementNS(SVG_NS, "rect");
          tag.setAttribute("width", "22");
          tag.setAttribute("height", "13");
          tag.setAttribute("rx", "2");
          tag.setAttribute("fill", style.fill);
          tag.setAttribute("stroke", style.stroke);
          tag.setAttribute("stroke-width", "1");
          const tagText = document.createElementNS(SVG_NS, "text");
          tagText.setAttribute("text-anchor", "middle");
          tagText.setAttribute("font-size", "9");
          tagText.setAttribute("font-family", "monospace");
          tagText.setAttribute("font-weight", "600");
          tagText.setAttribute("fill", "#ffffff");
          tagText.textContent = sw.label;
          g.appendChild(stem);
          g.appendChild(dot);
          g.appendChild(tag);
          g.appendChild(tagText);
          swingLayer.appendChild(g);
          swingElements.push({ ts: sw.ts, price: sw.price, label: sw.label, isHigh, tag, tagText, stem, dot, group: g });
        }
        svg.appendChild(swingLayer);

        const safePriceY = (price: number): number | null => {
          try {
            const v = candles.priceToCoordinate(price);
            return v == null || !Number.isFinite(Number(v)) ? null : Number(v);
          } catch { return null; }
        };
        const safeTimeX = (tsMs: number): number | null => {
          try {
            const v = chart.timeScale().timeToCoordinate(Math.floor(tsMs / 1000));
            return v == null || !Number.isFinite(Number(v)) ? null : Number(v);
          } catch { return null; }
        };

        const update = () => {
          try {
            if (destroyed || !chart || !candles || !overlayRef.current || !containerRef.current) return;
            const w = containerRef.current.clientWidth;
            const h = chartHeight;
            if (w <= 0) return;

            let psWidth = 60;
            try { psWidth = chart.priceScale("right").width() || 60; } catch { /* pre-render */ }
            const plotRight = w - psWidth;
            overlayRef.current.setAttribute("viewBox", `0 0 ${w} ${h}`);
            let timeScaleH = 0;
            try {
              timeScaleH = Number(chart.timeScale().height?.() ?? 0) || 0;
            } catch { /* noop */ }
            const plotBottom = Math.max(0, h - timeScaleH);
            // Layout: candles span [0, oteZoneLeft]; OTE zone [oteZoneLeft, plotRight];
            // price scale gutter [plotRight, w]. The OTE zone bg-rect (inside
            // fibBlockGroup) physically masks candles in [oteZoneLeft, plotRight].
            const oteZoneLeft = Math.max(0, plotRight - OTE_ZONE_WIDTH);
            // Clip overlay so FVG/fib don't bleed into the price-scale gutter
            // or onto the time axis. Swing layer clips to the same rect — tags
            // are pre-clamped below to stay inside.
            const clip = `inset(0px ${Math.max(0, w - plotRight)}px ${Math.max(0, timeScaleH)}px 0px)`;
            overlayRef.current.style.clipPath = clip;
            if (fibLabelLayer) fibLabelLayer.style.clipPath = clip;
            if (candleData.length > 0 && livePriceLine) {
              try {
                livePriceLine.applyOptions({ price: candleData[candleData.length - 1].close });
              } catch { /* noop */ }
            }

            // Position the OTE zone group at oteZoneLeft. The bg rect inside
            // (oteZoneBg) tracks plot height every frame. Hide the mask when
            // there's no fib content — no need to reserve a zone.
            const showOteZone = isBullish || data.fib_direction === "bearish";
            fibBlockGroup.setAttribute("transform", `translate(${oteZoneLeft}, 0)`);
            oteZoneBg.setAttribute("height", String(plotBottom));
            oteZoneBg.setAttribute("opacity", showOteZone ? "1" : "0");

            if (isBullish) {
              const fibHigh = data.fib_high!;
              const fibLow = data.fib_low!;
              const range = fibHigh - fibLow;
              const lineX1 = OTE_ZONE_INNER_LEFT_PAD;
              const lineX2 = OTE_ZONE_INNER_LEFT_PAD + FIB_LINE_W;
              const labelX = lineX2 + FIB_LABEL_GAP_W;
              for (const f of fibElements) {
                const rawY = safePriceY(fibHigh - f.ratio * range);
                if (rawY == null || rawY < 0 || rawY > plotBottom) {
                  f.group.setAttribute("opacity", "0");
                  continue;
                }
                const y = rawY;
                f.group.setAttribute("opacity", "1");
                f.line.setAttribute("x1", String(lineX1));
                f.line.setAttribute("x2", String(lineX2));
                f.line.setAttribute("y1", String(y));
                f.line.setAttribute("y2", String(y));
                f.text.setAttribute("x", String(labelX));
                f.text.setAttribute("y", String(y));
              }
            }

            if (placeholder) {
              const labelW = Math.max(120, OTE_ZONE_WIDTH - OTE_ZONE_INNER_LEFT_PAD - OTE_ZONE_INNER_RIGHT_PAD);
              placeholder.style.width = `${labelW}px`;
              placeholder.style.opacity = "1";
              placeholder.style.transform = `translate(${oteZoneLeft + OTE_ZONE_INNER_LEFT_PAD}px, ${Math.max(10, h / 2 - 6)}px)`;
            }

            // FVG visibility is driven by live refs so toggle is instant
            const fvgOn = fvgEnabledRef.current;
            const fvgLim = fvgLimitRef.current;
            const fvgAll = fvgElementsRef.current;
            const fvgThreshold = fvgAll.length - Math.max(1, fvgLim);
            for (let i = 0; i < fvgAll.length; i++) {
              const fv = fvgAll[i];
              if (!fvgOn || i < fvgThreshold) {
                fv.rect.setAttribute("opacity", "0");
                continue;
              }
              const x1 = safeTimeX(fv.ts);
              const x2 = safeTimeX(fv.end_ts);
              const yTop = safePriceY(fv.top);
              const yBot = safePriceY(fv.bottom);
              if (x1 == null || x2 == null || yTop == null || yBot == null) {
                fv.rect.setAttribute("opacity", "0");
                continue;
              }
              const left = Math.max(0, Math.min(x1, x2));
              const right = Math.min(plotRight, Math.max(x1, x2));
              const width = right - left;
              if (width <= 0) { fv.rect.setAttribute("opacity", "0"); continue; }
              const top = Math.min(yTop, yBot);
              const height = Math.abs(yBot - yTop);
              fv.rect.setAttribute("opacity", "1");
              fv.rect.setAttribute("x", String(left));
              fv.rect.setAttribute("y", String(top));
              fv.rect.setAttribute("width", String(width));
              fv.rect.setAttribute("height", String(Math.max(1, height)));
            }

            for (const sw of swingElements) {
              const x = safeTimeX(sw.ts);
              const y = safePriceY(sw.price);
              // Hide ONLY when lightweight-charts can't compute coordinates
              // (off the timeScale entirely). When the point is at the plot
              // border the tag must still render — clamp it inside.
              if (x == null || y == null) {
                sw.group.setAttribute("opacity", "0");
                sw.tag.setAttribute("x", String(SWING_OFFSCREEN));
                continue;
              }
              sw.group.setAttribute("opacity", "1");
              const offset = sw.isHigh ? -16 : 16;
              const rawTagY = y + offset - 6;
              const tagY = Math.max(2, Math.min(plotBottom - 15, rawTagY));
              // Tag is 22px wide centered on x. Clamp horizontally so it stays
              // inside the plot area at the right edge (otherwise the OTE-zone
              // mask or price-scale clip swallows the right half of HH/HL).
              const rawTagX = x - 11;
              const tagX = Math.max(2, Math.min(plotRight - 24, rawTagX));
              const tagCenterX = tagX + 11;
              sw.stem.setAttribute("x1", String(tagCenterX));
              sw.stem.setAttribute("x2", String(x));
              sw.stem.setAttribute("y1", String(y));
              sw.stem.setAttribute("y2", String(sw.isHigh ? tagY + 13 : tagY));
              sw.dot.setAttribute("cx", String(x));
              sw.dot.setAttribute("cy", String(y));
              sw.tag.setAttribute("x", String(tagX));
              sw.tag.setAttribute("y", String(tagY));
              sw.tagText.setAttribute("x", String(tagCenterX));
              sw.tagText.setAttribute("y", String(tagY + 10));
            }
          } catch {
            // Swallow — don't break the rAF loop on transient pan errors.
          } finally {
            if (!destroyed) rafId = requestAnimationFrame(update);
          }
        };
        rafId = requestAnimationFrame(update);

        const forceInitialChartSync = () => {
          if (destroyed || !chart || !candles) return;
          try { candles.priceScale().setAutoScale(true); } catch { /* older builds */ }
          try { chart.priceScale("right").setAutoScale(true); } catch { /* older builds */ }
          try {
            const pos = chart.timeScale().scrollPosition?.();
            if (Number.isFinite(Number(pos))) chart.timeScale().scrollToPosition(Number(pos), false);
          } catch { /* noop */ }
          try {
            const lr = chart.timeScale().getVisibleLogicalRange?.();
            if (lr && Number.isFinite(Number(lr.from)) && Number.isFinite(Number(lr.to))) {
              chart.timeScale().setVisibleLogicalRange({ from: Number(lr.from), to: Number(lr.to) });
            }
          } catch { /* noop */ }
          enforceRightPadding();
        };

        const firstRaf = requestAnimationFrame(() => {
          const secondRaf = requestAnimationFrame(forceInitialChartSync);
          initialSyncRafIds.push(secondRaf);
        });
        initialSyncRafIds.push(firstRaf);
        for (const delay of [0, 80, 180, 360]) {
          initialSyncTimerIds.push(window.setTimeout(forceInitialChartSync, delay));
        }

        setStatus("ok");
      } catch (e: any) {
        if (!destroyed) { setErr(e.message ?? "chart error"); setStatus("error"); }
      }
    };

    init();

    return () => {
      destroyed = true;
      cancelAnimationFrame(rafId);
      for (const id of initialSyncRafIds) cancelAnimationFrame(id);
      for (const id of initialSyncTimerIds) window.clearTimeout(id);
      fvgElementsRef.current = [];
      if (fibLabelLayerRef.current) fibLabelLayerRef.current.innerHTML = "";
      try { chart?.remove(); } catch { /* double-remove */ }
      chart = null;
      candles = null;
    };
  }, [symbol, interval, theme, chartHeight]);

  return (
    <div className="relative" style={{ height: chartHeight, background: t.bg }}>
      <div ref={containerRef} style={{ width: "100%", height: chartHeight }} />
      <svg
        ref={overlayRef}
        className="pointer-events-none absolute inset-0"
        style={{ zIndex: 10 }}
        width="100%"
        height={chartHeight}
      />
      <div
        ref={fibLabelLayerRef}
        className="pointer-events-none absolute inset-0"
        style={{ zIndex: 11 }}
      />
      {status === "loading" && (
        <div
          className="absolute inset-0 flex items-center justify-center text-xs"
          style={{ background: t.loadingBg, color: t.textColor }}
        >
          Loading…
        </div>
      )}
      {status === "error" && (
        <div
          className="absolute inset-0 flex items-center justify-center text-xs"
          style={{ background: t.loadingBg, color: "#752727" }}
        >
          {err}
        </div>
      )}
    </div>
  );
}

type ChartTab = "global" | "local" | "entry";

const TAB_LABELS: Record<ChartTab, string> = {
  global: "Global · D1",
  local: "Local · H1",
  entry: "Entry · M15",
};

const TAB_INTERVAL: Record<ChartTab, ChartInterval> = {
  global: "1d",
  local: "1h",
  entry: "15m",
};

const CHART_CAROUSEL_MS = 360;

function snapshotForChartTab(row: DashboardRow | null | undefined, tab: ChartTab): Snapshot | null {
  if (!row) return null;
  if (tab === "global") return row.global;
  if (tab === "local") return row.local;
  return null;
}

function formatChartPrice(p: number | null | undefined): string {
  if (p == null || !Number.isFinite(p)) return "—";
  if (p >= 100) return p.toFixed(2);
  if (p >= 1) return p.toFixed(3);
  return p.toPrecision(4);
}

function ChartHeaderOteInfo({ row, tab }: { row: DashboardRow; tab: ChartTab }) {
  const snap = snapshotForChartTab(row, tab);
  const oteValue =
    snap?.trend === "up" && snap.retracement != null
      ? `${(snap.retracement * 100).toFixed(1)}%`
      : snap
        ? "downtrend"
        : "—";

  return (
    <div
      className="absolute left-4 top-1/2 -translate-y-1/2 flex h-full items-center gap-2 font-mono leading-none pointer-events-none"
      style={{ color: "#a1a1aa" }}
    >
      <span className="text-[9px] uppercase tracking-[0.22em] text-muted">OTE</span>
      <span className={snap?.trend === "up" ? "text-[12px] text-discount" : "text-[11px] text-muted"}>
        {oteValue}
      </span>
    </div>
  );
}

function ChartModal({
  row,
  orderedSymbols,
  rows,
  source,
  onSwitchSymbol,
  onClose,
}: {
  row: DashboardRow;
  orderedSymbols: string[];
  rows: DashboardRow[];
  source: "local" | "global" | null;
  onSwitchSymbol: (symbol: string) => void;
  onClose: () => void;
}) {
  const modalCardRef = useRef<HTMLDivElement>(null);
  const [tab, setTab] = useState<ChartTab>(() => {
    if (source === "local") return "local";
    if (source === "global") return "global";
    const stored = localStorage.getItem(CHART_TAB_KEY);
    if (stored === "global" || stored === "local" || stored === "entry") return stored;
    return "global";
  });
  const [theme, setTheme] = useState<ChartTheme>(
    () => (localStorage.getItem(CHART_THEME_KEY) as ChartTheme) || "dark"
  );
  const [chartHeight, setChartHeight] = useState<number>(() => {
    const stored = Number(localStorage.getItem(CHART_HEIGHT_KEY));
    if (Number.isFinite(stored) && stored >= CHART_HEIGHT_MIN) {
      return Math.min(CHART_HEIGHT_MAX, stored);
    }
    return CHART_HEIGHT_DEFAULT;
  });
  const [fvgEnabled, setFvgEnabled] = useState<boolean>(
    () => localStorage.getItem(FVG_ENABLED_KEY) !== "0",
  );
  const [fvgLimit, setFvgLimit] = useState<number>(() => {
    const stored = Number(localStorage.getItem(FVG_LIMIT_KEY));
    if (Number.isFinite(stored) && stored >= 1 && stored <= 50) return stored;
    return FVG_DEFAULT_LIMIT;
  });
  const [activeSymbol, setActiveSymbol] = useState(row.symbol);
  const [outgoingSymbol, setOutgoingSymbol] = useState<string | null>(null);
  const [chartNavDir, setChartNavDir] = useState<"next" | "prev">("next");
  const [isFullscreen, setIsFullscreen] = useState<boolean>(false);
  const [viewportHeight, setViewportHeight] = useState<number>(() =>
    typeof window !== "undefined" ? window.innerHeight : 900
  );

  // Tracks which direction the user last navigated so CSS animations know
  // which way the center coin should slide in. Empty string = initial open.
  const navDirRef = useRef<"next" | "prev" | "">("");

  function toggleTheme() {
    setTheme((v) => {
      const next: ChartTheme = v === "dark" ? "light" : "dark";
      localStorage.setItem(CHART_THEME_KEY, next);
      return next;
    });
  }

  function resizeChart(delta: number) {
    setChartHeight((prev) => {
      const next = Math.max(CHART_HEIGHT_MIN, Math.min(CHART_HEIGHT_MAX, prev + delta));
      localStorage.setItem(CHART_HEIGHT_KEY, String(next));
      return next;
    });
  }

  function toggleFvg() {
    setFvgEnabled((v) => {
      const next = !v;
      localStorage.setItem(FVG_ENABLED_KEY, next ? "1" : "0");
      return next;
    });
  }

  function changeFvgLimit(value: number) {
    const clamped = Math.max(1, Math.min(50, Math.round(value)));
    setFvgLimit(clamped);
    localStorage.setItem(FVG_LIMIT_KEY, String(clamped));
  }

  const interval: ChartInterval = TAB_INTERVAL[tab];
  const isDark = theme === "dark";
  const effectiveChartHeight = isFullscreen
    ? Math.max(360, viewportHeight - 250)
    : chartHeight;

  // Find prev/next coin in the ordered list passed in from the table the
  // user clicked. If the modal was opened outside that flow (e.g. fall-back
  // from raw rows) the index is -1 and arrows hide gracefully.
  const idx = orderedSymbols.indexOf(row.symbol);
  const prevSymbol = idx > 0 ? orderedSymbols[idx - 1] : null;
  const nextSymbol = idx >= 0 && idx < orderedSymbols.length - 1
    ? orderedSymbols[idx + 1]
    : null;
  const currentLabel = displayName(row.symbol);
  const nearPrevLabel = idx > 0 ? displayName(orderedSymbols[idx - 1]) : "";
  const farPrevLabel = idx > 1 ? displayName(orderedSymbols[idx - 2]) : "";
  const nearNextLabel = idx >= 0 && idx < orderedSymbols.length - 1 ? displayName(orderedSymbols[idx + 1]) : "";
  const farNextLabel = idx >= 0 && idx < orderedSymbols.length - 2 ? displayName(orderedSymbols[idx + 2]) : "";

  useEffect(() => {
    if (idx < 0 || orderedSymbols.length === 0) return;
    const start = Math.max(0, idx - 3);
    const end = Math.min(orderedSymbols.length - 1, idx + 3);
    for (let i = start; i <= end; i++) {
      prefetchChart(orderedSymbols[i], interval);
    }
  }, [idx, orderedSymbols, interval]);

  // Keyboard navigation — left/right arrows step through the ordered list.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowLeft" && prevSymbol) {
        e.preventDefault();
        navDirRef.current = "prev";
        onSwitchSymbol(prevSymbol);
      } else if (e.key === "ArrowRight" && nextSymbol) {
        e.preventDefault();
        navDirRef.current = "next";
        onSwitchSymbol(nextSymbol);
      } else if (e.key === "Escape") {
        if (document.fullscreenElement) {
          e.preventDefault();
          void document.exitFullscreen();
        } else {
          onClose();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [prevSymbol, nextSymbol, onSwitchSymbol, onClose]);

  useEffect(() => {
    const onFsChange = () => {
      setIsFullscreen(document.fullscreenElement === modalCardRef.current);
    };
    const onResize = () => setViewportHeight(window.innerHeight);
    document.addEventListener("fullscreenchange", onFsChange);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("fullscreenchange", onFsChange);
      window.removeEventListener("resize", onResize);
    };
  }, []);

  async function toggleFullscreen() {
    const el = modalCardRef.current;
    if (!el) return;
    if (document.fullscreenElement === el) {
      await document.exitFullscreen();
      return;
    }
    await el.requestFullscreen();
  }

  const modalBg = "#18181b";
  const modalBorder = "#3f3f46";
  const modalText = "#f4f4f5";
  const subText = "#71717a";

  // Unified button base — every header control uses the same chrome so the
  // toolbar reads as one rhythm.
  const btnBase =
    "kz-btn h-8 inline-flex items-center justify-center rounded-md border text-[11px] tracking-widest uppercase select-none";
  const btnIcon = `${btnBase} w-8`;
  const btnText = `${btnBase} px-3`;
  const btnStyle: React.CSSProperties = {
    borderColor: modalBorder,
    color: subText,
    background: "transparent",
  };

  useEffect(() => {
    if (row.symbol === activeSymbol) return;
    const dir: "next" | "prev" = navDirRef.current === "prev" ? "prev" : "next";
    setChartNavDir(dir);
    setOutgoingSymbol(activeSymbol);
    setActiveSymbol(row.symbol);
    const id = window.setTimeout(() => setOutgoingSymbol(null), CHART_CAROUSEL_MS);
    return () => window.clearTimeout(id);
  }, [row.symbol, activeSymbol]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm kz-modal-enter"
      onClick={onClose}
    >
      <div
        ref={modalCardRef}
        className={`border kz-modal-pop ${
          isFullscreen
            ? "w-screen h-screen max-h-none rounded-none p-3 overflow-hidden"
            : "rounded-2xl p-4 w-[min(1080px,94vw)] max-h-[94vh] overflow-y-auto"
        }`}
        style={{ background: modalBg, borderColor: modalBorder }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Top block: OTE info on the left, 5-coin symmetric rail centered */}
        <div className="kz-modal-header">
          <ChartHeaderOteInfo row={row} tab={tab} />
          {/* Center rail — 5 coins + arrows, fully symmetric slots */}
          <div
            className="coinNav5"
            key={row.symbol}
            data-dir={navDirRef.current}
          >
            <span className="sideCoin sideCoin-far truncate" title={farPrevLabel}>{farPrevLabel}</span>
            <button
              type="button"
              onClick={() => { if (prevSymbol) { navDirRef.current = "prev"; onSwitchSymbol(prevSymbol); } }}
              disabled={!prevSymbol}
              className="kz-nav sideCoin sideCoin-near kz-coin-side h-6 w-[90px] justify-self-center truncate disabled:pointer-events-none disabled:opacity-0"
              title={nearPrevLabel ? `← ${nearPrevLabel}` : ""}
            >
              {nearPrevLabel}
            </button>
            <button
              type="button"
              onClick={() => { if (prevSymbol) { navDirRef.current = "prev"; onSwitchSymbol(prevSymbol); } }}
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
              onClick={() => { if (nextSymbol) { navDirRef.current = "next"; onSwitchSymbol(nextSymbol); } }}
              disabled={!nextSymbol}
              className="kz-nav arrow kz-coin-arrow h-7 w-[28px] justify-self-center disabled:pointer-events-none disabled:opacity-0"
              aria-label="Next coin"
            >
              ›
            </button>
            <button
              type="button"
              onClick={() => { if (nextSymbol) { navDirRef.current = "next"; onSwitchSymbol(nextSymbol); } }}
              disabled={!nextSymbol}
              className="kz-nav sideCoin sideCoin-near kz-coin-side h-6 w-[90px] justify-self-center truncate disabled:pointer-events-none disabled:opacity-0"
              title={nearNextLabel ? `${nearNextLabel} →` : ""}
            >
              {nearNextLabel}
            </button>
            <span className="sideCoin sideCoin-far truncate" title={farNextLabel}>{farNextLabel}</span>
          </div>
        </div>

        {/* Unified controls block — tabs on the left, ALL actions on the right */}
        <div className="kz-unified-toolbar">
          <div className="flex gap-1">
            {(Object.keys(TAB_LABELS) as ChartTab[]).map((t) => (
              <button
                key={t}
                onClick={() => {
                  setTab(t);
                  localStorage.setItem(CHART_TAB_KEY, t);
                }}
                className={`kz-tab h-8 px-3 inline-flex items-center rounded-md text-[11px] uppercase tracking-[0.22em] ${
                  tab === t ? "kz-tab-active" : ""
                }`}
                style={{
                  color: tab === t ? modalText : subText,
                  background: tab === t ? "rgba(63,63,70,0.55)" : "transparent",
                }}
              >
                {TAB_LABELS[t]}
              </button>
            ))}
          </div>

          <div className="kz-toolbar-actions">
            <button
              onClick={() => resizeChart(-60)}
              className={btnIcon}
              style={btnStyle}
              title="Smaller chart"
            >
              −
            </button>
            <button
              onClick={() => resizeChart(60)}
              className={btnIcon}
              style={btnStyle}
              title="Larger chart"
            >
              +
            </button>
            <button
              onClick={toggleTheme}
              className={btnText}
              style={btnStyle}
              title={`Switch to ${isDark ? "light" : "dark"} theme`}
            >
              {isDark ? "☀ Light chart" : "◑ Dark chart"}
            </button>
            <button
              onClick={() => {
                void toggleFullscreen();
              }}
              className={btnIcon}
              style={btnStyle}
              title={isFullscreen ? "Exit full screen" : "Full screen"}
              aria-label={isFullscreen ? "Exit full screen" : "Full screen"}
            >
              {isFullscreen ? "🗗" : "⛶"}
            </button>
            <label
              className="kz-btn h-8 px-3 inline-flex items-center gap-2 rounded-md border text-[11px] tracking-[0.22em] uppercase cursor-pointer"
              style={btnStyle}
              title={fvgEnabled ? "Hide FVG boxes" : "Show FVG boxes"}
            >
              <input
                type="checkbox"
                checked={fvgEnabled}
                onChange={toggleFvg}
                className="kz-checkbox"
              />
              <span>FVG</span>
            </label>
            <div
              className="h-8 inline-flex items-center rounded-md border overflow-hidden"
              style={btnStyle}
            >
              <button
                type="button"
                onClick={() => changeFvgLimit(fvgLimit - 1)}
                disabled={!fvgEnabled || fvgLimit <= 1}
                className="kz-step h-full px-2 text-sm leading-none disabled:opacity-30"
                title="Fewer FVG boxes"
              >
                −
              </button>
              <input
                type="number"
                min={1}
                max={50}
                value={fvgLimit}
                disabled={!fvgEnabled}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  if (Number.isFinite(n)) changeFvgLimit(n);
                }}
                className="kz-fvg-input w-10 h-full bg-transparent text-center text-[11px] font-mono outline-none disabled:opacity-30"
                style={{ color: modalText }}
                aria-label="FVG count"
              />
              <button
                type="button"
                onClick={() => changeFvgLimit(fvgLimit + 1)}
                disabled={!fvgEnabled || fvgLimit >= 50}
                className="kz-step h-full px-2 text-sm leading-none disabled:opacity-30"
                title="More FVG boxes"
              >
                +
              </button>
            </div>
            <button
              onClick={onClose}
              className={btnIcon}
              style={btnStyle}
              title="Close"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Chart — re-keyed on symbol/tab/theme/height/fvg toggle so each
            navigation triggers a clean fade transition. */}
        <div
          className="rounded-xl overflow-hidden border kz-chart-shell"
          style={{ borderColor: modalBorder }}
        >
          {outgoingSymbol && (
            <div
              key={`${outgoingSymbol}-${tab}-${theme}-${chartHeight}-out`}
              className={`kz-chart-pane kz-chart-pane-out navRow-out-${chartNavDir}`}
            >
              <CandleChart
                symbol={outgoingSymbol}
                interval={interval}
                theme={theme}
                chartHeight={effectiveChartHeight}
                fvgEnabled={fvgEnabled}
                fvgLimit={fvgLimit}
              />
            </div>
          )}
          <div
            key={`${activeSymbol}-${tab}-${theme}-${chartHeight}-in`}
            className={`kz-chart-pane ${outgoingSymbol ? `navRow-in-${chartNavDir}` : "kz-chart-frame"}`}
          >
            <CandleChart
              symbol={activeSymbol}
              interval={interval}
              theme={theme}
              chartHeight={effectiveChartHeight}
              fvgEnabled={fvgEnabled}
              fvgLimit={fvgLimit}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Main Dashboard ──────────────────────────────────────────────────────────

export function Dashboard({ onLogout }: { onLogout: () => void }) {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [density, setDensity] = useState<Density>(
    () => (localStorage.getItem(DENSITY_KEY) as Density) || "cozy"
  );
  const [feedOpen, setFeedOpen] = useState<boolean>(
    () => localStorage.getItem(FEED_OPEN_KEY) !== "0"
  );
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(
    () => localStorage.getItem(SIDEBAR_KEY) === "1"
  );
  const [swapped, setSwapped] = useState(() => localStorage.getItem(SWAPPED_KEY) === "1");
  const [page, setPageState] = useState<Page>(
    () => (localStorage.getItem(PAGE_KEY) as Page) || "ote"
  );
  const [chartSymbol, setChartSymbolState] = useState<string | null>(
    () => localStorage.getItem(CHART_SYMBOL_KEY)
  );
  const [chartOrder, setChartOrder] = useState<string[]>([]);
  const [chartSource, setChartSource] = useState<"local" | "global" | null>(null);
  const [fontSize, setFontSize] = useState<FontSizeValue>(() => {
    const stored = Number(localStorage.getItem(FONTSIZE_KEY));
    return (FONT_SIZES.includes(stored as FontSizeValue) ? stored : 13) as FontSizeValue;
  });
  const [tablesExpanded, setTablesExpanded] = useState<boolean>(
    () =>
      localStorage.getItem(TABLES_EXPANDED_KEY) !== "0" &&
      localStorage.getItem(GLOBAL_EXPANDED_KEY) !== "0" &&
      localStorage.getItem(LOCAL_EXPANDED_KEY) !== "0"
  );
  const [motionEnabled, setMotionEnabled] = useState<boolean>(true);

  function changeFontSize(delta: number) {
    setFontSize((v) => {
      const idx = FONT_SIZES.indexOf(v);
      const nextIdx = Math.max(0, Math.min(FONT_SIZES.length - 1, idx + delta));
      const next = FONT_SIZES[nextIdx];
      localStorage.setItem(FONTSIZE_KEY, String(next));
      return next;
    });
  }

  function toggleTablesExpanded() {
    setTablesExpanded((v) => {
      const next = !v;
      localStorage.setItem(TABLES_EXPANDED_KEY, next ? "1" : "0");
      localStorage.setItem(GLOBAL_EXPANDED_KEY, next ? "1" : "0");
      localStorage.setItem(LOCAL_EXPANDED_KEY, next ? "1" : "0");
      return next;
    });
  }

  function setPage(page: Page) {
    setPageState(page);
    localStorage.setItem(PAGE_KEY, page);
  }

  function setChartSymbol(
    symbol: string | null,
    order?: string[],
    source?: "local" | "global",
  ) {
    setChartSymbolState(symbol);
    if (symbol) localStorage.setItem(CHART_SYMBOL_KEY, symbol);
    else localStorage.removeItem(CHART_SYMBOL_KEY);
    if (order !== undefined) setChartOrder(order);
    if (source !== undefined) setChartSource(source);
    if (symbol === null) {
      setChartOrder([]);
      setChartSource(null);
    }
  }

  function toggleSwapped() {
    setSwapped((v) => {
      const next = !v;
      localStorage.setItem(SWAPPED_KEY, next ? "1" : "0");
      return next;
    });
  }

  async function refresh() {
    try {
      const d = await getDashboard();
      setData(d);
      setError(null);
    } catch (err: any) {
      if (err.message === "unauthorized") {
        onLogout();
        return;
      }
      setError(err.message ?? "failed to load");
    }
  }

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.motion = motionEnabled ? "on" : "off";
    localStorage.setItem(MOTION_KEY, motionEnabled ? "1" : "0");
  }, [motionEnabled]);

  function toggleDensity() {
    setDensity((d) => {
      const next = d === "cozy" ? "compact" : "cozy";
      localStorage.setItem(DENSITY_KEY, next);
      return next;
    });
  }

  function toggleFeed() {
    setFeedOpen((v) => {
      const next = !v;
      localStorage.setItem(FEED_OPEN_KEY, next ? "1" : "0");
      return next;
    });
  }

  function toggleSidebar() {
    setSidebarOpen((v) => {
      const next = !v;
      localStorage.setItem(SIDEBAR_KEY, next ? "1" : "0");
      return next;
    });
  }

  function toggleMotion() {
    setMotionEnabled((v) => !v);
  }

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    const sym = normalizeSymbol(input);
    if (!sym) return;
    setBusy(true);
    try {
      await addCoin(sym);
      setInput("");
      await refresh();
    } catch (err: any) {
      setError(err.message ?? "add failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleRemove(symbol: string) {
    if (!confirm(`Remove ${symbol}?`)) return;
    try {
      await removeCoin(symbol);
      await refresh();
    } catch (err: any) {
      setError(err.message ?? "remove failed");
    }
  }

  async function handleTogglePin(symbol: string) {
    setData((d) => {
      if (!d) return d;
      const maxOrder = d.rows.reduce(
        (m, r) => (r.pinned_order != null && r.pinned_order > m ? r.pinned_order : m),
        -1
      );
      return {
        ...d,
        rows: d.rows.map((r) => {
          if (r.symbol !== symbol) return r;
          return { ...r, pinned_order: r.pinned_order == null ? maxOrder + 1 : null };
        }),
      };
    });
    try {
      await togglePin(symbol);
      await refresh();
    } catch (err: any) {
      setError(err.message ?? "pin failed");
      await refresh();
    }
  }

  async function handleMovePin(symbol: string, direction: "up" | "down") {
    try {
      await movePin(symbol, direction);
      await refresh();
    } catch (err: any) {
      setError(err.message ?? "reorder failed");
    }
  }

  async function handleSetCall(symbol: string, tag: CallTag, note: string | null) {
    setData((d) =>
      d
        ? {
            ...d,
            rows: d.rows.map((r) =>
              r.symbol === symbol ? { ...r, call_tag: tag, call_note: note } : r
            ),
          }
        : d
    );
    try {
      await setCall(symbol, tag, note);
    } catch (err: any) {
      setError(err.message ?? "call save failed");
      await refresh();
    }
  }

  function logout() {
    setToken(null);
    onLogout();
  }

  const rows = data?.rows ?? [];
  const totals = data?.totals ?? { total: 0, ote: 0, discount: 0, premium: 0 };
  const recentAlerts = useMemo(() => data?.recent_alerts ?? [], [data?.recent_alerts]);
  const lastRefresh = data?.last_refresh_at
    ? new Date(data.last_refresh_at).toLocaleString()
    : "—";

  const chartRow = chartSymbol ? rows.find((r) => r.symbol === chartSymbol) ?? null : null;

  const leftPick = swapped
    ? (r: DashboardRow) => r.local
    : (r: DashboardRow) => r.global;
  const rightPick = swapped
    ? (r: DashboardRow) => r.global
    : (r: DashboardRow) => r.local;
  const leftExpanded = tablesExpanded;
  const rightExpanded = tablesExpanded;

  return (
    <div className="min-h-screen flex">
      {/* ── Sidebar ── */}
      <aside
        className={`${
          sidebarOpen ? "w-48" : "w-14"
        } shrink-0 border-r border-border bg-panel flex flex-col py-4 gap-1 transition-[width] duration-200 overflow-hidden`}
      >
        {/* Logo */}
        <div className="flex items-center gap-2.5 px-2 mb-2 shrink-0">
          <div className="relative w-9 h-9 rounded-full flex items-center justify-center text-accent font-bold shrink-0 text-sm overflow-hidden">
            <img
              src="/logo_tiger.png?v=20260426-1"
              alt=""
              className="h-full w-full object-contain"
              onError={(e) => {
                e.currentTarget.style.display = "none";
              }}
            />
          </div>
          {sidebarOpen && (
            <span className="text-accent font-bold text-sm tracking-[0.25em] whitespace-nowrap">
              KAZUS
            </span>
          )}
        </div>

        {/* Nav top */}
        <div className="flex flex-col gap-0.5 px-1.5">
          <NavBtn
            active={page === "ote"}
            open={sidebarOpen}
            icon={<FibIcon size={20} />}
            label="OTE"
            onClick={() => setPage("ote")}
            title="OTE Screener"
          />
          <NavBtn
            active={page === "tda"}
            open={sidebarOpen}
            icon={<TDAIcon size={20} />}
            label="TDA"
            onClick={() => setPage("tda")}
            title="Trade Data Analysis"
          />
        </div>

        {/* Nav bottom */}
        <div className="mt-auto flex flex-col gap-0.5 px-1.5">
          <button
            onClick={toggleMotion}
            className="flex items-center justify-center px-2 py-2 rounded-lg text-muted hover:text-zinc-200 hover:bg-white/[0.04] transition-colors"
            title={motionEnabled ? "Disable motion" : "Enable motion"}
          >
            <span className="text-[10px] uppercase tracking-widest whitespace-nowrap">
              {motionEnabled ? "motion on" : "motion off"}
            </span>
          </button>

          {/* Collapse toggle */}
          <button
            onClick={toggleSidebar}
            className="flex items-center justify-center px-2 py-2 rounded-lg text-muted hover:text-zinc-200 hover:bg-white/[0.04] transition-colors"
            title={sidebarOpen ? "Collapse" : "Expand"}
          >
            <span className="text-[10px]">{sidebarOpen ? "◀" : "▶"}</span>
          </button>

          {/* Logout */}
          <button
            onClick={logout}
            className="flex items-center gap-2 px-2 py-2 rounded-lg text-muted kz-premium-hover transition-colors"
            title="Log out"
          >
            <span className="text-[10px] uppercase tracking-widest whitespace-nowrap">exit</span>
          </button>
        </div>
      </aside>

      {/* ── Main ── */}
      <main className="flex-1 p-6 pb-32 space-y-6 min-w-0 overflow-hidden">
        {error && (
          <div
            className="rounded-lg px-4 py-2 text-sm kz-premium"
            style={{ background: "rgba(117, 39, 39, 0.12)", border: "1px solid rgba(117, 39, 39, 0.45)" }}
          >
            {error}
          </div>
        )}

        {page === "ote" && (
          <>
            {/* Tables + swap button */}
            <div className="flex items-start gap-1.5">
              <div className="flex-1 min-w-0">
                <ScreenerTable
                  title={swapped ? "LOCAL" : "GLOBAL"}
                  rows={rows}
                  pick={leftPick}
                  onRemove={handleRemove}
                  onTogglePin={handleTogglePin}
                  onMovePin={handleMovePin}
                  onSetCall={handleSetCall}
                  density={density}
                  expanded={leftExpanded}
                  onCoinClick={(sym, order, src) => setChartSymbol(sym, order, src)}
                  fontSize={fontSize}
                  storageKey={swapped ? "local" : "global"}
                />
              </div>

              {/* Swap + text size controls */}
              <div className="shrink-0 flex flex-col items-center gap-1.5 pt-[52px]">
                <button
                  onClick={toggleSwapped}
                  className="p-1.5 rounded-lg border border-border text-muted hover:text-zinc-200 hover:border-accent/50 transition-colors text-sm leading-none"
                  title="Swap table order"
                >
                  ⇄
                </button>
                <button
                  onClick={toggleTablesExpanded}
                  className="h-8 w-8 inline-flex flex-col items-center justify-center border border-border rounded-md text-muted hover:text-zinc-200 hover:border-accent/50 transition-colors leading-none"
                  title={tablesExpanded ? "Use compact table height" : "Use expanded table height"}
                >
                  <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                    <path d="M3 4.5L6 1.5L9 4.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" />
                    <path d="M3 7.5L6 10.5L9 7.5" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  <span className="text-[7px] font-mono">
                    {`${Math.min(10, rows.length)}/${rows.length}`}
                  </span>
                </button>
                <button
                  onClick={toggleDensity}
                  className={`h-8 w-8 flex items-center justify-center border rounded-md text-sm leading-none ${
                    density === "compact"
                      ? "border-accent text-accent"
                      : "border-border text-muted hover:text-zinc-200"
                  }`}
                  title="Toggle row density"
                >
                  ↕
                </button>
                <div className="flex flex-col items-center border border-border rounded-lg overflow-hidden">
                  <button
                    onClick={() => changeFontSize(1)}
                    disabled={fontSize === FONT_SIZES[FONT_SIZES.length - 1]}
                    className="px-2 py-1 text-[10px] leading-none text-muted hover:text-zinc-200 hover:bg-white/[0.04] transition-colors disabled:opacity-30"
                    title="Increase font size"
                  >
                    +
                  </button>
                  <div className="px-1 py-1 border-y border-border text-[10px] leading-none font-mono text-zinc-300 min-w-[28px] text-center select-none">
                    {fontSize}
                  </div>
                  <button
                    onClick={() => changeFontSize(-1)}
                    disabled={fontSize === FONT_SIZES[0]}
                    className="px-2 py-1 text-[10px] leading-none text-muted hover:text-zinc-200 hover:bg-white/[0.04] transition-colors disabled:opacity-30"
                    title="Decrease font size"
                  >
                    −
                  </button>
                </div>
              </div>

              <div className="flex-1 min-w-0">
                <ScreenerTable
                  title={swapped ? "GLOBAL" : "LOCAL"}
                  rows={rows}
                  pick={rightPick}
                  onRemove={handleRemove}
                  onTogglePin={handleTogglePin}
                  onMovePin={handleMovePin}
                  onSetCall={handleSetCall}
                  density={density}
                  expanded={rightExpanded}
                  onCoinClick={(sym, order, src) => setChartSymbol(sym, order, src)}
                  fontSize={fontSize}
                  storageKey={swapped ? "global" : "local"}
                />
              </div>
            </div>

            {/* Stats + add */}
            <div className="grid md:grid-cols-3 gap-6">
              <div className="bg-panel border border-border rounded-2xl p-5">
                <div className="flex gap-6">
                  <Counter label="Total" value={totals.total} />
                  <Counter label="OTE" value={totals.ote} color="text-ote" />
                  <Counter label="Dic" value={totals.discount} color="text-discount" />
                  <Counter label="Pre" value={totals.premium} color="kz-premium" />
                </div>
                <div className="text-[10px] uppercase tracking-widest text-muted mt-4">
                  Last refresh:{" "}
                  <span className="text-zinc-300">{lastRefresh}</span>
                </div>
                {data?.last_error && (
                  <div className="text-[10px] mt-1" style={{ color: "rgba(117, 39, 39, 0.82)" }}>{data.last_error}</div>
                )}
              </div>

              <form
                onSubmit={handleAdd}
                className="bg-panel border border-border rounded-2xl p-5 flex items-end gap-3 md:col-span-2"
              >
                <label className="flex-1">
                  <div className="text-[10px] uppercase tracking-widest text-muted mb-1">
                    Add coin (Binance Futures symbol)
                  </div>
                  <SymbolSuggestInput
                    className="w-full bg-bg border border-border rounded-lg px-3 py-2 font-mono uppercase focus:outline-none focus:border-accent"
                    placeholder="BTCUSDT"
                    value={input}
                    onChange={setInput}
                    exclude={rows.map((row) => row.symbol)}
                  />
                </label>
                <button
                  type="submit"
                  disabled={busy}
                  className="bg-accent text-black rounded-lg px-5 py-2 font-semibold uppercase tracking-widest disabled:opacity-50"
                >
                  Add
                </button>
              </form>
            </div>
          </>
        )}

        {page === "tda" && <TDA />}
      </main>

      {/* ── Chart Modal ── */}
      {chartRow && (
        <ChartModal
          row={chartRow}
          rows={rows}
          orderedSymbols={chartOrder.length > 0 ? chartOrder : rows.map((r) => r.symbol)}
          source={chartSource}
          onSwitchSymbol={(sym) => setChartSymbol(sym)}
          onClose={() => setChartSymbol(null)}
        />
      )}

      {/* ── Alert Feed ── */}
      <AlertFeed open={feedOpen} alerts={recentAlerts} onToggle={toggleFeed} />
    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────────────────

function NavBtn({
  active,
  open,
  icon,
  label,
  onClick,
  title,
}: {
  active: boolean;
  open: boolean;
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={`flex items-center gap-2.5 px-2.5 py-3 rounded-lg transition-colors w-full ${
        active
          ? "text-accent bg-accent/10"
          : "text-muted hover:text-zinc-200 hover:bg-white/[0.04]"
      }`}
    >
      <span className="shrink-0">{icon}</span>
      {open && (
        <span className="text-[12px] uppercase tracking-widest font-semibold whitespace-nowrap">
          {label}
        </span>
      )}
    </button>
  );
}

function Counter({
  label,
  value,
  color = "text-zinc-100",
}: {
  label: string;
  value: number;
  color?: string;
}) {
  return (
    <div>
      <div className={`text-3xl font-bold ${color}`}>{value.toString().padStart(2, "0")}</div>
      <div className="text-[10px] uppercase tracking-widest text-muted">{label}</div>
    </div>
  );
}

function AlertFeed({
  open,
  alerts,
  onToggle,
}: {
  open: boolean;
  alerts: AlertEvent[];
  onToggle: () => void;
}) {
  return (
    <>
      <button
        type="button"
        onClick={onToggle}
        className="fixed bottom-5 right-5 z-40 flex h-12 w-12 items-center justify-center rounded-full border border-accent/40 bg-panel/95 text-accent shadow-2xl backdrop-blur hover:border-accent hover:text-zinc-100"
        title={open ? "Hide alerts" : "Show alerts"}
      >
        <span className="text-lg leading-none">🔔</span>
      </button>

      <div
        className={`fixed bottom-20 right-5 z-30 w-[min(420px,calc(100vw-2rem))] rounded-2xl border border-border bg-panel/95 shadow-2xl backdrop-blur transition-all duration-200 ${
          open
            ? "translate-y-0 opacity-100"
            : "pointer-events-none translate-y-4 opacity-0"
        }`}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div>
            <div className="text-[11px] uppercase tracking-[0.28em] text-accent">Alerts</div>
            <div className="text-[10px] uppercase tracking-[0.16em] text-muted">
              Live telegram mirror
            </div>
          </div>
          <button type="button" onClick={onToggle} className="text-xs text-muted hover:text-zinc-100">
            hide
          </button>
        </div>
        <div className="max-h-[280px] space-y-2 overflow-y-auto px-4 py-3">
          {alerts.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border px-3 py-4 text-xs text-muted">
              No live alerts yet.
            </div>
          ) : (
            alerts.map((alert) => (
              <div key={alert.id} className="rounded-xl border border-border bg-bg/70 px-3 py-2">
                <div className="text-[11px] leading-relaxed text-zinc-100">{alert.message}</div>
                <div className="mt-1 text-[10px] uppercase tracking-[0.16em] text-muted">
                  {alert.timeframe} • {new Date(alert.created_at).toLocaleString()}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </>
  );
}
