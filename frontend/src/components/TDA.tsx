import { useEffect, useMemo, useRef, useState } from "react";
import { getTdaState, patchTdaState } from "../lib/api";
import { normalizeSymbol, SymbolSuggestInput } from "./SymbolSuggestInput";

const DEFAULT_COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"] as const;
const COLS = ["D nPOC", "D VA", "STACKED", "Single Print", "W nPOC", "W VA"] as const;
const DATA_KEY = "kazus_tda_v1";
const COINS_KEY = "kazus_tda_coins_v1";
const PHOTOS_KEY = "kazus_tda_photos_v1";
const BACKUPS_KEY = "kazus_tda_backups_v1";
const MAX_BACKUPS = 25;

type TDAData = Record<string, Record<string, string>>;
type TDAPhotos = Record<string, string>;
type TDABackup = {
  ts: number;
  coins: string[];
  data: TDAData;
  photos: TDAPhotos;
};

function loadData(): TDAData {
  try {
    const raw = JSON.parse(localStorage.getItem(DATA_KEY) ?? "{}");
    return Object.fromEntries(
      Object.entries(raw).map(([symbol, values]) => [
        normalizeSymbol(symbol),
        values,
      ])
    ) as TDAData;
  } catch {
    return {};
  }
}

function loadCoins(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(COINS_KEY) ?? "[]");
    if (Array.isArray(parsed) && parsed.length > 0) {
      return parsed.map((symbol) => String(symbol).toUpperCase());
    }
  } catch {
    // fall through to defaults
  }
  return [...DEFAULT_COINS];
}

function loadPhotos(): TDAPhotos {
  try {
    const parsed = JSON.parse(localStorage.getItem(PHOTOS_KEY) ?? "{}");
    if (!parsed || typeof parsed !== "object") return {};
    const out: TDAPhotos = {};
    for (const [symbol, value] of Object.entries(parsed as Record<string, unknown>)) {
      const sym = normalizeSymbol(symbol);
      const photo = String(value ?? "").trim();
      if (!sym || !photo) continue;
      out[sym] = photo;
    }
    return out;
  } catch {
    return {};
  }
}

function loadBackups(): TDABackup[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(BACKUPS_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map((item) => {
        if (!item || typeof item !== "object") return null;
        const ts = Number((item as any).ts);
        const coins = normalizeCoins(Array.isArray((item as any).coins) ? (item as any).coins : []);
        const data = normalizeData((item as any).data ?? {});
        const photos = normalizePhotos((item as any).photos ?? {});
        if (!Number.isFinite(ts) || ts <= 0) return null;
        return { ts, coins, data, photos } as TDABackup;
      })
      .filter((v): v is TDABackup => Boolean(v))
      .slice(0, MAX_BACKUPS);
  } catch {
    return [];
  }
}

function normalizeCoins(input: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of input) {
    const symbol = normalizeSymbol(item);
    if (!symbol || seen.has(symbol)) continue;
    seen.add(symbol);
    out.push(symbol);
  }
  return out;
}

function normalizeData(input: unknown): TDAData {
  if (!input || typeof input !== "object") return {};
  const raw = input as Record<string, unknown>;
  const out: TDAData = {};
  for (const [symbol, cols] of Object.entries(raw)) {
    const sym = normalizeSymbol(symbol);
    if (!sym || !cols || typeof cols !== "object") continue;
    const colsOut: Record<string, string> = {};
    for (const [col, value] of Object.entries(cols as Record<string, unknown>)) {
      colsOut[String(col)] = String(value ?? "");
    }
    out[sym] = colsOut;
  }
  return out;
}

function normalizePhotos(input: unknown): TDAPhotos {
  if (!input || typeof input !== "object") return {};
  const raw = input as Record<string, unknown>;
  const out: TDAPhotos = {};
  for (const [symbol, value] of Object.entries(raw)) {
    const sym = normalizeSymbol(symbol);
    const photo = String(value ?? "").trim();
    if (!sym || !photo) continue;
    out[sym] = photo;
  }
  return out;
}

function displayName(symbol: string) {
  let s = symbol.replace(/USDT$/, "");
  if (s.startsWith("1000")) s = s.slice(4);
  return s;
}

