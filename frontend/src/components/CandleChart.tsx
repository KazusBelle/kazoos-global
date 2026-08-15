import { useEffect, useRef, useState } from "react";
import {
  getChart,
  reportFrontendError,
  type ChartData,
  type StructureEvent,
  type SwingPoint,
} from "../lib/api";

export type ChartInterval = "1d" | "1h" | "15m" | "5m";
export type ChartTheme = "dark" | "light";

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

// Срок жизни записи в кеше. Без него Map держал данные до перезагрузки вкладки:
// открыв BTC·D1 утром, вечером вы видели утренние свечи, и линия "текущей цены"
// показывала закрытие последней загруженной свечи, а не рынок. Сбрасывался кеш
// только из saveStructure/deleteStructure, а те сейчас заглушки и бросают
// исключение — то есть не сбрасывался никогда.
// 60с: переключения таймфрейма и высоты по-прежнему идут из памяти, а данные
// перестают быть замороженными. На бэкенде у /api/chart свой TTL в 10с.
const CHART_CACHE_TTL_MS = 60_000;
const chartDataCache = new Map<string, { at: number; data: ChartData }>();
const chartDataInFlight = new Map<string, Promise<ChartData>>();

function chartCacheKey(symbol: string, interval: string) {
  return `${symbol.toUpperCase()}|${interval}`;
}

