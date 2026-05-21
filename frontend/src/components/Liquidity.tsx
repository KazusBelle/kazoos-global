import { useEffect, useState } from "react";
import { getLiquidityTop, type LiqRow } from "../lib/api";

const LIMIT_KEY = "kazus_liq_limit";
const ALLOWED_LIMITS = [100, 250, 500] as const;
type LiqLimit = (typeof ALLOWED_LIMITS)[number];

function loadLimit(): LiqLimit {
  const raw = Number(localStorage.getItem(LIMIT_KEY));
  if (ALLOWED_LIMITS.includes(raw as LiqLimit)) return raw as LiqLimit;
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
  const [limit, setLimitState] = useState<LiqLimit>(loadLimit);
  const [rows, setRows] = useState<LiqRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  function setLimit(next: LiqLimit) {
    setLimitState(next);
    localStorage.setItem(LIMIT_KEY, String(next));
  }

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getLiquidityTop(limit)
      .then((res) => {
        if (cancelled) return;
        setRows(res.rows);
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
  }, [limit]);

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
          {ALLOWED_LIMITS.map((n) => (
            <button
              key={n}
              onClick={() => setLimit(n)}
              className={`h-8 px-3 rounded-md border text-[11px] uppercase tracking-[0.22em] transition-colors ${
                limit === n
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border text-muted hover:text-zinc-200 hover:border-accent/50"
              }`}
              title={`Top ${n} by market cap`}
            >
              TOP {n}
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
                  className="border-t border-border/60 hover:bg-white/[0.02] transition-colors"
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
                      <span className="font-semibold text-zinc-100">{displayName(row.binance_symbol)}</span>
                      <span className="text-[10px] uppercase tracking-[0.14em] text-muted">{row.name}</span>
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
    </div>
  );
}
