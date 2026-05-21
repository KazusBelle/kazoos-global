import { useEffect, useMemo, useState } from "react";
import { getLiquidityTop, type LiqRow } from "../lib/api";
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

function loadSlice(): SliceLabel {
  const raw = Number(localStorage.getItem(SLICE_KEY));
  if (SLICES.some((s) => s.label === raw)) return raw as SliceLabel;
  return 100;
}

function formatBig(n: number | null): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)}T`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(2)}K`;
  return n.toFixed(2);
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

function displayName(symbol: string): string {
  let s = symbol.replace(/USDT$/, "");
  if (s.startsWith("1000")) s = s.slice(4);
  return s;
}

export function Liquidity() {
  const [sliceLabel, setSliceLabelState] = useState<SliceLabel>(loadSlice);
  const [allRows, setAllRows] = useState<LiqRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  function setSlice(next: SliceLabel) {
    setSliceLabelState(next);
    localStorage.setItem(SLICE_KEY, String(next));
  }

  // Always fetch the full 500-wide universe once. Slicing into the three
  // tiers is a pure client-side filter on rank, so switching tiers is
  // instant and there's no need to re-hit CoinGecko per click.
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
  const rows = allRows
    ? allRows.filter((r) => r.rank >= currentSlice.min && r.rank <= currentSlice.max)
    : null;

  const [chartSymbol, setChartSymbol] = useState<string | null>(null);
  const visibleSymbols = useMemo(
    () => (rows ?? []).map((r) => r.binance_symbol),
    [rows],
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex items-baseline gap-3">
          <div className="text-accent text-xl font-bold tracking-[0.3em]">LIQ</div>
          <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
            liquidity screener
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
            <tr className="text-[10px] uppercase tracking-[0.2em] text-muted border-b border-border">
              <th className="w-14 px-3 py-3 font-normal text-right">#</th>
              <th className="px-4 py-3 font-normal text-left whitespace-nowrap">COIN</th>
              <th className="px-3 py-3 font-normal text-right whitespace-nowrap">PRICE</th>
              <th className="px-3 py-3 font-normal text-right whitespace-nowrap">Δ 24H</th>
              <th className="px-3 py-3 font-normal text-right whitespace-nowrap">MARKET CAP</th>
              <th className="px-3 py-3 font-normal text-right whitespace-nowrap">VOLUME 24H</th>
            </tr>
          </thead>
          <tbody>
            {error && (
              <tr>
                <td colSpan={6} className="py-6 text-center text-sm" style={{ color: "rgba(214, 139, 139, 0.9)" }}>
                  {error}
                </td>
              </tr>
            )}
            {!error && loading && rows === null && (
              <tr>
                <td colSpan={6} className="py-8 text-center text-sm text-muted">
                  Loading…
                </td>
              </tr>
            )}
            {!error && rows && rows.length === 0 && (
              <tr>
                <td colSpan={6} className="py-8 text-center text-sm text-muted">
                  No coins in this slice are tradable on Binance Futures.
                </td>
              </tr>
            )}
            {rows?.map((row) => {
              const pct = row.change_24h_pct;
              const pctColor =
                pct == null
                  ? "text-muted"
                  : pct > 0
                    ? "text-discount"
                    : pct < 0
                      ? "text-[#d68b8b]"
                      : "text-zinc-300";
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
                      {/* Fixed 6ch slot reserves a uniform gap between
                          ticker and name regardless of ticker length — TRX,
                          BTC, 1INCH all leave the name column landing at
                          the same x-offset. */}
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
                  <td className="px-3 py-2 text-right text-zinc-200">{formatBig(row.market_cap)}</td>
                  <td className="px-3 py-2 text-right text-zinc-200">{formatBig(row.volume_24h)}</td>
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