export async function getChartCached(symbol: string, interval: string) {
  const key = chartCacheKey(symbol, interval);
  const cached = chartDataCache.get(key);
  if (cached && Date.now() - cached.at < CHART_CACHE_TTL_MS) return cached.data;

  const inFlight = chartDataInFlight.get(key);
  if (inFlight) return inFlight;

  const req = getChart(symbol, interval)
    .then((data) => {
      chartDataCache.set(key, { at: Date.now(), data });
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

export function prefetchChart(symbol: string, interval: string) {
  void getChartCached(symbol, interval).catch(() => {
    // best-effort prefetch
  });
}

export function invalidateChartCache(symbol: string, interval: string) {
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
//
// `zoom` and `setupAnchorIndex` are export-only knobs. With zoom <= 1 (the
// default — the dashboard modal never passes a zoom) the result is
// byte-identical to the baseline fit. With zoom > 1 the visible window is
// shrunk around the right edge so the drawn setup renders larger; the
// setup anchor then clamps the left edge so a setup sitting further back is
// never cropped out of frame.
function computeInitialFit(
  currentIndex: number,
  swingStartIndex: number,
  zoom: number = 1,
  setupAnchorIndex: number = -1,
): LogicalRange {
  if (currentIndex < 0) return { from: 0, to: 0 };
  let span: number;
  if (swingStartIndex < 0 || swingStartIndex >= currentIndex) {
    span = FALLBACK_VISIBLE_BARS;
  } else {
    span = currentIndex - swingStartIndex;
  }
  let visibleBars = Math.min(VISIBLE_BARS_MAX, Math.max(VISIBLE_BARS_MIN, span));
  let paddingBars = Math.min(
    PADDING_BARS_MAX,
    Math.max(PADDING_BARS_MIN, Math.round(visibleBars * PADDING_RATIO)),
  );
  if (zoom > 1) {
    visibleBars = Math.max(1, Math.round(visibleBars / zoom));
    paddingBars = Math.max(0, Math.round(paddingBars / zoom));
  }
  let from = Math.max(0, currentIndex - visibleBars);
  const to = currentIndex + paddingBars;
  if (zoom > 1 && setupAnchorIndex >= 0) {
    const margin = Math.max(2, Math.round(visibleBars * 0.15));
    from = Math.min(from, Math.max(0, setupAnchorIndex - margin));
  }
  return { from, to };
}

// Earliest bar index touched by the setup overlay (swing low or FVG
// formation), or -1 when there is no overlay. Used only to clamp the
// zoomed export window so the setup stays in frame.
function computeSetupAnchorIndex(
  overlay: SetupOverlay | null | undefined,
  barIndexByTime: Map<number, number>,
): number {
  if (!overlay) return -1;
  let best = -1;
  const tsCandidates = [
    overlay.swingLow?.ts,
    ...overlay.fvgs.map((f) => f.ts),
  ];
  for (const ts of tsCandidates) {
    if (ts == null) continue;
    const idx = barIndexByTime.get(Math.floor(ts / 1000));
    if (idx != null && (best < 0 || idx < best)) best = idx;
  }
  return best;
}

// Baseline (zoom=1) candle price range over the unzoomed fit window.
// The export's uniform magnification divides this range by the zoom
// factor, so the Y axis magnifies by exactly the same factor as the X
// axis (fewer bars across the same width). Candles then keep their
// normal aspect ratio instead of being stretched only horizontally.
function computeBaselinePriceRange(
  candleData: { high: number; low: number }[],
  baselineFit: LogicalRange,
): ChartPriceRange | null {
  const from = Math.max(0, Math.floor(baselineFit.from));
  const to = Math.min(candleData.length - 1, Math.ceil(baselineFit.to));
  if (to < from) return null;
  let low = Infinity;
  let high = -Infinity;
  for (let i = from; i <= to; i++) {
    if (candleData[i].low < low) low = candleData[i].low;
    if (candleData[i].high > high) high = candleData[i].high;
  }
  if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low) return null;
  return { minValue: low, maxValue: high };
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
function makeAutoscaleProvider(
  data: ChartData,
  magnify: { zoom: number; baseRange: ChartPriceRange } | null = null,
) {
  return (baseImplementation: () => { priceRange: ChartPriceRange | null; margins?: any } | null) => {
    const base = baseImplementation();
    const baseRange = base?.priceRange;
    if (baseRange == null) return base;

    // Export uniform magnification: the Y range is the padded baseline
    // range divided by the zoom factor, centred on the visible candles.
    // This makes the Y zoom factor equal the X zoom factor so candles
    // keep their normal aspect ratio. The visible candles are never
    // clipped — if they would not fit, the range widens to contain them.
    if (magnify != null && magnify.zoom > 1) {
      const paddedBase = padRangeToHeight(
        magnify.baseRange.minValue,
        magnify.baseRange.maxValue,
        (magnify.baseRange.minValue + magnify.baseRange.maxValue) / 2,
      );
      if (paddedBase != null) {
        const targetHeight =
          (paddedBase.maxValue - paddedBase.minValue) / magnify.zoom;
        const center = (baseRange.minValue + baseRange.maxValue) / 2;
        let lo = center - targetHeight / 2;
        let hi = center + targetHeight / 2;
        if (baseRange.minValue < lo || baseRange.maxValue > hi) {
          const fit = padRangeToHeight(baseRange.minValue, baseRange.maxValue, center);
          if (fit != null) {
            lo = Math.min(lo, fit.minValue);
            hi = Math.max(hi, fit.maxValue);
          }
        }
        return { priceRange: { minValue: lo, maxValue: hi }, margins: { above: 0, below: 0 } };
      }
    }

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

export type SwingClickAnchor = { idx: number; clientX: number; clientY: number };

// Setup overlay: caller-provided geometry for a specific FVG + swing low to
// highlight on top of the regular chart. The state label drives a small
// corner badge. Passed by the chart-export page so headless screenshots
// match the Telegram alert preview.
//
// `swingLow.ts` is optional — when missing, the dashed line spans the full
// plot width (which is the most common case: the swing-low price is known
// from SetupEvent but its bar ts is only on SetupState and not always
// forwarded).
export type SetupOverlay = {
  // Only the FVGs that compose the setup. INV/CRE carry one; STB carries
  // both (inversion bear FVG + creation bull FVG).
  fvgs: {
    ts: number;
    end_ts: number;
    top: number;
    bottom: number;
    kind: "bullish" | "bearish";
  }[];
  swingLow?: { ts?: number | null; price: number };
  state: "INV" | "CRE" | "STB";
};

const SETUP_STATE_COLOR: Record<SetupOverlay["state"], string> = {
  INV: "#c08a3d",  // amber for inversion
  CRE: "#5ca36a",  // green for creation
  STB: "#7aa6ff",  // blue for setup
};

// Setup-FVG fill, keyed by gap kind. Drawn behind candles, no stroke.
const SETUP_FVG_COLOR: Record<"bullish" | "bearish", string> = {
  bullish: "#a3a5b5",  // CRE
  bearish: "#a7a1ac",  // INV
};

export function CandleChart({
  symbol,
  interval,
  theme,
  chartHeight,
  fvgEnabled,
  fvgLimit,
  fvgNearestPairOnly,
  exportZoom,
  reloadKey,
  editMode,
  draft,
  setupOverlay,
  onSwingClick,
  onCanvasClick,
  onSwingDragEnd,
  onReady,
}: {
  symbol: string;
  interval: ChartInterval;
  theme: ChartTheme;
  chartHeight: number;
  fvgEnabled: boolean;
  fvgLimit: number;
  // Export-only: keep just the nearest bullish + nearest bearish FVG
  // (by price distance to the last close). Omitted by the dashboard
  // modal, so its FVG layer is unchanged.
  fvgNearestPairOnly?: boolean;
  // Export-only X-zoom (>1 shrinks the visible window). Omitted by the
  // dashboard modal, so its fit is unchanged.
  exportZoom?: number;
  reloadKey?: number;
  editMode?: boolean;
  draft?: StructureEvent[];
  setupOverlay?: SetupOverlay | null;
  onSwingClick?: (a: SwingClickAnchor) => void;
  onCanvasClick?: (ts: number, price: number) => void;
  onSwingDragEnd?: (idx: number, ts: number, price: number) => void;
  onReady?: () => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<SVGSVGElement>(null);
  const plotClipIdRef = useRef(`chart-plot-clip-${Math.random().toString(36).slice(2)}`);
  const [status, setStatus] = useState<"loading" | "ok" | "error">("loading");
  const [err, setErr] = useState<string | null>(null);
  const t = CHART_THEMES[theme];

  // Высота меняется кнопками "Smaller/Larger chart" и раньше входила и в key
  // компонента, и в зависимости главного эффекта — то есть каждое нажатие
  // уничтожало график и строило заново, теряя приближение, которое пользователь
  // выставил колесом. Теперь применяется на месте: экземпляр графика доступен
  // по ссылке, а rAF-цикл читает актуальную высоту через ref, а не через
  // замыкание эффекта.
  const chartApiRef = useRef<any>(null);
  const chartHeightRef = useRef(chartHeight);
  useEffect(() => {
    chartHeightRef.current = chartHeight;
    try {
      chartApiRef.current?.applyOptions({ height: chartHeight });
    } catch { /* график ещё не создан или уже уничтожен */ }
  }, [chartHeight]);

  // Live refs — read by the rAF loop each frame so toggling FVG
  // visibility never forces a chart remount.
  const fvgEnabledRef = useRef(fvgEnabled);
  const fvgLimitRef = useRef(fvgLimit);
  const fvgPrimitiveUpdateRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    fvgEnabledRef.current = fvgEnabled;
    fvgPrimitiveUpdateRef.current?.();
  }, [fvgEnabled]);
  useEffect(() => {
    fvgLimitRef.current = fvgLimit;
    fvgPrimitiveUpdateRef.current?.();
  }, [fvgLimit]);

  // Setup overlay — null means no-op (preserves baseline behavior).
  const setupOverlayRef = useRef<SetupOverlay | null>(setupOverlay ?? null);
  useEffect(() => { setupOverlayRef.current = setupOverlay ?? null; }, [setupOverlay]);
  const onReadyRef = useRef(onReady);
  useEffect(() => { onReadyRef.current = onReady; }, [onReady]);
  const readyFiredRef = useRef(false);

  type SetupEls = {
    swingLine: SVGLineElement;
    swingTag: SVGRectElement;
    swingText: SVGTextElement;
  };
  const setupElsRef = useRef<SetupEls | null>(null);

  type FvgEl = {
    ts: number;
    end_ts: number;
    top: number;
    bottom: number;
    fill: string;
    // Setup FVGs are always drawn (ignoring the fvg toggle/limit) and
    // have no stroke; regular FVGs respect the toggle and get a hairline.
    setup: boolean;
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
      readyFiredRef.current = false;
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

        // X-zoom fit anchors (also reused by the initial fit below).
        const currentIndex = candleData.length - 1;
        const swingStartIndex = computeSwingStartIndex(data, barIndexByTime);

        // Export uniform magnification: when an X-zoom is active, capture
        // the unzoomed baseline price range so the autoscale provider can
        // magnify the Y axis by the same factor as the X axis.
        let magnifyRange: { zoom: number; baseRange: ChartPriceRange } | null = null;
        const exportZoomVal = exportZoom ?? 1;
        if (exportZoomVal > 1 && currentIndex >= 0) {
          const baselineFit = computeInitialFit(currentIndex, swingStartIndex, 1, -1);
          const baseRange = computeBaselinePriceRange(candleData, baselineFit);
          if (baseRange != null) magnifyRange = { zoom: exportZoomVal, baseRange };
        }
        const autoscaleOverride = makeAutoscaleProvider(data, magnifyRange);
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
        // Ссылка наружу — по ней эффект высоты применяет размер на месте,
        // не трогая ни данные, ни выставленный пользователем масштаб.
        chartApiRef.current = chart;

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
          const setupAnchorIndex = computeSetupAnchorIndex(setupOverlay, barIndexByTime);
          const range = computeInitialFit(
            currentIndex, swingStartIndex, exportZoomVal, setupAnchorIndex,
          );
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

        // FVG layer. Drawn as a lightweight-charts bottom primitive so it
        // lives behind candles, while every other SVG overlay stays unchanged.
        const newFvgElements: FvgEl[] = [];
        if (setupOverlay && setupOverlay.fvgs.length > 0) {
          // Setup export: show ONLY the FVGs that compose the setup, each
          // coloured by gap kind, no stroke. The generic FVG scan is
          // skipped entirely so unrelated imbalances don't clutter the
          // alert chart.
          for (const f of setupOverlay.fvgs) {
            newFvgElements.push({
              ts: f.ts,
              end_ts: f.end_ts,
              top: f.top,
              bottom: f.bottom,
              fill: SETUP_FVG_COLOR[f.kind],
              setup: true,
            });
          }
        } else {
          let allFvgs = data.fvgs ?? [];
          // Export-only: collapse to the single nearest bullish + bearish
          // FVG. A kind with no FVG in the loaded window simply yields
          // nothing — off-screen primitives are skipped by the renderer.
          if (fvgNearestPairOnly && allFvgs.length > 0) {
            const lastBar = data.bars[data.bars.length - 1];
            const price = lastBar ? lastBar.close : null;
            if (price != null) {
              const gap = (f: { top: number; bottom: number }) =>
                price >= f.bottom && price <= f.top
                  ? 0
                  : price > f.top ? price - f.top : f.bottom - price;
              const nearest = (kind: string) =>
                allFvgs
                  .filter((f) => f.kind === kind)
                  .reduce<typeof allFvgs[number] | null>(
                    (best, f) => (best == null || gap(f) < gap(best) ? f : best),
                    null,
                  );
              allFvgs = [nearest("bullish"), nearest("bearish")].filter(
                (f): f is typeof allFvgs[number] => f != null,
              );
            }
          }
          for (const fvg of allFvgs) {
            const fill = fvg.kind === "bullish"
              ? "rgba(38, 166, 154, 0.18)"
              : "rgba(117, 39, 39, 0.24)";
            newFvgElements.push({
              ts: fvg.ts,
              end_ts: fvg.end_ts,
              top: fvg.top,
              bottom: fvg.bottom,
              fill,
              setup: false,
            });
          }
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
        // Обрезка по области свечей — как у zigzag/swing/setup слоёв ниже. Без
        // неё фибо оставался единственным неограниченным слоем: линии рисуются
        // на FIB_RIGHT_OFFSET_BARS правее последней свечи, подпись ставится ещё
        // на FIB_LABEL_GAP дальше, и у правого края она уезжала поверх шкалы
        // цен — "0.705" накладывалась на "63200.00", обе нечитаемы.
        fibLayer.setAttribute("clip-path", plotClipUrl);
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

        // Setup overlay layer — the swing-low line/tag and anchor for the
        // optional state badge. The setup FVGs themselves are drawn by the
        // bottom FVG primitive (behind candles), not here. Positions are
        // updated every frame in the rAF loop from setupOverlayRef.
        const setupLayer = document.createElementNS(SVG_NS, "g");
        setupLayer.setAttribute("data-layer", "setup");
        setupLayer.setAttribute("clip-path", plotClipUrl);
        const setupSwingLine = document.createElementNS(SVG_NS, "line");
        setupSwingLine.setAttribute("stroke", "#f87171");
        setupSwingLine.setAttribute("stroke-width", "1.2");
        setupSwingLine.setAttribute("stroke-dasharray", "6 4");
        setupSwingLine.setAttribute("opacity", "0");
        setupLayer.appendChild(setupSwingLine);
        const setupSwingTag = document.createElementNS(SVG_NS, "rect");
        setupSwingTag.setAttribute("width", "28");
        setupSwingTag.setAttribute("height", "13");
        setupSwingTag.setAttribute("rx", "2");
        setupSwingTag.setAttribute("fill", "#4a1f1f");
        setupSwingTag.setAttribute("stroke", "#f87171");
        setupSwingTag.setAttribute("stroke-width", "1");
        setupSwingTag.setAttribute("opacity", "0");
        setupLayer.appendChild(setupSwingTag);
        const setupSwingText = document.createElementNS(SVG_NS, "text");
        setupSwingText.setAttribute("text-anchor", "middle");
        setupSwingText.setAttribute("dominant-baseline", "middle");
        setupSwingText.setAttribute("font-size", "8");
        setupSwingText.setAttribute("font-family", "Inter, sans-serif");
        setupSwingText.setAttribute("fill", "#fecaca");
        setupSwingText.setAttribute("opacity", "0");
        setupSwingText.textContent = "low";
        setupLayer.appendChild(setupSwingText);
        svg.appendChild(setupLayer);
        setupElsRef.current = {
          swingLine: setupSwingLine,
          swingTag: setupSwingTag,
          swingText: setupSwingText,
        };

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
        const fvgPrimitive = {
          paneViews: () => [{
            zOrder: () => "bottom",
            renderer: () => ({
              draw: (target: any) => {
                target.useMediaCoordinateSpace(({ context: ctx, mediaSize }: any) => {
                  const fvgOn = fvgEnabledRef.current;
                  const fvgLim = fvgLimitRef.current;
                  const fvgAll = fvgElementsRef.current;
                  if (fvgAll.length === 0) return;
                  const fvgThreshold = fvgAll.length - Math.max(1, fvgLim);
                  for (let i = 0; i < fvgAll.length; i++) {
                    const fv = fvgAll[i];
                    // Regular FVGs honour the on/off toggle and recency
                    // limit; setup FVGs are always drawn.
                    if (!fv.setup && (!fvgOn || i < fvgThreshold)) continue;
                    const x1 = safeTimeX(fv.ts);
                    const yTop = safePriceY(fv.top);
                    const yBot = safePriceY(fv.bottom);
                    if (x1 == null || yTop == null || yBot == null) continue;
                    // The box extends to its most-recent bar; when end_ts
                    // sits past the last candle (timeToCoordinate fails)
                    // fall back to the right edge instead of dropping it.
                    const x2 = safeTimeX(fv.end_ts) ?? mediaSize.width;
                    const left = Math.max(0, Math.min(x1, x2));
                    const right = Math.min(mediaSize.width, Math.max(x1, x2));
                    const width = right - left;
                    if (width <= 0) continue;
                    const top = Math.min(yTop, yBot);
                    const height = Math.max(1, Math.abs(yBot - yTop));
                    ctx.fillStyle = fv.fill;
                    ctx.fillRect(left, top, width, height);
                    // Setup FVGs are stroke-free; regular FVGs get a hairline.
                    if (!fv.setup) {
                      ctx.strokeStyle = fv.fill;
                      ctx.lineWidth = 0.5;
                      ctx.strokeRect(left, top, width, height);
                    }
                  }
                });
              },
            }),
          }],
          attached: ({ requestUpdate }: { requestUpdate: () => void }) => {
            fvgPrimitiveUpdateRef.current = requestUpdate;
            requestUpdate();
          },
          detached: () => {
            fvgPrimitiveUpdateRef.current = null;
          },
        };
        try { candles.attachPrimitive(fvgPrimitive); } catch { /* older builds */ }

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
            // Через ref, а не из замыкания: эффект больше не перезапускается при
            // смене высоты, поэтому захваченное значение устарело бы и слой
            // разметки считал бы обрезку по прежнему размеру.
            const h = chartHeightRef.current;
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

            // Fire onReady once the plot has measurable extent. Must happen
            // before any overlay code so a stray throw downstream doesn't
            // block the headless screenshot (window.__chartReady).
            if (!readyFiredRef.current) {
              readyFiredRef.current = true;
              try { onReadyRef.current?.(); } catch { /* noop */ }
            }

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

            // Setup overlay — dashed swing-low line/tag. The setup FVGs
            // themselves are drawn by the bottom FVG primitive. All
            // elements stay invisible when setupOverlay is null.
            const setupEls = setupElsRef.current;
            const overlay = setupOverlayRef.current;
            if (setupEls) {
              if (overlay) {
                if (overlay.swingLow) {
                  const sy = safePriceY(overlay.swingLow.price);
                  const sxStart =
                    overlay.swingLow.ts != null
                      ? safeTimeX(overlay.swingLow.ts)
                      : null;
                  if (sy != null) {
                    const lineFrom = sxStart != null ? Math.max(0, sxStart) : 0;
                    setupEls.swingLine.setAttribute("x1", String(lineFrom));
                    setupEls.swingLine.setAttribute("y1", String(sy));
                    setupEls.swingLine.setAttribute("x2", String(plotRight));
                    setupEls.swingLine.setAttribute("y2", String(sy));
                    setupEls.swingLine.setAttribute("opacity", "0.9");
                    const tagW = 28;
                    const tagH = 13;
                    const tagX = Math.max(0, Math.min(plotRight - tagW, plotRight - tagW - 4));
                    const tagY = sy - tagH / 2;
                    setupEls.swingTag.setAttribute("x", String(tagX));
                    setupEls.swingTag.setAttribute("y", String(tagY));
                    setupEls.swingTag.setAttribute("opacity", "1");
                    setupEls.swingText.setAttribute("x", String(tagX + tagW / 2));
                    setupEls.swingText.setAttribute("y", String(sy + 0.5));
                    setupEls.swingText.setAttribute("opacity", "1");
                  } else {
                    setupEls.swingLine.setAttribute("opacity", "0");
                    setupEls.swingTag.setAttribute("opacity", "0");
                    setupEls.swingText.setAttribute("opacity", "0");
                  }
                } else {
                  setupEls.swingLine.setAttribute("opacity", "0");
                  setupEls.swingTag.setAttribute("opacity", "0");
                  setupEls.swingText.setAttribute("opacity", "0");
                }
              } else {
                setupEls.swingLine.setAttribute("opacity", "0");
                setupEls.swingTag.setAttribute("opacity", "0");
                setupEls.swingText.setAttribute("opacity", "0");
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
      setupElsRef.current = null;
      dragRef.current = null;
      try { cleanupListeners?.(); } catch { /* noop */ }
      cleanupListeners = null;
      chartApiRef.current = null;
      try { chart?.remove(); } catch { /* double-remove */ }
      chart = null;
      candles = null;
    };
    // chartHeight здесь больше нет — высота применяется на месте эффектом выше,
    // поэтому пересоздавать график не нужно и масштаб пользователя сохраняется.
    // theme убран как мёртвый код: он входит в key компонента в Dashboard, а
    // смена key заставляет React выбросить старый экземпляр и создать новый —
    // до зависимостей старого эффекта дело не доходит никогда. Держать его в
    // списке значило вводить в заблуждение: правки здесь ни на что не влияли.
  }, [symbol, interval, reloadKey]);

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
    <div
      className="relative"
      style={{ height: chartHeight, background: t.bg }}
      data-chart-status={status}
    >
      <div ref={containerRef} style={{ width: "100%", height: chartHeight }} />
      <svg
        ref={overlayRef}
        className="pointer-events-none absolute inset-0"
        style={{ zIndex: 12 }}
        width="100%"
        height={chartHeight}
      />
      {setupOverlay && (
        <div
          className="absolute top-2 left-2 pointer-events-none font-mono text-[10px] font-bold tracking-wider rounded px-2 py-1"
          style={{
            // Above the price-scale canvases (z 30) which raiseRightPriceScaleLayer
            // promotes each frame. Positioned on the left so the right-side price
            // scale never covers the state label.
            zIndex: 40,
            background: "rgba(10,10,10,0.78)",
            color: SETUP_STATE_COLOR[setupOverlay.state],
            border: `1px solid ${SETUP_STATE_COLOR[setupOverlay.state]}`,
          }}
        >
          {setupOverlay.state}
        </div>
      )}
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
