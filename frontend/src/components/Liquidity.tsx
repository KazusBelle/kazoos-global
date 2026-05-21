import { useEffect, useMemo, useRef, useState } from "react";
import {
  getLiquidityMetricsSnapshot,
  getLiquidityTop,
  type LiqMetricsSnapshot,
  type LiqRow,
} from "../lib/api";
import { LiquidityChartModal } from "./LiquidityChartModal";

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
type ColorMode = "higher_better" | "lower_better" | "signed" | "warning_pos";

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
    title: "Total USD of forced liquidations over last 60s. Only for symbols actively opened (WS-only).",
    format: (v) => (v == null || v === 0 ? "—" : `$${formatBig(v)}`),
    colorMode: "warning_pos",
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

  const visibleSymbols = useMemo(
    () => (filteredRows ?? []).map((r) => r.binance_symbol),
    [filteredRows],
  );

  // Poll the metric snapshot for the symbols currently visible in the
  // tier. Updates every SNAPSHOT_POLL_MS so REST-metric columns reflect
  // the worker's latest write and WS-metric cells light up the moment a
  // symbol becomes active.
  const visibleSymbolsKey = visibleSymbols.join(",");
  const symbolsRef = useRef(visibleSymbols);
  useEffect(() => {
    symbolsRef.current = visibleSymbols;
  }, [visibleSymbols]);

  useEffect(() => {
    if (visibleSymbols.length === 0) return;
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
  }, [visibleSymbolsKey]);

  // Build sorted rows + cached metric distributions (per column, for color).
  const sortedRows = useMemo(() => {
    if (!filteredRows) return null;
    const getMetric = (sym: string, key: string): number | null => {
      const m = snapshot?.symbols?.[sym]?.[key];
      return m?.value ?? null;
    };
    const getSortValue = (r: LiqRow): number | null => {
      if (sort.key === "rank") return r.rank;
      if (sort.key === "price") return r.price;
      if (sort.key === "change_24h_pct") return r.change_24h_pct;
      if (sort.key === "volume_24h") return r.volume_24h;
      return getMetric(r.binance_symbol, sort.key as string);
    };
    const dirMul = sort.dir === "asc" ? 1 : -1;
    const arr = [...filteredRows];
    arr.sort((a, b) => {
      const va = getSortValue(a);
      const vb = getSortValue(b);
      // null always at the bottom (regardless of direction)
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (va === vb) return 0;
      return va < vb ? -1 * dirMul : 1 * dirMul;
    });
    return arr;
  }, [filteredRows, sort, snapshot]);

  // Per-metric distributions for color normalization. Sorted ascending,
  // nulls stripped.
  const distributions = useMemo(() => {
    const out: Record<string, number[]> = {};
    if (!filteredRows || !snapshot) return out;
    for (const col of METRIC_COLS) {
      const values: number[] = [];
      for (const r of filteredRows) {
        const v = snapshot.symbols?.[r.binance_symbol]?.[col.key]?.value;
        if (v != null && Number.isFinite(v)) values.push(v);
      }
      values.sort((a, b) => a - b);
      out[col.key] = values;
    }
    return out;
  }, [filteredRows, snapshot]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex items-baseline gap-3">
          <div className="text-accent text-xl font-bold tracking-[0.3em]">LIQ</div>
          <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
            liquidity scanner
          </div>
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

      <div className="bg-panel border border-border rounded-2xl overflow-x-auto">
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.2em] text-muted border-b border-border select-none">
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
            </tr>
          </thead>
          <tbody>
            {error && (
              <tr>
                <td colSpan={5 + METRIC_COLS.length} className="py-6 text-center text-sm" style={{ color: "rgba(214, 139, 139, 0.9)" }}>
                  {error}
                </td>
              </tr>
            )}
            {!error && loading && sortedRows === null && (
              <tr>
                <td colSpan={5 + METRIC_COLS.length} className="py-8 text-center text-sm text-muted">
                  Loading…
                </td>
              </tr>
            )}
            {!error && sortedRows && sortedRows.length === 0 && (
              <tr>
                <td colSpan={5 + METRIC_COLS.length} className="py-8 text-center text-sm text-muted">
                  No coins in this slice are tradable on Binance Futures.
                </td>
              </tr>
            )}
            {sortedRows?.map((row) => {
              const pct = row.change_24h_pct;
              const pctColor =
                pct == null ? "text-muted" : pct > 0 ? "text-discount" : pct < 0 ? "text-[#d68b8b]" : "text-zinc-300";
              const metrics = snapshot?.symbols?.[row.binance_symbol] ?? {};
              return (
                <tr
                  key={row.binance_symbol}
                  onClick={() => setChartSymbol(row.binance_symbol)}
                  className="border-t border-border/60 hover:bg-white/[0.02] transition-colors cursor-pointer"
                >
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
                      <span className="text-[10px] lowercase tracking-[0.14em] text-muted">
                        {row.name}
                      </span>
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right text-zinc-200">{formatPrice(row.price)}</td>
                  <td className={`px-3 py-2 text-right ${pctColor}`}>{formatPct(pct)}</td>
                  <td className="px-3 py-2 text-right text-zinc-200">{formatBig(row.volume_24h ?? NaN)}</td>
                  {METRIC_COLS.map((c) => {
                    const v = metrics[c.key]?.value ?? null;
                    const bg = cellBackground(v, distributions[c.key] ?? [], c.colorMode);
                    return (
                      <td
                        key={c.key}
                        className="px-3 py-2 text-right text-zinc-200"
                        style={bg ? { backgroundColor: bg } : undefined}
                      >
                        {c.format(v)}
                      </td>
                    );
                  })}
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
