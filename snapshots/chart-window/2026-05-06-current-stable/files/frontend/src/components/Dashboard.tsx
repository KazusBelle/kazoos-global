import { useEffect, useMemo, useRef, useState } from "react";
import {
  addCoin,
  deleteStructure,
  getChart,
  getDashboard,
  movePin,
  removeCoin,
  reportFrontendError,
  saveStructure,
  setCall,
  setToken,
  togglePin,
  type AlertEvent,
  type CallTag,
  type ChartData,
  type DashboardResponse,
  type DashboardRow,
  type Snapshot,
  type StructureEvent,
  type SwingPoint,
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
    wickColor: "#525757",
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

// Initial fit (logical bars only — no pixel paddings, no masks).
//   visibleBars = clamp(max(50, currentIndex - swingStartIndex), 50, 180)
//   paddingBars = clamp(round(visibleBars * 0.20), 12, 40)
// After this single call, X range is fully under user control.
const VISIBLE_BARS_MIN = 50;
const VISIBLE_BARS_MAX = 180;
const FALLBACK_VISIBLE_BARS = 120;
const PADDING_RATIO = 0.20;
const PADDING_BARS_MIN = 60;
const PADDING_BARS_MAX = 60;

// Y autoscale: candles fill ~70% of plot height; OTE band is included only
// if its height does not exceed the candle range by more than 20%.
const TARGET_CANDLE_HEIGHT_RATIO = 0.70;
const OTE_HEIGHT_TOLERANCE = 1.2;

// Fib visual: right-side mode in chart-space, same model as Pine/TradingView.
// X is logical bars, Y is price. The chart projects both to screen coords.
const FIB_LINE_LENGTH_BARS = 20;
const FIB_RIGHT_OFFSET_BARS = 40;
const FIB_LABEL_GAP = 10;          // visual gap between line end and label

const SWING_TAG_W = 22;
const SWING_TAG_H = 13;
const CANDLE_STROKE_COLOR = "#2e2e2e";
const DARK_WICK_COLOR = "#525757";
const LIGHT_CANDLE_STROKE_COLOR = "#000000";
const CURRENT_PRICE_COLOR = "#056656";
// Backend defaults the chart limit per interval (d1=500, h1=900, 15m=600) —
// matches worker/compute.py so engine state is identical to the screener.

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

function invalidateChartCache(symbol: string, interval: string) {
  chartDataCache.delete(chartCacheKey(symbol, interval));
  chartDataInFlight.delete(chartCacheKey(symbol, interval));
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

type ChartPriceRange = { minValue: number; maxValue: number };
type LogicalRange = { from: number; to: number };

// Locate the swing start anchoring the active fib:
//   bullish → last HL before the most recent HH
//   bearish → last LH before the most recent LL
// Returns the candle index, or -1 if not resolvable.
function computeSwingStartIndex(
  data: ChartData,
  barIndexByTime: Map<number, number>,
): number {
  if (data.fib_direction !== "bullish" && data.fib_direction !== "bearish") return -1;
  const swings = data.swings;
  if (!swings || swings.length === 0) return -1;
  const isBullish = data.fib_direction === "bullish";
  const peakLabel = isBullish ? "HH" : "LL";
  const baseLabel = isBullish ? "HL" : "LH";
  let lastPeakIdx = -1;
  for (let i = swings.length - 1; i >= 0; i--) {
    if (swings[i].label === peakLabel) { lastPeakIdx = i; break; }
  }
  if (lastPeakIdx < 0) return -1;
  for (let i = lastPeakIdx - 1; i >= 0; i--) {
    if (swings[i].label === baseLabel) {
      const tsSec = Math.floor(swings[i].ts / 1000);
      const idx = barIndexByTime.get(tsSec);
      return idx == null ? -1 : idx;
    }
  }
  return -1;
}

// Initial fit — the SOLE source of truth for the X range. Pure function.
function computeInitialFit(
  currentIndex: number,
  swingStartIndex: number,
): LogicalRange {
  if (currentIndex < 0) return { from: 0, to: 0 };
  let span: number;
  if (swingStartIndex < 0 || swingStartIndex >= currentIndex) {
    span = FALLBACK_VISIBLE_BARS;
  } else {
    span = currentIndex - swingStartIndex;
  }
  const visibleBars = Math.min(VISIBLE_BARS_MAX, Math.max(VISIBLE_BARS_MIN, span));
  const paddingBars = Math.min(
    PADDING_BARS_MAX,
    Math.max(PADDING_BARS_MIN, Math.round(visibleBars * PADDING_RATIO)),
  );
  return {
    from: Math.max(0, currentIndex - visibleBars),
    to: currentIndex + paddingBars,
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
  ) return null;
  const fibRange = data.fib_high - data.fib_low;
  const oteA = data.fib_high - fibRange * 0.618;
  const oteB = data.fib_high - fibRange * 0.786;
  return { low: Math.min(oteA, oteB), high: Math.max(oteA, oteB) };
}

function padRangeToHeight(
  minValue: number,
  maxValue: number,
  fallbackPrice: number,
): ChartPriceRange | null {
  let low = minValue;
  let high = maxValue;
  if (!Number.isFinite(low) || !Number.isFinite(high)) return null;
  if (high < low) { const tmp = high; high = low; low = tmp; }
  let range = high - low;
  if (!Number.isFinite(range) || range <= 0) {
    const basis = Math.max(Math.abs(fallbackPrice), Math.abs(high), 1);
    range = basis * 0.002;
    low -= range / 2;
    high += range / 2;
  }
  const pad = range * ((1 - TARGET_CANDLE_HEIGHT_RATIO) / 2 / TARGET_CANDLE_HEIGHT_RATIO);
  return { minValue: low - pad, maxValue: high + pad };
}

// Y autoscale: include OTE band only if its vertical extent does not dwarf
// the visible candle range. Otherwise the band is ignored entirely so the
// candles stay proportional. No clamping, no edge stretching.
function makeAutoscaleProvider(data: ChartData) {
  return (baseImplementation: () => { priceRange: ChartPriceRange | null; margins?: any } | null) => {
    const base = baseImplementation();
    const baseRange = base?.priceRange;
    if (baseRange == null) return base;
    const candleLow = baseRange.minValue;
    const candleHigh = baseRange.maxValue;
    const candleHeight = Math.max(candleHigh - candleLow, Math.abs(candleHigh) * 0.002, 1e-9);
    let minValue = candleLow;
    let maxValue = candleHigh;
    // 0.0 and 1.0 fib levels are always visible.
    if (data.fib_high != null && data.fib_low != null && data.fib_high > data.fib_low) {
      minValue = Math.min(minValue, data.fib_low);
      maxValue = Math.max(maxValue, data.fib_high);
    }
    const oteZone = getBullishOteZone(data);
    if (oteZone != null) {
      const oteHeight = oteZone.high - oteZone.low;
      if (oteHeight <= candleHeight * OTE_HEIGHT_TOLERANCE) {
        minValue = Math.min(minValue, oteZone.low);
        maxValue = Math.max(maxValue, oteZone.high);
      }
    }
    const padded = padRangeToHeight(minValue, maxValue, (candleHigh + candleLow) / 2);
    return padded == null
      ? base
      : { priceRange: padded, margins: { above: 0, below: 0 } };
  };
}

type SwingClickAnchor = { idx: number; clientX: number; clientY: number };

function CandleChart({
  symbol,
  interval,
  theme,
  chartHeight,
  fvgEnabled,
  fvgLimit,
  reloadKey,
  editMode,
  draft,
  onSwingClick,
  onCanvasClick,
  onSwingDragEnd,
}: {
  symbol: string;
  interval: ChartInterval;
  theme: ChartTheme;
  chartHeight: number;
  fvgEnabled: boolean;
  fvgLimit: number;
  reloadKey?: number;
  editMode?: boolean;
  draft?: StructureEvent[];
  onSwingClick?: (a: SwingClickAnchor) => void;
  onCanvasClick?: (ts: number, price: number) => void;
  onSwingDragEnd?: (idx: number, ts: number, price: number) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<SVGSVGElement>(null);
  const plotClipIdRef = useRef(`chart-plot-clip-${Math.random().toString(36).slice(2)}`);
  const [status, setStatus] = useState<"loading" | "ok" | "error">("loading");
  const [err, setErr] = useState<string | null>(null);
  const t = CHART_THEMES[theme];

  // Live refs — read by the rAF loop each frame so toggling FVG
  // visibility never forces a chart remount.
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

  // Swing/zigzag refs survive across effects: the main effect builds the
  // chart + initial swing layer; a secondary effect rebuilds the swing layer
  // from the draft when edit mode toggles or the draft changes. The rAF
  // loop reads the current swing array via the ref so positioning continues
  // to work after a rebuild.
  type SwingEl = {
    ts: number;
    price: number;
    label: string;
    isHigh: boolean;
    tag: SVGRectElement;
    tagText: SVGTextElement;
    stem: SVGLineElement;
    dot: SVGCircleElement;
    hit: SVGRectElement | null;
    group: SVGGElement;
  };
  const swingElementsRef = useRef<SwingEl[]>([]);
  const zigzagPolylineRef = useRef<SVGPolylineElement | null>(null);
  const previewLineRef = useRef<SVGLineElement | null>(null);
  const rebuildSwingsRef = useRef<((source: SwingPoint[], edit: boolean) => void) | null>(null);
  const serverSwingsRef = useRef<SwingPoint[]>([]);
  // Live mirrors so listeners (set up once in init) always see latest props.
  const editModeRef = useRef(!!editMode);
  const draftRef = useRef<StructureEvent[] | undefined>(draft);
  const onCanvasClickRef = useRef(onCanvasClick);
  const onSwingDragEndRef = useRef(onSwingDragEnd);
  const onSwingClickRef = useRef(onSwingClick);
  useEffect(() => { editModeRef.current = !!editMode; }, [editMode]);
  useEffect(() => { draftRef.current = draft; }, [draft]);
  useEffect(() => { onCanvasClickRef.current = onCanvasClick; }, [onCanvasClick]);
  useEffect(() => { onSwingDragEndRef.current = onSwingDragEnd; }, [onSwingDragEnd]);
  useEffect(() => { onSwingClickRef.current = onSwingClick; }, [onSwingClick]);
  // Cursor / drag state read by the rAF loop for live preview.
  const cursorRef = useRef<{ x: number; y: number; inside: boolean }>({ x: 0, y: 0, inside: false });
  const dragRef = useRef<{ idx: number; x: number; y: number; moved: boolean } | null>(null);
  const reportedRafErrorRef = useRef(false);

  useEffect(() => {
    let destroyed = false;
    let chart: any;
    let candles: any;
    let livePriceLine: any;
    let rafId = 0;
    let cleanupListeners: (() => void) | null = null;

    const init = async () => {
      if (destroyed || !containerRef.current) return;
      try {
        setStatus("loading");
        setErr(null);

        const { createChart, ColorType, CandlestickSeries, CrosshairMode } = await import("lightweight-charts");
        const data = await getChartCached(symbol, interval);
        if (destroyed || !containerRef.current) return;

        const candleData = data.bars.map((b) => ({
          time: Math.floor(b.ts / 1000) as any,
          open: b.open,
          high: b.high,
          low: b.low,
          close: b.close,
          borderColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : CANDLE_STROKE_COLOR,
          wickColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : DARK_WICK_COLOR,
        }));
        const barIndexByTime = new Map<number, number>();
        candleData.forEach((bar, index) => {
          barIndexByTime.set(Number(bar.time), index);
        });

        const fibActive =
          data.fib_high != null && data.fib_low != null && data.fib_high > data.fib_low;
        const isBullish = fibActive && data.fib_direction === "bullish";
        const autoscaleOverride = makeAutoscaleProvider(data);
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
            mode: CrosshairMode.Normal,
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
            rightOffset: 0,
          },
        });

        candles = chart.addSeries(CandlestickSeries, {
          ...(theme === "light"
            ? { upColor: "#9598a1", downColor: "#434651" }
            : { upColor: t.bullColor, downColor: t.bearColor }),
          borderVisible: true,
          borderUpColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : CANDLE_STROKE_COLOR,
          borderDownColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : CANDLE_STROKE_COLOR,
          wickVisible: true,
          wickUpColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : DARK_WICK_COLOR,
          wickDownColor: theme === "light" ? LIGHT_CANDLE_STROKE_COLOR : DARK_WICK_COLOR,
          priceLineVisible: false,
          lastValueVisible: false,
          priceFormat: { type: "price", precision: pricePrecision, minMove },
          autoscaleInfoProvider: autoscaleOverride,
        });

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

        // ── INITIAL FIT — sole place that touches the X range. After this
        // point, no subscribe→set, no resize→set, no timers, no enforce.
        if (candleData.length > 0) {
          const currentIndex = candleData.length - 1;
          const swingStartIndex = computeSwingStartIndex(data, barIndexByTime);
          const range = computeInitialFit(currentIndex, swingStartIndex);
          try { chart.timeScale().setVisibleLogicalRange(range); } catch { /* ignore */ }
        }

        // ── SVG OVERLAY ─────────────────────────────────────────────────
        const svg = overlayRef.current;
        if (!svg) return;
        while (svg.firstChild) svg.removeChild(svg.firstChild);
        const plotClipId = plotClipIdRef.current;
        const defs = document.createElementNS(SVG_NS, "defs");
        const plotClip = document.createElementNS(SVG_NS, "clipPath");
        plotClip.setAttribute("id", plotClipId);
        plotClip.setAttribute("clipPathUnits", "userSpaceOnUse");
        const plotClipRect = document.createElementNS(SVG_NS, "rect");
        plotClip.appendChild(plotClipRect);
        defs.appendChild(plotClip);
        svg.appendChild(defs);

        const isLocal = interval === "1h";
        const halfColor = isLocal ? "#5c7bd5" : "#752727";
        const plotClipUrl = `url(#${plotClipId})`;

        // FVG layer (created here, visibility per-frame from refs).
        const allFvgs = data.fvgs ?? [];
        const newFvgElements: FvgEl[] = [];
        for (const fvg of allFvgs) {
          const fill = fvg.kind === "bullish"
            ? "rgba(38, 166, 154, 0.18)"
            : "rgba(117, 39, 39, 0.24)";
          const rect = document.createElementNS(SVG_NS, "rect");
          rect.setAttribute("fill", fill);
          rect.setAttribute("stroke", fill);
          rect.setAttribute("stroke-width", "0.5");
          rect.setAttribute("clip-path", plotClipUrl);
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

        // Fib layer can extend into the right price-scale area. The native
        // price-scale canvases are raised above it so OTE sits behind labels.
        type FibEl = {
          ratio: number;
          color: string;
          label: string;
          group: SVGGElement;
          line: SVGLineElement;
          text: SVGTextElement;
        };
        const fibElements: FibEl[] = [];
        const fibLayer = document.createElementNS(SVG_NS, "g");
        fibLayer.setAttribute("data-layer", "fib");
        svg.appendChild(fibLayer);

        if (isBullish) {
          const fibConfig = [
            { ratio: 0.0,   color: "#787b86", label: "0.0"   },
            { ratio: 0.5,   color: halfColor, label: "0.5"   },
            { ratio: 0.618, color: "#056656", label: "0.618" },
            { ratio: 0.705, color: "#056656", label: "0.705" },
            { ratio: 0.786, color: "#056656", label: "0.786" },
            { ratio: 1.0,   color: "#787b86", label: "1.0"   },
          ];
          for (const f of fibConfig) {
            const g = document.createElementNS(SVG_NS, "g");
            g.setAttribute("data-fib-ratio", f.label);
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
            text.setAttribute("font-size", "7");
            text.setAttribute("font-weight", "600");
            text.setAttribute("fill", f.color);
            text.setAttribute("font-family", "monospace");
            text.setAttribute("paint-order", "stroke fill");
            text.setAttribute("stroke", t.bg);
            text.setAttribute("stroke-width", "2.5");
            text.setAttribute("stroke-linejoin", "round");
            text.textContent = f.label;
            g.appendChild(line);
            g.appendChild(text);
            fibLayer.appendChild(g);
            fibElements.push({ ratio: f.ratio, color: f.color, label: f.label, group: g, line, text });
          }
        }

        // Bearish placeholder text (no zone, just a centered hint).
        let placeholder: SVGTextElement | null = null;
        if (!isBullish && data.fib_direction === "bearish") {
          placeholder = document.createElementNS(SVG_NS, "text");
          placeholder.setAttribute("text-anchor", "middle");
          placeholder.setAttribute("dominant-baseline", "middle");
          placeholder.setAttribute("font-size", "12");
          placeholder.setAttribute("font-weight", "700");
          placeholder.setAttribute("font-family", "monospace");
          placeholder.setAttribute("fill", "#71717a");
          placeholder.setAttribute("letter-spacing", "1.6");
          placeholder.textContent = "DOWNTREND";
          fibLayer.appendChild(placeholder);
        }

        // Zigzag layer — sits BELOW swing tags so chips remain readable.
        // Empty until edit mode is engaged; the rAF loop updates points.
        const zigzagLayer = document.createElementNS(SVG_NS, "g");
        zigzagLayer.setAttribute("data-layer", "zigzag");
        zigzagLayer.setAttribute("clip-path", plotClipUrl);
        const zigzagPolyline = document.createElementNS(SVG_NS, "polyline");
        zigzagPolyline.setAttribute("fill", "none");
        zigzagPolyline.setAttribute("stroke", theme === "dark" ? "#a1a1aa" : "#52525b");
        zigzagPolyline.setAttribute("stroke-width", "1.4");
        zigzagPolyline.setAttribute("stroke-linecap", "round");
        zigzagPolyline.setAttribute("stroke-linejoin", "round");
        zigzagPolyline.setAttribute("stroke-dasharray", "5 4");
        zigzagPolyline.setAttribute("opacity", "0");
        zigzagLayer.appendChild(zigzagPolyline);
        // Preview leg from last point to cursor while in edit mode.
        const previewLine = document.createElementNS(SVG_NS, "line");
        previewLine.setAttribute("stroke", theme === "dark" ? "#a1a1aa" : "#52525b");
        previewLine.setAttribute("stroke-width", "1.4");
        previewLine.setAttribute("stroke-linecap", "round");
        previewLine.setAttribute("stroke-dasharray", "5 4");
        previewLine.setAttribute("opacity", "0");
        zigzagLayer.appendChild(previewLine);
        svg.appendChild(zigzagLayer);
        zigzagPolylineRef.current = zigzagPolyline;
        previewLineRef.current = previewLine;

        // Swing layer — appended LAST so it always sits above FVG/fib/zigzag,
        // but clipped to the live plot rect. That keeps swing tags anchored to
        // their true candle/price coordinates instead of nudging them inward
        // when they approach the price or time scales.
        const swingLayer = document.createElementNS(SVG_NS, "g");
        swingLayer.setAttribute("data-layer", "swings");
        swingLayer.setAttribute("clip-path", plotClipUrl);
        const swingStyles = {
          bullish: { fill: "#2a3d27", stroke: "#395935" },
          bearish: { fill: "#4a282a", stroke: "#703639" },
        } as const;

        // Builds (or rebuilds) the swing layer DOM from a swing list. In
        // edit mode each swing also gets an invisible 28×28 hit-rect that
        // captures pointer events and routes clicks back to the parent
        // modal (which opens the popover). Outside edit mode no listeners
        // are attached so pan/zoom of the chart is unaffected.
        const buildSwings = (source: SwingPoint[], edit: boolean) => {
          while (swingLayer.firstChild) swingLayer.removeChild(swingLayer.firstChild);
          const next: SwingEl[] = [];
          for (let idx = 0; idx < source.length; idx++) {
            const sw = source[idx];
            const isHigh = sw.label === "HH" || sw.label === "LH";
            const bullishTag = sw.label === "HH" || sw.label === "HL";
            const style = bullishTag ? swingStyles.bullish : swingStyles.bearish;
            const g = document.createElementNS(SVG_NS, "g");
            g.setAttribute("data-swing-idx", String(idx));
            const stem = document.createElementNS(SVG_NS, "line");
            stem.setAttribute("stroke", style.stroke);
            stem.setAttribute("stroke-width", edit ? "1.4" : "1");
            stem.setAttribute("opacity", edit ? "0.95" : "0.7");
            const dot = document.createElementNS(SVG_NS, "circle");
            dot.setAttribute("r", edit ? "3.5" : "2.5");
            dot.setAttribute("fill", style.stroke);
            dot.setAttribute("stroke", "#0a0a0a");
            dot.setAttribute("stroke-width", "0.6");
            const tag = document.createElementNS(SVG_NS, "rect");
            tag.setAttribute("width", String(SWING_TAG_W));
            tag.setAttribute("height", String(SWING_TAG_H));
            tag.setAttribute("rx", "2");
            tag.setAttribute("fill", style.fill);
            tag.setAttribute("stroke", style.stroke);
            tag.setAttribute("stroke-width", edit ? "1.4" : "1");
            const tagText = document.createElementNS(SVG_NS, "text");
            tagText.setAttribute("text-anchor", "middle");
            tagText.setAttribute("alignment-baseline", "middle");
            tagText.setAttribute("dominant-baseline", "middle");
            tagText.setAttribute("font-size", "8");
            tagText.setAttribute("font-family", "Inter, sans-serif");
            tagText.setAttribute("font-weight", "200");
            tagText.setAttribute("fill", "#dbdbdb");
            tagText.setAttribute("stroke", "none");
            tagText.setAttribute("stroke-width", "0");
            tagText.textContent = sw.label;
            g.appendChild(stem);
            g.appendChild(dot);
            g.appendChild(tag);
            g.appendChild(tagText);
            let hit: SVGRectElement | null = null;
            if (edit) {
              hit = document.createElementNS(SVG_NS, "rect");
              hit.setAttribute("width", "32");
              hit.setAttribute("height", "32");
              hit.setAttribute("fill", "transparent");
              hit.setAttribute("style", "cursor: grab; pointer-events: all;");
              const swingIdx = idx;
              hit.addEventListener("pointerdown", (e) => {
                e.stopPropagation();
                e.preventDefault();
                const target = e.currentTarget as SVGRectElement;
                try { target.setPointerCapture(e.pointerId); } catch { /* noop */ }
                target.setAttribute("style", "cursor: grabbing; pointer-events: all;");
                dragRef.current = { idx: swingIdx, x: e.clientX, y: e.clientY, moved: false };
              });
              hit.addEventListener("pointermove", (e) => {
                const drag = dragRef.current;
                if (!drag || drag.idx !== swingIdx) return;
                if (Math.abs(e.clientX - drag.x) > 3 || Math.abs(e.clientY - drag.y) > 3) {
                  drag.moved = true;
                }
                drag.x = e.clientX;
                drag.y = e.clientY;
              });
              const finishDrag = (e: PointerEvent) => {
                const drag = dragRef.current;
                const target = e.currentTarget as SVGRectElement;
                target.setAttribute("style", "cursor: grab; pointer-events: all;");
                try { target.releasePointerCapture(e.pointerId); } catch { /* noop */ }
                if (!drag || drag.idx !== swingIdx) {
                  dragRef.current = null;
                  return;
                }
                if (!drag.moved) {
                  dragRef.current = null;
                  const rect = target.getBoundingClientRect();
                  onSwingClickRef.current?.({
                    idx: swingIdx,
                    clientX: rect.left + rect.width / 2,
                    clientY: rect.top + rect.height / 2,
                  });
                  return;
                }
                const root = containerRef.current;
                if (!root) { dragRef.current = null; return; }
                const rootRect = root.getBoundingClientRect();
                const localX = e.clientX - rootRect.left;
                const localY = e.clientY - rootRect.top;
                let newPrice: number | null = null;
                let newTs: number | null = null;
                try {
                  const p = candles.coordinateToPrice(localY);
                  if (p != null && Number.isFinite(Number(p))) newPrice = Number(p);
                } catch { /* noop */ }
                try {
                  const t = chart.timeScale().coordinateToTime?.(localX);
                  if (t != null) newTs = Number(t) * 1000;
                } catch { /* noop */ }
                if (newPrice == null || newTs == null) {
                  dragRef.current = null;
                  return;
                }
                dragRef.current = null;
                onSwingDragEndRef.current?.(swingIdx, newTs, newPrice);
              };
              hit.addEventListener("pointerup", finishDrag);
              hit.addEventListener("pointercancel", finishDrag);
              hit.addEventListener("mouseenter", () => {
                if (dragRef.current) return;
                tag.setAttribute("stroke-width", "2.2");
                dot.setAttribute("r", "4.5");
              });
              hit.addEventListener("mouseleave", () => {
                tag.setAttribute("stroke-width", "1.4");
                dot.setAttribute("r", "3.5");
              });
              g.appendChild(hit);
            }
            swingLayer.appendChild(g);
            next.push({ ts: sw.ts, price: sw.price, label: sw.label, isHigh, tag, tagText, stem, dot, hit, group: g });
          }
          swingElementsRef.current = next;
        };
        svg.appendChild(swingLayer);
        rebuildSwingsRef.current = buildSwings;
        serverSwingsRef.current = data.swings;
        buildSwings(data.swings, false);

        // ── Coordinate helpers ──────────────────────────────────────────
        const safePriceY = (price: number): number | null => {
          try {
            const v = candles.priceToCoordinate(price);
            return v == null || !Number.isFinite(Number(v)) ? null : Number(v);
          } catch { return null; }
        };
        const safeTimeX = (tsMs: number): number | null => {
          const time = Math.floor(tsMs / 1000);
          try {
            const v = chart.timeScale().timeToCoordinate(time);
            if (v != null && Number.isFinite(Number(v))) return Number(v);
          } catch { /* fall through */ }
          const logicalIndex = barIndexByTime.get(time);
          if (logicalIndex == null) return null;
          try {
            const v = chart.timeScale().logicalToCoordinate?.(logicalIndex);
            if (v != null && Number.isFinite(Number(v))) return Number(v);
          } catch { /* noop */ }
          return null;
        };
        const safeLogicalX = (logicalIndex: number, plotRight: number): number | null => {
          if (!Number.isFinite(logicalIndex)) return null;
          try {
            const v = chart.timeScale().logicalToCoordinate?.(logicalIndex);
            if (v != null && Number.isFinite(Number(v))) return Number(v);
          } catch { /* fall through */ }

          // Fallback projection mirrors the chart's logical range transform.
          try {
            const lr = chart.timeScale().getVisibleLogicalRange?.();
            const from = Number(lr?.from);
            const to = Number(lr?.to);
            if (Number.isFinite(from) && Number.isFinite(to) && to > from && plotRight > 0) {
              return ((logicalIndex - from) / (to - from)) * plotRight;
            }
          } catch { /* noop */ }
          return null;
        };
        const getRightSideFibRange = (): { x1: number; x2: number } | null => {
          if (candleData.length <= 0) return null;
          const currentIndex = candleData.length - 1;
          const x2 = currentIndex + FIB_RIGHT_OFFSET_BARS;
          return { x1: x2 - FIB_LINE_LENGTH_BARS, x2 };
        };
        const logicalIndexFromTs = (tsMs: number): number | null => {
          const time = Math.floor(tsMs / 1000);
          const idx = barIndexByTime.get(time);
          return idx == null ? null : idx;
        };
        const raiseRightPriceScaleLayer = (plotRight: number) => {
          const root = containerRef.current;
          if (!root) return;
          const rootRect = root.getBoundingClientRect();
          for (const canvas of Array.from(root.querySelectorAll("canvas"))) {
            const rect = canvas.getBoundingClientRect();
            const left = rect.left - rootRect.left;
            if (rect.width > 0 && left >= plotRight - 1) {
              canvas.style.zIndex = "30";
              const parent = canvas.parentElement;
              if (parent) {
                parent.style.position = parent.style.position || "relative";
                parent.style.zIndex = "30";
              }
            }
          }
        };

        // ── Cursor tracking + click-to-add (edit mode) ──────────────────
        const root = containerRef.current;
        const onPointerMove = (e: PointerEvent) => {
          if (!root) return;
          const r = root.getBoundingClientRect();
          cursorRef.current = {
            x: e.clientX - r.left,
            y: e.clientY - r.top,
            inside: true,
          };
        };
        const onPointerLeave = () => {
          cursorRef.current = { x: 0, y: 0, inside: false };
        };
        // Click-to-add: distinguish click from chart pan via mousedown→up
        // displacement (chart pans on pressed-mouse-move).
        let downAt: { x: number; y: number; t: number } | null = null;
        const onPointerDown = (e: PointerEvent) => {
          if (!editModeRef.current) return;
          // If the press lands on a swing hit-rect, the swing-level handler
          // takes over (drag or popover-click). Skip the canvas-add path.
          const tgt = e.target as Element | null;
          if (tgt && tgt.closest && tgt.closest("[data-swing-idx]")) {
            downAt = null;
            return;
          }
          downAt = { x: e.clientX, y: e.clientY, t: Date.now() };
        };
        const onPointerUp = (e: PointerEvent) => {
          const start = downAt;
          downAt = null;
          // If a swing drag is in flight, the swing-level handler will commit;
          // skip canvas-add to avoid double-adding a point.
          if (dragRef.current) return;
          if (!editModeRef.current || !start || !root) return;
          if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > 4) return;
          if (Date.now() - start.t > 600) return;
          const r = root.getBoundingClientRect();
          const localX = e.clientX - r.left;
          const localY = e.clientY - r.top;
          let newPrice: number | null = null;
          let newTs: number | null = null;
          try {
            const p = candles.coordinateToPrice(localY);
            if (p != null && Number.isFinite(Number(p))) newPrice = Number(p);
          } catch { /* noop */ }
          try {
            const tt = chart.timeScale().coordinateToTime?.(localX);
            if (tt != null) newTs = Number(tt) * 1000;
          } catch { /* noop */ }
          if (newPrice == null || newTs == null) return;
          onCanvasClickRef.current?.(newTs, newPrice);
        };
        // capture:true so we still see the events even if lightweight-charts
        // installs its own listeners on inner canvases.
        root?.addEventListener("pointermove", onPointerMove, true);
        root?.addEventListener("pointerleave", onPointerLeave, true);
        root?.addEventListener("pointerdown", onPointerDown, true);
        root?.addEventListener("pointerup", onPointerUp, true);
        cleanupListeners = () => {
          root?.removeEventListener("pointermove", onPointerMove, true);
          root?.removeEventListener("pointerleave", onPointerLeave, true);
          root?.removeEventListener("pointerdown", onPointerDown, true);
          root?.removeEventListener("pointerup", onPointerUp, true);
        };

        // ── rAF render loop ─────────────────────────────────────────────
        // Repositions overlay only. NEVER calls setVisibleLogicalRange.
        const update = () => {
          try {
            if (destroyed || !chart || !candles || !overlayRef.current || !containerRef.current) return;
            const w = containerRef.current.clientWidth;
            const h = chartHeight;
            if (w <= 0) return;

            let psWidth = 60;
            try { psWidth = chart.priceScale("right").width() || 60; } catch { /* pre-render */ }
            const plotRight = Math.max(0, Math.min(w, w - psWidth));
            raiseRightPriceScaleLayer(plotRight);
            overlayRef.current.setAttribute("viewBox", `0 0 ${w} ${h}`);
            let timeScaleH = 0;
            try { timeScaleH = Number(chart.timeScale().height?.() ?? 0) || 0; } catch { /* noop */ }
            const plotBottom = Math.max(0, Math.min(h, h - timeScaleH));
            plotClipRect.setAttribute("x", "0");
            plotClipRect.setAttribute("y", "0");
            plotClipRect.setAttribute("width", String(plotRight));
            plotClipRect.setAttribute("height", String(plotBottom));
            if (plotRight <= 0 || plotBottom <= 0) return;

            if (candleData.length > 0 && livePriceLine) {
              try { livePriceLine.applyOptions({ price: candleData[candleData.length - 1].close }); } catch { /* noop */ }
            }

            // Fib lines — chart-space primitives:
            // X = logical bars, Y = price. Zoom/scroll/resize comes from the chart projection.
            if (isBullish && data.fib_high != null && data.fib_low != null) {
              const fibHigh = data.fib_high;
              const fibLow = data.fib_low;
              const range = fibHigh - fibLow;
              const fibRange = getRightSideFibRange();
              const lineX1 = fibRange == null ? null : safeLogicalX(fibRange.x1, plotRight);
              const lineX2 = fibRange == null ? null : safeLogicalX(fibRange.x2, plotRight);
              for (const f of fibElements) {
                const y = safePriceY(fibHigh - f.ratio * range);
                if (lineX1 == null || lineX2 == null || y == null || y < 0 || y > plotBottom) {
                  f.group.setAttribute("opacity", "0");
                  continue;
                }
                f.group.setAttribute("opacity", "1");
                f.line.setAttribute("x1", String(lineX1));
                f.line.setAttribute("x2", String(lineX2));
                f.line.setAttribute("y1", String(y));
                f.line.setAttribute("y2", String(y));
                f.text.setAttribute("x", String(lineX2 + FIB_LABEL_GAP));
                f.text.setAttribute("y", String(y));
              }
            }

            if (placeholder) {
              const fibRange = getRightSideFibRange();
              const lineX1 = fibRange == null ? null : safeLogicalX(fibRange.x1, plotRight);
              const lineX2 = fibRange == null ? null : safeLogicalX(fibRange.x2, plotRight);
              const cx = lineX1 != null && lineX2 != null ? (lineX1 + lineX2) / 2 : plotRight / 2;
              // Y: center of bearish OTE zone (0.618–0.786 retracement from fib_low upward).
              let cy = Math.max(12, h / 2);
              let fontSize = 12;
              if (data.fib_high != null && data.fib_low != null && data.fib_high > data.fib_low) {
                const range = data.fib_high - data.fib_low;
                const oteA = data.fib_low + range * 0.618;
                const oteB = data.fib_low + range * 0.786;
                const yA = safePriceY(oteA);
                const yB = safePriceY(oteB);
                if (yA != null && yB != null) {
                  cy = (yA + yB) / 2;
                  fontSize = Math.max(8, Math.min(20, Math.abs(yB - yA) * 0.6));
                }
              }
              placeholder.setAttribute("x", String(cx));
              placeholder.setAttribute("y", String(cy));
              placeholder.setAttribute("font-size", String(Math.round(fontSize)));
            }

            // FVG visibility is driven by live refs so toggle is instant.
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

            // Swings — visibility controlled by logical index range only.
            // floor/ceil so bars at fractional edges stay visible (lightweight
            // charts returns floating-point logical bounds when zoomed).
            // x == null does not invalidate state; the group simply hides
            // until coordinates resolve again on a future frame.
            const lr = chart.timeScale().getVisibleLogicalRange?.();
            const lrFrom = lr && Number.isFinite(Number(lr.from)) ? Math.floor(Number(lr.from)) : -Infinity;
            const lrTo = lr && Number.isFinite(Number(lr.to)) ? Math.ceil(Number(lr.to)) : Infinity;
            const currentSwings = swingElementsRef.current;
            const drag = dragRef.current;
            // Track on-screen positions so the zig-zag layer can connect
            // them in this same frame (saves a second pass).
            const zigzagPoints: { x: number; y: number }[] = [];
            for (let si = 0; si < currentSwings.length; si++) {
              const sw = currentSwings[si];
              // While dragging, override this point's screen position with
              // the cursor so the visual follows in real time before commit.
              if (drag && drag.idx === si && containerRef.current) {
                const r = containerRef.current.getBoundingClientRect();
                const dx = drag.x - r.left;
                const dy = drag.y - r.top;
                sw.group.setAttribute("opacity", "1");
                const offset = sw.isHigh ? -16 : 16;
                const tagY = dy + offset - 6;
                const tagX = dx - SWING_TAG_W / 2;
                sw.stem.setAttribute("x1", String(dx));
                sw.stem.setAttribute("x2", String(dx));
                sw.stem.setAttribute("y1", String(dy));
                sw.stem.setAttribute("y2", String(sw.isHigh ? tagY + SWING_TAG_H : tagY));
                sw.dot.setAttribute("cx", String(dx));
                sw.dot.setAttribute("cy", String(dy));
                sw.tag.setAttribute("x", String(tagX));
                sw.tag.setAttribute("y", String(tagY));
                sw.tagText.setAttribute("x", String(dx));
                sw.tagText.setAttribute("y", String(tagY + SWING_TAG_H / 2 + 0.5));
                if (sw.hit) {
                  sw.hit.setAttribute("x", String(dx - 16));
                  sw.hit.setAttribute("y", String(Math.min(dy, tagY) - 4));
                  sw.hit.setAttribute("height", String(Math.abs(tagY + SWING_TAG_H - dy) + 12));
                }
                zigzagPoints.push({ x: dx, y: dy });
                continue;
              }
              const idx = logicalIndexFromTs(sw.ts);
              if (idx == null || idx < lrFrom || idx > lrTo) {
                sw.group.setAttribute("opacity", "0");
                continue;
              }
              const x = safeTimeX(sw.ts);
              const y = safePriceY(sw.price);
              if (x == null || y == null) {
                sw.group.setAttribute("opacity", "0");
                continue;
              }
              sw.group.setAttribute("opacity", "1");
              const offset = sw.isHigh ? -16 : 16;
              const rawTagY = y + offset - 6;
              const tagY = rawTagY;
              const tagX = x - SWING_TAG_W / 2;
              sw.stem.setAttribute("x1", String(x));
              sw.stem.setAttribute("x2", String(x));
              sw.stem.setAttribute("y1", String(y));
              sw.stem.setAttribute("y2", String(sw.isHigh ? tagY + SWING_TAG_H : tagY));
              sw.dot.setAttribute("cx", String(x));
              sw.dot.setAttribute("cy", String(y));
              sw.tag.setAttribute("x", String(tagX));
              sw.tag.setAttribute("y", String(tagY));
              sw.tagText.setAttribute("x", String(x));
              sw.tagText.setAttribute("y", String(tagY + SWING_TAG_H / 2 + 0.5));
              if (sw.hit) {
                sw.hit.setAttribute("x", String(x - 16));
                sw.hit.setAttribute("y", String(Math.min(y, tagY) - 4));
                sw.hit.setAttribute("height", String(Math.abs(tagY + SWING_TAG_H - y) + 12));
              }
              zigzagPoints.push({ x, y });
            }
            const zz = zigzagPolylineRef.current;
            const inEdit = currentSwings.length > 0 && currentSwings[0].hit !== null;
            if (zz) {
              if (zigzagPoints.length > 1 && inEdit) {
                zz.setAttribute(
                  "points",
                  zigzagPoints.map((p) => `${p.x},${p.y}`).join(" ")
                );
                zz.setAttribute("opacity", "0.7");
              } else {
                zz.setAttribute("opacity", "0");
              }
            }
            // Preview leg from last swing to cursor while in edit mode.
            const pv = previewLineRef.current;
            if (pv) {
              const cur = cursorRef.current;
              if (
                inEdit &&
                cur.inside &&
                !drag &&
                zigzagPoints.length > 0 &&
                cur.x >= 0 && cur.x <= plotRight &&
                cur.y >= 0 && cur.y <= plotBottom
              ) {
                const last = zigzagPoints[zigzagPoints.length - 1];
                pv.setAttribute("x1", String(last.x));
                pv.setAttribute("y1", String(last.y));
                pv.setAttribute("x2", String(cur.x));
                pv.setAttribute("y2", String(cur.y));
                pv.setAttribute("opacity", "0.5");
              } else {
                pv.setAttribute("opacity", "0");
              }
            }
          } catch (e: any) {
            if (!reportedRafErrorRef.current) {
              reportedRafErrorRef.current = true;
              void reportFrontendError({
                kind: "chart.raf",
                message: e?.message ?? "chart raf error",
                stack: e?.stack,
                source: "CandleChart.update",
                context: { symbol, interval, theme, chartHeight, reloadKey },
              });
            }
          } finally {
            if (!destroyed) rafId = requestAnimationFrame(update);
          }
        };
        rafId = requestAnimationFrame(update);

        setStatus("ok");
      } catch (e: any) {
        void reportFrontendError({
          kind: "chart.init",
          message: e?.message ?? "chart error",
          stack: e?.stack,
          source: "CandleChart.init",
          context: { symbol, interval, theme, chartHeight, reloadKey },
        });
        if (!destroyed) { setErr(e.message ?? "chart error"); setStatus("error"); }
      }
    };

    init();

    return () => {
      destroyed = true;
      cancelAnimationFrame(rafId);
      fvgElementsRef.current = [];
      rebuildSwingsRef.current = null;
      zigzagPolylineRef.current = null;
      previewLineRef.current = null;
      swingElementsRef.current = [];
      dragRef.current = null;
      try { cleanupListeners?.(); } catch { /* noop */ }
      cleanupListeners = null;
      try { chart?.remove(); } catch { /* double-remove */ }
      chart = null;
      candles = null;
    };
  }, [symbol, interval, theme, chartHeight, reloadKey]);

  // Rebuild swing layer when edit mode toggles or draft changes. Edit mode
  // sources swings from the draft (instant feedback for clicks); outside
  // edit mode we revert to the engine-computed swings from the server.
  useEffect(() => {
    const rebuild = rebuildSwingsRef.current;
    if (!rebuild) return;
    const source: SwingPoint[] = editMode && draft
      ? draft.map((d) => ({ ts: d.ts, price: d.price, label: d.label }))
      : serverSwingsRef.current;
    rebuild(source, !!editMode);
  }, [editMode, draft]);

  return (
    <div className="relative" style={{ height: chartHeight, background: t.bg }}>
      <div ref={containerRef} style={{ width: "100%", height: chartHeight }} />
      <svg
        ref={overlayRef}
        className="pointer-events-none absolute inset-0"
        style={{ zIndex: 12 }}
        width="100%"
        height={chartHeight}
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
  onAfterSave,
}: {
  row: DashboardRow;
  orderedSymbols: string[];
  rows: DashboardRow[];
  source: "local" | "global" | null;
  onSwitchSymbol: (symbol: string) => void;
  onClose: (reason?: string) => void;
  onAfterSave: () => void;
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

  // ── Structure edit mode ──────────────────────────────────────────────────
  // Single source of truth for both the chart SVG and the screener tables.
  // Entering edit mode seeds the draft from the override (if any) or from
  // the engine-computed swings currently shown on the chart.
  const [editMode, setEditMode] = useState(false);
  const [draft, setDraft] = useState<StructureEvent[]>([]);
  const [draftDirty, setDraftDirty] = useState(false);
  const [editStatus, setEditStatus] = useState<
    | { kind: "idle" }
    | { kind: "loading" }
    | { kind: "saving" }
    | { kind: "saved" }
    | { kind: "error"; message: string }
  >({ kind: "idle" });
  const [chartReloadKey, setChartReloadKey] = useState(0);
  const [popover, setPopover] = useState<{ idx: number; x: number; y: number } | null>(null);

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

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.__kazusChartContext = {
      symbol: activeSymbol,
      interval,
      tab,
      theme,
      editMode,
    };
    return () => {
      if (window.__kazusChartContext?.symbol === activeSymbol) {
        window.__kazusChartContext = null;
      }
    };
  }, [activeSymbol, interval, tab, theme, editMode]);

  useEffect(() => {
    void reportFrontendError({
      kind: "chart.modal",
      message: `modal active ${activeSymbol} ${interval}`,
      source: "ChartModal.lifecycle",
      context: { symbol: activeSymbol, interval, tab, theme, editMode },
    });
    return () => {
      void reportFrontendError({
        kind: "chart.modal",
        message: `modal cleanup ${activeSymbol} ${interval}`,
        source: "ChartModal.lifecycle",
        context: { symbol: activeSymbol, interval, tab, theme, editMode },
      });
    };
  }, [activeSymbol, interval, tab, theme, editMode]);

  // ── Edit-mode helpers ────────────────────────────────────────────────────
  function confirmDiscardIfDirty(): boolean {
    if (!editMode || !draftDirty) return true;
    return window.confirm("Discard unsaved structure edits?");
  }

  async function enterEditMode() {
    setEditStatus({ kind: "loading" });
    try {
      // Seed draft from the structure currently displayed on the chart so
      // that toggling edit mode never changes what the user sees. The
      // backend already applies any saved override before computing this
      // timeline, so this captures override + engine extension as one.
      const chart = await getChartCached(activeSymbol, interval);
      const seed: StructureEvent[] = chart.swings
        .filter((s) =>
          s.label === "HH" || s.label === "HL" || s.label === "LL" || s.label === "LH"
        )
        .map((s) => ({
          ts: s.ts,
          price: s.price,
          label: s.label as StructureEvent["label"],
        }));
      setDraft(seed);
      setDraftDirty(false);
      setEditMode(true);
      setEditStatus({ kind: "idle" });
    } catch (err: any) {
      setEditStatus({
        kind: "error",
        message: err?.message ?? "failed to load structure",
      });
    }
  }

  function exitEditMode() {
    if (!confirmDiscardIfDirty()) return;
    setEditMode(false);
    setDraft([]);
    setDraftDirty(false);
    setEditStatus({ kind: "idle" });
    setPopover(null);
  }

  function updateDraft(next: StructureEvent[]) {
    setDraft(next);
    setDraftDirty(true);
  }

  async function saveDraft() {
    setEditStatus({ kind: "saving" });
    try {
      // Sort by ts so storage matches what the engine will replay.
      const events = [...draft].sort((a, b) => a.ts - b.ts);
      await saveStructure(activeSymbol, interval, events);
      invalidateChartCache(activeSymbol, interval);
      setChartReloadKey((k) => k + 1);
      setDraftDirty(false);
      setEditStatus({ kind: "saved" });
      onAfterSave();
      // Keep edit mode open after save — common workflow is iterative.
    } catch (err: any) {
      setEditStatus({
        kind: "error",
        message: err?.message ?? "save failed",
      });
    }
  }

  async function resetToAuto() {
    if (!window.confirm("Delete saved structure for this symbol/timeframe?")) return;
    setEditStatus({ kind: "saving" });
    try {
      await deleteStructure(activeSymbol, interval);
      invalidateChartCache(activeSymbol, interval);
      setChartReloadKey((k) => k + 1);
      // Re-seed draft from engine swings of the freshly-fetched chart.
      const chart = await getChartCached(activeSymbol, interval);
      const seed: StructureEvent[] = chart.swings
        .filter((s) =>
          s.label === "HH" || s.label === "HL" || s.label === "LL" || s.label === "LH"
        )
        .map((s) => ({
          ts: s.ts,
          price: s.price,
          label: s.label as StructureEvent["label"],
        }));
      setDraft(seed);
      setDraftDirty(false);
      setEditStatus({ kind: "saved" });
      onAfterSave();
    } catch (err: any) {
      setEditStatus({
        kind: "error",
        message: err?.message ?? "reset failed",
      });
    }
  }

  // Switching coin / tab / closing while editing must confirm discard.
  function guardedSwitchSymbol(sym: string) {
    if (!confirmDiscardIfDirty()) return;
    if (editMode) {
      setEditMode(false);
      setDraft([]);
      setDraftDirty(false);
      setEditStatus({ kind: "idle" });
      setPopover(null);
    }
    onSwitchSymbol(sym);
  }
  function guardedSetTab(t: ChartTab) {
    if (t === tab) return;
    if (!confirmDiscardIfDirty()) return;
    if (editMode) {
      setEditMode(false);
      setDraft([]);
      setDraftDirty(false);
      setEditStatus({ kind: "idle" });
      setPopover(null);
    }
    setTab(t);
    localStorage.setItem(CHART_TAB_KEY, t);
  }
  function guardedClose(reason = "unknown") {
    if (!confirmDiscardIfDirty()) return;
    void reportFrontendError({
      kind: "chart.close",
      message: `close chart ${activeSymbol} ${interval} via ${reason}`,
      source: "ChartModal.guardedClose",
      context: {
        symbol: activeSymbol,
        interval,
        tab,
        theme,
        editMode,
        reason,
      },
    });
    onClose(reason);
  }
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
        guardedSwitchSymbol(prevSymbol);
      } else if (e.key === "ArrowRight" && nextSymbol) {
        e.preventDefault();
        navDirRef.current = "next";
        guardedSwitchSymbol(nextSymbol);
      } else if (e.key === "Escape") {
        if (document.fullscreenElement) {
          e.preventDefault();
          void document.exitFullscreen();
        } else {
          guardedClose("escape");
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prevSymbol, nextSymbol, editMode, draftDirty]);

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
              onClick={() => { if (prevSymbol) { navDirRef.current = "prev"; guardedSwitchSymbol(prevSymbol); } }}
              disabled={!prevSymbol}
              className="kz-nav sideCoin sideCoin-near kz-coin-side h-6 w-[90px] justify-self-center truncate disabled:pointer-events-none disabled:opacity-0"
              title={nearPrevLabel ? `← ${nearPrevLabel}` : ""}
            >
              {nearPrevLabel}
            </button>
            <button
              type="button"
              onClick={() => { if (prevSymbol) { navDirRef.current = "prev"; guardedSwitchSymbol(prevSymbol); } }}
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
              onClick={() => { if (nextSymbol) { navDirRef.current = "next"; guardedSwitchSymbol(nextSymbol); } }}
              disabled={!nextSymbol}
              className="kz-nav arrow kz-coin-arrow h-7 w-[28px] justify-self-center disabled:pointer-events-none disabled:opacity-0"
              aria-label="Next coin"
            >
              ›
            </button>
            <button
              type="button"
              onClick={() => { if (nextSymbol) { navDirRef.current = "next"; guardedSwitchSymbol(nextSymbol); } }}
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
                onClick={() => guardedSetTab(t)}
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
              disabled={isFullscreen}
              className={`${btnIcon} disabled:opacity-30 disabled:pointer-events-none`}
              style={btnStyle}
              title={isFullscreen ? "Disabled in full screen" : "Smaller chart"}
            >
              −
            </button>
            <button
              onClick={() => resizeChart(60)}
              disabled={isFullscreen}
              className={`${btnIcon} disabled:opacity-30 disabled:pointer-events-none`}
              style={btnStyle}
              title={isFullscreen ? "Disabled in full screen" : "Larger chart"}
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
            {/* ── Structure edit controls ── */}
            {!editMode ? (
              <button
                onClick={enterEditMode}
                className="kz-btn h-8 px-3 inline-flex items-center rounded-md border text-[11px] tracking-[0.22em] uppercase"
                style={btnStyle}
                title="Edit structure (manual swings)"
                disabled={editStatus.kind === "loading"}
              >
                {editStatus.kind === "loading" ? "…" : "✎ Edit"}
              </button>
            ) : (
              <>
                <button
                  onClick={saveDraft}
                  disabled={!draftDirty || editStatus.kind === "saving"}
                  className="kz-btn h-8 px-3 inline-flex items-center rounded-md border text-[11px] tracking-[0.22em] uppercase disabled:opacity-40 disabled:pointer-events-none"
                  style={{ ...btnStyle, color: "#7fb678", borderColor: "#7fb678" }}
                  title="Save manual structure"
                >
                  {editStatus.kind === "saving" ? "…" : "Save"}
                </button>
                <button
                  onClick={exitEditMode}
                  className="kz-btn h-8 px-3 inline-flex items-center rounded-md border text-[11px] tracking-[0.22em] uppercase"
                  style={btnStyle}
                  title="Cancel edits"
                >
                  Cancel
                </button>
                <button
                  onClick={resetToAuto}
                  disabled={editStatus.kind === "saving"}
                  className="kz-btn h-8 px-3 inline-flex items-center rounded-md border text-[11px] tracking-[0.22em] uppercase"
                  style={{ ...btnStyle, color: "#d68b8b", borderColor: "#d68b8b" }}
                  title="Delete saved structure (revert to auto)"
                >
                  Reset
                </button>
                {editStatus.kind === "saving" && (
                  <span className="text-[10px] font-mono ml-1" style={{ color: subText }}>
                    saving…
                  </span>
                )}
                {editStatus.kind === "saved" && (
                  <span className="text-[10px] font-mono ml-1" style={{ color: "#7fb678" }}>
                    saved · tables update ~15s
                  </span>
                )}
                {editStatus.kind === "error" && (
                  <span className="text-[10px] font-mono ml-1" style={{ color: "#d68b8b" }}>
                    {editStatus.message}
                  </span>
                )}
              </>
            )}
            <button
              onClick={() => guardedClose("button")}
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
              reloadKey={chartReloadKey}
              editMode={editMode}
              draft={draft}
              onSwingClick={(a) =>
                setPopover({ idx: a.idx, x: a.clientX, y: a.clientY })
              }
              onCanvasClick={(ts, price) => {
                // Auto-label by direction relative to the previous opposite-
                // side swing: above prior high → HH, below → LH; above prior
                // low → HL, below → LL. With no prior counterpart, default
                // to the natural alternation from the last point.
                const sorted = [...draft].sort((a, b) => a.ts - b.ts);
                const last = sorted[sorted.length - 1];
                const isHigh = (l: StructureEvent["label"]) => l === "HH" || l === "LH";
                const isLow = (l: StructureEvent["label"]) => l === "HL" || l === "LL";
                let label: StructureEvent["label"];
                if (!last) {
                  label = "HH";
                } else if (isHigh(last.label)) {
                  // New point is a low.
                  const prevLow = [...sorted].reverse().find((s) => isLow(s.label));
                  label = prevLow ? (price > prevLow.price ? "HL" : "LL") : "HL";
                } else {
                  // New point is a high.
                  const prevHigh = [...sorted].reverse().find((s) => isHigh(s.label));
                  label = prevHigh ? (price > prevHigh.price ? "HH" : "LH") : "HH";
                }
                const next = [...draft, { ts, price, label }].sort((a, b) => a.ts - b.ts);
                updateDraft(next);
              }}
              onSwingDragEnd={(idx, ts, price) => {
                const next = draft.map((d, i) =>
                  i === idx ? { ...d, ts, price } : d
                );
                next.sort((a, b) => a.ts - b.ts);
                updateDraft(next);
              }}
            />
          </div>
        </div>
        {popover && editMode && draft[popover.idx] && (
          <SwingPopover
            event={draft[popover.idx]}
            anchor={popover}
            onClose={() => setPopover(null)}
            onChangeLabel={(label) => {
              updateDraft(
                draft.map((d, i) =>
                  i === popover.idx ? { ...d, label } : d
                )
              );
            }}
            onDelete={() => {
              updateDraft(draft.filter((_, i) => i !== popover.idx));
              setPopover(null);
            }}
          />
        )}

      </div>
    </div>
  );
}

