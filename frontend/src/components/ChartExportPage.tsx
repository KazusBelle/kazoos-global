import { useEffect, useMemo, useState } from "react";
import {
  CandleChart,
  type ChartInterval,
  type ChartTheme,
  type SetupOverlay,
} from "./CandleChart";

// Headless render endpoint: the worker's Playwright runner navigates here
// to capture a PNG of the same CandleChart shown in the UI. Mount happens
// before window.__chartReady is set; the flag flips on the first frame
// where the chart has measurable extent (see CandleChart onReady).

declare global {
  interface Window {
    __chartReady?: boolean;
    __chartError?: string;
  }
}

const ALLOWED_INTERVALS: ChartInterval[] = ["1d", "1h", "15m", "5m"];
const ALLOWED_THEMES: ChartTheme[] = ["dark", "light"];
const ALLOWED_STATES: SetupOverlay["state"][] = ["INV", "CRE", "STB"];

function numParam(params: URLSearchParams, key: string): number | null {
  const raw = params.get(key);
  if (raw == null || raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

export function ChartExportPage() {
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const [ready, setReady] = useState(false);

  const symbol = (params.get("symbol") ?? "").toUpperCase();
  const tfRaw = params.get("tf") ?? "1h";
  const tf: ChartInterval = (ALLOWED_INTERVALS as string[]).includes(tfRaw)
    ? (tfRaw as ChartInterval)
    : "1h";
  const themeRaw = params.get("theme") ?? "dark";
  const theme: ChartTheme = (ALLOWED_THEMES as string[]).includes(themeRaw)
    ? (themeRaw as ChartTheme)
    : "dark";
  const w = Math.max(320, numParam(params, "w") ?? 1200);
  const h = Math.max(240, numParam(params, "h") ?? 675);

  const fvgEnabled = params.get("fvg") !== "0";
  const fvgLimit = Math.max(1, numParam(params, "fvg_limit") ?? 6);
  // Export-only: HTF render passes fvg_pair=1 to keep just the nearest
  // bullish + bearish imbalance. Modal/LTF never set it.
  const fvgNearestPairOnly = params.get("fvg_pair") === "1";
  // Export-only X-zoom. Clamped to [1, 5]; 1 = baseline fit. Only the
  // headless export passes this — the dashboard modal never does.
  const zoom = Math.min(5, Math.max(1, numParam(params, "zoom") ?? 1));

  const setupOverlay: SetupOverlay | null = useMemo(() => {
    const stateRaw = params.get("state");
    const fvgsRaw = params.get("fvgs");
    if (
      stateRaw == null ||
      !(ALLOWED_STATES as string[]).includes(stateRaw) ||
      !fvgsRaw
    ) {
      return null;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(fvgsRaw);
    } catch {
      return null;
    }
    if (!Array.isArray(parsed)) return null;
    const fvgs: SetupOverlay["fvgs"] = [];
    for (const raw of parsed) {
      if (raw == null || typeof raw !== "object") continue;
      const r = raw as Record<string, unknown>;
      const top = Number(r.top);
      const bottom = Number(r.bottom);
      const ts = Number(r.ts);
      const endTs = Number(r.end_ts);
      const kind = r.kind === "bearish" ? "bearish" : "bullish";
      if (
        !Number.isFinite(top) ||
        !Number.isFinite(bottom) ||
        !Number.isFinite(ts) ||
        !Number.isFinite(endTs)
      ) {
        continue;
      }
      fvgs.push({ ts, end_ts: endTs, top, bottom, kind });
    }
    if (fvgs.length === 0) return null;
    const swingLowPrice = numParam(params, "swing_low_price");
    const swingLowTs = numParam(params, "swing_low_ts");
    const swingLow =
      swingLowPrice != null
        ? { ts: swingLowTs ?? undefined, price: swingLowPrice }
        : undefined;
    return {
      state: stateRaw as SetupOverlay["state"],
      fvgs,
      swingLow,
    };
  }, [params]);

  useEffect(() => {
    // Lock viewport-sized container; the worker passes width/height that
    // matches its Playwright viewport so the rendered PNG is exact.
    document.documentElement.style.background = theme === "dark" ? "#171717" : "#b2b5be";
    document.body.style.margin = "0";
    document.body.style.background = theme === "dark" ? "#171717" : "#b2b5be";
  }, [theme]);

  if (!symbol) {
    window.__chartError = "missing symbol param";
    return (
      <div style={{ padding: 16, color: "#f87171", fontFamily: "monospace" }}>
        chart-export: missing symbol param
      </div>
    );
  }

  return (
    <div
      id="chart-export-root"
      style={{ width: w, height: h, background: theme === "dark" ? "#171717" : "#b2b5be" }}
      data-chart-ready={ready ? "1" : "0"}
    >
      <CandleChart
        symbol={symbol}
        interval={tf}
        theme={theme}
        chartHeight={h}
        fvgEnabled={fvgEnabled}
        fvgLimit={fvgLimit}
        fvgNearestPairOnly={fvgNearestPairOnly}
        exportZoom={zoom}
        setupOverlay={setupOverlay}
        onReady={() => {
          // Two-frame defer so the lightweight-charts canvas has actually
          // painted before the headless screenshot fires.
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              window.__chartReady = true;
              setReady(true);
            });
          });
        }}
      />
    </div>
  );
}
