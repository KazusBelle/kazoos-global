import { useEffect, useMemo, useRef, useState } from "react";
import {
  addAnnotation,
  addLiquidityPin,
  getLiquidityMetricsSnapshot,
  getLiquidityReplayRange,
  getLiquidityReplaySnapshot,
  getLiquidityTop,
  getLiquidityWsStatus,
  listLiquidityPins,
  moveLiquidityPin,
  patchLiquidityAlertValidation,
  postLiquidityAlert,
  removeLiquidityPin,
  type AnnotationKind,
  type LiqMetricsSnapshot,
  type LiqPin,
  type LiqRow,
  type LiqWsStatus,
} from "../lib/api";
import {
  aggregateValidation,
  alertId,
  alertPriority,
  computeAdaptiveThresholds,
  computeConfidence,
  computeFlags,
  detectRegime,
  explainRegime,
  intelligenceBreakdown,
  intelligenceScore,
  percentile,
  proposeAlerts,
  REGIME_COLORS,
  REGIME_RANK,
  SEVERITY_COLOR,
  validateAlert,
  type AlertEvent,
  type AlertKind,
  type AlertProposal,
  type AlertValidation,
  type CohortStats,
  type Confidence,
  type Flag,
  type IntelBreakdown,
  type MetricsMap,
  type Regime,
  type RegimeExplanation,
  type Series,
  type Severity,
  type ValidationStats,
} from "../lib/liquidityIntelligence";
import { LiquidityChartModal } from "./LiquidityChartModal";
import { ObservationBanner } from "./ObservationBanner";

const PIN_CAP = 20;
const STALE_AFTER_MS = 8000;        // sample older than this → grey dot
// Metric keys eligible for the 8s stale-cell dimming: value-path / realtime
// (WS) metrics only, ~1–5s cadence. REST/poller metrics (~60s cadence) update
// far slower than 8s, so dimming them would create a false "stale" signal.
// Exact membership only (STALE_DIM_METRICS.has(key)) — "obi" (REST) and
// "obi_rt" (WS) are distinct metrics; never substring/prefix match.
const STALE_DIM_METRICS = new Set<string>([
  "credible_depth",
  "obi_rt",
  "liq_stress",
]);
const WS_STATUS_POLL_MS = 4000;

// Non-overlapping slices of CoinGecko's top-500 by market cap. The label
// shows the upper bound; the slice is (prev_upper, upper]. Tier 1 is
// ranks 1-100, Tier 2 101-250, Tier 3 251-500.
const SLICES = [
  { label: 100, min: 1, max: 100 },
  { label: 250, min: 101, max: 250 },
  { label: 500, min: 251, max: 500 },
] as const;
type SliceLabel = (typeof SLICES)[number]["label"];

const SLICE_KEY = "kazus_liq_slice";
const SORT_KEY = "kazus_liq_sort";
const SNAPSHOT_POLL_MS = 5000;

// Metric-column definitions — drive headers, sort, formatting, colors.
// `colorMode`:
//   higher_better → linear percentile, low=red high=green
//   lower_better  → linear percentile, low=green high=red
//   signed        → sign-based, +=green -=red, neutral around 0
//   warning_pos   → 0=neutral, >0 percentile-scaled toward red
type ColorMode =
  | "higher_better"
  | "lower_better"
  | "signed"
  | "warning_pos"
  | "diverging_funding";  // 0 = neutral, + = long-crowded (red), − = short-crowded (blue)

type MetricCol = {
  key: string;
  header: string;
  title: string;
  format: (v: number | null) => string;
  colorMode: ColorMode;
};

const METRIC_COLS: MetricCol[] = [
  {
    key: "atr_liquidity",
    header: "ATR LIQ",
    title: "ATR Liquidity — 24h volume / ATR(14). Higher = more flow absorbed per unit price range.",
    format: (v) => (v == null ? "—" : formatBig(v)),
    colorMode: "higher_better",
  },
  {
    key: "spread",
    header: "SPREAD",
    title: "Best-ask − best-bid / mid. Tighter is more liquid.",
    format: formatSpread,
    colorMode: "lower_better",
  },
  {
    key: "obi",
    header: "OBI",
    title: "Order-book imbalance over top 20 levels. + = bid-heavy, − = ask-heavy.",
    format: formatObi,
    colorMode: "signed",
  },
  {
    key: "credible_depth",
    header: "CRED DEPTH",
    title: "USD of orderbook levels within ±0.5% of mid that have lived ≥400ms. Only for symbols actively opened (WS-only).",
    format: (v) => (v == null ? "—" : `$${formatBig(v)}`),
    colorMode: "higher_better",
  },
  {
    key: "liq_stress",
    header: "LIQ STRESS",
    title: "Total USD of forced liquidations over last 60s. Only for symbols actively opened (WS-only). “—” = no measurement; “0” = measured, zero liquidations.",
    format: (v) => (v == null ? "—" : v === 0 ? "0" : `$${formatBig(v)}`),
    colorMode: "warning_pos",
  },
  {
    key: "oi",
    header: "OI",
    title: "Open Interest in USD notional — current outstanding futures position size.",
    format: (v) => (v == null ? "—" : `$${formatBig(v)}`),
    colorMode: "higher_better",
  },
  {
    key: "oi_delta_1h",
    header: "OI Δ 1H",
    title: "1h change in open interest (%). +OI on rising price = real participation; −OI = unwind/short-covering.",
    format: formatPctChange,
    colorMode: "signed",
  },
  {
    key: "funding",
    header: "FUNDING",
    title: "Current funding rate per 8h. Positive = longs pay shorts (long-crowded); negative = shorts pay longs (short-crowded).",
    format: formatFunding,
    colorMode: "diverging_funding",
  },
  {
    key: "funding_z",
    header: "FUND Z",
    title: "Funding z-score over the last 7d. |Z|>2 = unusually extreme positioning; squeeze risk.",
    format: formatZ,
    colorMode: "diverging_funding",
  },
];

type SortKey = "rank" | "price" | "change_24h_pct" | "volume_24h" | string; // string for metric keys
type SortDir = "asc" | "desc";
type SortState = { key: SortKey; dir: SortDir };

function loadSlice(): SliceLabel {
  const raw = Number(localStorage.getItem(SLICE_KEY));
  if (SLICES.some((s) => s.label === raw)) return raw as SliceLabel;
  return 100;
}

function loadSort(): SortState {
  try {
    const raw = JSON.parse(localStorage.getItem(SORT_KEY) ?? "");
    if (raw && typeof raw.key === "string" && (raw.dir === "asc" || raw.dir === "desc")) {
      return raw as SortState;
    }
  } catch {
    // fall through
  }
  return { key: "rank", dir: "asc" };
}

function formatBig(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e12) return `${(n / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  if (abs >= 1) return n.toFixed(2);
  if (abs >= 0.01) return n.toFixed(4);
  return n.toPrecision(3);
}

function formatPrice(n: number | null): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(3);
  if (n >= 0.01) return n.toFixed(5);
  return n.toPrecision(4);
}

function formatPct(n: number | null): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

function formatSpread(v: number | null): string {
  // Spread comes in as a fraction (e.g. 0.0001 = 1bps). Display tightly:
  // sub-1bps → ppm, sub-1% → bps, else %.
  if (v == null || !Number.isFinite(v)) return "—";
  if (v < 0.0001) return `${(v * 1e6).toFixed(1)}ppm`;
  if (v < 0.01) return `${(v * 1e4).toFixed(2)}bps`;
  return `${(v * 100).toFixed(2)}%`;
}

function formatObi(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}`;
}

