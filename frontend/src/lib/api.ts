export type Snapshot = {
  symbol: string;
  timeframe: string;
  price: number | null;
  direction: string;
  zone: "premium" | "discount" | "equilibrium" | "none" | string;
  in_ote: boolean;
  setup: "yes" | "no" | string;
  retracement: number | null;
  fib_low: number | null;
  fib_high: number | null;
  ote_low_price: number | null;
  ote_high_price: number | null;
  trend: string;
  updated_at: string;
};

export type DashboardRow = {
  symbol: string;
  price: number | null;
  global: Snapshot | null;
  local: Snapshot | null;
};

export type DashboardResponse = {
  rows: DashboardRow[];
  totals: { total: number; ote: number; discount: number; premium: number };
  last_refresh_at: string | null;
  last_error: string | null;
};

const TOKEN_KEY = "kazus_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
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

export async function login(username: string, password: string): Promise<string> {
  const res = await request<{ access_token: string }>("/auth/login-json", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  setToken(res.access_token);
  return res.access_token;
}

export async function listCoins() {
  return request<{ id: number; symbol: string; is_active: boolean }[]>("/coins");
}

export async function addCoin(symbol: string) {
  return request<{ id: number; symbol: string; is_active: boolean }>("/coins", {
    method: "POST",
    body: JSON.stringify({ symbol }),
  });
}

export async function removeCoin(symbol: string) {
  return request<void>(`/coins/${encodeURIComponent(symbol)}`, { method: "DELETE" });
}

export async function getDashboard() {
  return request<DashboardResponse>("/dashboard");
}
