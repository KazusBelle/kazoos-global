export type Snapshot = {
  symbol: string;
  timeframe: string;
  price: number | null;
  direction: string;
  zone: "premium" | "discount" | "ote" | "none" | string;
  in_ote: boolean;
  setup: "yes" | "no" | string;
  retracement: number | null;
  fib_low: number | null;
  fib_high: number | null;
  ote_low_price: number | null;
  ote_high_price: number | null;
  trend: string;
  closes: number[];
  updated_at: string;
};

export type CallTag = "vanga" | "voldemar" | "makiavelli" | "me" | null;

export type DashboardRow = {
  symbol: string;
  price: number | null;
  pinned_order: number | null;
  call_tag: CallTag;
  call_note: string | null;
  global: Snapshot | null;
  local: Snapshot | null;
};

export type Coin = {
  id: number;
  symbol: string;
  is_active: boolean;
  pinned_order: number | null;
  call_tag: CallTag;
  call_note: string | null;
};

export type DashboardResponse = {
  rows: DashboardRow[];
  totals: { total: number; ote: number; discount: number; premium: number };
  recent_alerts: AlertEvent[];
  last_refresh_at: string | null;
  last_error: string | null;
};

export type AlertEvent = {
  id: number;
  timeframe: string;
  message: string;
  created_at: string;
};