function formatPctChange(v: number | null): string {
  // Already comes through as a percent (e.g. 1.23 means +1.23 %).
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function formatFunding(v: number | null): string {
  // Funding rate is a fraction per 8h period. Display as bps so a typical
  // 0.0001 (~0.01 %) reads as "1.0 bps" instead of a wall of zeros.
  if (v == null || !Number.isFinite(v)) return "—";
  const bps = v * 10000;
  const sign = bps > 0 ? "+" : "";
  return `${sign}${bps.toFixed(2)}bps`;
}

function formatZ(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}σ`;
}

// Intelligence/flag/regime/confidence/alert logic lives in
// ../lib/liquidityIntelligence — this component just wires snapshot
// streams into those functions and renders the result.

function displayName(symbol: string): string {
  let s = symbol.replace(/USDT$/, "");
  if (s.startsWith("1000")) s = s.slice(4);
  return s;
}

// Map normalized t ∈ [0,1] to a soft background tint. Returns CSS color.
//   "good" → green tint, "bad" → red tint, "warn" → yellow→red.
function tintColor(t: number, palette: "good" | "bad" | "warn"): string {
  const clamped = Math.max(0, Math.min(1, t));
  const alpha = 0.08 + clamped * 0.22;
  if (palette === "good") return `rgba(82, 158, 121, ${alpha.toFixed(3)})`;
  if (palette === "bad") return `rgba(214, 105, 105, ${alpha.toFixed(3)})`;
  // warn: 0 = yellow, 1 = red. Interpolate between (227,180,87) → (214,105,105)
  const r = Math.round(227 + (214 - 227) * clamped);
  const g = Math.round(180 + (105 - 180) * clamped);
  const b = Math.round(87 + (105 - 87) * clamped);
  return `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})`;
}

function cellBackground(
  value: number | null,
  values: number[],
  mode: ColorMode,
): string | undefined {
  if (value == null || !Number.isFinite(value)) return undefined;

  if (mode === "signed") {
    // OBI-style: -1..+1, neutral around 0. Magnitude 0.2 is the threshold
    // where we start showing meaningful color.
    if (value > 0.05) return tintColor(Math.min(1, value / 0.6), "good");
    if (value < -0.05) return tintColor(Math.min(1, -value / 0.6), "bad");
    return undefined;
  }

  if (values.length < 2) return undefined;
  const lo = values[0];
  const hi = values[values.length - 1];
  if (lo === hi) return undefined;
  const t = (value - lo) / (hi - lo);

  if (mode === "higher_better") return tintColor(t, "good");
  if (mode === "lower_better") return tintColor(1 - t, "good");
  if (mode === "warning_pos") {
    if (value <= 0) return undefined;
    // Among positive values only, scale toward red.
    const positives = values.filter((v) => v > 0);
    if (positives.length === 0) return undefined;
    const minPos = positives[0];
    const maxPos = positives[positives.length - 1];
    if (minPos === maxPos) return tintColor(0.5, "warn");
    const tw = (value - minPos) / (maxPos - minPos);
    return tintColor(tw, "warn");
  }
  return undefined;
}

export function Liquidity() {
  const [sliceLabel, setSliceLabelState] = useState<SliceLabel>(loadSlice);
  const [allRows, setAllRows] = useState<LiqRow[] | null>(null);
  const [snapshot, setSnapshot] = useState<LiqMetricsSnapshot | null>(null);
  const [sort, setSortState] = useState<SortState>(loadSort);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [chartSymbol, setChartSymbol] = useState<string | null>(null);
  const [pins, setPins] = useState<LiqPin[]>([]);
  const [wsStatus, setWsStatus] = useState<LiqWsStatus | null>(null);
  const [pinError, setPinError] = useState<string | null>(null);

  // ── Replay / time-machine state ─────────────────────────────────────
  const [replayActive, setReplayActive] = useState(false);
  const [replayRange, setReplayRange] = useState<{ earliest_ts: number | null; latest_ts: number | null } | null>(null);
  const [replayAsOf, setReplayAsOf] = useState<number | null>(null);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const [replaySpeed, setReplaySpeed] = useState(60); // multiplier — 60× = one wall-second equals one replay-minute
  // Which symbol's audit pane is open (collapsible). Null = closed.
  const [auditSymbol, setAuditSymbol] = useState<string | null>(null);

  const pinOrder = useMemo(() => {
    const m: Record<string, number> = {};
    for (const p of pins) m[p.symbol] = p.pinned_order;
    return m;
  }, [pins]);

  const subscribedSet = useMemo(
    () => new Set(wsStatus?.subscribed ?? []),
    [wsStatus],
  );

  async function togglePin(symbol: string) {
    setPinError(null);
    const isPinned = symbol in pinOrder;
    try {
      if (isPinned) {
        await removeLiquidityPin(symbol);
      } else {
        if (pins.length >= PIN_CAP) {
          setPinError(`max ${PIN_CAP} pinned symbols`);
          return;
        }
        await addLiquidityPin(symbol);
      }
      const next = await listLiquidityPins();
      setPins(next);
    } catch (err: any) {
      setPinError(err?.message ?? "pin failed");
    }
  }

  async function movePin(symbol: string, direction: "up" | "down") {
    try {
      const next = await moveLiquidityPin(symbol, direction);
      setPins(next);
    } catch (err: any) {
      setPinError(err?.message ?? "move failed");
    }
  }

  function setSlice(next: SliceLabel) {
    setSliceLabelState(next);
    localStorage.setItem(SLICE_KEY, String(next));
  }

  function setSort(next: SortState) {
    setSortState(next);
    localStorage.setItem(SORT_KEY, JSON.stringify(next));
  }

  function toggleSort(key: SortKey) {
    if (sort.key === key) {
      setSort({ key, dir: sort.dir === "asc" ? "desc" : "asc" });
    } else {
      // Default to desc for metric columns (best/worst first), asc for rank.
      const defaultDir: SortDir = key === "rank" ? "asc" : "desc";
      setSort({ key, dir: defaultDir });
    }
  }

  // Pins + WS status — initial load.
  useEffect(() => {
    listLiquidityPins().then(setPins).catch(() => undefined);
  }, []);

  // Poll WS status so live/stale/reconnect badges stay current.
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const s = await getLiquidityWsStatus();
        if (!cancelled) setWsStatus(s);
      } catch {
        // transient
      }
    };
    poll();
    const id = window.setInterval(poll, WS_STATUS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  // One-time CoinGecko + Binance Futures universe fetch.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getLiquidityTop(500)
      .then((res) => {
        if (cancelled) return;
        setAllRows(res.rows);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err?.message ?? "failed to load");
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const currentSlice = SLICES.find((s) => s.label === sliceLabel)!;
  const filteredRows = allRows
    ? allRows.filter((r) => r.rank >= currentSlice.min && r.rank <= currentSlice.max)
    : null;

  // Symbols actually rendered: tier-filtered set ∪ pinned symbols (even
  // those outside the current tier). Pinned-out-of-tier rows still get
  // their metric snapshot polled by including them here.
  const visibleSymbols = useMemo(() => {
    const filteredSyms = (filteredRows ?? []).map((r) => r.binance_symbol);
    const pinnedSyms = pins.map((p) => p.symbol);
    return Array.from(new Set([...pinnedSyms, ...filteredSyms]));
  }, [filteredRows, pins]);

  // Poll the metric snapshot for the symbols currently visible in the
  // tier. Updates every SNAPSHOT_POLL_MS so REST-metric columns reflect
  // the worker's latest write and WS-metric cells light up the moment a
  // symbol becomes active.
  const visibleSymbolsKey = visibleSymbols.join(",");
  const symbolsRef = useRef(visibleSymbols);
  useEffect(() => {
    symbolsRef.current = visibleSymbols;
  }, [visibleSymbols]);

  // Live snapshot poller — disabled while replay is active so historical
  // state isn't overwritten by a live tick mid-scrub.
  useEffect(() => {
    if (visibleSymbols.length === 0 || replayActive) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const snap = await getLiquidityMetricsSnapshot(symbolsRef.current);
        if (!cancelled) setSnapshot(snap);
      } catch {
        // transient — next tick retries
      }
    };
    poll();
    const id = window.setInterval(poll, SNAPSHOT_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleSymbolsKey, replayActive]);

  // Replay loader — fires whenever as_of changes (slider drag, play tick).
  // Loads the historical snapshot for the visible symbols at that ts.
  useEffect(() => {
    if (!replayActive || replayAsOf == null) return;
    if (visibleSymbols.length === 0) return;
    let cancelled = false;
    getLiquidityReplaySnapshot(symbolsRef.current, replayAsOf)
      .then((snap) => {
        if (!cancelled) setSnapshot(snap);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replayActive, replayAsOf, visibleSymbolsKey]);

  // Replay range — fetched once when entering replay so the slider
  // bounds are correct. We also seed `replayAsOf` to the latest sample.
  useEffect(() => {
    if (!replayActive) return;
    let cancelled = false;
    getLiquidityReplayRange()
      .then((r) => {
        if (cancelled) return;
        setReplayRange(r);
        if (replayAsOf == null && r.latest_ts != null) {
          setReplayAsOf(r.latest_ts);
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replayActive]);

  // Auto-advance replay when playing — each wall-clock interval bumps
  // replayAsOf by `replaySpeed × interval` so 60× makes 1s of real time
  // equal 1 minute of replay. Stops at the latest available ts.
  useEffect(() => {
    if (!replayActive || !replayPlaying) return;
    const tickMs = 500;
    const id = window.setInterval(() => {
      setReplayAsOf((cur) => {
        if (cur == null || replayRange?.latest_ts == null) return cur;
        const next = cur + replaySpeed * tickMs;
        if (next >= replayRange.latest_ts) {
          setReplayPlaying(false);
          return replayRange.latest_ts;
        }
        return next;
      });
    }, tickMs);
    return () => window.clearInterval(id);
  }, [replayActive, replayPlaying, replaySpeed, replayRange]);

  // Unsorted "visible" row set: pinned (full universe) + filtered tier.
  // Sort + intelligence calc both consume this so we don't recompute the
  // intersection twice. Pinned-out-of-tier rows still appear.
  const visibleRows = useMemo(() => {
    if (!filteredRows || !allRows) return null;
    const pinnedRows = allRows
      .filter((r) => r.binance_symbol in pinOrder)
      .sort((a, b) => pinOrder[a.binance_symbol] - pinOrder[b.binance_symbol]);
    const unpinned = filteredRows.filter((r) => !(r.binance_symbol in pinOrder));
    return { pinnedRows, unpinned };
  }, [filteredRows, allRows, pinOrder]);

  // Per-column sorted-value array, used to scale the bar width + cohort
  // percentiles for flags / score. Computed off the visible set so a
  // cell's color reflects how it sits in the user's current view.
  const colDistributions = useMemo(() => {
    const out: Record<string, number[]> = {};
    if (!visibleRows) return out;
    const all = [...visibleRows.pinnedRows, ...visibleRows.unpinned];
    for (const c of METRIC_COLS) {
      const vals: number[] = [];
      for (const r of all) {
        const v = snapshot?.symbols?.[r.binance_symbol]?.[c.key]?.value;
        if (v != null && Number.isFinite(v)) vals.push(v);
      }
      vals.sort((a, b) => a - b);
      out[c.key] = vals;
    }
    return out;
  }, [visibleRows, snapshot]);

  // Cohort percentile bundle — feeds adaptive thresholds + scoring.
  // Recomputed when the snapshot refreshes so flags/score adapt to
  // whatever subset the user is viewing.
  const cohortStats = useMemo<CohortStats>(() => {
    const lsVals = (colDistributions["liq_stress"] ?? []).filter((v) => v > 0);
    return {
      atrLiquidityP90: percentile(colDistributions["atr_liquidity"] ?? [], 0.9),
      credDepthP90: percentile(colDistributions["credible_depth"] ?? [], 0.9),
      credDepthP10: percentile(colDistributions["credible_depth"] ?? [], 0.1),
      spreadP10: percentile(colDistributions["spread"] ?? [], 0.1),
      spreadP95: percentile(colDistributions["spread"] ?? [], 0.95),
      liqStressP90: percentile(lsVals, 0.9),
      fragilityP90: percentile(colDistributions["fragility_score"] ?? [], 0.9),
    };
  }, [colDistributions]);

  // Track previous-tick regime per symbol so confidence + alert engine
  // can react to transitions. We update this AFTER intelByRow finishes
  // so the current snapshot's `prevRegime` reflects the prior cycle.
  const prevRegimeRef = useRef<Record<string, Regime>>({});

  // Per-symbol intelligence bundle (regime, flags, score, quality,
  // confidence, anomaly priority). Pinned and visible symbols only —
  // computing for the entire 500-row universe would be wasted work.
  const intelByRow = useMemo(() => {
    const out: Record<
      string,
      {
        score: number;
        quality: number;
        regime: Regime;
        flags: Flag[];
        confidence: Confidence;
        anomalyPriority: number;
        proposals: AlertProposal[];
      }
    > = {};
    if (!visibleRows) return out;

    const lastWsFrameMs =
      wsStatus?.last_message_at != null
        ? Date.now() - wsStatus.last_message_at * 1000
        : null;

    for (const r of [...visibleRows.pinnedRows, ...visibleRows.unpinned]) {
      const sym = r.binance_symbol;
      const metrics: MetricsMap = snapshot?.symbols?.[sym] ?? {};
      const prevRegime = prevRegimeRef.current[sym];

      // Step 1: provisional regime + thresholds derived from prevRegime
      // so adaptive widening kicks in this tick (using current regime
      // would create a feedback loop — extreme spreads in cascade would
      // raise the threshold and immediately drop the flag).
      const provisionalThresholds = computeAdaptiveThresholds(
        cohortStats,
        prevRegime ?? "HEALTHY_TREND",
        metrics,
      );
      const provisionalFlags = computeFlags(metrics, cohortStats, provisionalThresholds);
      const regime = detectRegime(metrics, provisionalFlags);

      // Step 2: re-compute against the now-resolved regime so the displayed
      // thresholds/flags match the regime badge shown in the UI.
      const thresholds = computeAdaptiveThresholds(cohortStats, regime, metrics);
      const flags = computeFlags(metrics, cohortStats, thresholds);
      const intel = intelligenceScore(metrics, cohortStats, regime);

      const isSubscribed = new Set(wsStatus?.subscribed ?? []).has(sym);
      const newestSampleTs = Math.max(
        metrics.credible_depth?.ts ?? 0,
        metrics.obi?.ts ?? 0,
        metrics.atr_liquidity?.ts ?? 0,
      );
      const snapshotAgeMs = newestSampleTs > 0 ? Date.now() - newestSampleTs : null;

      const confidence = computeConfidence(metrics, {
        isSubscribed,
        wsConnected: !!wsStatus?.connected,
        lastWsFrameMs,
        snapshotAgeMs,
        flags,
        regime,
        intel,
        prevRegime,
      });

      const proposals = proposeAlerts(metrics, flags, regime, thresholds, prevRegime);

      const anomalyPriority =
        REGIME_RANK[regime] + flags.length * 5 + (100 - (intel?.score ?? 100));

      out[sym] = {
        score: intel?.score ?? NaN,
        quality: intel?.quality ?? NaN,
        regime,
        flags,
        confidence,
        anomalyPriority,
        proposals,
      };
    }
    return out;
  }, [visibleRows, snapshot, cohortStats, wsStatus]);

  // After computing intel, snapshot the regime into the prev-ref so the
  // NEXT tick sees this tick's regime as "prevRegime". Mutating a ref
  // inside a useEffect avoids re-render churn.
  useEffect(() => {
    const next: Record<string, Regime> = { ...prevRegimeRef.current };
    for (const sym of Object.keys(intelByRow)) {
      next[sym] = intelByRow[sym].regime;
    }
    prevRegimeRef.current = next;
  }, [intelByRow]);

  // ── Smart alert engine ────────────────────────────────────────────────
  // Each tick, proposals are de-duped by alertId. New proposals start at
  // their `startedAt`; if the same kind keeps proposing for ≥PERSISTENCE_MS
  // we promote it to an active alert. Active alerts stay in the timeline
  // until they haven't been re-proposed for ≥COOLDOWN_MS.
  const PERSISTENCE_MS = 8_000;
  const COOLDOWN_MS = 90_000;
  const TIMELINE_MAX = 40;
  const pendingRef = useRef<Map<string, { startedAt: number; lastSeenAt: number; proposal: AlertProposal; symbol: string; regime: Regime; confidence: number }>>(new Map());
  const [timeline, setTimeline] = useState<AlertEvent[]>([]);
  const [activeAlertsBySymbol, setActiveAlertsBySymbol] = useState<Record<string, AlertEvent[]>>({});

  useEffect(() => {
    const now = Date.now();
    const pending = pendingRef.current;
    // Track current-tick proposal keys so we can age out the ones that
    // disappeared this tick.
    const seenThisTick = new Set<string>();

    const promotedThisTick: AlertEvent[] = [];

    for (const [sym, intel] of Object.entries(intelByRow)) {
      // Skip LOW-confidence rows entirely — Phase 5 explicitly suppresses
      // noise from disagreeing or stale signals. UNKNOWN is also skipped:
      // proposeAlerts returns [] when metrics are absent, but the guard
      // here makes the contract explicit (no alerts for rows where we
      // haven't measured anything yet).
      if (intel.confidence.state === "LOW" || intel.confidence.state === "UNKNOWN") continue;
      for (const p of intel.proposals) {
        // Bucket by 30s windows so a long-lived condition produces ONE
        // alert (and updates lastSeenAt), not a fresh id every tick.
        const bucket = Math.floor(now / 30_000);
        const id = alertId(sym, p.kind, bucket);
        seenThisTick.add(id);
        const existing = pending.get(id);
        if (existing) {
          existing.lastSeenAt = now;
          existing.proposal = p;     // keep severity fresh as it escalates
          existing.regime = intel.regime;
          existing.confidence = intel.confidence.score;
        } else {
          pending.set(id, {
            startedAt: now,
            lastSeenAt: now,
            proposal: p,
            symbol: sym,
            regime: intel.regime,
            confidence: intel.confidence.score,
          });
        }
      }
    }

    // Promote pending entries that have persisted long enough into the
    // public timeline. Already-active alerts (same id) get updated.
    setTimeline((prev) => {
      const byId = new Map(prev.map((a) => [a.id, a]));
      // Tally co-occurring alert kinds per symbol for priority scoring —
      // a single SPREAD_EXPLOSION matters less than the same alert
      // alongside DEPTH_COLLAPSE + LIQ_CASCADE on the same name.
      const coOccur: Record<string, number> = {};
      for (const p of pending.values()) {
        coOccur[p.symbol] = (coOccur[p.symbol] ?? 0) + 1;
      }
      for (const [id, p] of pending) {
        const lifetime = p.lastSeenAt - p.startedAt;
        if (lifetime >= PERSISTENCE_MS || byId.has(id)) {
          const existing = byId.get(id);
          const priority = alertPriority(
            p.proposal.kind,
            p.proposal.severity,
            p.regime,
            p.confidence,
            Math.max(0, (coOccur[p.symbol] ?? 1) - 1),
          );
          const event: AlertEvent = {
            id,
            symbol: p.symbol,
            kind: p.proposal.kind,
            trigger: p.proposal.trigger,
            regime: p.regime,
            severity: p.proposal.severity,
            confidence: p.confidence,
            priority,
            rank: 0,
            startedAt: existing?.startedAt ?? p.startedAt,
            lastSeenAt: p.lastSeenAt,
          };
          byId.set(id, event);
          if (!existing) promotedThisTick.push(event);
        }
      }
      // Cooldown: drop alerts whose proposal hasn't been re-seen in a while
      // AND that aren't pending anymore. Sort by composite priority so the
      // worst-first ordering matches the UI's triage intent — recent ties
      // broken by lastSeenAt.
      const all = Array.from(byId.values())
        .filter((a) => now - a.lastSeenAt < COOLDOWN_MS || pending.has(a.id))
        .sort((a, b) => b.priority - a.priority || b.lastSeenAt - a.lastSeenAt)
        .slice(0, TIMELINE_MAX)
        .map((a, idx) => ({ ...a, rank: idx + 1 }));
      return all;
    });

    // Reap pending that didn't fire this tick AND haven't been refreshed
    // in 30s — keeps the map bounded.
    for (const [id, p] of pending) {
      if (!seenThisTick.has(id) && now - p.lastSeenAt > 30_000) {
        pending.delete(id);
      }
    }
  }, [intelByRow]);

  // Persist alerts to the research backend. POST on every state-changing
  // tick (UPSERT on alert_id) so escalations land too; PATCH validation
  // outcomes once the cooldown elapses so we never replay a stale verdict.
  const postedAlertsRef = useRef<Map<string, AlertEvent>>(new Map());
  const validatedAlertsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    for (const a of timeline) {
      const prev = postedAlertsRef.current.get(a.id);
      const changed =
        !prev ||
        prev.severity !== a.severity ||
        prev.regime !== a.regime ||
        Math.abs((prev.confidence ?? 0) - (a.confidence ?? 0)) > 5 ||
        Math.abs((prev.priority ?? 0) - (a.priority ?? 0)) > 5 ||
        a.lastSeenAt - (prev.lastSeenAt ?? 0) > 30_000;
      if (!changed) continue;
      postedAlertsRef.current.set(a.id, a);
      postLiquidityAlert({
        alert_id: a.id,
        symbol: a.symbol,
        kind: a.kind,
        severity: a.severity,
        regime: a.regime,
        confidence: a.confidence,
        priority: a.priority,
        trigger: a.trigger,
        started_at_ms: a.startedAt,
        last_seen_at_ms: a.lastSeenAt,
      }).catch(() => {
        // best-effort — telemetry shouldn't break the UI
      });
    }
  }, [timeline]);

  // Validate alerts that have aged past the cooldown — we use the
  // already-computed timeline metadata to classify as followed_through
  // when severity ever escalated to critical or the alert persisted for
  // ≥30s; everything else is "noise". The detail-modal view (Phase 6)
  // exposes a richer per-alert validateAlert() that the user can run on
  // demand from the audit panel.
  useEffect(() => {
    const now = Date.now();
    for (const a of timeline) {
      if (validatedAlertsRef.current.has(a.id)) continue;
      if (now - a.lastSeenAt < COOLDOWN_MS) continue;     // still active
      if (now - a.lastSeenAt > 6 * 60_000) continue;      // window closed too long ago
      // Alert engine uses 30s id buckets (`bucket = floor(now/30_000)`)
      // so a single alert id can never persist >30s — at second 30 a
      // new id is minted for the same (symbol, kind). The original
      // threshold of `>= 30_000ms` was therefore physically
      // unreachable, which is why the audit found 100% noise / 0%
      // followed_through across hundreds of resolved alerts. Lower to
      // 12s — well under the bucket cap, but enough to filter genuine
      // one-tick blips that snap back the next snapshot poll.
      const persisted = a.lastSeenAt - a.startedAt >= 12_000;
      const outcome: "followed_through" | "noise" = persisted ? "followed_through" : "noise";
      validatedAlertsRef.current.add(a.id);
      patchLiquidityAlertValidation(a.id, outcome).catch(() => undefined);
    }
  }, [timeline]);

  // Group active alerts by symbol for the ALERT column.
  useEffect(() => {
    const grouped: Record<string, AlertEvent[]> = {};
    const now = Date.now();
    // Same display-collapse as the timeline: one entry per (symbol, kind) so the
    // per-symbol ALERT column doesn't show bucketed-id duplicates. timeline is
    // pre-sorted priority desc → first seen per key is the highest-priority gen.
    const seenKeys = new Set<string>();
    for (const a of timeline) {
      if (now - a.lastSeenAt > COOLDOWN_MS) continue;
      const k = `${a.symbol}:${a.kind}`;
      if (seenKeys.has(k)) continue;
      seenKeys.add(k);
      (grouped[a.symbol] ??= []).push(a);
    }
    setActiveAlertsBySymbol(grouped);
  }, [timeline]);

  // Final sorted rows. The synthetic keys (intelligence_score,
  // market_quality_score, anomaly_priority) plug straight into the
  // existing sort framework — sort.key just dispatches to the right
  // value-extractor.
  const sortedRows = useMemo(() => {
    if (!visibleRows) return null;
    const dirMul = sort.dir === "asc" ? 1 : -1;
    const getMetric = (sym: string, key: string): number | null => {
      const m = snapshot?.symbols?.[sym]?.[key];
      return m?.value ?? null;
    };
    const getSortValue = (r: LiqRow): number | null => {
      if (sort.key === "rank") return r.rank;
      if (sort.key === "price") return r.price;
      if (sort.key === "change_24h_pct") return r.change_24h_pct;
      if (sort.key === "volume_24h") return r.volume_24h;
      if (sort.key === "intelligence_score") {
        const v = intelByRow[r.binance_symbol]?.score;
        return v != null && Number.isFinite(v) ? v : null;
      }
      if (sort.key === "market_quality_score") {
        const v = intelByRow[r.binance_symbol]?.quality;
        return v != null && Number.isFinite(v) ? v : null;
      }
      if (sort.key === "anomaly_priority") {
        const v = intelByRow[r.binance_symbol]?.anomalyPriority;
        return v != null && Number.isFinite(v) ? v : null;
      }
      return getMetric(r.binance_symbol, sort.key as string);
    };
    const unpinned = [...visibleRows.unpinned];
    unpinned.sort((a, b) => {
      const va = getSortValue(a);
      const vb = getSortValue(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (va === vb) return 0;
      return va < vb ? -1 * dirMul : 1 * dirMul;
    });
    return [...visibleRows.pinnedRows, ...unpinned];
  }, [visibleRows, sort, snapshot, intelByRow]);

  return (
    <div className="space-y-4">
      <ObservationBanner />
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex items-baseline gap-3">
          <div className="text-accent text-xl font-bold tracking-[0.3em]">LIQ</div>
          <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
            liquidity scanner
          </div>
          <WsStatusPill status={wsStatus} pinnedCount={pins.length} />
          {pinError && (
            <span className="text-[10px] uppercase tracking-[0.2em] text-[#d68b8b]">
              {pinError}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {SLICES.map((s) => (
            <button
              key={s.label}
              onClick={() => setSlice(s.label)}
              className={`h-8 px-3 rounded-md border text-[11px] uppercase tracking-[0.22em] transition-colors ${
                sliceLabel === s.label
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border text-muted hover:text-zinc-200 hover:border-accent/50"
              }`}
              title={`Ranks ${s.min}-${s.max} by market cap`}
            >
              TOP {s.label}
            </button>
          ))}
        </div>
      </div>

      <ValidationStatsBar timeline={timeline} />

      <ReplayBar
        active={replayActive}
        onToggle={() => {
          setReplayActive((a) => {
            const next = !a;
            if (!next) {
              setReplayPlaying(false);
              setReplayAsOf(null);
            }
            return next;
          });
        }}
        range={replayRange}
        asOf={replayAsOf}
        onSeek={(ts) => setReplayAsOf(ts)}
        playing={replayPlaying}
        onPlayToggle={() => setReplayPlaying((p) => !p)}
        onStep={(deltaMs) => {
          setReplayAsOf((cur) => {
            if (cur == null) return cur;
            const next = cur + deltaMs;
            if (replayRange?.earliest_ts != null && next < replayRange.earliest_ts) return replayRange.earliest_ts;
            if (replayRange?.latest_ts != null && next > replayRange.latest_ts) return replayRange.latest_ts;
            return next;
          });
        }}
        speed={replaySpeed}
        onSpeed={(s) => setReplaySpeed(s)}
        annotationTarget={auditSymbol}
        onAnnotate={async (kind, note) => {
          if (!auditSymbol || replayAsOf == null) return;
          try {
            await addAnnotation({
              symbol: auditSymbol,
              ts_ms: replayAsOf,
              kind,
              note: note || undefined,
            });
          } catch {
            // swallow — annotation failures are non-fatal
          }
        }}
      />

      <AnomalyTimeline
        timeline={timeline}
        onJump={(sym) => setChartSymbol(sym)}
      />

      {auditSymbol && intelByRow[auditSymbol] && (
        <AuditPanel
          symbol={auditSymbol}
          metrics={snapshot?.symbols?.[auditSymbol] ?? {}}
          cohort={cohortStats}
          intel={intelByRow[auditSymbol]}
          onClose={() => setAuditSymbol(null)}
        />
      )}

      <div className="bg-panel border border-border rounded-2xl overflow-x-auto">
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.2em] text-muted border-b border-border select-none">
              <th className="px-2 py-3 font-normal text-left w-10">PIN</th>
              <SortHeader label="#" sortKey="rank" current={sort} onSort={toggleSort} align="right" />
              <th className="px-4 py-3 font-normal text-left whitespace-nowrap">COIN</th>
              <SortHeader label="PRICE" sortKey="price" current={sort} onSort={toggleSort} align="right" />
              <SortHeader label="Δ 24H" sortKey="change_24h_pct" current={sort} onSort={toggleSort} align="right" />
              <SortHeader label="VOL 24H" sortKey="volume_24h" current={sort} onSort={toggleSort} align="right" />
              {METRIC_COLS.map((c) => (
                <SortHeader
                  key={c.key}
                  label={c.header}
                  sortKey={c.key}
                  current={sort}
                  onSort={toggleSort}
                  align="right"
                  title={c.title}
                />
              ))}
              <SortHeader
                label="SCORE"
                sortKey="intelligence_score"
                current={sort}
                onSort={toggleSort}
                align="right"
                title="Composite Liquidity Intelligence Score (0–100). Blends spread, depth, OBI, liquidations, funding, OI Δ, and (when available) resiliency + Kyle impact."
              />
              <SortHeader
                label="QUALITY"
                sortKey="market_quality_score"
                current={sort}
                onSort={toggleSort}
                align="right"
                title="Market Quality Score — average of the worst third of intelligence components, so a single broken signal pulls quality down even if the mean looks fine."
              />
              <th
                className="px-3 py-3 font-normal text-left whitespace-nowrap"
                title="Market regime derived from liquidity + positioning + flags. Click to sort by anomaly priority."
                onClick={() => toggleSort("anomaly_priority")}
                style={{ cursor: "pointer" }}
              >
                REGIME{sort.key === "anomaly_priority" ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
              </th>
              <th
                className="px-3 py-3 font-normal text-left whitespace-nowrap"
                title="Confidence in the signals: rises with WS freshness and signal agreement, falls on stale data, disagreement, or regime jitter."
              >
                CONF
              </th>
              <th
                className="px-3 py-3 font-normal text-left whitespace-nowrap"
                title="Active alerts: persistence-confirmed + cooldown-debounced. Color matches severity."
              >
                ALERTS
              </th>
              <th
                className="px-3 py-3 font-normal text-left whitespace-nowrap"
                title="Anomaly flags derived from the visible cohort"
              >
                FLAGS
              </th>
            </tr>
          </thead>
          <tbody>
            {error && (
              <tr>
                <td colSpan={12 + METRIC_COLS.length} className="py-6 text-center text-sm" style={{ color: "rgba(214, 139, 139, 0.9)" }}>
                  {error}
                </td>
              </tr>
            )}
            {!error && loading && sortedRows === null && (
              <tr>
                <td colSpan={12 + METRIC_COLS.length} className="py-8 text-center text-sm text-muted">
                  Loading…
                </td>
              </tr>
            )}
            {!error && sortedRows && sortedRows.length === 0 && (
              <tr>
                <td colSpan={12 + METRIC_COLS.length} className="py-8 text-center text-sm text-muted">
                  No coins in this slice are tradable on Binance Futures.
                </td>
              </tr>
            )}
            {sortedRows?.map((row) => {
              const pct = row.change_24h_pct;
              const pctColor =
                pct == null ? "text-muted" : pct > 0 ? "text-discount" : pct < 0 ? "text-[#d68b8b]" : "text-zinc-300";
              const metrics = snapshot?.symbols?.[row.binance_symbol] ?? {};
              const isPinned = row.binance_symbol in pinOrder;
              const isSubscribed = subscribedSet.has(row.binance_symbol);
              // LiveDot GREEN must require credible_depth specifically. A fresh
              // obi_rt / liq_stress must NEVER mask a frozen/stale/missing
              // credible_depth (anti-false-GREEN): green only when the
              // credible_depth value exists and its ts is recent.
              const cd = metrics.credible_depth;
              const fresh = cd?.value != null && Date.now() - cd.ts < STALE_AFTER_MS;
              const pinnedCount = pins.length;
              const pinIdx = pinOrder[row.binance_symbol];
              // Worst severity from active alerts feeds the left-edge
              // stripe — at-a-glance triage for the row.
              const alerts = activeAlertsBySymbol[row.binance_symbol] ?? [];
              const worstSeverity: Severity | null = alerts.some((a) => a.severity === "critical")
                ? "critical"
                : alerts.some((a) => a.severity === "warn")
                  ? "warn"
                  : alerts.length > 0
                    ? "info"
                    : null;
              return (
                <tr
                  key={row.binance_symbol}
                  onClick={() => setChartSymbol(row.binance_symbol)}
                  className={`border-t border-border/60 hover:bg-white/[0.02] transition-colors cursor-pointer ${
                    isPinned ? "bg-white/[0.015]" : ""
                  }`}
                  style={
                    worstSeverity
                      ? { boxShadow: `inset 3px 0 0 0 ${SEVERITY_COLOR[worstSeverity]}` }
                      : undefined
                  }
                >
                  <td className="px-2 py-2" onClick={(e) => e.stopPropagation()}>
                    <div className="flex items-center gap-0.5">
                      <button
                        onClick={() => togglePin(row.binance_symbol)}
                        className={`flex h-6 w-6 items-center justify-center rounded ${
                          isPinned ? "text-accent" : "text-transparent hover:text-accent"
                        } group-hover:text-accent`}
                        title={isPinned ? "Unpin" : "Pin (auto-subscribes WS)"}
                        aria-label={isPinned ? "Unpin" : "Pin"}
                      >
                        <PinIcon pinned={isPinned} />
                      </button>
                      {isPinned && pinnedCount > 1 && (
                        <div className="flex flex-col leading-none">
                          <button
                            onClick={() => movePin(row.binance_symbol, "up")}
                            disabled={pinIdx === 0}
                            className="text-[8px] text-muted hover:text-accent disabled:opacity-20"
                            title="Move pin up"
                            aria-label="Move pin up"
                          >▲</button>
                          <button
                            onClick={() => movePin(row.binance_symbol, "down")}
                            disabled={pinIdx === pinnedCount - 1}
                            className="text-[8px] text-muted hover:text-accent disabled:opacity-20"
                            title="Move pin down"
                            aria-label="Move pin down"
                          >▼</button>
                        </div>
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right text-muted">{row.rank}</td>
                  <td className="px-4 py-2">
                    <div className="flex items-center gap-2">
                      {row.image && (
                        <img
                          src={row.image}
                          alt=""
                          className="h-5 w-5 rounded-full shrink-0"
                          onError={(e) => {
                            (e.currentTarget as HTMLImageElement).style.display = "none";
                          }}
                        />
                      )}
                      <span className="font-semibold text-zinc-100 inline-block min-w-[6ch]">
                        {displayName(row.binance_symbol)}
                      </span>
                      <LiveDot subscribed={isSubscribed} fresh={fresh} pinned={isPinned} />
                      <span className="text-[10px] lowercase tracking-[0.14em] text-muted">
                        {row.name}
                      </span>
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right text-zinc-200">{formatPrice(row.price)}</td>
                  <td className={`px-3 py-2 text-right ${pctColor}`}>{formatPct(pct)}</td>
                  <td className="px-3 py-2 text-right text-zinc-200">{formatBig(row.volume_24h ?? NaN)}</td>
                  {METRIC_COLS.map((c) => {
                    const cell = metrics[c.key];
                    const v = cell?.value ?? null;
                    const bar = barFor(v, colDistributions[c.key] ?? [], c.colorMode);
                    // Anti-false-GREEN: a present value whose sample ts is older
                    // than STALE_AFTER_MS is dimmed + age-labelled so a frozen
                    // collection path can't read as a live number. Null keeps
                    // the existing "—" rendering untouched.
                    // Stale-dimming applies ONLY to value-path/realtime
                    // metrics (exact key membership). REST/poller metrics
                    // (~60s cadence: obi, oi, spread, funding, atr_liquidity…)
                    // would always read >8s old and must NOT be dimmed.
                    const cellAge =
                      STALE_DIM_METRICS.has(c.key) && v != null && cell
                        ? Date.now() - cell.ts
                        : null;
                    const isStale = cellAge != null && cellAge > STALE_AFTER_MS;
                    return (
                      <td key={c.key} className="px-3 py-2 text-right text-zinc-200 relative">
                        <span
                          className="relative z-10"
                          style={isStale ? { opacity: 0.5 } : undefined}
                          title={isStale ? `stale: ${Math.round((cellAge as number) / 1000)}s old` : undefined}
                        >
                          {c.format(v)}
                        </span>
                        {bar && (
                          <span
                            className="absolute left-1 right-1 bottom-1 h-[2px] rounded-sm pointer-events-none"
                            style={{ width: `calc(${bar.width * 100}% - 8px)`, background: bar.color, opacity: 0.85 }}
                          />
                        )}
                      </td>
                    );
                  })}
                  <td className="px-3 py-2 text-right">
                    <ScoreCell score={intelByRow[row.binance_symbol]?.score} />
                  </td>
                  <td className="px-3 py-2 text-right">
                    <ScoreCell score={intelByRow[row.binance_symbol]?.quality} subtle />
                  </td>
                  <td
                    className="px-3 py-2 align-top"
                    onClick={(e) => {
                      e.stopPropagation();
                      setAuditSymbol((cur) => (cur === row.binance_symbol ? null : row.binance_symbol));
                    }}
                    style={{ cursor: "pointer" }}
                    title="Click for regime audit"
                  >
                    <RegimeBadge regime={intelByRow[row.binance_symbol]?.regime} />
                  </td>
                  <td className="px-3 py-2 align-top">
                    <ConfidenceCell confidence={intelByRow[row.binance_symbol]?.confidence} />
                  </td>
                  <td className="px-3 py-2 align-top">
                    <AlertsCell alerts={activeAlertsBySymbol[row.binance_symbol] ?? []} />
                  </td>
                  <td className="px-3 py-2 align-top">
                    <FlagsCell flags={intelByRow[row.binance_symbol]?.flags ?? []} alerts={alerts} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {chartSymbol && (
        <LiquidityChartModal
          symbol={chartSymbol}
          orderedSymbols={visibleSymbols}
          onSwitchSymbol={(sym) => setChartSymbol(sym)}
          onClose={() => setChartSymbol(null)}
        />
      )}
    </div>
  );
}

// ── PinIcon / LiveDot / WsStatusPill ───────────────────────────────────────

function PinIcon({ pinned }: { pinned: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <path
        d="M4 2.25h6v2L8.9 5.4v2.35l1.25 1.25v.75H3.85V9l1.25-1.25V5.4L4 4.25v-2Z"
        fill={pinned ? "#E3822D" : "currentColor"}
        stroke="#E3822D"
        strokeWidth="1"
        strokeLinejoin="round"
      />
      <path d="M7 9.75v2" stroke="#E3822D" strokeWidth="1" strokeLinecap="round" />
    </svg>
  );
}

function LiveDot({
  subscribed,
  fresh,
  pinned,
}: {
  subscribed: boolean;
  fresh: boolean;
  pinned: boolean;
}) {
  // Only render the dot for symbols that have a WS reason to be live —
  // either explicitly pinned or currently subscribed (active modal).
  if (!subscribed && !pinned) return null;
  const color = !subscribed
    ? "#7d7d7d"           // pinned but worker hasn't picked it up yet
    : fresh
      ? "#52b97a"         // live: subscribed + a recent frame
      : "#c89a3a";        // stale: subscribed but no recent frames
  const title = !subscribed
    ? "Pinned — awaiting WS subscription"
    : fresh
      ? "Live (WS streaming)"
      : "Stale (no recent frames)";
  return (
    <span
      title={title}
      className="inline-block h-1.5 w-1.5 rounded-full"
      style={{ background: color, boxShadow: fresh ? `0 0 6px ${color}` : undefined }}
      aria-label={title}
    />
  );
}

function WsStatusPill({
  status,
  pinnedCount,
}: {
  status: LiqWsStatus | null;
  pinnedCount: number;
}) {
  if (!status) {
    return (
      <span className="text-[10px] uppercase tracking-[0.2em] text-muted">
        ws: …
      </span>
    );
  }
  const now = Date.now() / 1000;
  const ageSec = status.last_message_at ? now - status.last_message_at : null;
  const isStale = ageSec != null && ageSec > 10;
  const lastUpdatedAge = status.updated_at ? now - status.updated_at : null;
  // If the worker hasn't written status in 30s, treat the worker as down.
  const workerDead = lastUpdatedAge == null || lastUpdatedAge > 30;

  let color = "#7d7d7d";
  let label = "OFFLINE";
  if (workerDead) {
    color = "#d68b8b";
    label = "WORKER DOWN";
  } else if (!status.connected) {
    color = "#c89a3a";
    label = "RECONNECTING";
  } else if (isStale) {
    color = "#c89a3a";
    label = "STALE";
  } else {
    color = "#52b97a";
    label = "LIVE";
  }

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md border border-border/60 px-2 py-0.5 text-[10px] uppercase tracking-[0.2em]"
      title={
        `conn #${status.conn_id} · ${status.subscribed.length} streamed · ${pinnedCount}/${PIN_CAP} pinned` +
        (ageSec != null ? ` · last frame ${ageSec.toFixed(1)}s ago` : "")
      }
    >
      <span
        className="inline-block h-1.5 w-1.5 rounded-full"
        style={{ background: color, boxShadow: !isStale && status.connected ? `0 0 6px ${color}` : undefined }}
      />
      <span style={{ color }}>{label}</span>
      <span className="text-muted">
        {status.subscribed.length}↑ · {pinnedCount}/{PIN_CAP} pin
      </span>
    </span>
  );
}

