import type { DashboardRow, Snapshot } from "../lib/api";

type Props = {
  title: string;
  subtitle: string;
  rows: DashboardRow[];
  pick: (row: DashboardRow) => Snapshot | null;
  onRemove: (symbol: string) => void;
};

function zoneClass(zone?: string) {
  switch (zone) {
    case "discount":
      return "text-discount";
    case "premium":
      return "text-premium";
    case "equilibrium":
      return "text-eq";
    default:
      return "text-muted";
  }
}

function setupClass(setup?: string) {
  return setup === "yes" ? "text-ote font-semibold" : "text-muted";
}

function sparklineTint(trend?: string) {
  if (trend === "up") return "text-discount";
  if (trend === "down") return "text-premium";
  return "text-muted";
}

function MiniSparkline({ trend }: { trend?: string }) {
  // deterministic decorative sparkline; real chart is on TradingView — UI is a screener summary
  const points =
    trend === "up"
      ? "0,10 8,8 16,7 24,5 32,4 40,3 48,4 56,3 64,2 72,1"
      : trend === "down"
      ? "0,2 8,3 16,4 24,3 32,5 40,6 48,5 56,7 64,8 72,10"
      : "0,6 8,5 16,7 24,5 32,6 40,4 48,6 56,5 64,7 72,6";
  return (
    <svg width="80" height="14" viewBox="0 0 80 14" className={sparklineTint(trend)}>
      <polyline
        fill="none"
        strokeWidth="1.2"
        stroke="currentColor"
        points={points}
      />
    </svg>
  );
}

function formatPrice(p: number | null | undefined) {
  if (p == null) return "—";
  if (p >= 100) return p.toFixed(2);
  if (p >= 1) return p.toFixed(3);
  return p.toPrecision(4);
}

export function ScreenerTable({ title, subtitle, rows, pick, onRemove }: Props) {
  return (
    <div className="bg-panel border border-border rounded-2xl overflow-hidden">
      <div className="px-5 pt-4 pb-2 flex items-baseline justify-between">
        <div>
          <div className="text-accent text-xl font-bold tracking-[0.3em]">
            {title}
          </div>
          <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
            {subtitle}
          </div>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.2em] text-muted">
              <th className="text-left px-5 py-3 font-normal">Coin</th>
              <th className="text-left px-2 py-3 font-normal">Price</th>
              <th className="text-left px-2 py-3 font-normal">Fibo / Zone</th>
              <th className="text-left px-2 py-3 font-normal">Trend</th>
              <th className="text-left px-2 py-3 font-normal">OTE</th>
              <th className="text-left px-2 py-3 font-normal">Setup</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="text-center text-muted py-8">
                  No coins. Add one below.
                </td>
              </tr>
            )}
            {rows.map((row) => {
              const s = pick(row);
              return (
                <tr
                  key={row.symbol + title}
                  className="border-t border-border/60 hover:bg-white/[0.02]"
                >
                  <td className="px-5 py-3 font-semibold">{row.symbol.replace("USDT", "")}</td>
                  <td className="px-2 py-3 text-zinc-300">
                    {formatPrice(row.price)}
                  </td>
                  <td className={`px-2 py-3 uppercase tracking-widest ${zoneClass(s?.zone)}`}>
                    {s?.zone ?? "—"}
                  </td>
                  <td className="px-2 py-3"><MiniSparkline trend={s?.trend} /></td>
                  <td className="px-2 py-3 text-muted">
                    {s?.retracement != null ? (s.retracement * 100).toFixed(1) + "%" : "—"}
                  </td>
                  <td className={`px-2 py-3 uppercase ${setupClass(s?.setup)}`}>
                    {s?.setup ?? "no"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => onRemove(row.symbol)}
                      className="text-muted hover:text-premium text-xs"
                      title="Remove"
                    >
                      ✕
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