const TOKEN_KEY = "kazus_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY) ?? sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null, remember = true) {
  localStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
  if (token) {
    const storage = remember ? localStorage : sessionStorage;
    storage.setItem(TOKEN_KEY, token);
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(`/api${path}`, { ...init, headers });
  if (res.status === 401) {
    setToken(null);
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let detail: any;
    try {
      detail = await res.json();
    } catch {
      detail = await res.text();
    }
    throw new Error(typeof detail === "string" ? detail : detail?.detail || "request failed");
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export async function login(username: string, password: string, remember = true): Promise<string> {
  const res = await request<{ access_token: string }>("/auth/login-json", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  setToken(res.access_token, remember);
  return res.access_token;
}

export async function listCoins() {
  return request<Coin[]>("/coins");
}

export async function addCoin(symbol: string) {
  return request<Coin>("/coins", {
    method: "POST",
    body: JSON.stringify({ symbol }),
  });
}

export async function suggestCoins(query: string, limit = 12) {
  const params = new URLSearchParams({
    q: query,
    limit: String(limit),
  });
  return request<string[]>(`/coins/suggestions?${params.toString()}`);
}

export async function removeCoin(symbol: string) {
  return request<void>(`/coins/${encodeURIComponent(symbol)}`, { method: "DELETE" });
}

export async function togglePin(symbol: string) {
  return request<Coin>(`/coins/${encodeURIComponent(symbol)}/pin`, { method: "POST" });
}

export async function movePin(symbol: string, direction: "up" | "down") {
  return request<Coin[]>(`/coins/${encodeURIComponent(symbol)}/pin/move`, {
    method: "POST",
    body: JSON.stringify({ direction }),
  });
}

export async function setCall(symbol: string, tag: CallTag, note: string | null) {
  return request<Coin>(`/coins/${encodeURIComponent(symbol)}/call`, {
    method: "POST",
    body: JSON.stringify({ tag, note }),
  });
}

export async function getDashboard() {
  return request<DashboardResponse>("/dashboard");
}

export type OHLCVBar = {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type SwingPoint = {
  ts: number;
  price: number;
  label: "HH" | "HL" | "LL" | "LH" | string;
};

// Compatibility shim for older chart-modal backups that supported manual
// structure editing directly from the frontend.
export type StructureEvent = SwingPoint;

export type FvgBox = {
  ts: number;
  end_ts: number;
  top: number;
  bottom: number;
  kind: "bullish" | "bearish" | string;
};

export type ChartData = {
  symbol: string;
  interval: string;
  bars: OHLCVBar[];
  swings: SwingPoint[];
  fvgs: FvgBox[];
  fib_high: number | null;
  fib_low: number | null;
  fib_direction: "bullish" | "bearish" | "none" | string;
};

export async function getChart(symbol: string, interval: string, limit?: number) {
  // When limit is undefined the backend picks a per-interval default that
  // matches the worker (d1=500, h1=900, 15m=600) so the chart's engine state
  // is identical to what the screener table shows.
  const params = new URLSearchParams({ interval });
  if (limit != null) params.set("limit", String(limit));
  return request<ChartData>(
    `/chart/${encodeURIComponent(symbol)}?${params.toString()}`
  );
}

export async function reportFrontendError(payload: {
  kind: string;
  message: string;
  source?: string;
  stack?: string;
  url?: string;
  user_agent?: string;
  context?: Record<string, unknown>;
}) {
  try {
    const headers = new Headers({ "Content-Type": "application/json" });
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    await fetch("/api/frontend-errors", {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      keepalive: true,
    });
  } catch {
    // Never throw while reporting frontend crashes.
  }
}

export async function saveStructure(
  _symbol: string,
  _interval: string,
  _events: StructureEvent[]
) {
  throw new Error("manual structure editing is unavailable in this preview");
}

export async function deleteStructure(_symbol: string, _interval: string) {
  throw new Error("manual structure editing is unavailable in this preview");
}

export type TDAState = {
  coins: string[];
  data: Record<string, Record<string, string>>;
  photos: Record<string, string>;
};

export async function getTdaState() {
  return request<TDAState>("/tda/state");
}

export async function saveTdaState(state: TDAState) {
  return request<TDAState>("/tda/state", {
    method: "PUT",
    body: JSON.stringify(state),
  });
}

export async function patchTdaState(state: Partial<TDAState>) {
  try {
    return await request<TDAState>("/tda/state", {
      method: "PATCH",
      body: JSON.stringify(state),
    });
  } catch {
    // PATCH failed — fall back to a full PUT merged with current server state.
    const current = await getTdaState().catch(
      () => ({ coins: [], data: {}, photos: {} }) as TDAState
    );
    const merged: TDAState = {
      coins: state.coins ?? current.coins,
      data: state.data ?? current.data,
      photos: state.photos ?? current.photos,
    };
    try {
      return await saveTdaState(merged);
    } catch {
      // PUT also failed — last resort: strip photos to reduce payload size and retry.
      return saveTdaState({ coins: merged.coins, data: merged.data, photos: {} });
    }
  }
}

export type LiqRow = {
  rank: number;
  coingecko_symbol: string;
  binance_symbol: string;
  name: string;
  market_cap: number | null;
  volume_24h: number | null;
  price: number | null;
  change_24h_pct: number | null;
  image: string | null;
};

export type LiqResponse = {
  limit: number;
  rows: LiqRow[];
  fetched_at: number;
};

export async function getLiquidityTop(limit: 100 | 250 | 500) {
  return request<LiqResponse>(`/liquidity/top?limit=${limit}`);
}

export type LiqMetricMeta = {
  name: string;
  label: string;
};

export type LiqMetricSample = {
  ts: number;
  value: number | null;
  price: number | null;
};

export type LiqMetricSeries = {
  symbol: string;
  metric: string;
  label: string;
  window: string;
  samples: LiqMetricSample[];
};

export async function listLiquidityMetrics() {
  return request<LiqMetricMeta[]>("/liquidity/metrics");
}

export async function getLiquidityMetricSeries(
  symbol: string,
  metric: string,
  window: "1h" | "24h" | "7d" | "30d",
  since?: number,
) {
  const params = new URLSearchParams({ metric, window });
  if (since != null) params.set("since", String(since));
  return request<LiqMetricSeries>(
    `/liquidity/metrics/${encodeURIComponent(symbol)}?${params.toString()}`,
  );
}

export async function heartbeatLiquidityActive(symbol: string, ttlSeconds = 120) {
  return request<{ symbol: string; expires_at: number }>("/liquidity/active", {
    method: "POST",
    body: JSON.stringify({ symbol, ttl_seconds: ttlSeconds }),
  });
}

export type LiqMetricLatest = {
  value: number | null;
  ts: number;
};

export type LiqMetricsSnapshot = {
  symbols: Record<string, Record<string, LiqMetricLatest>>;
};

export async function getLiquidityMetricsSnapshot(symbols: string[]) {
  if (symbols.length === 0) return { symbols: {} } as LiqMetricsSnapshot;
  const params = new URLSearchParams({ symbols: symbols.join(",") });
  return request<LiqMetricsSnapshot>(`/liquidity/snapshot?${params.toString()}`);
}