// ── Score cell & regime badge ─────────────────────────────────────────────

function scoreColor(score: number): string {
  // 0–100 mapped through three stops: red < 40 < orange < 70 < green.
  if (!Number.isFinite(score)) return "#5a5f6b";
  if (score >= 70) {
    const t = Math.min(1, (score - 70) / 30);
    const r = Math.round(120 + (82 - 120) * t);
    const g = Math.round(175 + (185 - 175) * t);
    const b = Math.round(120 + (122 - 120) * t);
    return `rgb(${r}, ${g}, ${b})`;
  }
  if (score >= 40) {
    const t = (score - 40) / 30;
    const r = Math.round(227 + (180 - 227) * t);
    const g = Math.round(180 + (175 - 180) * t);
    const b = Math.round(87 + (120 - 87) * t);
    return `rgb(${r}, ${g}, ${b})`;
  }
  const t = Math.max(0, score) / 40;
  const r = Math.round(214 + (227 - 214) * t);
  const g = Math.round(105 + (180 - 105) * t);
  const b = Math.round(105 + (87 - 105) * t);
  return `rgb(${r}, ${g}, ${b})`;
}

function ScoreCell({ score, subtle }: { score: number | undefined; subtle?: boolean }) {
  if (score == null || !Number.isFinite(score)) {
    return <span className="text-muted">—</span>;
  }
  const color = scoreColor(score);
  return (
    <span
      className={`inline-block font-semibold ${subtle ? "opacity-80" : ""}`}
      style={{ color }}
      title={subtle ? "Market quality (weakest-third average)" : "Composite intelligence score"}
    >
      {score.toFixed(0)}
    </span>
  );
}

