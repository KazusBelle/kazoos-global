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

export type SignalKindStats = {
  kind: string;
  total: number;
  resolved: number;
  followed_through: number;
  noise: number;
  pending: number;
  precision: number | null;
  false_positive_rate: number | null;
  avg_priority: number | null;
  avg_confidence: number | null;
};

export type SignalStatsResponse = {
  since_ms: number | null;
  until_ms: number | null;
  kinds: SignalKindStats[];
};

export async function getSignalStats(params: { since_ms?: number; until_ms?: number } = {}) {
  const usp = new URLSearchParams();
  if (params.since_ms != null) usp.set("since_ms", String(params.since_ms));
  if (params.until_ms != null) usp.set("until_ms", String(params.until_ms));
  const q = usp.toString();
  return request<SignalStatsResponse>(`/liquidity/research/signal-stats${q ? `?${q}` : ""}`);
}

export type DriftPoint = { bucket_ts: number; p10: number | null; p50: number | null; p90: number | null };
export type DriftSeries = { metric: string; bucket_minutes: number; points: DriftPoint[] };

export async function getDriftSeries(metric: string, bucket_minutes = 60, since_ms?: number) {
  const usp = new URLSearchParams({ metric, bucket_minutes: String(bucket_minutes) });
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<DriftSeries>(`/liquidity/research/drift?${usp.toString()}`);
}

export type SimilarityMatch = {
  symbol: string;
  ts: number;
  distance: number;
  metrics: Record<string, number>;
};

export type SimilarityResponse = {
  symbol: string;
  reference_ts: number;
  matches: SimilarityMatch[];
};

