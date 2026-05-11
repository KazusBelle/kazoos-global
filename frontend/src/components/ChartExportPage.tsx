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

  const setupOverlay: SetupOverlay | null = useMemo(() => {
    const stateRaw = params.get("state");
    const fvgTop = numParam(params, "fvg_top");
    const fvgBottom = numParam(params, "fvg_bottom");
    const fvgTs = numParam(params, "fvg_ts");
    const fvgEndTs = numParam(params, "fvg_end_ts");
    if (
      stateRaw == null ||
      !(ALLOWED_STATES as string[]).includes(stateRaw) ||
      fvgTop == null ||
      fvgBottom == null ||
      fvgTs == null ||
      fvgEndTs == null
    ) {
      return null;
    }
    const swingLowPrice = numParam(params, "swing_low_price");
    const swingLowTs = numParam(params, "swing_low_ts");
    const swingLow =
      swingLowPrice != null
        ? { ts: swingLowTs ?? undefined, price: swingLowPrice }
        : undefined;
    return {
      state: stateRaw as SetupOverlay["state"],
      fvg: { ts: fvgTs, end_ts: fvgEndTs, top: fvgTop, bottom: fvgBottom },
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