// ── Swing Popover (edit-mode only) ──────────────────────────────────────────
// Anchored to the swing's screen position via fixed positioning. The four
// label pills swap the label in-place; ✕ deletes the swing from the draft.
// Outside-click and Esc close the popover.

const SWING_POPOVER_LABELS: { value: StructureEvent["label"]; tone: "bull" | "bear" }[] = [
  { value: "HH", tone: "bull" },
  { value: "HL", tone: "bull" },
  { value: "LH", tone: "bear" },
  { value: "LL", tone: "bear" },
];

function SwingPopover({
  event,
  anchor,
  onClose,
  onChangeLabel,
  onDelete,
}: {
  event: StructureEvent;
  anchor: { x: number; y: number };
  onClose: () => void;
  onChangeLabel: (label: StructureEvent["label"]) => void;
  onDelete: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      if (!ref.current) return;
      if (!ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // defer so the click that opened the popover doesn't immediately close it.
    const id = window.setTimeout(() => {
      document.addEventListener("mousedown", onDocClick);
    }, 0);
    document.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(id);
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const date = new Date(event.ts);
  const pad = (n: number) => String(n).padStart(2, "0");
  const formatted = `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())} UTC`;

  return (
    <div
      ref={ref}
      className="fixed z-[60] rounded-md border shadow-lg p-2 font-mono"
      style={{
        left: Math.max(8, Math.min(window.innerWidth - 220, anchor.x - 100)),
        top: Math.max(8, anchor.y - 96),
        background: "#18181b",
        borderColor: "#3f3f46",
        color: "#e4e4e7",
        width: 200,
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-[9px] uppercase tracking-[0.18em]" style={{ color: "#a1a1aa" }}>
          Swing point
        </span>
        <button
          type="button"
          onClick={onDelete}
          className="kz-btn h-5 w-5 inline-flex items-center justify-center rounded border text-[10px]"
          style={{ color: "#d68b8b", borderColor: "#3f3f46" }}
          title="Delete swing"
        >
          ✕
        </button>
      </div>
      <div className="grid grid-cols-4 gap-1 mb-2">
        {SWING_POPOVER_LABELS.map((opt) => {
          const active = opt.value === event.label;
          const tone =
            opt.tone === "bull"
              ? { fill: "#2a3d27", stroke: "#7fb678" }
              : { fill: "#752727", stroke: "#d68b8b" };
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => onChangeLabel(opt.value)}
              className="h-7 rounded text-[11px] font-bold border"
              style={{
                background: active ? tone.fill : "transparent",
                color: active ? "#ffffff" : tone.stroke,
                borderColor: tone.stroke,
              }}
            >
              {opt.value}
            </button>
          );
        })}
      </div>
      <div className="text-[10px]" style={{ color: "#a1a1aa" }}>
        <div>{formatted}</div>
        <div>price {event.price}</div>
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
  const lastChartRowRef = useRef<DashboardRow | null>(null);

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
    reason?: string,
  ) {
    void reportFrontendError({
      kind: "chart.symbol",
      message: symbol ? `open chart ${symbol}` : "close chart",
      source: "Dashboard.setChartSymbol",
      context: { symbol, source, orderSize: order?.length, reason },
    });
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

  const freshChartRow = chartSymbol ? rows.find((r) => r.symbol === chartSymbol) ?? null : null;
  if (freshChartRow) {
    lastChartRowRef.current = freshChartRow;
  }
  const chartRow =
    freshChartRow ??
    (lastChartRowRef.current?.symbol === chartSymbol ? lastChartRowRef.current : null);

  useEffect(() => {
    if (!chartSymbol) return;
    if (freshChartRow) return;
    void reportFrontendError({
      kind: "chart.row_missing",
      message: `chart row missing for ${chartSymbol}`,
      source: "Dashboard.chartRow",
      context: {
        symbol: chartSymbol,
        rows: rows.length,
        hasFallback: lastChartRowRef.current?.symbol === chartSymbol,
      },
    });
  }, [chartSymbol, freshChartRow, rows.length]);

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
          onClose={(reason) => setChartSymbol(null, undefined, undefined, reason)}
          onAfterSave={() => {
            void refresh();
          }}
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