export async function getSimilarity(symbol: string, opts: { top_k?: number; sample_minutes?: number; lookback_days?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.top_k) usp.set("top_k", String(opts.top_k));
  if (opts.sample_minutes) usp.set("sample_minutes", String(opts.sample_minutes));
  if (opts.lookback_days) usp.set("lookback_days", String(opts.lookback_days));
  return request<SimilarityResponse>(
    `/liquidity/research/similarity/${encodeURIComponent(symbol)}${usp.toString() ? `?${usp}` : ""}`,
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

export async function deleteAnnotation(id: number) {
  return request<void>(`/liquidity/annotations/${id}`, { method: "DELETE" });
}

export async function listAnnotations(opts: { symbol?: string; since_ms?: number; limit?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.symbol) usp.set("symbol", opts.symbol);
  if (opts.since_ms != null) usp.set("since_ms", String(opts.since_ms));
  if (opts.limit != null) usp.set("limit", String(opts.limit));
  return request<Annotation[]>(`/liquidity/annotations${usp.toString() ? `?${usp}` : ""}`);
}

export type VenueStats = {
  exchange: string;
  samples: number;
  avg_spread_bps: number | null;
  avg_oi_usd: number | null;
  median_funding_bps: number | null;
  avg_mid_divergence_pct: number | null;
  avg_funding_divergence_bps: number | null;
};

export type VenueQualityResponse = { since_ms: number; venues: VenueStats[] };

export async function getVenueQuality(since_ms?: number) {
  const usp = new URLSearchParams();
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<VenueQualityResponse>(`/liquidity/research/venue-quality${usp.toString() ? `?${usp}` : ""}`);
}

export type RegimeTransition = { from_regime: string; to_regime: string; count: number };
export type RegimeStatsResponse = {
  since_ms: number;
  regime_counts: Record<string, number>;
  top_transitions: RegimeTransition[];
};

export async function getRegimeStats(since_ms?: number) {
  const usp = new URLSearchParams();
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<RegimeStatsResponse>(`/liquidity/research/regime-stats${usp.toString() ? `?${usp}` : ""}`);
}

// ── Phase-8 edge discovery ────────────────────────────────────────────────

export type InteractionCell = { a: string; b: string; r: number | null; n: number };
export type InteractionMatrix = {
  metrics: string[];
  cells: InteractionCell[];
  since_ms: number;
  bucket_minutes: number;
};

export async function getInteractionMatrix(since_ms?: number, bucket_minutes = 5) {
  const usp = new URLSearchParams({ bucket_minutes: String(bucket_minutes) });
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<InteractionMatrix>(`/liquidity/research/interactions?${usp.toString()}`);
}

export type EdgeComboRow = {
  resiliency: "low" | "mid" | "high";
  fragility: "low" | "mid" | "high";
  funding_z: "low" | "mid" | "high";
  total: number;
  outcomes: number;
  rate: number;
  lift: number | null;
};

export type EdgeRanking = {
  since_ms: number;
  bucket_minutes: number;
  alert_kind: string | null;
  metrics: string[];
  tertile_cuts: Record<string, { low_high: [number, number] }>;
  total_buckets: number;
  base_rate: number;
  outcome_window_ms: number;
  combos: EdgeComboRow[];
};

export async function getEdgeRanking(opts: { since_ms?: number; bucket_minutes?: number; alert_kind?: string } = {}) {
  const usp = new URLSearchParams();
  if (opts.since_ms != null) usp.set("since_ms", String(opts.since_ms));
  if (opts.bucket_minutes != null) usp.set("bucket_minutes", String(opts.bucket_minutes));
  if (opts.alert_kind) usp.set("alert_kind", opts.alert_kind);
  return request<EdgeRanking>(`/liquidity/research/edge-ranking${usp.toString() ? `?${usp}` : ""}`);
}

export type RegimeOutcomeRow = {
  regime: string;
  count: number;
  out_transitions: number;
  transitions: Record<string, number>;
  collapse_prob: number;
  avg_duration_ms: number | null;
};

export type RegimeOutcomes = {
  since_ms: number;
  regimes: RegimeOutcomeRow[];
  transition_pairs: RegimeTransition[];
};

export async function getRegimeOutcomes(since_ms?: number) {
  const usp = new URLSearchParams();
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<RegimeOutcomes>(`/liquidity/research/regime-outcomes${usp.toString() ? `?${usp}` : ""}`);
}

export type VenueLagRow = {
  exchange: string;
  samples: number;
  mean_lag_s: number;
  share_led_binance: number;
  share_lagged_binance: number;
};

export type VenueLagMetric = { metric: string; venues: VenueLagRow[] };

export type VenueLeadership = {
  since_ms: number;
  pair_count: number;
  max_lag_s: number;
  metrics: VenueLagMetric[];
};

export async function getVenueLeadership(since_ms?: number, max_lag_s = 120) {
  const usp = new URLSearchParams({ max_lag_s: String(max_lag_s) });
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<VenueLeadership>(`/liquidity/research/venue-leadership?${usp.toString()}`);
}

export type MetaState = {
  symbol: string;
  rarity_pct: number | null;
  n: number;
  similar_count: number | null;
  threshold: number | null;
  reference: Record<string, number> | null;
};

export async function getMetaState(symbol: string, lookback_days = 30) {
  const usp = new URLSearchParams({ lookback_days: String(lookback_days) });
  return request<MetaState>(`/liquidity/research/meta-state/${encodeURIComponent(symbol)}?${usp.toString()}`);
}

// ── Phase-9 operational intelligence ──────────────────────────────────────

export type EdgePersistenceBucket = {
  bucket_ts: number;
  total: number;
  resolved: number;
  precision: number | null;
  avg_priority: number | null;
  avg_confidence: number | null;
};

export type EdgePersistenceKind = {
  kind: string;
  series: EdgePersistenceBucket[];
  slope_per_window: number | null;
  slope_per_day: number | null;
  intercept: number | null;
  half_life_days: number | null;
  latest_precision: number | null;
};

export type EdgePersistenceOut = {
  since_ms: number;
  window_days: number;
  alert_kind: string | null;
  kinds: EdgePersistenceKind[];
};

export async function getEdgePersistence(opts: { since_ms?: number; window_days?: number; alert_kind?: string } = {}) {
  const usp = new URLSearchParams();
  if (opts.since_ms != null) usp.set("since_ms", String(opts.since_ms));
  if (opts.window_days != null) usp.set("window_days", String(opts.window_days));
  if (opts.alert_kind) usp.set("alert_kind", opts.alert_kind);
  return request<EdgePersistenceOut>(`/liquidity/research/edge-persistence${usp.toString() ? `?${usp}` : ""}`);
}

export type ReliabilityState = "STRONG" | "STABLE" | "WEAK" | "DEGRADED";

export type ReliabilityKind = {
  kind: string;
  total: number;
  resolved: number;
  accuracy: number;
  weekly_buckets: number;
  weekly_precision_std: number;
  regime_buckets: number;
  regime_precision_std: number;
  components: {
    accuracy: number;
    stability: number;
    regime_consistency: number;
    sample_size: number;
  };
  reliability_score: number;
  state: ReliabilityState;
  rank: number;
};

export type ReliabilityOut = { since_ms: number; kinds: ReliabilityKind[] };

export async function getSignalReliability(since_ms?: number) {
  const usp = new URLSearchParams();
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<ReliabilityOut>(`/liquidity/research/signal-reliability${usp.toString() ? `?${usp}` : ""}`);
}

export type TransitionForecastRegime = {
  regime: string;
  count: number;
  out_transitions: number;
  next_probs: Record<string, number>;
  expected_latency_ms: number | null;
  median_latency_ms: number | null;
  collapse_prob: number;
  stabilization_prob: number;
  volatility_expansion_prob: number;
};

export type TransitionForecastOut = {
  since_ms: number;
  regimes: TransitionForecastRegime[];
};

export async function getTransitionForecast(since_ms?: number) {
  const usp = new URLSearchParams();
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<TransitionForecastOut>(`/liquidity/research/transition-forecast${usp.toString() ? `?${usp}` : ""}`);
}

export type RiskInstabilityRow = { symbol: string; stress: number; components: Record<string, number> };

export type RiskState = {
  risk_state_score: number;
  systemic_stress_level: "QUIET" | "WATCH" | "ELEVATED" | "SEVERE";
  n_symbols: number;
  drivers: Record<string, number>;
  instability_rank: RiskInstabilityRow[];
};

export async function getRiskState() {
  return request<RiskState>("/liquidity/research/risk-state");
}

export type MarketNarrative = {
  headline: string;
  score: number;
  level: "QUIET" | "WATCH" | "ELEVATED" | "SEVERE";
  bullets: string[];
  alert_summary: string;
  regime_summary: string;
  historical_context: string;
  top_alert_kinds: { kind: string; count: number }[];
  top_regimes: { regime: string; count: number }[];
  fetched_at_ms: number;
};

export async function getMarketNarrative() {
  return request<MarketNarrative>("/liquidity/research/market-narrative");
}

// ── Phase-10 strategic intelligence ───────────────────────────────────────

export type CorrDrift = { a: string; b: string; r_prev: number | null; r_cur: number | null; delta: number };
export type MetricMigration = { metric: string; prev_median: number; cur_median: number; pct_delta: number };
export type RegimeShareDrift = { regime: string; prev_share: number; cur_share: number; delta: number };

export type StructuralBreakOut = {
  window_days: number;
  structural_break_score: number;
  break_confidence: number;
  components: { correlation_drift: number; median_migration: number; regime_mix_shift: number };
  affected_correlations: CorrDrift[];
  affected_metrics: MetricMigration[];
  affected_regimes: RegimeShareDrift[];
  cur_since: number;
  prev_since: number;
};

export async function getStructuralBreaks(window_days = 7) {
  return request<StructuralBreakOut>(`/liquidity/research/structural-breaks?window_days=${window_days}`);
}

export type RegimeShiftSignal = { name: string; value: number };
export type RegimeShiftWarning = {
  fetched_at_ms: number;
  regime_shift_probability: number;
  instability_acceleration: number;
  warning_state: "STABLE" | "WATCH" | "ELEVATED_TRANSITION_RISK" | "PRE_CASCADE" | "INSUFFICIENT_DATA";
  signals: RegimeShiftSignal[];
};

export async function getRegimeShiftWarning() {
  return request<RegimeShiftWarning>("/liquidity/research/regime-shift-warning");
}

export type AdaptiveReliabilityKind = ReliabilityKind & {
  regime_multiplier: number;
  regime_adjusted_reliability: number;
  adjusted_rank: number;
};

export type AdaptiveReliabilityOut = {
  since_ms: number;
  dominant_regime: string;
  kinds: AdaptiveReliabilityKind[];
};

export async function getAdaptiveReliability(since_ms?: number) {
  const usp = new URLSearchParams();
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<AdaptiveReliabilityOut>(`/liquidity/research/adaptive-reliability${usp.toString() ? `?${usp}` : ""}`);
}

export type MetaConfidence = {
  meta_confidence_score: number;
  confidence_stability: number;
  trustworthiness_state: "TRUSTWORTHY" | "GUARDED" | "UNRELIABLE" | "UNKNOWN";
  components: { avg_confidence: number; noise_resistance: number; regime_jitter: number; structural_break_inv: number } | null;
  n_alerts: number;
  noise_rate: number | null;
  avg_distinct_regimes: number | null;
};

export async function getMetaConfidence(since_ms?: number) {
  const usp = new URLSearchParams();
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<MetaConfidence>(`/liquidity/research/meta-confidence${usp.toString() ? `?${usp}` : ""}`);
}

export type EdgeSurvivalKind = {
  kind: string;
  deaths: number;
  expected_remaining_days: number | null;
  degradation_acceleration: number | null;
  latest_precision: number | null;
  threshold: number;
  survival_curve: { ts: number; s: number }[];
};

export type EdgeSurvivalOut = { since_ms: number; threshold: number; kinds: EdgeSurvivalKind[] };

export async function getEdgeSurvival(opts: { since_ms?: number; threshold?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.since_ms != null) usp.set("since_ms", String(opts.since_ms));
  if (opts.threshold != null) usp.set("threshold", String(opts.threshold));
  return request<EdgeSurvivalOut>(`/liquidity/research/edge-survival${usp.toString() ? `?${usp}` : ""}`);
}

export type EvolutionMetric = {
  metric: string;
  series: { ts: number; v: number }[];
  slope_per_day: number | null;
};

export type EntropyPoint = { ts: number; entropy: number; dominant_regime: string };

export type MarketEvolution = {
  lookback_days: number;
  bucket_days: number;
  metric_trends: EvolutionMetric[];
  regime_entropy_series: EntropyPoint[];
};

export async function getMarketEvolution(lookback_days = 60, bucket_days = 7) {
  const usp = new URLSearchParams({ lookback_days: String(lookback_days), bucket_days: String(bucket_days) });
  return request<MarketEvolution>(`/liquidity/research/market-evolution?${usp.toString()}`);
}

export type StrategicState = {
  state: "STABLE_INSTITUTIONAL_FLOW" | "FRAGILE_SPECULATIVE_MARKET" | "TRANSITIONAL_UNSTABLE" | "CASCADE_RISK_ENVIRONMENT" | "LIQUIDITY_DETERIORATION_PHASE";
  trustworthiness: "TRUSTWORTHY" | "GUARDED" | "UNRELIABLE" | "UNKNOWN";
  rationale: string[];
  inputs: {
    stress_level: string;
    stress_score: number;
    shift_warning: string;
    structural_break_score: number;
    dominant_regime: string;
  };
};

export async function getStrategicState() {
  return request<StrategicState>("/liquidity/research/strategic-state");
}

// ── Phase-11 self-calibration & meta-learning ─────────────────────────────

export type ThresholdCalibrationKind = {
  kind: string;
  total: number;
  resolved: number;
  precision: number | null;
  per_day: number;
  action: "TIGHTEN" | "LOOSEN" | "HOLD";
  adjustment_multiplier: number;
  calibration_confidence: number;
  rationale: string[];
};

export type ThresholdCalibration = { since_ms: number; kinds: ThresholdCalibrationKind[] };

export async function getThresholdCalibration(since_ms?: number) {
  const usp = new URLSearchParams();
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<ThresholdCalibration>(`/liquidity/research/threshold-calibration${usp.toString() ? `?${usp}` : ""}`);
}

export type MetricWeight = {
  metric: string;
  samples: number;
  extreme_hits: number;
  extreme_share: number;
  relevance_score: number;
  weight: number;
};

export type AdaptiveWeights = { since_ms: number; weights: MetricWeight[] };

export async function getAdaptiveMetricWeights(since_ms?: number) {
  const usp = new URLSearchParams();
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<AdaptiveWeights>(`/liquidity/research/adaptive-metric-weights${usp.toString() ? `?${usp}` : ""}`);
}

export type StateEmbedding = {
  metrics: string[];
  fingerprint: Record<string, number>;
  ts_ms: number;
};

export async function getStateEmbedding() {
  return request<StateEmbedding>("/liquidity/research/state-embedding");
}

export type AnomalyKind =
  | "structural_break" | "regime_collapse" | "venue_divergence"
  | "pre_cascade" | "edge_inversion" | "regime_emergence";

export type AnomalyMemoryItem = {
  id: number;
  kind: AnomalyKind | string;
  severity: string;
  occurred_at_ms: number;
  fingerprint: Record<string, number>;
  novelty_score: number;
  recurrence_count: number;
  notes: string | null;
};

export type AnomalyMemoryOut = { items: AnomalyMemoryItem[]; counts_by_kind: Record<string, number> };

export async function listAnomalyMemory(opts: { kind?: AnomalyKind; since_ms?: number; limit?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.kind) usp.set("kind", opts.kind);
  if (opts.since_ms != null) usp.set("since_ms", String(opts.since_ms));
  if (opts.limit != null) usp.set("limit", String(opts.limit));
  return request<AnomalyMemoryOut>(`/liquidity/research/anomaly-memory${usp.toString() ? `?${usp}` : ""}`);
}

export async function recordAnomaly(payload: {
  kind: AnomalyKind;
  severity?: string;
  fingerprint: Record<string, number>;
  related_alert_ids?: string[];
  notes?: string;
}) {
  return request<AnomalyMemoryItem & { best_match_id: number | null; best_match_distance: number | null }>(
    "/liquidity/research/anomaly-memory",
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export type EdgeMutationKind = {
  kind: string;
  recent_precision: number | null;
  prior_precision: number | null;
  recent_resolved: number;
  prior_resolved: number;
  delta: number | null;
  mutation_velocity_per_day: number | null;
  mutation_score: number;
  mutation_direction: "STRENGTHENING" | "WEAKENING" | "INVERTED" | "NEUTRAL";
  inverted: boolean;
};

export type EdgeMutationOut = { since_ms: number; window_days: number; kinds: EdgeMutationKind[] };

export async function getEdgeMutation(opts: { since_ms?: number; window_days?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.since_ms != null) usp.set("since_ms", String(opts.since_ms));
  if (opts.window_days != null) usp.set("window_days", String(opts.window_days));
  return request<EdgeMutationOut>(`/liquidity/research/edge-mutation${usp.toString() ? `?${usp}` : ""}`);
}

export type RegimeCompressionCell = { a: string; b: string; cosine: number; distance: number };
export type RegimeCompressionMerge = { a: string; b: string; cosine: number };
export type RegimeCompression = {
  since_ms: number;
  regimes: string[];
  matrix: RegimeCompressionCell[];
  merge_candidates: RegimeCompressionMerge[];
};

export async function getRegimeCompression(since_ms?: number) {
  const usp = new URLSearchParams();
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<RegimeCompression>(`/liquidity/research/regime-compression${usp.toString() ? `?${usp}` : ""}`);
}

export type MetaHealth = {
  meta_intelligence_health: number;
  state: "HEALTHY" | "DRIFTING" | "DEGRADING" | "CRITICAL";
  self_consistency_score: number;
  adaptation_quality: number;
  components: {
    meta_confidence: number;
    structural_stability: number;
    alert_saturation: number;
    edge_consistency: number;
    regime_focus: number;
  };
  alert_saturation_ratio: number;
  avg_distinct_regimes_per_day: number;
  mutation_magnitude_sum: number;
};

export async function getMetaHealth() {
  return request<MetaHealth>("/liquidity/research/meta-intelligence-health");
}

// ── Phase-12 coordination & synthesis ─────────────────────────────────────

export type SynthesisLayer = { name: string; score: number; weight: number; delta_from_mean: number };
export type Synthesis = {
  fetched_at_ms: number;
  synthesized_stress: number;
  coordinated_state:
    | "STABLE_COORDINATED_MARKET"
    | "EARLY_STRUCTURAL_STRESS"
    | "STRUCTURAL_MARKET_DETERIORATION"
    | "FRAGMENTING_LIQUIDITY_ENVIRONMENT"
    | "ESCALATING_SYSTEMIC_INSTABILITY"
    | "ACTIVE_CASCADE_PROPAGATION";
  cross_layer_agreement: number;
  layers: SynthesisLayer[];
  components: {
    stress_level: string;
    shift_warning: string;
    structural_break_score: number;
    meta_confidence_state: string;
    intelligence_health_state: string;
    strategic_state: string;
  };
};

export async function getSynthesis() {
  return request<Synthesis>("/liquidity/research/synthesis");
}

export type ConflictItem = {
  kind: string;
  description: string;
  ops_score?: number;
  structural_score?: number;
  dominant_horizon?: string;
  shift_probability?: number;
  confidence_deficit?: number;
};

export type Conflicts = {
  fetched_at_ms: number;
  conflict_score: number;
  conflicts: ConflictItem[];
  dominant_layer: string;
  suppressed_layers: string[];
};

export async function getConflicts() {
  return request<Conflicts>("/liquidity/research/conflicts");
}

export type SuppressionCluster = {
  cluster_id: number;
  symbol: string;
  kind: string;
  count: number;
  max_severity: string;
  last_seen_ms: number;
  redundancy_score: number;
};

export type Suppression = {
  window_minutes: number;
  total_alerts: number;
  unique_clusters: number;
  alert_compression_ratio: number;
  redundant_clusters: SuppressionCluster[];
};

export async function getAlertSuppression(window_minutes = 60) {
  return request<Suppression>(`/liquidity/research/alert-suppression?window_minutes=${window_minutes}`);
}

export type CrisisCluster = {
  cluster_id: number;
  size: number;
  dominant_kind: string;
  kinds: Record<string, number>;
  frequency_per_day: number;
  earliest_ts: number;
  latest_ts: number;
  avg_novelty: number;
  centroid: Record<string, number>;
  recent_members: { id: number; kind: string; ts: number; novelty: number }[];
};

export type CrisisClusters = { clusters: CrisisCluster[]; anomaly_count: number };

export async function getCrisisClusters(max_clusters = 8) {
  return request<CrisisClusters>(`/liquidity/research/crisis-clusters?max_clusters=${max_clusters}`);
}

export type NarrativeMetricChange = {
  metric: string;
  h1: number | null;
  h24: number | null;
  d7: number | null;
  change_1h_vs_24h_pct: number | null;
  change_24h_vs_7d_pct: number | null;
};

export type NarrativeEvolution = {
  fetched_at_ms: number;
  horizons: { label: string; window_ms: number }[];
  metric_changes: NarrativeMetricChange[];
  short_term_bullets: string[];
  long_term_bullets: string[];
};

export async function getNarrativeEvolution() {
  return request<NarrativeEvolution>("/liquidity/research/narrative-evolution");
}

export type MultiHorizon = {
  fetched_at_ms: number;
  scores: { short: number | null; medium: number | null; long: number | null };
  horizon_alignment_score: number | null;
  horizon_conflict_map: { short_vs_medium: number | null; short_vs_long: number | null; medium_vs_long: number | null };
  dominant_horizon: string | null;
  structural_alignment_state: "ALIGNED" | "DIVERGENT" | "FRAGMENTED" | "INSUFFICIENT_DATA";
};

export async function getMultiHorizon() {
  return request<MultiHorizon>("/liquidity/research/multi-horizon");
}

export type AutoAnomalyDecision = {
  kind: string;
  action: "recorded" | "cooldown" | "below_threshold" | "error";
  score?: number;
  state?: string;
  error?: string;
  regime?: string;
  alert_kind?: string;
  pct?: number;
  next_eligible_in_ms?: number;
};

export type AutoAnomalyOut = {
  fetched_at_ms: number;
  inserted: { id: number; kind: string; severity: string; occurred_at_ms: number; novelty_score: number; recurrence_count: number }[];
  decisions: AutoAnomalyDecision[];
};

export async function triggerAutoAnomalyScan() {
  return request<AutoAnomalyOut>("/liquidity/research/auto-anomaly-scan", { method: "POST" });
}

// ── Phase-13 market memory & evolution ────────────────────────────────────

export type AnomalyNode = {
  id: number;
  kind: string;
  severity: string;
  occurred_at_ms: number;
  novelty_score: number;
  recurrence_count: number;
  fingerprint: Record<string, number>;
};

export type AnomalyEdge = {
  from_id: number;
  to_id: number;
  kind: "caused_by" | "evolved_into" | "historically_similar" | "preceded" | "destabilized" | "stabilized" | string;
  weight: number;
  depth?: number;
};

export type AnomalyLineage = {
  id: number;
  root: AnomalyNode | null;
  parents: AnomalyNode[][];
  descendants: AnomalyNode[][];
  edges: AnomalyEdge[];
  lineage_depth: number;
  neighborhood_size: number;
};

export async function getAnomalyLineage(anomaly_id: number, depth = 3) {
  return request<AnomalyLineage>(`/liquidity/research/anomaly-lineage/${anomaly_id}?depth=${depth}`);
}

export type MemoryGraph = {
  nodes: AnomalyNode[];
  edges: AnomalyEdge[];
  edge_counts_by_kind: Record<string, number>;
};

export async function getMemoryGraph(limit_nodes = 100) {
  return request<MemoryGraph>(`/liquidity/research/memory-graph?limit_nodes=${limit_nodes}`);
}

export type CrisisEvolutionState = {
  state: string;
  persist_count: number;
  escalate_count: number;
  stabilize_count: number;
  total_transitions: number;
  escalation_prob: number;
  stabilization_prob: number;
};

export type CrisisEvolution = {
  since_ms: number;
  states: CrisisEvolutionState[];
  tree: { from_state: string; to_state: string; count: number }[];
};

export async function getCrisisEvolutionTree(lookback_days = 30) {
  return request<CrisisEvolution>(`/liquidity/research/crisis-evolution-tree?lookback_days=${lookback_days}`);
}

export type RegimeAncestry = {
  since_ms: number;
  nodes: { regime: string; count: number }[];
  edges: { parent_regime: string; child_regime: string; weight: number }[];
  dominant_lineage: string[];
};

export async function getRegimeAncestry(lookback_days = 30) {
  return request<RegimeAncestry>(`/liquidity/research/regime-ancestry?lookback_days=${lookback_days}`);
}

export type EdgeLineage = {
  kind: string;
  since_ms: number;
  bucket_days: number;
  series: { bucket_ts: number; precision: number | null; total: number }[];
  phases: { phase: "STRENGTHENING" | "DEGRADATION" | "INVERTED" | "STEADY" | string; start_ts: number; end_ts: number }[];
  origin_ts: number | null;
};

export async function getEdgeLineage(kind: string, opts: { lookback_days?: number; bucket_days?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  if (opts.bucket_days != null) usp.set("bucket_days", String(opts.bucket_days));
  return request<EdgeLineage>(`/liquidity/research/edge-lineage/${encodeURIComponent(kind)}${usp.toString() ? `?${usp}` : ""}`);
}

export type IntelligenceSnapshotRow = {
  ts_ms: number;
  synthesized_stress: number | null;
  coordinated_state: string | null;
  cross_layer_agreement: number | null;
  structural_break_score: number | null;
  meta_confidence_score: number | null;
  meta_intelligence_health: number | null;
  health_state: string | null;
  risk_state_score: number | null;
  regime_shift_probability: number | null;
  dominant_regime: string | null;
};

export type IntelligenceHistory = { since_ms: number | null; count: number; series: IntelligenceSnapshotRow[] };

export async function getIntelligenceHistory(since_ms?: number, limit = 500) {
  const usp = new URLSearchParams({ limit: String(limit) });
  if (since_ms != null) usp.set("since_ms", String(since_ms));
  return request<IntelligenceHistory>(`/liquidity/research/intelligence-history?${usp.toString()}`);
}

export type CycleRun = {
  phase: string;
  start_ts: number;
  end_ts: number;
  duration_ms: number;
  open?: boolean;
};

export type MarketCycle = {
  since_ms: number;
  runs: CycleRun[];
  current_phase: string | null;
  avg_duration_ms_per_phase: Record<string, number | null>;
  transition_matrix: { from_phase: string; to_phase: string; count: number; probability: number }[];
};

export async function getMarketCycle(lookback_days = 60) {
  return request<MarketCycle>(`/liquidity/research/market-cycle?lookback_days=${lookback_days}`);
}

export type NarrativeChronicle = {
  since_ms: number;
  summary: string;
  highlights: string[];
  anomaly_count: number;
  anomaly_counts_by_kind?: Record<string, number>;
  first_state?: string | null;
  last_state?: string | null;
};

export async function getNarrativeChronicle(lookback_days = 21) {
  return request<NarrativeChronicle>(`/liquidity/research/narrative-chronicle?lookback_days=${lookback_days}`);
}

// ── Phase-14 discovery ────────────────────────────────────────────────────

export type RobustnessFlag =
  | "LOW_SUPPORT"
  | "HIGH_LIFT_LOW_SUPPORT"
  | "SINGLE_WINDOW"
  | "LOW_RECURRENCE"
  | "REGIME_FRAGILE"
  | "BUCKET_SENSITIVE";

export type DiscoveredPattern = {
  discovered_pattern_id: string;
  // tertile per metric; "na" when a metric wasn't populated in the
  // bucket (e.g. WS-only metric on a non-pinned symbol).
  signature: Record<string, "low" | "mid" | "high" | "na">;
  support: number;
  outcome_rate: number;
  lift: number | null;
  effective_lift: number;
  dominant_alert_kind: string | null;
  dominant_alert_count: number;
  novelty_score: number;
  stability_score: number;
  pattern_confidence: number;
  robustness_flags: RobustnessFlag[];
  suppressed_reason: string | null;
  day_span: number;
  first_half_support: number;
  second_half_support: number;
};

export type DataQuality = "HIGH" | "MEDIUM" | "LOW" | "INSUFFICIENT";

export type PatternDiscovery = {
  since_ms: number;
  min_support: number;
  bucket_minutes: number;
  metrics: string[];
  base_rate: number;
  total_buckets: number;
  patterns: DiscoveredPattern[];
  data_quality: DataQuality;
  suppressed_count: number;
  scarcity_factor: number;
};

export async function getPatternDiscovery(opts: { since_ms?: number; min_support?: number; bucket_minutes?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.since_ms != null) usp.set("since_ms", String(opts.since_ms));
  if (opts.min_support != null) usp.set("min_support", String(opts.min_support));
  if (opts.bucket_minutes != null) usp.set("bucket_minutes", String(opts.bucket_minutes));
  return request<PatternDiscovery>(`/liquidity/research/pattern-discovery${usp.toString() ? `?${usp}` : ""}`);
}

export type Archetype = {
  archetype_id: string;
  archetype_label: string;
  cluster_id: number;
  size: number;
  dominant_kind: string;
  kinds: Record<string, number>;
  frequency_per_day: number;
  avg_novelty: number;
  escalation_profile: string;
  recovery_probability: number;
  structural_severity: number;
  centroid: Record<string, number>;
};

export type CrisisArchetypes = { archetypes: Archetype[]; anomaly_count: number; vocabulary: string[]; data_quality: DataQuality };

export async function getCrisisArchetypes(max_archetypes = 8) {
  return request<CrisisArchetypes>(`/liquidity/research/crisis-archetypes?max_archetypes=${max_archetypes}`);
}

export type HiddenRegimeCluster = {
  cluster_id: number;
  label_hint: string;
  size: number;
  dominant_coordinated_state: string | null;
  earliest_ts: number;
  latest_ts: number;
  centroid: Record<string, number>;
  emergent_regime_score: number;
  regime_stability: number;
  is_emergent: boolean;
};

export type HiddenRegimes = { since_ms: number; snapshot_count: number; clusters: HiddenRegimeCluster[]; data_quality: DataQuality };

export async function getHiddenRegimes(opts: { lookback_days?: number; max_clusters?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  if (opts.max_clusters != null) usp.set("max_clusters", String(opts.max_clusters));
  return request<HiddenRegimes>(`/liquidity/research/hidden-regimes${usp.toString() ? `?${usp}` : ""}`);
}

export type PropagationEdge = {
  from_symbol: string;
  to_symbol: string;
  count: number;
  avg_lead_ms: number;
  avg_lead_s: number;
  lead_std_s: number;
  confidence: "HIGH" | "MEDIUM" | "LOW";
  confidence_score: number;
  volume_strength: number;
  lead_clarity: number;
  lead_consistency: number;
  temporal_consistency: number;
  recurrence_stability: number;
  symmetry_penalty: number;
  leader_stability: number;
  base_confidence: number;
};

export type PropagationNode = {
  symbol: string;
  out_count: number;
  in_count: number;
  net_lead: number;
  leader_stability: number;
  follower_stability: number;
};

export type PropagationIntegrityComponents = {
  avg_confidence: number;
  symmetric_share: number;
  weak_share: number;
  coverage: number;
};

export type Propagation = {
  since_ms: number;
  lead_window_ms: number;
  edges: PropagationEdge[];
  nodes: PropagationNode[];
  systemic_contagion_score: number;
  average_propagation_velocity_s: number | null;
  total_alerts: number;
  integrity_score: number;
  integrity_components: PropagationIntegrityComponents | null;
};

export async function getPropagation(opts: { lookback_days?: number; lead_window_minutes?: number; min_lead_seconds?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  if (opts.lead_window_minutes != null) usp.set("lead_window_minutes", String(opts.lead_window_minutes));
  if (opts.min_lead_seconds != null) usp.set("min_lead_seconds", String(opts.min_lead_seconds));
  return request<Propagation>(`/liquidity/research/propagation${usp.toString() ? `?${usp}` : ""}`);
}

export type EvolutionaryBehavior = {
  lookback_days: number;
  bucket_days: number;
  behavioral_shift_rate: number;
  instability_acceleration: number;
  structural_maturity_score: number;
  evolutionary_state: "DETERIORATION" | "INSTABILITY_GROWTH" | "SLOW_MUTATION" | "STABLE_MATURATION";
  bad_directions: number;
  metric_trends: EvolutionMetric[];
  regime_entropy_series: EntropyPoint[];
};

export async function getEvolutionaryBehavior(opts: { lookback_days?: number; bucket_days?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  if (opts.bucket_days != null) usp.set("bucket_days", String(opts.bucket_days));
  return request<EvolutionaryBehavior>(`/liquidity/research/evolutionary-behavior${usp.toString() ? `?${usp}` : ""}`);
}

export type AbstractionRow = {
  archetype_id: string;
  label: string;
  share_of_memory: number;
  members: number;
  frequency_per_day: number;
  structural_severity: number;
};

export type MemoryAbstraction = {
  total_anomalies: number;
  abstractions: AbstractionRow[];
  memory_density_score: number;
};

export async function getMemoryAbstraction() {
  return request<MemoryAbstraction>("/liquidity/research/memory-abstraction");
}

export type ForecastEntry = {
  metric: string;
  current: number;
  slope_per_day: number;
  raw_slope_per_day: number;
  slope_capped: boolean;
  extrapolation_capped: boolean;
  slope_consistency: number;
  forecast_in_days: number;
  forecast_value: number;
  rmse: number;
  horizon_decay: number;
  confidence: number;
  data_quality: DataQuality;
};

export type IntelligenceForecast = {
  horizon_days: number;
  forecasts: ForecastEntry[];
  trajectory: "ESCALATING" | "DEESCALATING" | "STEADY" | "DRIFTING" | "UNKNOWN" | null;
  snapshot_count: number;
  data_quality: DataQuality;
};

export async function getIntelligenceForecast(horizon_days = 7) {
  return request<IntelligenceForecast>(`/liquidity/research/intelligence-forecast?horizon_days=${horizon_days}`);
}

export type AdaptationRec = {
  action: "STRENGTHEN" | "WEAKEN" | "TIGHTEN_THRESHOLD" | "LOOSEN_THRESHOLD" | "REWEIGHT_UNSTABLE" | "STRENGTHEN_EDGE";
  target: string;
  rationale: string;
  importance_shift: number;
  // Present only when the Phase-16 adaptation loop scaled this row.
  raw_importance_shift?: number | null;
};

export type AdaptationOut = {
  fetched_at_ms: number;
  recommendations: AdaptationRec[];
  adaptation_score: number;
};

export async function getAdaptationRecommendations() {
  return request<AdaptationOut>("/liquidity/research/adaptation-recommendations");
}

export type SanityCategory =
  | "validation"
  | "propagation"
  | "discovery"
  | "forecast"
  | "regime"
  | "adaptation"
  | "anomaly"
  | "general";

export type SanityTrend =
  | "NEW"
  | "WORSENING"
  | "STABILIZING"
  | "RECURRING"
  | "CHRONIC"
  | "TRANSIENT";

export type CausalVerdict =
  | "DIRECTIONAL"
  | "COMMON_DRIVEN"
  | "COINCIDENCE"
  | "UNDER_EVIDENCED"
  | "AMBIGUOUS"
  | "EXPLORATORY";

export type CausalRole =
  | "LEADER"
  | "AMPLIFIER"
  | "FOLLOWER"
  | "INSTABILITY_HUB"
  | "ISOLATED";

export type CausalEdge = {
  from_symbol: string;
  to_symbol: string;
  count: number;
  reverse_count: number;
  asymmetry: number;
  evidence_count: number;
  n_windows: number;
  symmetry_penalty: number;
  common_driver: string | null;
  common_driver_strength: number;
  causal_confidence: number;
  verdict: CausalVerdict;
  rationale: string;
  factors: {
    volume: number;
    asymmetry: number;
    evidence: number;
    common_driver_penalty: number;
    symmetry: number;
    scarcity: number;
  };
};

export type CausalNode = {
  symbol: string;
  out_count: number;
  in_count: number;
  avg_out_confidence: number;
  avg_in_confidence: number;
  stability: number;
  role: CausalRole;
  role_rationale: string;
};

export type CausalPropagation = {
  since_ms: number;
  lookback_days: number;
  n_windows: number;
  total_alerts: number;
  edges: CausalEdge[];
  nodes: CausalNode[];
  verdict_counts: Partial<Record<CausalVerdict, number>>;
  role_counts: Partial<Record<CausalRole, number>>;
  data_quality: DataQuality;
};

export async function getCausalPropagation(opts: { lookback_days?: number; n_windows?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  if (opts.n_windows != null) usp.set("n_windows", String(opts.n_windows));
  return request<CausalPropagation>(`/liquidity/research/causal-propagation${usp.toString() ? `?${usp}` : ""}`);
}

export type InfluenceChain = {
  path: string[];
  min_confidence: number;
  step_confidences: number[];
  rationale: string;
};

export type DependencyCluster = {
  cluster_id: number;
  cluster_type: "common_driver";
  driver: string;
  members: string[];
  size: number;
  min_driver_strength: number;
  rationale: string;
};

export type DominantDriver = {
  symbol: string;
  reach_depth: number;
  reach_size: number;
  direct_out_count: number;
  avg_out_confidence: number;
  influence_score: number;
  reachable_sample: string[];
  rationale: string;
};

export type SynchronizedGroup = {
  group_id: number;
  members: string[];
  size: number;
  coincidence_edges: number;
  rationale: string;
};

export type StructuralDependencies = {
  since_ms: number;
  lookback_days: number;
  data_quality: DataQuality;
  exploratory: boolean;
  directional_edge_count: number;
  common_driven_edge_count: number;
  coincidence_edge_count: number;
  influence_chains: InfluenceChain[];
  dependency_clusters: DependencyCluster[];
  dominant_drivers: DominantDriver[];
  synchronized_groups: SynchronizedGroup[];
  summary: string;
};

export async function getStructuralDependencies(opts: { lookback_days?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  return request<StructuralDependencies>(`/liquidity/research/structural-dependencies${usp.toString() ? `?${usp}` : ""}`);
}

export type TransitionVerdict =
  | "PERSISTENT"
  | "ACCELERATING"
  | "FLICKER"
  | "REVERSED";

export type MarketTransition = {
  ts_ms: number;
  from_state: string;
  to_state: string;
  persistence_snapshots: number;
  persistence_seconds: number;
  was_reverted: boolean;
  pre_stress_slope: number | null;
  post_stress_slope: number | null;
  acceleration: number | null;
  meta_confidence_at: number;
  verdict: TransitionVerdict;
  confidence: number;
  rationale: string;
};

export type OscillationPeriod = {
  start_ms: number;
  end_ms: number;
  transition_count: number;
  rationale: string;
};

export type MarketStateTransitions = {
  since_ms: number;
  lookback_days: number;
  data_quality: DataQuality;
  exploratory: boolean;
  snapshot_count: number;
  state_vocabulary: string[];
  state_counts: Record<string, number>;
  current_state: string | null;
  current_state_duration_snapshots: number;
  current_state_duration_seconds: number;
  transition_count: number;
  flicker_count: number;
  flicker_ratio: number;
  transition_rate_per_day: number;
  transitions: MarketTransition[];
  oscillation_periods: OscillationPeriod[];
  summary: string;
};

export async function getMarketStateTransitions(opts: { lookback_days?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  return request<MarketStateTransitions>(`/liquidity/research/state-transitions${usp.toString() ? `?${usp}` : ""}`);
}

export type CrisisGenesisStatus = "calm" | "elevated" | "hot" | "insufficient";
export type CrisisGenesisVerdict =
  | "CALM"
  | "EARLY_DISTORTION"
  | "ELEVATED_RISK"
  | "PRE_CASCADE"
  | "INSUFFICIENT";

export type CrisisGenesisProbe = {
  kind: string;
  name: string;
  score: number;
  status: CrisisGenesisStatus;
  rationale: string;
  metric_value: number | null;
  contributes: boolean;
};

export type CrisisGenesis = {
  fetched_at_ms: number;
  lookback_days: number;
  genesis_score: number;
  verdict: CrisisGenesisVerdict;
  confidence: number;
  probe_count: number;
  hot_count: number;
  elevated_count: number;
  calm_count: number;
  insufficient_count: number;
  probes: CrisisGenesisProbe[];
  summary: string;
};

export async function getCrisisGenesis(opts: { lookback_days?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  return request<CrisisGenesis>(`/liquidity/research/crisis-genesis${usp.toString() ? `?${usp}` : ""}`);
}

export type NarrativeSection = {
  kind: "state" | "propagation" | "structural" | "genesis" | "uncertainty";
  title: string;
  text: string;
  confidence: number | null;
};

export type NarrativeCausality = {
  fetched_at_ms: number;
  lookback_days: number;
  headline: string;
  verdict: CrisisGenesisVerdict;
  overall_confidence: number;
  sections: NarrativeSection[];
  paragraph: string;
};

export async function getNarrativeCausality(opts: { lookback_days?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  return request<NarrativeCausality>(`/liquidity/research/narrative-causality${usp.toString() ? `?${usp}` : ""}`);
}

export type AdaptationAuditEntry = {
  layer: string;
  reason: string;
  old_value: number;
  new_value: number;
  raw_unclipped: number;
  input_value: unknown;
  trigger_threshold: unknown;
  input_confidence: number;
  ts_ms: number;
  applied_at: string | null;
};

export type AdaptationUpstreamSnapshot = {
  narrative_overall_confidence: number;
  genesis_score: number;
  genesis_verdict: string | null;
  transitions_flicker_ratio: number;
  transitions_oscillating: boolean;
  sanity_overall_state: string;
  sanity_overall_score: number;
  meta_confidence_score: number | null;
  structural_break_score: number | null;
};

export type AdaptationState = {
  fetched_at_ms: number;
  lookback_days: number;
  modifiers: Record<string, number>;
  audit_trail: AdaptationAuditEntry[];
  summary: string;
  upstream_snapshot: AdaptationUpstreamSnapshot;
};

export async function getAdaptationState(opts: { lookback_days?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  return request<AdaptationState>(`/liquidity/research/adaptation-state${usp.toString() ? `?${usp}` : ""}`);
}

export type AdaptedRecsOut = AdaptationOut & {
  discovery_suppression_modifier: number;
  modifier_applied: boolean;
  modifier_reason: string | null;
};

export async function getAdaptedRecommendations(opts: { lookback_days?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  return request<AdaptedRecsOut>(`/liquidity/research/adapted-recommendations${usp.toString() ? `?${usp}` : ""}`);
}

export type OperatorEscalation = "NORMAL" | "WATCH" | "IMPORTANT" | "CRITICAL";
export type OperatorLifecycle =
  | "NEW"
  | "WORSENING"
  | "STABILIZING"
  | "PERSISTENT"
  | "RESOLVED";

export type OperatorAckAction = "ack" | "ignore" | "resolve" | "mute";

export type OperatorAck = {
  action: OperatorAckAction;
  created_at_ms: number;
  expires_at_ms: number | null;
  note: string | null;
};

export type OperatorPriorityItem = {
  key: string;
  source_layer: "sanity" | "genesis" | "transitions" | "causal" | "structural" | "adaptation";
  kind: string;
  headline: string;
  detail: string;
  rationale: string;
  severity_raw: number;
  confidence: number;
  recency: number;
  source_weight: number;
  priority_score: number;
  escalation_state: OperatorEscalation;
  lifecycle: OperatorLifecycle;
  priority_delta: number | null;
  members: string[];
  extra: Record<string, unknown>;
  // Pass-B fields:
  first_seen_at_ms?: number | null;
  occurrence_count?: number | null;
  ack?: OperatorAck | null;
};

export type OperatorPriorities = {
  fetched_at_ms: number;
  lookback_days: number;
  attention_budget: number;
  snapshot_fresh: boolean;
  items: OperatorPriorityItem[];
  resolved: OperatorPriorityItem[];
  filtered_count: number;
  total_items: number;
  escalation_counts: Record<OperatorEscalation, number>;
  summary: string;
  narrative_headline: string | null;
};

export async function getOperatorPriorities(opts: { lookback_days?: number; attention_budget?: number } = {}) {
  const usp = new URLSearchParams();
  if (opts.lookback_days != null) usp.set("lookback_days", String(opts.lookback_days));
  if (opts.attention_budget != null) usp.set("attention_budget", String(opts.attention_budget));
  return request<OperatorPriorities>(`/liquidity/research/operator-priorities${usp.toString() ? `?${usp}` : ""}`);
}

export async function postOperatorAck(
  priority_key: string,
  payload: { action: OperatorAckAction; note?: string; mute_minutes?: number },
) {
  return request<{ priority_key: string; action: OperatorAckAction; created_at_ms: number; expires_at_ms: number | null; note: string | null }>(
    `/liquidity/research/operator-priorities/${encodeURIComponent(priority_key)}/ack`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export type OperatorPriorityEventOut = {
  ts_ms: number;
  event_type: string;
  priority_before: number | null;
  priority_after: number | null;
  escalation_before: string | null;
  escalation_after: string | null;
  note: string | null;
};

export type OperatorPriorityHistoryRow = {
  source_layer: string;
  kind: string;
  headline: string;
  current_status: string;
  current_escalation: OperatorEscalation;
  current_lifecycle: OperatorLifecycle;
  priority_score: number;
  peak_priority_score: number;
  peak_escalation: OperatorEscalation;
  first_seen_at_ms: number;
  last_seen_at_ms: number;
  resolved_at_ms: number | null;
  occurrence_count: number;
};

export type OperatorAckHistoryEntry = {
  action: OperatorAckAction;
  created_at_ms: number;
  expires_at_ms: number | null;
  note: string | null;
  active: boolean;
};

export type OperatorEscalationHistory = {
  priority_key: string;
  found: boolean;
  history: OperatorPriorityHistoryRow | null;
  events: OperatorPriorityEventOut[];
  acknowledgements: OperatorAckHistoryEntry[];
};

export async function getOperatorEscalationHistory(priority_key: string, limit = 50) {
  return request<OperatorEscalationHistory>(
    `/liquidity/research/operator-priorities/${encodeURIComponent(priority_key)}/history?limit=${limit}`,
  );
}

export type OperatorDigestEntry = {
  priority_key: string;
  headline: string;
  source_layer: string;
  event_type: string;
  ts_ms: number;
  priority_before: number | null;
  priority_after: number | null;
  escalation_before: string | null;
  escalation_after: string | null;
  note: string | null;
};

export type OperatorDigestActiveRow = {
  priority_key: string;
  headline: string;
  priority_score: number;
  source_layer: string;
  first_seen_at_ms: number;
};

export type OperatorDigest = {
  window_hours: number;
  since_ms: number;
  fetched_at_ms: number;
  new: OperatorDigestEntry[];
  worsened: OperatorDigestEntry[];
  stabilized: OperatorDigestEntry[];
  resolved: OperatorDigestEntry[];
  reappeared: OperatorDigestEntry[];
  active_critical: OperatorDigestActiveRow[];
  active_important: OperatorDigestActiveRow[];
  contributing_layers: Record<string, number>;
  summary: string;
};

export async function getOperatorDigest(window_hours = 24) {
  return request<OperatorDigest>(`/liquidity/research/operator-digest?window_hours=${window_hours}`);
}

export type SanityFinding = {
  kind: string;
  category: SanityCategory;
  severity: "critical" | "warn" | "info";
  severity_score: number;
  detail: string;
  metric_value: number;
  info_threshold: number;
  warn_threshold: number;
  critical_threshold: number;
  threshold_unit: string;
  trend: SanityTrend;
};

export type SanityAudit = {
  fetched_at_ms: number;
  overall_state: "CRITICAL" | "WARN" | "INFO" | "CLEAN";
  overall_score: number;
  findings: SanityFinding[];
  check_count: number;
};

export async function getSanityAudit() {
  return request<SanityAudit>("/liquidity/research/sanity-audit");
}
