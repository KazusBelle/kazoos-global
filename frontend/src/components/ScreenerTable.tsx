import { useEffect, useMemo, useRef, useState } from "react";
import type { CallTag, DashboardRow, Snapshot } from "../lib/api";

type SortKey = "coin" | "fibo" | "trend" | "ote" | null;
type SortDir = "asc" | "desc";

type Density = "cozy" | "compact";

type Props = {
  title: string;
  rows: DashboardRow[];
  pick: (row: DashboardRow) => Snapshot | null;
  onRemove: (symbol: string) => void;
  onTogglePin: (symbol: string) => void;
  onMovePin: (symbol: string, direction: "up" | "down") => void;
  onSetCall: (symbol: string, tag: CallTag, note: string | null) => void;
  density: Density;
  expanded: boolean;
  onCoinClick?: (symbol: string, orderedSymbols: string[], source: "local" | "global") => void;
  fontSize?: number;
  storageKey?: string;
};

// Ranks used for categorical sort (Fibo / Trend). Higher rank == "stronger".
const FIBO_RANK: Record<string, number> = {
  premium: 1,
  discount: 2,
  ote: 3,
  abnormal: 0,
  none: 0,
};
const TREND_RANK: Record<string, number> = {
  down: 1,
  none: 2,
  up: 3,
};

const COLLAPSED_HEIGHT = 470;
const EXPANDED_HEIGHT = 760;

type CallOption = { tag: Exclude<CallTag, null>; label: string; color: string };

// Order matches user spec: Vanga (purple), Voldemar (yellow), Makiavelli
// (blue), Я (accent). Rendered in this sequence left→right.
const CALL_OPTIONS: CallOption[] = [
  { tag: "vanga", label: "Vanga", color: "#a855f7" },
  { tag: "voldemar", label: "Voldemar", color: "#facc15" },
  { tag: "makiavelli", label: "Makiavelli", color: "#38bdf8" },
  { tag: "me", label: "Я", color: "#E3822D" },
];

function displayName(symbol: string) {
  // Binance futures meme coins are listed as 1000XXXUSDT. In the UI we
  // show the canonical ticker (PEPE, SHIB, BONK, FLOKI). Works generically
  // for any `1000*USDT` symbol.
  let s = symbol.replace(/USDT$/, "");
  if (s.startsWith("1000")) s = s.slice(4);
  return s;
}

function compareSymbols(a: DashboardRow, b: DashboardRow, dir: SortDir = "asc") {
  const mult = dir === "asc" ? 1 : -1;
  const byDisplay = displayName(a.symbol).localeCompare(displayName(b.symbol), "en", {
    numeric: false,
    sensitivity: "base",
  });
  if (byDisplay !== 0) return byDisplay * mult;
  return a.symbol.localeCompare(b.symbol, "en") * mult;
}

function zoneClass(zone?: string) {
  switch (zone) {
    case "discount":
      return "text-discount";
    case "premium":
      return "kz-premium";
    case "ote":
      return "text-discount";
    case "abnormal":
      return "text-zinc-400";
    default:
      return "text-muted";
  }
}

function zoneLabel(zone?: string) {
  if (zone === "abnormal") return "ABNORMAL";
  return zone ?? "—";
}

function trendTintClass(trend?: string) {
  if (trend === "up") return "text-discount";
  if (trend === "down") return "kz-premium";
  return "text-muted";
}

