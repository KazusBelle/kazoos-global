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

export type ServerMetricPoint = {
  created_at: string;
  load_1m: number | null;
  load_5m: number | null;
  load_15m: number | null;
  cpu_percent: number | null;
  memory_percent: number | null;
  swap_percent: number | null;
  disk_percent: number | null;
  net_connections: number | null;
};

export type ServerMetricsResponse = {
  points: ServerMetricPoint[];
  latest: ServerMetricPoint | null;
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

// Default client-side request timeout. A hung backend (e.g. analytics
// endpoints aggregating the ~66M-row liquidity_samples while the DB is
// saturated) would otherwise leave fetch pending forever and panels stuck on
// "Loading…". The timeout aborts the request so the promise rejects, letting
// each panel's existing .catch render an error/unavailable state.
const DEFAULT_TIMEOUT_MS = 20_000;

async function request<T>(
  path: string,
  init: RequestInit = {},
  opts: { timeoutMs?: number } = {}
): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let res: Response;
  try {
    res = await fetch(`/api${path}`, { ...init, headers, signal: controller.signal });
  } catch (e) {
    // AbortController.abort() rejects fetch with an AbortError — surface it as
    // a clear, non-retried timeout. Other network errors pass through.
    if (controller.signal.aborted) {
      throw new Error(`request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }

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

export async function getServerMetrics(hours = 24) {
  const params = new URLSearchParams({
    hours: String(hours),
    limit: String(Math.min(12_000, hours * 60 + 10)),
  });
  return request<ServerMetricsResponse>(`/system/metrics?${params.toString()}`);
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

export type LiqPin = { symbol: string; pinned_order: number };

export async function listLiquidityPins() {
  return request<LiqPin[]>("/liquidity/pins");
}

export async function addLiquidityPin(symbol: string) {
  return request<LiqPin>("/liquidity/pins", {
    method: "POST",
    body: JSON.stringify({ symbol }),
  });
}

export async function removeLiquidityPin(symbol: string) {
  return request<void>(`/liquidity/pins/${encodeURIComponent(symbol)}`, {
    method: "DELETE",
  });
}

export async function moveLiquidityPin(symbol: string, direction: "up" | "down") {
  return request<LiqPin[]>(`/liquidity/pins/${encodeURIComponent(symbol)}/move`, {
    method: "POST",
    body: JSON.stringify({ direction }),
  });
}

export type LiqWsStatus = {
  conn_id: number;
  connected: boolean;
  subscribed: string[];
  last_message_at: number | null;
  updated_at: number | null;
};

export async function getLiquidityWsStatus() {
  return request<LiqWsStatus>("/liquidity/ws/status");
}

// ── Replay ────────────────────────────────────────────────────────────────

export type LiqReplayRange = {
  earliest_ts: number | null;
  latest_ts: number | null;
};

export async function getLiquidityReplayRange() {
  return request<LiqReplayRange>("/liquidity/replay/range");
}

export async function getLiquidityReplaySnapshot(symbols: string[], asOfMs: number) {
  if (symbols.length === 0) return { symbols: {} } as LiqMetricsSnapshot;
  const params = new URLSearchParams({
    symbols: symbols.join(","),
    as_of: String(asOfMs),
  });
  return request<LiqMetricsSnapshot>(`/liquidity/snapshot/replay?${params.toString()}`);
}

// ── Cross-exchange ────────────────────────────────────────────────────────

export type CrossExSnapshot = {
  exchange: string;
  symbol: string;
  funding_rate: number | null;
  open_interest_usd: number | null;
  spread_fraction: number | null;
  mid_price: number | null;
  ts_ms: number;
};

export type CrossExDivergence = {
  exchange: string;
  funding_diff: number | null;
  oi_diff_pct: number | null;
  spread_diff_pct: number | null;
  mid_price_diff_pct: number | null;
};

export type CrossExResponse = {
  symbol: string;
  snapshots: CrossExSnapshot[];
  divergences: CrossExDivergence[];
  reference: string;
  fetched_at_ms: number;
};

export async function getCrossExchange(symbol: string) {
  return request<CrossExResponse>(`/liquidity/crossex/${encodeURIComponent(symbol)}`);
}

// ── Phase-7 research ──────────────────────────────────────────────────────

export type AlertLogIn = {
  alert_id: string;
  symbol: string;
  kind: string;
  severity: string;
  regime: string;
  confidence: number;
  priority: number;
  trigger: string;
  started_at_ms: number;
  last_seen_at_ms: number;
};

export type AlertLogOut = AlertLogIn & {
  validated_outcome: string | null;
  validated_at_ms: number | null;
  validation_notes: string | null;
};

export async function postLiquidityAlert(payload: AlertLogIn) {
  return request<AlertLogOut>("/liquidity/alerts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function patchLiquidityAlertValidation(
  alert_id: string,
  outcome: "followed_through" | "noise" | "pending",
  notes?: string,
) {
  return request<AlertLogOut>(
    `/liquidity/alerts/${encodeURIComponent(alert_id)}/validate`,
    {
      method: "PATCH",
      body: JSON.stringify({ validated_outcome: outcome, notes: notes ?? null }),
    },
  );
}


export type AnnotationKind =
  | "useful_signal" | "false_signal" | "manipulation"
  | "interesting_setup" | "liquidation_event" | "spoof_behavior" | "other";

export type Annotation = {
  id: number;
  symbol: string;
  ts_ms: number;
  kind: AnnotationKind;
  note: string | null;
  user_id: number | null;
  created_at: number;
};

export async function addAnnotation(payload: { symbol: string; ts_ms: number; kind: AnnotationKind; note?: string }) {
  return request<Annotation>("/liquidity/annotations", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}


// ── Phase-8 edge discovery ────────────────────────────────────────────────


// ── Phase-9 operational intelligence ──────────────────────────────────────


// ── Phase-10 strategic intelligence ───────────────────────────────────────


// ── Phase-11 self-calibration & meta-learning ─────────────────────────────


// ── Phase-12 coordination & synthesis ─────────────────────────────────────


// ── Phase-13 market memory & evolution ────────────────────────────────────


// ── Phase-14 discovery ────────────────────────────────────────────────────


// ── Phase 18 — Investigation & Casework Layer ─────────────────────────


// ── Pass B: causal tree, similarity, export ──────────────────────────


// ── Phase 19 — Replay Intelligence ────────────────────────────────────


// ── Runtime health (Maintenance Pass §6) ─────────────────────────────
//
// Surface degraded-state visibility on operator surfaces. Polled at a
// low cadence — the system is generally stable, and the only role of
// this client is to put one chip on the top of DISC.


// ── Observation Period runtime-state (Stage 2) ──────────────────────────────
export type FlowStatus = "GREEN" | "YELLOW" | "RED";

export type FlowIndicator = {
  latest_age_s: number | null;
  status: FlowStatus;
};

export type ValuePathSymbol = {
  symbol: string;
  age_s: number | null;
  dv: number;
};

export type ValuePathStatus = {
  ok: boolean;
  window_s: number;
  per_symbol: ValuePathSymbol[];
};

export type RuntimeState = {
  t0_new: string;
  elapsed_s: number;
  hrs_to_3d: number;
  hrs_to_7d: number;
  baseline: string[];
  subscribed_count: number | null;
  conn_id: number | null;
  failure_boundary: string;
  health_age_s: number | null;
  value_path: ValuePathStatus;
  flows: Record<string, FlowIndicator>;
  continuity: Record<string, unknown> | null;
  derived_status: FlowStatus;
};

export async function getRuntimeState() {
  // Polled every 10s by ObservationBanner — use a tighter 5s timeout so a slow
  // backend cannot make polls overlap/pile up; a timed-out poll rejects and the
  // banner shows its RED "status unavailable" state until the next poll.
  return request<RuntimeState>("/liquidity/runtime-state", {}, { timeoutMs: 5_000 });
}