function RegimeBadge({ regime }: { regime: Regime | undefined }) {
  if (!regime) return <span className="text-muted">—</span>;
  const color = REGIME_COLORS[regime];
  const isUnknown = regime === "UNKNOWN";
  // Render "—" for UNKNOWN so the first paint reads as "we have not
  // measured yet" rather than confidently green "HEALTHY TREND". The
  // outer badge structure stays identical to other regimes so the cell
  // doesn't reshape when real data arrives.
  const label = isUnknown ? "—" : regime.replace(/_/g, " ");
  return (
    <span
      className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em] whitespace-nowrap"
      style={{
        color,
        borderColor: color.replace(/0\.95\)$/, "0.45)"),
        background: color.replace(/0\.95\)$/, "0.10)"),
      }}
      title={isUnknown ? "Awaiting metric samples" : `Regime: ${label}`}
    >
      {label}
    </span>
  );
}

// ── Confidence chip ───────────────────────────────────────────────────────

function ConfidenceCell({ confidence }: { confidence: Confidence | undefined }) {
  if (!confidence) return <span className="text-muted">—</span>;
  const colorByState: Record<typeof confidence.state, string> = {
    UNKNOWN: "rgba(140, 140, 150, 0.95)",
    HIGH: "rgba(82, 185, 122, 0.95)",
    MEDIUM: "rgba(227, 180, 87, 0.95)",
    LOW: "rgba(214, 105, 105, 0.95)",
  };
  const color = colorByState[confidence.state];
  const isUnknown = confidence.state === "UNKNOWN";
  // For UNKNOWN we show "— —" instead of "HIGH 92": the dot stays at
  // the same x-position so the column doesn't shift width when the real
  // confidence arrives, but neither the state label nor the score
  // number imply that we have measured anything yet.
  const title = isUnknown
    ? confidence.reasons.join(" · ") || "Awaiting metric samples"
    : confidence.reasons.length > 0
      ? `${confidence.score.toFixed(0)} · ${confidence.reasons.join(" · ")}`
      : `Confidence ${confidence.score.toFixed(0)}`;
  return (
    <span
      className="inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.14em]"
      title={title}
      style={{ color }}
    >
      <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {isUnknown ? "—" : confidence.state}
      <span className="text-muted">{isUnknown ? "—" : confidence.score.toFixed(0)}</span>
    </span>
  );
}

