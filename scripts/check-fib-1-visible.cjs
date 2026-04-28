const { chromium } = require("playwright");
const fs = require("node:fs");

const baseUrl = process.env.KAZUS_BASE_URL || "http://127.0.0.1:8080";
const username = process.env.KAZUS_USER || "kazus";
const password = process.env.KAZUS_PASSWORD || "globalocal";
const symbol = process.env.KAZUS_SYMBOL || "WUSDT";
const tab = process.env.KAZUS_CHART_TAB || "local";
const interval = tab === "global" ? "1d" : tab === "entry" ? "15m" : "1h";
const rangeMode = process.env.KAZUS_RANGE_MODE || "clear";

async function getToken() {
  const res = await fetch(`${baseUrl}/api/auth/login-json`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    throw new Error(`login failed: ${res.status} ${await res.text()}`);
  }
  const body = await res.json();
  return body.access_token;
}

async function main() {
  const token = await getToken();
  let presetRange = null;
  if (rangeMode === "last50") {
    const res = await fetch(`${baseUrl}/api/chart/${encodeURIComponent(symbol)}?interval=${interval}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      throw new Error(`chart fetch failed: ${res.status} ${await res.text()}`);
    }
    const chart = await res.json();
    const bars = chart.bars.slice(0, -1);
    if (bars.length < 50) throw new Error(`not enough bars for last50 range: ${bars.length}`);
    presetRange = {
      from: Math.floor(bars[bars.length - 50].ts / 1000),
      to: Math.floor(bars[bars.length - 1].ts / 1000),
    };
  }

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({
    viewport: { width: 1792, height: 1244 },
    deviceScaleFactor: 1,
  });

  await page.addInitScript(
    ({ token, symbol, tab, interval, presetRange }) => {
      localStorage.setItem("kazus_token", token);
      localStorage.setItem("kazus_chart_symbol", symbol);
      localStorage.setItem("kazus_chart_tab", tab);
      localStorage.setItem("kazus_chart_theme", "dark");
      localStorage.setItem("kazus_motion", "0");
      const rangeKey = `kazus_chart_range_${symbol}_${interval}`;
      if (presetRange) localStorage.setItem(rangeKey, JSON.stringify(presetRange));
      else localStorage.removeItem(rangeKey);
    },
    { token, symbol, tab, interval, presetRange },
  );

  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.waitForSelector(".kz-modal-header", { timeout: 30000 });
  await page.waitForSelector('[data-fib-line="1.0"]', { state: "attached", timeout: 30000 });

  await page.waitForFunction(() => {
    const group = document.querySelector('[data-fib-ratio="1.0"]');
    const line = document.querySelector('[data-fib-line="1.0"]');
    const htmlLine = document.querySelector('[data-fib-html-line="1.0"]');
    const label = document.querySelector('[data-fib-label="1.0"]');
    if (
      !(group instanceof SVGElement) ||
      !(line instanceof SVGLineElement) ||
      !(htmlLine instanceof HTMLElement) ||
      !(label instanceof HTMLElement)
    ) {
      return false;
    }

    const lineRect = line.getBoundingClientRect();
    const htmlLineRect = htmlLine.getBoundingClientRect();
    const htmlLineStyle = window.getComputedStyle(htmlLine);
    const labelRect = label.getBoundingClientRect();
    const labelStyle = window.getComputedStyle(label);
    const groupOpacity = group.getAttribute("opacity");
    const lineWidth = Math.abs(Number(line.getAttribute("x2")) - Number(line.getAttribute("x1")));

    return (
      groupOpacity === "1" &&
      htmlLineStyle.opacity !== "0" &&
      labelStyle.opacity !== "0" &&
      lineWidth >= 80 &&
      htmlLineRect.width >= 80 &&
      htmlLineRect.height >= 1 &&
      lineRect.left >= 0 &&
      lineRect.right <= window.innerWidth &&
      lineRect.top >= 0 &&
      lineRect.bottom <= window.innerHeight &&
      htmlLineRect.left >= 0 &&
      htmlLineRect.right <= window.innerWidth &&
      htmlLineRect.top >= 0 &&
      htmlLineRect.bottom <= window.innerHeight &&
      labelRect.width > 10 &&
      labelRect.height > 6 &&
      labelRect.left >= 0 &&
      labelRect.right <= window.innerWidth &&
      labelRect.top >= 0 &&
      labelRect.bottom <= window.innerHeight
    );
  }, { timeout: 30000 });

  const result = await page.evaluate(() => {
    const group = document.querySelector('[data-fib-ratio="1.0"]');
    const line = document.querySelector('[data-fib-line="1.0"]');
    const htmlLine = document.querySelector('[data-fib-html-line="1.0"]');
    const label = document.querySelector('[data-fib-label="1.0"]');
    const chart = line?.closest("svg");
    const rectOf = (el) => {
      const r = el.getBoundingClientRect();
      return {
        left: Math.round(r.left),
        top: Math.round(r.top),
        right: Math.round(r.right),
        bottom: Math.round(r.bottom),
        width: Math.round(r.width),
        height: Math.round(r.height),
      };
    };
    return {
      symbol: document.querySelector(".currentCoin")?.textContent?.trim() || null,
      groupOpacity: group?.getAttribute("opacity"),
      clamped: group?.getAttribute("data-fib-clamped"),
      lineStroke: line?.getAttribute("stroke"),
      lineStrokeWidth: line?.getAttribute("stroke-width"),
      lineWidth: Math.round(Math.abs(Number(line?.getAttribute("x2")) - Number(line?.getAttribute("x1")))),
      htmlLineRect: htmlLine ? rectOf(htmlLine) : null,
      lineRect: line ? rectOf(line) : null,
      labelRect: label ? rectOf(label) : null,
      chartRect: chart ? rectOf(chart) : null,
    };
  });

  fs.mkdirSync("/tmp/kazus-global", { recursive: true });
  await page.screenshot({ path: "/tmp/kazus-global/fib-1-visible.png", fullPage: true });
  await browser.close();

  if (result.symbol !== symbol.replace(/USDT$/, "")) {
    throw new Error(`opened wrong symbol: ${JSON.stringify(result)}`);
  }

  console.log(JSON.stringify(result, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
