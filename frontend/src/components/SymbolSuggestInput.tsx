import { useEffect, useMemo, useRef, useState } from "react";
import { suggestCoins } from "../lib/api";

type Props = {
  value: string;
  onChange: (value: string) => void;
  onPick?: (symbol: string) => void;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  exclude?: string[];
};

function normalizeQuery(value: string) {
  return value.trim().toUpperCase();
}

function displayName(symbol: string) {
  let s = symbol.replace(/USDT$/, "");
  if (s.startsWith("1000")) s = s.slice(4);
  return s;
}

export function normalizeSymbol(value: string) {
  const raw = normalizeQuery(value);
  if (!raw) return "";
  return raw.endsWith("USDT") ? raw : `${raw}USDT`;
}

export function SymbolSuggestInput({
  value,
  onChange,
  onPick,
  placeholder = "BTCUSDT",
  disabled = false,
  className = "",
  exclude = [],
}: Props) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const excludeSet = useMemo(() => new Set(exclude.map((s) => s.toUpperCase())), [exclude]);

  useEffect(() => {
    let cancelled = false;
    const q = normalizeQuery(value);
    if (!q) {
      setSuggestions([]);
      return;
    }
    const id = window.setTimeout(async () => {
      try {
        const list = await suggestCoins(q, 10);
        if (!cancelled) {
          setSuggestions(list.filter((symbol) => !excludeSet.has(symbol)));
          setOpen(true);
        }
      } catch {
        if (!cancelled) setSuggestions([]);
      }
    }, 160);
    return () => {
      cancelled = true;
      window.clearTimeout(id);
    };
  }, [value, excludeSet]);

  useEffect(() => {
    function handlePointerDown(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, []);

  function pick(symbol: string) {
    onChange(symbol);
    onPick?.(symbol);
    setOpen(false);
  }

  return (
    <div ref={rootRef} className="relative">
      <input
        className={className}
        placeholder={placeholder}
        value={value}
        disabled={disabled}
        onFocus={() => value.trim() && setOpen(true)}
        onChange={(e) => onChange(e.target.value.toUpperCase())}
        onKeyDown={(e) => {
          if (e.key === "Enter" && suggestions[0] && value.trim() !== suggestions[0]) {
            e.preventDefault();
            pick(suggestions[0]);
          }
          if (e.key === "Escape") setOpen(false);
        }}
      />
      {open && suggestions.length > 0 && (
        <div className="absolute left-0 right-0 top-full z-30 mt-1 overflow-hidden rounded-lg border border-border bg-panel shadow-2xl">
          {suggestions.map((symbol) => (
            <button
              key={symbol}
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(symbol)}
              className="flex w-full items-center justify-between px-3 py-2 text-left font-mono text-xs text-zinc-200 hover:bg-white/[0.05] hover:text-accent"
            >
              <span>{displayName(symbol)}</span>
              <span className="text-[10px] text-muted">{symbol}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