function Sparkline({ closes, trend }: { closes: number[]; trend?: string }) {
  const width = 84;
  const height = 18;
  const path = useMemo(() => {
    if (!closes || closes.length < 2) return null;
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const range = max - min || 1;
    const step = width / (closes.length - 1);
    return closes
      .map((v, i) => {
        const x = (i * step).toFixed(2);
        const y = (height - ((v - min) / range) * height).toFixed(2);
        return `${i === 0 ? "M" : "L"}${x},${y}`;
      })
      .join(" ");
  }, [closes]);

  if (!path) return <span className="text-muted text-xs">—</span>;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={trendTintClass(trend)}
    >
      <path
        d={path}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
        strokeLinecap="round"
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

function SortHeader({
  label,
  activeKey,
  dir,
  k,
  onClick,
}: {
  label: string;
  activeKey: SortKey;
  dir: SortDir;
  k: SortKey;
  onClick: (k: SortKey) => void;
}) {
  const active = activeKey === k;
  const arrow = !active ? "" : dir === "asc" ? " ▲" : " ▼";
  return (
    <th
      className={`px-2 py-3 font-normal select-none cursor-pointer hover:text-zinc-200 text-left ${
        active ? "text-zinc-100" : ""
      }`}
      onClick={() => onClick(k)}
    >
      {label}
      <span className="text-accent">{arrow}</span>
    </th>
  );
}

function CallCell({
  row,
  onSetCall,
}: {
  row: DashboardRow;
  onSetCall: (symbol: string, tag: CallTag, note: string | null) => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(row.call_note ?? "");

  useEffect(() => {
    setDraft(row.call_note ?? "");
  }, [row.call_note, row.symbol]);

  useEffect(() => {
    if (!editing) return;
    inputRef.current?.focus();
  }, [editing]);

  useEffect(() => {
    if (!editing) return;
    function handlePointerDown(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) {
        setEditing(false);
        setDraft(row.call_note ?? "");
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [editing, row.call_note]);

  function handleClick(tag: CallTag) {
    if (row.call_tag === tag) {
      onSetCall(row.symbol, null, null);
      setEditing(false);
      setDraft("");
      return;
    }
    onSetCall(row.symbol, tag, row.call_note ?? null);
    setDraft(row.call_note ?? "");
    setEditing(true);
  }

  function saveNote() {
    onSetCall(row.symbol, row.call_tag, draft.trim() ? draft.trim() : null);
    setEditing(false);
  }

  return (
    <div ref={rootRef} className="relative flex justify-end">
      <div className="flex items-center gap-0.5">
        {CALL_OPTIONS.map((opt) => {
          const active = row.call_tag === opt.tag;
          const tooltip = active && row.call_note
            ? `${opt.label}: ${row.call_note}`
            : opt.label;
          return (
            <div key={opt.tag} className="relative group">
              <button
                type="button"
                onClick={() => handleClick(opt.tag)}
                aria-label={opt.label}
                className="h-2 w-2 rounded-full border transition-transform hover:scale-110 focus:outline-none"
                style={{
                  backgroundColor: active ? opt.color : "transparent",
                  borderColor: active ? opt.color : "#3a3d45",
                }}
              />
              <div className="pointer-events-none absolute right-0 top-full z-10 mt-1 whitespace-nowrap rounded-md border border-border bg-bg/95 px-2 py-1 text-[10px] uppercase tracking-[0.16em] text-zinc-200 opacity-0 shadow-lg transition-opacity duration-75 group-hover:opacity-100 group-focus-within:opacity-100">
                {tooltip}
              </div>
            </div>
          );
        })}
      </div>
      {editing && row.call_tag && (
        <div className="absolute right-0 top-full z-10 mt-2 min-w-[180px] rounded-xl border border-border bg-bg/95 p-2 shadow-xl backdrop-blur">
          <div className="space-y-2">
            <input
              ref={inputRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Comment"
              className="w-full rounded-lg border border-border bg-panel px-2 py-1.5 text-xs text-zinc-100 focus:border-accent focus:outline-none"
            />
            <div className="flex justify-end gap-2 text-[10px] uppercase tracking-[0.16em]">
              <button
                type="button"
                onClick={() => {
                  setEditing(false);
                  setDraft(row.call_note ?? "");
                }}
                className="rounded-md border border-border px-2 py-1 text-muted hover:text-zinc-100"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={saveNote}
                className="rounded-md border border-accent px-2 py-1 text-accent hover:bg-accent hover:text-black"
              >
                Save
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export function ScreenerTable({
  title,
  rows,
  pick,
  onRemove,
  onTogglePin,
  onMovePin,
  onSetCall,
  density,
  expanded,
  onCoinClick,
  fontSize = 13,
  storageKey = title,
}: Props) {
  const prevPricesRef = useRef<Record<string, number | null>>({});
  const [flashBySymbol, setFlashBySymbol] = useState<Record<string, true>>({});
  const sortStorageKey = `kazus_sort_${storageKey}`;
  const [sortKey, setSortKey] = useState<SortKey>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(sortStorageKey) ?? "{}");
      return saved.key ?? "ote";
    } catch {
      return "ote";
    }
  });
  const [sortDir, setSortDir] = useState<SortDir>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(sortStorageKey) ?? "{}");
      return saved.dir ?? "desc";
    } catch {
      return "desc";
    }
  });
  function onSort(k: SortKey) {
    if (k === sortKey) {
      const nextDir = sortDir === "asc" ? "desc" : "asc";
      setSortDir(nextDir);
      localStorage.setItem(sortStorageKey, JSON.stringify({ key: sortKey, dir: nextDir }));
    } else {
      setSortKey(k);
      const nextDir = k === "ote" ? "desc" : "asc";
      setSortDir(nextDir);
      localStorage.setItem(sortStorageKey, JSON.stringify({ key: k, dir: nextDir }));
    }
  }

  // 1) Split pinned / unpinned so pinned always stays on top in their own order.
  // 2) Apply the column sort to the unpinned slice only.
  const sortedRows = useMemo(() => {
    const pinned = rows
      .filter((r) => r.pinned_order != null)
      .sort((a, b) => (a.pinned_order! - b.pinned_order!));
    const unpinned = rows.filter((r) => r.pinned_order == null);

    const dirMult = sortDir === "asc" ? 1 : -1;
    const unpinnedSorted = [...unpinned];
    if (sortKey) {
      unpinnedSorted.sort((a, b) => {
        const sa = pick(a);
        const sb = pick(b);
        switch (sortKey) {
          case "coin":
            return compareSymbols(a, b, sortDir);
          case "fibo": {
            const ra = FIBO_RANK[sa?.zone ?? "none"] ?? 0;
            const rb = FIBO_RANK[sb?.zone ?? "none"] ?? 0;
            if (ra === rb) {
              return compareSymbols(a, b);
            }
            return (ra - rb) * dirMult;
          }
          case "trend": {
            const ra = TREND_RANK[sa?.trend ?? "none"] ?? 0;
            const rb = TREND_RANK[sb?.trend ?? "none"] ?? 0;
            if (ra === rb) {
              return compareSymbols(a, b);
            }
            return (ra - rb) * dirMult;
          }
          case "ote": {
            // downtrend (non-long) ranks below all real retracement values
            const ra = sa?.trend === "up" ? (sa?.retracement ?? -1) : -2;
            const rb = sb?.trend === "up" ? (sb?.retracement ?? -1) : -2;
            return (ra - rb) * dirMult;
          }
          default:
            return 0;
        }
      });
    } else {
      unpinnedSorted.sort((a, b) => compareSymbols(a, b));
    }
    return [...pinned, ...unpinnedSorted];
  }, [rows, sortKey, sortDir, pick]);

  const visibleRows = sortedRows;

  useEffect(() => {
    const changed: string[] = [];
    const nextMap: Record<string, number | null> = { ...prevPricesRef.current };
    for (const row of rows) {
      const prev = prevPricesRef.current[row.symbol];
      const cur = row.price ?? null;
      if (prev != null && cur != null && prev !== cur) {
        changed.push(row.symbol);
      }
      nextMap[row.symbol] = cur;
    }
    prevPricesRef.current = nextMap;
    if (changed.length === 0) return;

    setFlashBySymbol((state) => {
      const next = { ...state };
      for (const sym of changed) next[sym] = true;
      return next;
    });

    const timer = window.setTimeout(() => {
      setFlashBySymbol((state) => {
        const next = { ...state };
        for (const sym of changed) delete next[sym];
        return next;
      });
    }, 850);

    return () => window.clearTimeout(timer);
  }, [rows]);

  // Row height control ("cozy" by default, compact halves the vertical padding).
  const rowPad = density === "compact" ? "py-1.5" : "py-3";

  const pinnedCount = rows.filter((r) => r.pinned_order != null).length;
  const tableHeight = expanded ? EXPANDED_HEIGHT : COLLAPSED_HEIGHT;

  return (
    <div className="bg-panel border border-border rounded-2xl flex flex-col min-h-0 kz-pop-in">
      <div className="px-5 pt-4 pb-2 shrink-0">
        <div className="text-accent text-base font-medium tracking-[0.32em] uppercase">
          {title}
        </div>
      </div>

      <div
        className="overflow-x-auto overflow-y-auto min-h-0 kz-table-scroll"
        style={{
          height: tableHeight,
          maxHeight: tableHeight,
          overscrollBehaviorY: "contain",
          overscrollBehaviorX: "contain",
        }}
      >
        <table
          className="w-full font-mono"
          style={{ tableLayout: "fixed", fontSize: `${fontSize}px` }}
        >
          <colgroup>
            <col style={{ width: "56px" }} />
            <col style={{ width: "90px" }} />
            <col style={{ width: "82px" }} />
            <col style={{ width: "100px" }} />
            <col style={{ width: "100px" }} />
            <col style={{ width: "72px" }} />
            <col style={{ width: "54px" }} />
            <col style={{ width: "96px" }} />
            <col style={{ width: "32px" }} />
          </colgroup>
          <thead className="sticky top-0 bg-panel/95 backdrop-blur z-20">
            <tr className="text-[10px] uppercase tracking-[0.2em] text-muted border-b border-border">
              <th className="px-2 py-3 font-normal text-left">Pin</th>
              <SortHeader label="Coin" activeKey={sortKey} dir={sortDir} k="coin" onClick={onSort} />
              <th className="px-2 py-3 font-normal text-left">Price</th>
              <SortHeader label="Fibo" activeKey={sortKey} dir={sortDir} k="fibo" onClick={onSort} />
              <SortHeader label="Trend" activeKey={sortKey} dir={sortDir} k="trend" onClick={onSort} />
              <SortHeader label="OTE %" activeKey={sortKey} dir={sortDir} k="ote" onClick={onSort} />
              <th className="px-2 py-3 font-normal text-left">Setup</th>
              <th className="px-2 py-3 font-normal text-right">Call</th>
              <th className="px-2 py-3" />
            </tr>
          </thead>
          <tbody>
            {visibleRows.length === 0 && (
              <tr>
                <td colSpan={9} className="text-center text-muted py-8">
                  No coins. Add one below.
                </td>
              </tr>
            )}
            {visibleRows.map((row) => {
              const s = pick(row);
              const isPinned = row.pinned_order != null;
              return (
                <tr
                  key={row.symbol + title}
                  className={`border-t border-border/60 hover:bg-white/[0.02] ${
                    isPinned ? "bg-white/[0.015]" : ""
                  } ${flashBySymbol[row.symbol] ? "kz-row-flash" : ""}`}
                >
                  <td className={`px-2 ${rowPad}`}>
                    <div className="flex items-center gap-0.5">
                      <button
                        onClick={() => onTogglePin(row.symbol)}
                        className={`group h-5 w-5 inline-flex items-center justify-center leading-none transition-colors ${
                          isPinned ? "text-accent" : "text-transparent hover:text-accent"
                        }`}
                        title={isPinned ? "Unpin" : "Pin to top"}
                        aria-label={isPinned ? "Unpin coin" : "Pin coin"}
                      >
                        <PinIcon pinned={isPinned} />
                      </button>
                      {isPinned && pinnedCount > 1 && (
                        <div className="flex flex-col leading-[0.6]">
                          <button
                            onClick={() => onMovePin(row.symbol, "up")}
                            className="text-[9px] text-muted hover:text-accent disabled:opacity-30"
                            disabled={row.pinned_order === 0}
                            title="Move up"
                            aria-label="Move up"
                          >
                            ▲
                          </button>
                          <button
                            onClick={() => onMovePin(row.symbol, "down")}
                            className="text-[9px] text-muted hover:text-accent disabled:opacity-30"
                            disabled={row.pinned_order === pinnedCount - 1}
                            title="Move down"
                            aria-label="Move down"
                          >
                            ▼
                          </button>
                        </div>
                      )}
                    </div>
                  </td>
                  <td className={`px-2 ${rowPad} font-semibold truncate`}>
                    {onCoinClick ? (
                      <button
                        type="button"
                        onClick={() =>
                          onCoinClick(
                            row.symbol,
                            sortedRows.map((r) => r.symbol),
                            storageKey === "local" ? "local" : "global",
                          )
                        }
                        className="hover:text-accent transition-colors text-left w-full truncate"
                        title="View chart"
                      >
                        {displayName(row.symbol)}
                      </button>
                    ) : (
                      displayName(row.symbol)
                    )}
                  </td>
                  <td className={`px-2 ${rowPad} text-zinc-300 truncate`}>
                    {formatPrice(row.price)}
                  </td>
                  <td
                    className={`px-2 ${rowPad} uppercase tracking-widest ${
                      s?.trend === "up" ? zoneClass(s?.zone) : "text-muted"
                    }`}
                  >
                    {s?.trend === "up" ? zoneLabel(s?.zone) : "downtrend"}
                  </td>
                  <td className={`px-2 ${rowPad}`}>
                    <Sparkline closes={s?.closes ?? []} trend={s?.trend} />
                  </td>
                  <td className={`px-2 ${rowPad} text-muted`}>
                    {s?.trend === "up" && s?.retracement != null
                      ? `${(s.retracement * 100).toFixed(1)}%`
                      : <span className="text-muted">downtrend</span>}
                  </td>
                  <td className={`px-2 ${rowPad} uppercase text-muted`}>
                    no
                  </td>
                  <td className={`px-2 ${rowPad}`}>
                    <CallCell row={row} onSetCall={onSetCall} />
                  </td>
                  <td className={`px-2 ${rowPad} text-right`}>
                    <button
                      onClick={() => onRemove(row.symbol)}
                      className="text-muted kz-premium-hover text-xs"
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