// ── Alerts cell ───────────────────────────────────────────────────────────

function AlertsCell({ alerts }: { alerts: AlertEvent[] }) {
  if (alerts.length === 0) return <span className="text-muted">—</span>;
  // Worst severity wins for the cell's dominant color, but each alert is
  // rendered as its own chip so multiple kinds are visible at once.
  return (
    <div className="flex flex-wrap gap-1 max-w-[240px]">
      {alerts.map((a) => (
        <span
          key={a.id}
          title={`${a.kind} · ${a.trigger} · regime=${a.regime} · conf=${a.confidence.toFixed(0)}`}
          className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
          style={{
            color: SEVERITY_COLOR[a.severity],
            borderColor: SEVERITY_COLOR[a.severity].replace(/0\.95\)$/, "0.5)"),
            background: SEVERITY_COLOR[a.severity].replace(/0\.95\)$/, "0.10)"),
          }}
        >
          {a.kind.replace(/_/g, " ")}
        </span>
      ))}
    </div>
  );
}

// ── Replay controls ───────────────────────────────────────────────────────

function fmtTs(ts: number | null | undefined): string {
  if (ts == null) return "—";
  try {
    return new Date(ts).toISOString().replace("T", " ").slice(0, 19) + " UTC";
  } catch {
    return "—";
  }
}