export function TDA() {
  const [data, setData] = useState<TDAData>(loadData);
  const [coins, setCoins] = useState<string[]>(loadCoins);
  const [photos, setPhotos] = useState<TDAPhotos>(loadPhotos);
  const [backups, setBackups] = useState<TDABackup[]>(loadBackups);
  const [input, setInput] = useState("");
  const [dragging, setDragging] = useState<string | null>(null);
  const [previewCoin, setPreviewCoin] = useState<string | null>(null);
  // Coin currently armed to receive a clipboard image paste. When non-null,
  // a global paste listener routes Ctrl/Cmd+V images to this coin.
  const [pasteArmedCoin, setPasteArmedCoin] = useState<string | null>(null);

  const hydratedRef = useRef(false);
  const saveTimerRef = useRef<number | null>(null);
  const backupTimerRef = useRef<number | null>(null);
  const pendingLocalChangesRef = useRef(false);
  const lastLocalMutationRef = useRef<number>(0);
  const coinsRef = useRef<string[]>(coins);
  const dataRef = useRef<TDAData>(data);
  const photosRef = useRef<TDAPhotos>(photos);

  const exclude = useMemo(() => coins.map((coin) => coin.toUpperCase()), [coins]);
  const fileInputRefs = useRef<Record<string, HTMLInputElement | null>>({});

  // Listen for native paste while a coin is armed. The listener fires when
  // the user presses Ctrl/Cmd+V after clicking the "+" button — they don't
  // have to focus a specific element first.
  useEffect(() => {
    if (!pasteArmedCoin) return;
    const handlePaste = (event: ClipboardEvent) => {
      const items = event.clipboardData?.items;
      if (!items) return;
      for (const item of items) {
        if (item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) {
            event.preventDefault();
            setPhotoForCoin(pasteArmedCoin, file);
            setPasteArmedCoin(null);
            return;
          }
        }
      }
    };
    document.addEventListener("paste", handlePaste);
    // Auto-disarm after a short window so paste doesn't capture unrelated
    // copies later in the session.
    const timer = window.setTimeout(() => setPasteArmedCoin(null), 30_000);
    return () => {
      document.removeEventListener("paste", handlePaste);
      window.clearTimeout(timer);
    };
  }, [pasteArmedCoin]);

  async function handleAttachClick(coin: string) {
    // Try clipboard image first — when it succeeds the user gets a one-click
    // paste flow without ever opening the file dialog. If clipboard is empty
    // or permission is denied, fall back to the native file picker and arm
    // the row so a subsequent Ctrl/Cmd+V is also routed to this coin.
    const pasted = await pasteImageFromClipboard(coin);
    if (pasted) return;
    setPasteArmedCoin(coin);
    fileInputRefs.current[coin]?.click();
  }

  async function pullFromServer() {
    try {
      const remote = await getTdaState();
      const remoteCoins = normalizeCoins(remote?.coins ?? []);
      const remoteData = normalizeData(remote?.data ?? {});
      const remotePhotos = normalizePhotos(remote?.photos ?? {});

      // Only skip applying server state if server is completely empty but local has data.
      // In all other cases, server is the source of truth.
      const serverEmpty = remoteCoins.length === 0 && Object.keys(remoteData).length === 0;
      const localHasData = coinsRef.current.length > 0 || Object.keys(dataRef.current).length > 0;
      if (serverEmpty && localHasData) return;

      // Apply all three fields atomically so state is never partially-synced.
      setCoins(remoteCoins);
      setData(remoteData);
      setPhotos(remotePhotos);
      localStorage.setItem(COINS_KEY, JSON.stringify(remoteCoins));
      localStorage.setItem(DATA_KEY, JSON.stringify(remoteData));
      localStorage.setItem(PHOTOS_KEY, JSON.stringify(remotePhotos));
    } catch {
      // best-effort pull
    }
  }

  function persistBackups(next: TDABackup[]) {
    localStorage.setItem(BACKUPS_KEY, JSON.stringify(next.slice(0, MAX_BACKUPS)));
  }

  function pushBackupSnapshot(nextCoins: string[], nextData: TDAData, nextPhotos: TDAPhotos) {
    const snapshot: TDABackup = {
      ts: Date.now(),
      coins: normalizeCoins(nextCoins),
      data: normalizeData(nextData),
      photos: normalizePhotos(nextPhotos),
    };
    setBackups((prev) => {
      const prevTop = prev[0];
      if (
        prevTop &&
        JSON.stringify(prevTop.coins) === JSON.stringify(snapshot.coins) &&
        JSON.stringify(prevTop.data) === JSON.stringify(snapshot.data) &&
        JSON.stringify(prevTop.photos) === JSON.stringify(snapshot.photos)
      ) {
        return prev;
      }
      const next = [snapshot, ...prev].slice(0, MAX_BACKUPS);
      persistBackups(next);
      return next;
    });
  }

  function restoreLatestBackup() {
    const latest = backups[0];
    if (!latest) return;
    pendingLocalChangesRef.current = true;
    lastLocalMutationRef.current = Date.now();
    setCoins(latest.coins);
    setData(latest.data);
    setPhotos(latest.photos);
    localStorage.setItem(COINS_KEY, JSON.stringify(latest.coins));
    localStorage.setItem(DATA_KEY, JSON.stringify(latest.data));
    localStorage.setItem(PHOTOS_KEY, JSON.stringify(latest.photos));
  }

  function saveCoins(next: string[]) {
    pendingLocalChangesRef.current = true;
    lastLocalMutationRef.current = Date.now();
    setCoins(next);
    localStorage.setItem(COINS_KEY, JSON.stringify(next));
  }

  function addCoin(symbolValue = input) {
    const symbol = normalizeSymbol(symbolValue);
    if (!symbol || coins.includes(symbol)) return;
    saveCoins([...coins, symbol]);
    setInput("");
  }

  function removeCoin(symbol: string) {
    pendingLocalChangesRef.current = true;
    lastLocalMutationRef.current = Date.now();
    saveCoins(coins.filter((coin) => coin !== symbol));
    setData((prev) => {
      const next = { ...prev };
      delete next[symbol];
      return next;
    });
    setPhotos((prev) => {
      if (!prev[symbol]) return prev;
      const next = { ...prev };
      delete next[symbol];
      return next;
    });
  }

  function set(coin: string, col: string, value: string) {
    pendingLocalChangesRef.current = true;
    lastLocalMutationRef.current = Date.now();
    setData((prev) => {
      const next = { ...prev, [coin]: { ...(prev[coin] ?? {}), [col]: value } };
      localStorage.setItem(DATA_KEY, JSON.stringify(next));
      return next;
    });
  }

  function moveCoin(symbol: string, overSymbol: string) {
    if (symbol === overSymbol) return;
    pendingLocalChangesRef.current = true;
    lastLocalMutationRef.current = Date.now();
    setCoins((prev) => {
      const from = prev.indexOf(symbol);
      const to = prev.indexOf(overSymbol);
      if (from < 0 || to < 0 || from === to) return prev;
      const next = [...prev];
      const [item] = next.splice(from, 1);
      next.splice(to, 0, item);
      localStorage.setItem(COINS_KEY, JSON.stringify(next));
      return next;
    });
  }

  function setPhotoForCoin(coin: string, file: File | null) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const image = typeof reader.result === "string" ? reader.result : "";
      if (!image) return;
      pendingLocalChangesRef.current = true;
      lastLocalMutationRef.current = Date.now();
      setPhotos((prev) => {
        const next = { ...prev, [coin]: image };
        localStorage.setItem(PHOTOS_KEY, JSON.stringify(next));
        return next;
      });
    };
    reader.readAsDataURL(file);
  }

  async function pasteImageFromClipboard(coin: string): Promise<boolean> {
    // Modern Clipboard API path. Returns false if no image was found or
    // permissions blocked, so the caller can decide whether to fall back.
    try {
      if (!navigator.clipboard || typeof (navigator.clipboard as any).read !== "function") {
        return false;
      }
      const items = await (navigator.clipboard as any).read();
      for (const item of items) {
        for (const type of item.types as string[]) {
          if (type.startsWith("image/")) {
            const blob: Blob = await item.getType(type);
            const ext = type.split("/")[1] || "png";
            const file = new File([blob], `${coin.toLowerCase()}.${ext}`, { type });
            setPhotoForCoin(coin, file);
            return true;
          }
        }
      }
    } catch {
      // permission denied / unsupported browser — silently fall through
    }
    return false;
  }

  function removePhotoForCoin(coin: string) {
    pendingLocalChangesRef.current = true;
    lastLocalMutationRef.current = Date.now();
    setPhotos((prev) => {
      if (!prev[coin]) return prev;
      const next = { ...prev };
      delete next[coin];
      localStorage.setItem(PHOTOS_KEY, JSON.stringify(next));
      return next;
    });
    if (previewCoin === coin) setPreviewCoin(null);
  }

  useEffect(() => {
    if (!dragging) return;

    function handlePointerMove(event: PointerEvent) {
      const target = document
        .elementFromPoint(event.clientX, event.clientY)
        ?.closest("[data-tda-symbol]");
      const overSymbol = target?.getAttribute("data-tda-symbol");
      if (dragging && overSymbol) moveCoin(dragging, overSymbol);
    }

    function stopDragging() {
      setDragging(null);
    }

    document.body.style.cursor = "grabbing";
    document.body.style.userSelect = "none";
    document.addEventListener("pointermove", handlePointerMove);
    document.addEventListener("pointerup", stopDragging, { once: true });
    document.addEventListener("pointercancel", stopDragging, { once: true });
    return () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.removeEventListener("pointermove", handlePointerMove);
      document.removeEventListener("pointerup", stopDragging);
      document.removeEventListener("pointercancel", stopDragging);
    };
  }, [dragging]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      await pullFromServer();
      if (
        !cancelled &&
        coinsRef.current.length === 0 &&
        Object.keys(dataRef.current).length === 0 &&
        backups.length > 0
      ) {
        restoreLatestBackup();
      }
      if (!cancelled) hydratedRef.current = true;
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    localStorage.setItem(COINS_KEY, JSON.stringify(coins));
    coinsRef.current = coins;
  }, [coins]);

  useEffect(() => {
    localStorage.setItem(DATA_KEY, JSON.stringify(data));
    dataRef.current = data;
  }, [data]);

  useEffect(() => {
    localStorage.setItem(PHOTOS_KEY, JSON.stringify(photos));
    photosRef.current = photos;
  }, [photos]);

  useEffect(() => {
    persistBackups(backups);
  }, [backups]);

  useEffect(() => {
    if (!hydratedRef.current) return;
    if (!pendingLocalChangesRef.current) return;

    if (saveTimerRef.current != null) {
      window.clearTimeout(saveTimerRef.current);
    }
    saveTimerRef.current = window.setTimeout(() => {
      void patchTdaState({ coins, data, photos })
        .then(() => {
          pendingLocalChangesRef.current = false;
        })
        .catch(() => {
          // keep pendingLocalChangesRef true so polling loop retries
        });
    }, 150);

    return () => {
      if (saveTimerRef.current != null) {
        window.clearTimeout(saveTimerRef.current);
        saveTimerRef.current = null;
      }
    };
  }, [coins, data, photos]);

  useEffect(() => {
    const id = window.setInterval(() => {
      if (!hydratedRef.current) return;

      const msSinceLastMutation = Date.now() - lastLocalMutationRef.current;

      // If local changes are pending, try to save first.
      // But if saving has been failing for >30s, force a pull to unblock sync.
      if (pendingLocalChangesRef.current) {
        if (msSinceLastMutation < 30_000) {
          void patchTdaState({
            coins: coinsRef.current,
            data: dataRef.current,
            photos: photosRef.current,
          })
            .then(() => { pendingLocalChangesRef.current = false; })
            .catch(() => { /* retry next tick */ });
          return;
        }
        // Saves have been failing for 30s — stop blocking pulls.
        pendingLocalChangesRef.current = false;
      }

      // Don't pull immediately after a local mutation (save is still in flight).
      if (msSinceLastMutation < 1500) return;

      void pullFromServer();
    }, 1500);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    function onFocus() {
      if (!hydratedRef.current) return;
      void pullFromServer();
    }
    function onVisibility() {
      if (!hydratedRef.current) return;
      if (document.visibilityState === "visible") {
        void pullFromServer();
      }
    }
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  useEffect(() => {
    if (!hydratedRef.current) return;
    if (backupTimerRef.current != null) {
      window.clearTimeout(backupTimerRef.current);
    }
    backupTimerRef.current = window.setTimeout(() => {
      pushBackupSnapshot(coins, data, photos);
    }, 1200);
    return () => {
      if (backupTimerRef.current != null) {
        window.clearTimeout(backupTimerRef.current);
        backupTimerRef.current = null;
      }
    };
  }, [coins, data, photos]);

  const previewPhoto = previewCoin ? photos[previewCoin] : "";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex items-baseline gap-3">
          <div className="text-accent text-xl font-bold tracking-[0.3em]">TDA</div>
          <div className="text-[11px] uppercase tracking-[0.3em] text-muted">
            trade data analysis
          </div>
          <button
            type="button"
            onClick={restoreLatestBackup}
            className="rounded-md border border-border px-2 py-1 text-[10px] uppercase tracking-[0.14em] text-muted hover:text-zinc-200 disabled:opacity-40"
            title={backups[0] ? `Restore backup ${new Date(backups[0].ts).toLocaleString()}` : "No backups yet"}
            disabled={backups.length === 0}
          >
            restore backup
          </button>
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            addCoin();
          }}
          className="flex min-w-[280px] items-end gap-2"
        >
          <label className="flex-1">
            <div className="mb-1 text-[10px] uppercase tracking-widest text-muted">
              Add coin
            </div>
            <SymbolSuggestInput
              value={input}
              onChange={setInput}
              onPick={addCoin}
              exclude={exclude}
              className="w-full rounded-lg border border-border bg-bg px-3 py-2 font-mono uppercase focus:border-accent focus:outline-none"
            />
          </label>
          <button
            type="submit"
            className="rounded-lg bg-accent px-4 py-2 font-semibold uppercase tracking-widest text-black disabled:opacity-50"
            disabled={!input.trim()}
          >
            Add
          </button>
        </form>
      </div>

      <div className="bg-panel border border-border rounded-2xl overflow-x-auto">
        <table className="w-full text-sm font-mono" style={{ tableLayout: "auto" }}>
          <thead>
            <tr className="text-[10px] uppercase tracking-[0.2em] text-muted border-b border-border">
              <th className="w-12 px-3 py-3" />
              <th className="px-4 py-3 font-normal text-left whitespace-nowrap">COIN</th>
              {COLS.map((col) => (
                <th key={col} className="px-3 py-3 font-normal text-left whitespace-nowrap">
                  {col}
                </th>
              ))}
              <th className="px-3 py-3" />
            </tr>
          </thead>
          <tbody>
            {coins.map((coin) => {
              const hasPhoto = Boolean(photos[coin]);

              return (
                <tr
                  key={coin}
                  data-tda-symbol={coin}
                  className={`border-t border-border/60 transition-[background-color,opacity,transform] duration-150 ease-out hover:bg-white/[0.02] ${
                    dragging === coin ? "bg-white/[0.06] opacity-70" : ""
                  }`}
                >
                  <td className="px-3 py-2 align-middle">
                    <button
                      type="button"
                      aria-label={`Move ${displayName(coin)}`}
                      title="Drag to reorder"
                      onPointerDown={(event) => {
                        event.preventDefault();
                        setDragging(coin);
                      }}
                      className="group flex h-8 w-8 cursor-pointer touch-none items-center justify-center rounded-md hover:bg-white/[0.04] active:cursor-grabbing"
                    >
                      <span className="flex w-5 flex-col gap-[3px]">
                        <span className="h-[2px] w-5 rounded-full bg-[#86898e] transition-colors group-hover:bg-zinc-300" />
                        <span className="h-[2px] w-5 rounded-full bg-[#86898e] transition-colors group-hover:bg-zinc-300" />
                        <span className="h-[2px] w-5 rounded-full bg-[#86898e] transition-colors group-hover:bg-zinc-300" />
                      </span>
                    </button>
                  </td>
                  <td className="px-4 py-2 font-semibold text-zinc-100">{displayName(coin)}</td>
                  {COLS.map((col) => (
                    <td key={col} className="px-2 py-1.5">
                      <input
                        value={data[coin]?.[col] ?? ""}
                        onChange={(e) => set(coin, col, e.target.value)}
                        placeholder="—"
                        className="w-full min-w-[72px] bg-bg border border-border rounded-md px-2 py-1 text-xs text-zinc-200 placeholder-muted focus:outline-none focus:border-accent font-mono"
                      />
                    </td>
                  ))}
                  <td className="px-3 py-1.5 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <>
                          <button
                            type="button"
                            onClick={() => { void handleAttachClick(coin); }}
                            className={`inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-md border text-base leading-none transition-colors ${
                              pasteArmedCoin === coin
                                ? "border-accent text-accent"
                                : "border-border text-muted hover:text-accent hover:border-accent/60"
                            }`}
                            title={
                              pasteArmedCoin === coin
                                ? "Press Ctrl/Cmd+V to paste image, or pick a file"
                                : hasPhoto
                                  ? "Change idea photo (click to pick or paste)"
                                  : "Attach idea photo (click to pick or paste)"
                            }
                            aria-label={hasPhoto ? "Change idea photo" : "Attach idea photo"}
                          >
                            +
                          </button>
                          <input
                            ref={(el) => {
                              fileInputRefs.current[coin] = el;
                            }}
                            type="file"
                            accept="image/*"
                            className="hidden"
                            onChange={(e) => {
                              setPhotoForCoin(coin, e.currentTarget.files?.[0] ?? null);
                              e.currentTarget.value = "";
                              setPasteArmedCoin(null);
                            }}
                          />
                          {hasPhoto && (
                            <button
                              type="button"
                              onClick={() => setPreviewCoin(coin)}
                              className="inline-flex items-center gap-1 rounded-md border border-accent/40 px-1.5 py-1 text-[10px] uppercase tracking-[0.12em] text-accent"
                              title="Open trading idea"
                            >
                              <span className="h-1.5 w-1.5 rounded-full bg-accent" />
                              photo
                            </button>
                          )}
                        </>
                      <button
                        type="button"
                        onClick={() => removeCoin(coin)}
                        className="text-xs text-muted kz-premium-hover"
                        title="Remove"
                      >
                        x
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
            {coins.length === 0 && (
              <tr>
                <td colSpan={COLS.length + 3} className="py-8 text-center text-sm text-muted">
                  Add a coin above.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {previewCoin && previewPhoto && (
        <div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/75 p-4"
          onClick={() => setPreviewCoin(null)}
        >
          <div
            className="w-[min(980px,96vw)] rounded-2xl border border-border bg-panel p-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="text-sm uppercase tracking-[0.22em] text-zinc-100">
                {displayName(previewCoin)} Idea Photo
              </div>
              <div className="flex items-center gap-2">
                <label className="cursor-pointer rounded-md border border-border px-3 py-1.5 text-[11px] uppercase tracking-[0.14em] text-muted hover:text-accent hover:border-accent/60">
                  Replace
                  <input
                    type="file"
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => {
                      setPhotoForCoin(previewCoin, e.currentTarget.files?.[0] ?? null);
                      e.currentTarget.value = "";
                    }}
                  />
                </label>
                <button
                  type="button"
                  onClick={() => removePhotoForCoin(previewCoin)}
                  className="rounded-md border border-[#752727]/60 px-3 py-1.5 text-[11px] uppercase tracking-[0.14em] text-[#b96d6d] hover:text-[#d38a8a]"
                >
                  Remove
                </button>
                <button
                  type="button"
                  onClick={() => setPreviewCoin(null)}
                  className="rounded-md border border-border px-3 py-1.5 text-[11px] uppercase tracking-[0.14em] text-muted hover:text-zinc-200"
                >
                  Close
                </button>
              </div>
            </div>
            <div className="max-h-[78vh] overflow-auto rounded-xl border border-border bg-bg/40 p-2">
              <img src={previewPhoto} alt={`${displayName(previewCoin)} trading idea`} className="w-full rounded-md" />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
