import { useEffect, useMemo, useRef, useState } from "react";
import {
  addCoin,
  deleteStructure,
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
  type DashboardResponse,
  type DashboardRow,
  type Snapshot,
  type StructureEvent,
} from "../lib/api";
import {
  CandleChart,
  getChartCached,
  invalidateChartCache,
  prefetchChart,
  type ChartInterval,
  type ChartTheme,
} from "./CandleChart";
import { Liquidity } from "./Liquidity";
import { ServerHealth } from "./ServerHealth";
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
type Page = "ote" | "tda" | "liq" | "server";

const PAGES: readonly Page[] = ["ote", "tda", "liq", "server"] as const;

function isPage(v: string | null): v is Page {
  return v !== null && (PAGES as readonly string[]).includes(v);
}

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

function ServerIcon({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <rect x="3" y="4" width="14" height="5" rx="1.4" stroke="currentColor" strokeWidth="1.5" />
      <rect x="3" y="11" width="14" height="5" rx="1.4" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6 6.5h.01M6 13.5h.01M9 6.5h5M9 13.5h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
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

function LiqIcon({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3 C8 9 5.5 12.5 5.5 15.5 C5.5 19 8.5 21.5 12 21.5 C15.5 21.5 18.5 19 18.5 15.5 C18.5 12.5 16 9 12 3 Z" />
      <path d="M9 15.5 C9 17 10.3 18 12 18" />
    </svg>
  );
}

// Symbols that get an extra M5 chart tab. Mirrors M5_SYMBOLS in
// shared/kazus_logic/compute.py — these run an additional H1→M5 setup
// detector path on top of the standard H1→M15 one.
const M5_TAB_SYMBOLS = new Set(["BTCUSDT", "ETHUSDT", "SOLUSDT"]);

const FVG_ENABLED_KEY = "kazus_fvg_enabled";
const FVG_LIMIT_KEY = "kazus_fvg_limit";
const FVG_DEFAULT_LIMIT = 6;

type ChartTab = "global" | "local" | "entry" | "entry5";

const TAB_LABELS: Record<ChartTab, string> = {
  global: "Global · D1",
  local: "Local · H1",
  entry: "Entry · M15",
  entry5: "Entry · M5",
};

const TAB_INTERVAL: Record<ChartTab, ChartInterval> = {
  global: "1d",
  local: "1h",
  entry: "15m",
  entry5: "5m",
};

function tabsForSymbol(symbol: string): ChartTab[] {
  const base: ChartTab[] = ["global", "local", "entry"];
  if (M5_TAB_SYMBOLS.has(symbol.toUpperCase())) base.push("entry5");
  return base;
}

const CHART_CAROUSEL_MS = 360;

function snapshotForChartTab(row: DashboardRow | null | undefined, tab: ChartTab): Snapshot | null {
  if (!row) return null;
  if (tab === "global") return row.global;
  if (tab === "local") return row.local;
  // entry / entry5 — confirmation timeframes have no separate snapshot row.
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
    if (stored === "global" || stored === "local" || stored === "entry" || stored === "entry5") {
      // entry5 only valid for M5-eligible symbols; else fall through.
      if (stored !== "entry5" || M5_TAB_SYMBOLS.has(row.symbol.toUpperCase())) return stored;
    }
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

  // If the user navigates from a M5-eligible symbol to one without an M5
  // tab while entry5 is active, fall back to the M15 entry tab so the
  // chart stays mounted on a valid interval.
  useEffect(() => {
    if (tab === "entry5" && !M5_TAB_SYMBOLS.has(activeSymbol.toUpperCase())) {
      setTab("entry");
    }
  }, [activeSymbol, tab]);

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
            {tabsForSymbol(activeSymbol).map((t) => (
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
  const [page, setPageState] = useState<Page>(() => {
    // A stored value can name a page that no longer exists (an operator whose
    // last session ended on a since-removed tab). Fall back to OTE rather than
    // rendering an empty main area.
    const stored = localStorage.getItem(PAGE_KEY);
    return isPage(stored) ? stored : "ote";
  });
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
  // Читаем сохранённое значение, как и остальные настройки рядом. Раньше здесь
  // стояло безусловное true: выбор "motion off" записывался в localStorage, но
  // при следующей загрузке игнорировался, а эффект ниже тут же перезаписывал
  // его обратно на "1". Переключатель сбрасывался каждый раз, а вместе с ним
  // пропадала и подсветка строк по цене — она анимируется только при motion=on.
  const [motionEnabled, setMotionEnabled] = useState<boolean>(
    () => localStorage.getItem(MOTION_KEY) !== "0"
  );
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
          <NavBtn
            active={page === "liq"}
            open={sidebarOpen}
            icon={<LiqIcon size={20} />}
            label="LIQ"
            onClick={() => setPage("liq")}
            title="Liquidity Screener"
          />
        </div>

        {/* Nav bottom */}
        <div className="mt-auto flex flex-col gap-0.5 px-1.5">
          <NavBtn
            active={page === "server"}
            open={sidebarOpen}
            icon={<ServerIcon size={20} />}
            label="SYS"
            onClick={() => setPage("server")}
            title="Server Load"
          />

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

        {page === "liq" && <Liquidity />}

        {page === "server" && <ServerHealth />}
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