const ANNOTATION_KINDS: { kind: AnnotationKind; label: string; color: string }[] = [
  { kind: "useful_signal",    label: "USEFUL",      color: "rgba(82, 185, 122, 0.95)" },
  { kind: "false_signal",     label: "FALSE",       color: "rgba(214, 105, 105, 0.95)" },
  { kind: "manipulation",     label: "MANIP",       color: "rgba(214, 75, 75, 0.95)" },
  { kind: "interesting_setup", label: "SETUP",      color: "rgba(227, 180, 87, 0.95)" },
  { kind: "liquidation_event", label: "LIQ EVENT",  color: "rgba(214, 105, 105, 0.95)" },
  { kind: "spoof_behavior",   label: "SPOOF",       color: "rgba(214, 139, 105, 0.95)" },
  { kind: "other",            label: "OTHER",       color: "rgba(140, 170, 235, 0.95)" },
];

function ReplayBar({
  active,
  onToggle,
  range,
  asOf,
  onSeek,
  playing,
  onPlayToggle,
  onStep,
  speed,
  onSpeed,
  annotationTarget,
  onAnnotate,
}: {
  active: boolean;
  onToggle: () => void;
  range: { earliest_ts: number | null; latest_ts: number | null } | null;
  asOf: number | null;
  onSeek: (ts: number) => void;
  playing: boolean;
  onPlayToggle: () => void;
  onStep: (deltaMs: number) => void;
  speed: number;
  onSpeed: (s: number) => void;
  annotationTarget: string | null;
  onAnnotate: (kind: AnnotationKind, note: string) => Promise<void> | void;
}) {
  const [annotationKind, setAnnotationKind] = useState<AnnotationKind | null>(null);
  const [annotationNote, setAnnotationNote] = useState("");
  const [annotationSaving, setAnnotationSaving] = useState(false);
  const [annotationFlash, setAnnotationFlash] = useState<string | null>(null);
  if (!active) {
    return (
      <div className="flex items-center justify-between rounded-xl border border-border bg-panel/40 px-3 py-2">
        <span className="text-[10px] uppercase tracking-[0.2em] text-muted">
          LIVE — toggle to replay historical state
        </span>
        <button
          onClick={onToggle}
          className="h-7 px-3 rounded-md border border-border text-[10px] uppercase tracking-[0.22em] text-muted hover:text-accent hover:border-accent/60"
        >
          ⏵ Enter replay
        </button>
      </div>
    );
  }

  const min = range?.earliest_ts ?? 0;
  const max = range?.latest_ts ?? 0;
  const span = Math.max(1, max - min);
  const v = asOf != null ? Math.max(min, Math.min(max, asOf)) : max;
  const pct = max > min ? ((v - min) / span) * 100 : 0;
  const SPEEDS = [10, 30, 60, 120, 600];

  return (
    <div
      className="rounded-xl border px-3 py-2 space-y-2"
      style={{ borderColor: "rgba(214, 139, 105, 0.45)", background: "rgba(214, 139, 105, 0.08)" }}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="text-[10px] uppercase tracking-[0.22em]" style={{ color: "rgba(214, 139, 105, 0.95)" }}>
          REPLAY — historical state
        </span>
        <span className="text-[10px] text-muted whitespace-nowrap font-mono">
          {fmtTs(v)}
        </span>
        <button
          onClick={onToggle}
          className="h-7 px-3 rounded-md border border-border text-[10px] uppercase tracking-[0.22em] text-muted hover:text-zinc-200"
        >
          ✕ Exit
        </button>
      </div>
      <div className="flex items-center gap-2">
        <button
          onClick={() => onStep(-60_000)}
          className="h-7 px-2 rounded-md border border-border text-[10px] text-muted hover:text-zinc-200"
          title="Back 1m"
        >
          ⟸1m
        </button>
        <button
          onClick={() => onStep(-5_000)}
          className="h-7 px-2 rounded-md border border-border text-[10px] text-muted hover:text-zinc-200"
          title="Back 5s"
        >
          ⟵5s
        </button>
        <button
          onClick={onPlayToggle}
          className="h-7 w-10 rounded-md border border-accent/60 text-accent text-[12px] flex items-center justify-center"
          title={playing ? "Pause" : "Play"}
        >
          {playing ? "⏸" : "▶"}
        </button>
        <button
          onClick={() => onStep(5_000)}
          className="h-7 px-2 rounded-md border border-border text-[10px] text-muted hover:text-zinc-200"
          title="Forward 5s"
        >
          5s⟶
        </button>
        <button
          onClick={() => onStep(60_000)}
          className="h-7 px-2 rounded-md border border-border text-[10px] text-muted hover:text-zinc-200"
          title="Forward 1m"
        >
          1m⟹
        </button>
        <div className="flex items-center gap-1 ml-2">
          {SPEEDS.map((s) => (
            <button
              key={s}
              onClick={() => onSpeed(s)}
              className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.12em] ${
                speed === s
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border text-muted hover:text-zinc-200"
              }`}
            >
              {s}×
            </button>
          ))}
        </div>
        <input
          type="range"
          min={min}
          max={max}
          step={1000}
          value={v}
          onChange={(e) => onSeek(Number(e.target.value))}
          className="flex-1 accent-orange-400"
        />
        <span className="text-[9px] text-muted whitespace-nowrap font-mono w-12 text-right">
          {pct.toFixed(0)}%
        </span>
      </div>

      {/* Annotation row — tag the current (symbol, ts) for the research
          dataset. Symbol comes from the audit panel selection so the
          user "anchors" a row before annotating. */}
      <div className="flex flex-wrap items-center gap-1 pt-1">
        <span className="text-[9px] uppercase tracking-[0.18em] text-muted mr-1">
          annotate {annotationTarget ? `· ${displayName(annotationTarget)} @ ${fmtTs(v).slice(11, 19)}` : "· pick a row first"}
        </span>
        {ANNOTATION_KINDS.map((k) => (
          <button
            key={k.kind}
            type="button"
            disabled={!annotationTarget}
            onClick={() => setAnnotationKind(k.kind)}
            className={`h-6 px-2 rounded border text-[9px] uppercase tracking-[0.12em] ${
              annotationKind === k.kind ? "" : "hover:bg-white/[0.04]"
            } ${!annotationTarget ? "opacity-30 cursor-not-allowed" : ""}`}
            style={{
              color: k.color,
              borderColor: k.color.replace(/0\.95\)$/, "0.5)"),
              background: annotationKind === k.kind ? k.color.replace(/0\.95\)$/, "0.18)") : "transparent",
            }}
            title={k.kind}
          >
            {k.label}
          </button>
        ))}
        {annotationKind && annotationTarget && (
          <>
            <input
              type="text"
              value={annotationNote}
              onChange={(e) => setAnnotationNote(e.target.value)}
              placeholder="note (optional)"
              className="h-6 px-2 rounded border border-border bg-bg/60 text-[10px] text-zinc-200 min-w-[200px] flex-1"
            />
            <button
              type="button"
              disabled={annotationSaving}
              onClick={async () => {
                setAnnotationSaving(true);
                try {
                  await onAnnotate(annotationKind, annotationNote);
                  setAnnotationFlash("saved");
                  setAnnotationNote("");
                  setAnnotationKind(null);
                  setTimeout(() => setAnnotationFlash(null), 1500);
                } catch {
                  setAnnotationFlash("failed");
                } finally {
                  setAnnotationSaving(false);
                }
              }}
              className="h-6 px-3 rounded border border-accent/60 text-[10px] uppercase tracking-[0.18em] text-accent hover:bg-accent/10 disabled:opacity-40"
            >
              {annotationSaving ? "…" : "save"}
            </button>
          </>
        )}
        {annotationFlash && (
          <span
            className="text-[9px] uppercase tracking-[0.18em]"
            style={{ color: annotationFlash === "saved" ? "rgba(82, 185, 122, 0.95)" : "rgba(214, 105, 105, 0.95)" }}
          >
            {annotationFlash}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Validation stats bar ─────────────────────────────────────────────────

function ValidationStatsBar({ timeline }: { timeline: AlertEvent[] }) {
  // Per-kind precision derived from the recent timeline using validateAlert.
  // We resolve each alert by re-querying its symbol's series synchronously
  // from the snapshot — but the snapshot only has the latest values, not
  // the post-alert tape we need. So this banner shows a lighter-weight
  // measure: outcome buckets from "did the alert remain hot ≥30s" as a
  // proxy for persistence. Real per-kind precision needs series fetches
  // and lives in the detail modal.
  const now = Date.now();
  const counts: Record<string, { total: number; persistent: number; recentCrit: number }> = {};
  for (const a of timeline) {
    const c = counts[a.kind] ?? { total: 0, persistent: 0, recentCrit: 0 };
    c.total += 1;
    if (a.lastSeenAt - a.startedAt >= 30_000) c.persistent += 1;
    if (a.severity === "critical" && now - a.lastSeenAt < 5 * 60_000) c.recentCrit += 1;
    counts[a.kind] = c;
  }
  const totalAlerts = timeline.length;
  // Previously this returned null when timeline was empty, which meant
  // the bar appeared OUT OF NOWHERE the moment the first alert promoted
  // and pushed the whole table down. Now we always reserve the row with
  // a placeholder so first-paint and post-first-snapshot have the same
  // page geometry.
  if (totalAlerts === 0) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-panel/40 px-3 py-2">
        <span className="text-[10px] uppercase tracking-[0.22em] text-muted">
          validation · last 0
        </span>
      </div>
    );
  }
  const top = Object.entries(counts)
    .sort((a, b) => b[1].total - a[1].total)
    .slice(0, 6);

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-panel/40 px-3 py-2">
      <span className="text-[10px] uppercase tracking-[0.22em] text-muted">
        validation · last {totalAlerts}
      </span>
      {top.map(([kind, c]) => {
        const precision = c.total === 0 ? 0 : c.persistent / c.total;
        return (
          <span
            key={kind}
            title={`${kind}: ${c.persistent}/${c.total} persisted ≥30s · ${c.recentCrit} critical`}
            className="inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.14em]"
          >
            <span className="text-muted">{kind.replace(/_/g, " ")}</span>
            <span style={{ color: precision >= 0.6 ? "rgba(82, 185, 122, 0.95)" : precision >= 0.3 ? "rgba(227, 180, 87, 0.95)" : "rgba(214, 105, 105, 0.95)" }}>
              {(precision * 100).toFixed(0)}%
            </span>
          </span>
        );
      })}
    </div>
  );
}

// ── Audit panel ──────────────────────────────────────────────────────────
// Renders WHY a regime was chosen + the intelligence-score breakdown for
// one symbol. Opens via clicking the REGIME cell on a row.

function AuditPanel({
  symbol,
  metrics,
  cohort,
  intel,
  onClose,
}: {
  symbol: string;
  metrics: MetricsMap;
  cohort: CohortStats;
  intel: {
    score: number;
    quality: number;
    regime: Regime;
    flags: Flag[];
    confidence: Confidence;
    proposals: AlertProposal[];
  };
  onClose: () => void;
}) {
  const thresholds = useMemo(
    () => computeAdaptiveThresholds(cohort, intel.regime, metrics),
    [cohort, intel.regime, metrics],
  );
  const regimeExplain: RegimeExplanation = useMemo(
    () => explainRegime(metrics, intel.flags, thresholds, intel.confidence.score),
    [metrics, intel.flags, thresholds, intel.confidence.score],
  );
  const breakdown: IntelBreakdown | null = useMemo(
    () => intelligenceBreakdown(metrics, cohort, intel.regime),
    [metrics, cohort, intel.regime],
  );

  return (
    <div className="rounded-xl border border-border bg-panel/60 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-baseline gap-3">
          <span className="text-zinc-100 font-semibold tracking-[0.18em]">{displayName(symbol)}</span>
          <RegimeBadge regime={intel.regime} />
          <span className="text-[10px] uppercase tracking-[0.2em] text-muted">
            audit · regime confidence {regimeExplain.confidence.toFixed(0)}
          </span>
        </div>
        <button
          onClick={onClose}
          className="h-7 w-7 rounded-md border border-border text-muted hover:text-zinc-200"
          title="Close"
        >
          ✕
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <div className="text-[10px] uppercase tracking-[0.22em] text-muted mb-2">
            Why this regime?
          </div>
          <ul className="space-y-1 text-[11px] font-mono text-zinc-200">
            {regimeExplain.drivers.map((d, i) => (
              <li key={i} className="leading-tight">
                <span className="text-muted">▸</span> {d}
              </li>
            ))}
          </ul>
          {regimeExplain.candidates.length > 1 && (
            <div className="mt-2 text-[10px] text-muted">
              Other candidates: {regimeExplain.candidates.slice(1).join(", ")}
            </div>
          )}
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-[0.22em] text-muted mb-2">
            Intelligence breakdown · score {intel.score.toFixed(0)} · quality {intel.quality.toFixed(0)}
          </div>
          {breakdown ? (
            <div className="grid grid-cols-2 gap-3 text-[10px] font-mono">
              <div>
                <div className="text-[9px] uppercase tracking-[0.15em] text-muted mb-1">Lifting score</div>
                {breakdown.positive.length === 0 && <div className="text-muted">—</div>}
                {breakdown.positive.map((c) => (
                  <div key={c.key} className="flex items-center justify-between">
                    <span className="text-zinc-200">{c.label}</span>
                    <span style={{ color: "rgba(82, 185, 122, 0.95)" }}>
                      +{c.delta.toFixed(1)}
                    </span>
                  </div>
                ))}
              </div>
              <div>
                <div className="text-[9px] uppercase tracking-[0.15em] text-muted mb-1">Dragging score</div>
                {breakdown.negative.length === 0 && <div className="text-muted">—</div>}
                {breakdown.negative.map((c) => (
                  <div key={c.key} className="flex items-center justify-between">
                    <span className="text-zinc-200">{c.label}</span>
                    <span style={{ color: "rgba(214, 105, 105, 0.95)" }}>
                      {c.delta.toFixed(1)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="text-[10px] text-muted">No intelligence components available</div>
          )}
        </div>
      </div>

      {intel.confidence.reasons.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-[0.22em] text-muted mb-1">
            Confidence ({intel.confidence.state} · {intel.confidence.score.toFixed(0)})
          </div>
          <div className="text-[10px] text-zinc-300">
            {intel.confidence.reasons.join(" · ")}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Anomaly timeline ─────────────────────────────────────────────────────
// Horizontal strip of the most-recent confirmed alerts across all
// visible symbols — at a glance "what just changed in the market".
// Clicking a chip jumps to the symbol's detail view.

function AnomalyTimeline({
  timeline,
  onJump,
}: {
  timeline: AlertEvent[];
  onJump: (symbol: string) => void;
}) {
  if (timeline.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-panel/40 px-3 py-2 text-[10px] uppercase tracking-[0.2em] text-muted">
        no recent anomalies
      </div>
    );
  }
  const fmtAge = (ts: number) => {
    const age = Math.max(0, (Date.now() - ts) / 1000);
    if (age < 60) return `${age.toFixed(0)}s`;
    if (age < 3600) return `${(age / 60).toFixed(0)}m`;
    return `${(age / 3600).toFixed(0)}h`;
  };
  // Display-level collapse: the alert engine mints a new bucketed alertId every
  // 30s, so a persistent condition yields several timeline entries with the
  // same (symbol, kind). Show ONE chip per condition — keep the highest-priority
  // generation (timeline is pre-sorted priority desc, lastSeenAt desc) and
  // surface the freshest lastSeenAt for its age. Presentation only: the
  // underlying timeline array, alertId, persistence and validation are untouched.
  const collapsed: AlertEvent[] = [];
  const repByKey = new Map<string, AlertEvent>();
  for (const a of timeline) {
    const k = `${a.symbol}:${a.kind}`;
    const rep = repByKey.get(k);
    if (!rep) {
      const copy = { ...a };
      repByKey.set(k, copy);
      collapsed.push(copy);
    } else if (a.lastSeenAt > rep.lastSeenAt) {
      rep.lastSeenAt = a.lastSeenAt; // freshen displayed age; keep highest-priority rep
    }
  }
  return (
    <div className="rounded-xl border border-border bg-panel/40 px-3 py-2 flex items-center gap-2 overflow-x-auto">
      <span className="text-[10px] uppercase tracking-[0.2em] text-muted whitespace-nowrap">
        anomaly timeline
      </span>
      <div className="flex items-center gap-1 flex-1 min-w-0">
        {collapsed.map((a) => (
          <button
            key={`${a.symbol}:${a.kind}`}
            type="button"
            onClick={() => onJump(a.symbol)}
            className="flex items-center gap-1 rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em] whitespace-nowrap hover:bg-white/[0.04]"
            title={`${a.symbol} · ${a.kind} · ${a.trigger} · regime=${a.regime} · ${fmtAge(a.lastSeenAt)} ago`}
            style={{
              color: SEVERITY_COLOR[a.severity],
              borderColor: SEVERITY_COLOR[a.severity].replace(/0\.95\)$/, "0.5)"),
              background: SEVERITY_COLOR[a.severity].replace(/0\.95\)$/, "0.10)"),
            }}
          >
            <span className="font-semibold">{displayName(a.symbol)}</span>
            <span>{a.kind.replace(/_/g, " ")}</span>
            <span className="text-muted">{fmtAge(a.lastSeenAt)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Flag chips ─────────────────────────────────────────────────────────────

// Display-only dedup: a flag whose root fact is already shown in the row by a
// more informative instance is hidden. The engine still computes every flag and
// alert unchanged — this only filters what the FLAGS cell renders so one fact
// occupies one chip. Maps a flag label to the alert kind that supersedes it
// (the alert carries severity + persistence, so it is the more informative twin).
const FLAG_TWIN_ALERT: Record<string, AlertKind> = {
  "THIN": "DEPTH_COLLAPSE",
  "SPOOF-RISK": "DEPTH_COLLAPSE",
  "LIQ-STRESS": "LIQ_CASCADE",
  "FRAGILITY": "FRAGILITY_SPIKE",
  "FUNDING-EXTREME": "FUNDING_EXTREME",
  // OI-SURGE± is a dynamic label (`OI-SURGE${dir}`) → matched by prefix below.
};

function dedupeFlagsForDisplay(flags: Flag[], alerts: AlertEvent[]): Flag[] {
  const kinds = new Set(alerts.map((a) => a.kind));
  const labels = new Set(flags.map((f) => f.label));
  return flags.filter((f) => {
    if (f.label === "THIN" && labels.has("SPOOF-RISK")) return false;       // subset: THIN ⊂ SPOOF-RISK
    if (f.label.startsWith("OI-SURGE") && kinds.has("OI_SURGE")) return false; // OI twin (prefix match)
    const twin = FLAG_TWIN_ALERT[f.label];
    if (twin && kinds.has(twin)) return false;                              // alert twin already shown
    return true;
  });
}

function FlagsCell({ flags, alerts }: { flags: Flag[]; alerts: AlertEvent[] }) {
  const shown = dedupeFlagsForDisplay(flags, alerts);
  if (shown.length === 0) return <span className="text-muted">—</span>;
  return (
    <div className="flex flex-wrap gap-1 max-w-[220px]">
      {shown.map((f) => (
        <span
          key={f.label}
          title={f.title}
          className="inline-block rounded-sm border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.12em]"
          style={{
            color: f.color,
            borderColor: f.color.replace(/0\.95\)$/, "0.45)"),
            background: f.color.replace(/0\.95\)$/, "0.10)"),
          }}
        >
          {f.label}
        </span>
      ))}
    </div>
  );
}

// ── Cell bar (mini-heatmap) ────────────────────────────────────────────────

function barFor(
  value: number | null,
  sortedValues: number[],
  mode: ColorMode,
): { width: number; color: string } | null {
  if (value == null || !Number.isFinite(value)) return null;

  if (mode === "signed") {
    // OBI / OI Δ: bar magnitude scales with |value|, colored by sign.
    // OBI fits a [-0.6, 0.6] band; OI Δ is in %. Use the larger scale
    // automatically by inferring from observed range when |value| < 5,
    // otherwise treat it like OI Δ in % (cap at 10%).
    const denom = sortedValues.some((v) => Math.abs(v) > 1.5) ? 10 : 0.6;
    const mag = Math.min(1, Math.abs(value) / denom);
    if (mag < 0.06) return null;
    const color = value > 0 ? "rgba(82, 185, 122, 0.95)" : "rgba(214, 105, 105, 0.95)";
    return { width: mag, color };
  }

  if (mode === "diverging_funding") {
    // Funding: 0 = neutral (no bar). + = long-crowded (orange→red),
    // − = short-crowded (cyan→purple). Auto-scale by observed magnitude
    // for funding-rate columns; for z-score columns (where the range is
    // bounded around |z|<5) the same denom heuristic still gives a sane
    // bar width.
    const denom = sortedValues.some((v) => Math.abs(v) > 1)
      ? 5                   // funding_z scale
      : 0.0005;             // raw funding scale (~5 bps "extreme")
    const mag = Math.min(1, Math.abs(value) / denom);
    if (mag < 0.08) return null;
    if (value > 0) {
      // long-crowded: warm
      const r = Math.round(227 + (214 - 227) * mag);
      const g = Math.round(150 + (75 - 150) * mag);
      const b = Math.round(70 + (90 - 70) * mag);
      return { width: mag, color: `rgba(${r}, ${g}, ${b}, 0.95)` };
    }
    // short-crowded: cool
    const r = Math.round(90 + (140 - 90) * mag);
    const g = Math.round(170 + (110 - 170) * mag);
    const b = Math.round(220 + (235 - 220) * mag);
    return { width: mag, color: `rgba(${r}, ${g}, ${b}, 0.95)` };
  }

  if (sortedValues.length < 2) return null;
  const lo = sortedValues[0];
  const hi = sortedValues[sortedValues.length - 1];
  if (lo === hi) return null;
  const t = Math.max(0, Math.min(1, (value - lo) / (hi - lo)));

  if (mode === "higher_better") {
    return { width: t, color: "rgba(82, 185, 122, 0.9)" };
  }
  if (mode === "lower_better") {
    return { width: 1 - t, color: "rgba(82, 185, 122, 0.9)" };
  }
  if (mode === "warning_pos") {
    if (value <= 0) return null;
    const positives = sortedValues.filter((v) => v > 0);
    if (positives.length === 0) return null;
    const minPos = positives[0];
    const maxPos = positives[positives.length - 1];
    const tw = minPos === maxPos ? 0.5 : (value - minPos) / (maxPos - minPos);
    const r = Math.round(227 + (214 - 227) * tw);
    const g = Math.round(180 + (105 - 180) * tw);
    const b = Math.round(87 + (105 - 87) * tw);
    return { width: Math.max(0.15, tw), color: `rgba(${r}, ${g}, ${b}, 0.95)` };
  }
  return null;
}

// ── SortHeader ─────────────────────────────────────────────────────────────

function SortHeader({
  label,
  sortKey,
  current,
  onSort,
  align,
  title,
}: {
  label: string;
  sortKey: SortKey;
  current: SortState;
  onSort: (k: SortKey) => void;
  align: "left" | "right";
  title?: string;
}) {
  const active = current.key === sortKey;
  const arrow = !active ? "" : current.dir === "asc" ? " ▲" : " ▼";
  return (
    <th
      className={`px-3 py-3 font-normal cursor-pointer hover:text-zinc-200 whitespace-nowrap ${align === "right" ? "text-right" : "text-left"} ${active ? "text-zinc-100" : ""}`}
      onClick={() => onSort(sortKey)}
      title={title}
    >
      {label}
      <span className="text-accent">{arrow}</span>
    </th>
  );
}
