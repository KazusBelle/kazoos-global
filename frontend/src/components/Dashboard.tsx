import { useEffect, useState } from "react";
import {
  addCoin,
  getDashboard,
  removeCoin,
  setToken,
  type DashboardResponse,
} from "../lib/api";
import { ScreenerTable } from "./ScreenerTable";

const POLL_MS = 15_000;

export function Dashboard({ onLogout }: { onLogout: () => void }) {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

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

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    const sym = input.trim().toUpperCase();
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

  function logout() {
    setToken(null);
    onLogout();
  }

  const rows = data?.rows ?? [];
  const totals = data?.totals ?? { total: 0, ote: 0, discount: 0, premium: 0 };
  const lastRefresh = data?.last_refresh_at
    ? new Date(data.last_refresh_at).toLocaleString()
    : "—";

  return (
    <div className="min-h-screen flex">
      {/* Sidebar */}
      <aside className="w-14 shrink-0 border-r border-border bg-panel flex flex-col items-center py-4 gap-3">
        <div className="w-9 h-9 rounded-full border-2 border-accent flex items-center justify-center text-accent font-bold">
          K
        </div>
        <div className="mt-auto w-full flex flex-col items-center gap-3 text-muted">
          <button
            onClick={logout}
            className="text-[10px] uppercase tracking-widest hover:text-premium"
            title="Log out"
          >
            exit
          </button>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 p-6 space-y-6">
        {error && (
          <div className="bg-premium/10 border border-premium/40 rounded-lg px-4 py-2 text-premium text-sm">
            {error}
          </div>
        )}

        <div className="grid md:grid-cols-2 gap-6">
          <ScreenerTable
            title="GLOBAL"
            subtitle="higher timeframe perspective — D1"
            rows={rows}
            pick={(r) => r.global}
            onRemove={handleRemove}
          />
          <ScreenerTable
            title="LOCAL"
            subtitle="lower timeframe perspective — H1"
            rows={rows}
            pick={(r) => r.local}
            onRemove={handleRemove}
          />
        </div>

        {/* Totals + add */}
        <div className="grid md:grid-cols-3 gap-6">
          <div className="bg-panel border border-border rounded-2xl p-5">
            <div className="flex gap-6">
              <Counter label="Total" value={totals.total} />
              <Counter label="OTE" value={totals.ote} color="text-ote" />
              <Counter label="Dic" value={totals.discount} color="text-discount" />
              <Counter label="Pre" value={totals.premium} color="text-premium" />
            </div>
            <div className="text-[10px] uppercase tracking-widest text-muted mt-4">
              Last refresh: <span className="text-zinc-300">{lastRefresh}</span>
            </div>
            {data?.last_error && (
              <div className="text-[10px] text-premium/80 mt-1">{data.last_error}</div>
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
              <input
                className="w-full bg-bg border border-border rounded-lg px-3 py-2 font-mono uppercase focus:outline-none focus:border-accent"
                placeholder="BTCUSDT"
                value={input}
                onChange={(e) => setInput(e.target.value)}
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
      </main>
    </div>
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
      <div className={`text-3xl font-bold ${color}`}>
        {value.toString().padStart(2, "0")}
      </div>
      <div className="text-[10px] uppercase tracking-widest text-muted">{label}</div>
    </div>
  );
}
