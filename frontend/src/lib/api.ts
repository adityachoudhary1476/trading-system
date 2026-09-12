// API client for terminal widgets - proxies to existing Finova API endpoints
// This is a thin wrapper around fetch to match the OpenTerminal pattern

export async function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }

  const contentType = res.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    throw new Error("API routing error: expected JSON response");
  }

  return (await res.json()) as T;
}

export async function apiPost<T>(path: string, body: unknown, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    body: JSON.stringify(body),
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }

  return (await res.json()) as T;
}

export function fmt(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}

export function fmtBig(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (v >= 1e7) return `${(v / 1e7).toFixed(2)} Cr`;
  if (v >= 1e5) return `${(v / 1e5).toFixed(2)} L`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(2)} K`;
  return v.toLocaleString("en-IN");
}

export function pctClass(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "dim";
  return v >= 0 ? "up" : "down";
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Quote {
  symbol: string;
  price: number;
  previousClose: number;
  change: number | null;
  changePercent: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  volume: number | null;
  avgVolume: number | null;
  marketCap: number | null;
  pe: number | null;
  eps: number | null;
  dividendYield: number | null;
  week52High: number | null;
  week52Low: number | null;
  beta: number | null;
  sharesOutstanding: number | null;
  name: string;
  exchange: string | null;
  currency: string | null;
  source: string;
  bid: number | null;
  ask: number | null;
}

export interface MarketQuote {
  symbol: string;
  providerSymbol: string;
  name: string;
  exchange: string;
  instrumentType: string;
  price: number;
  previousClose: number | undefined;
  change: number | undefined;
  changePct: number | undefined;
  dayOpen: number | undefined;
  dayHigh: number | undefined;
  dayLow: number | undefined;
  volume: number | undefined;
  vwap: number | undefined;
  dayRange: string;
  volatility: number | undefined;
  sessionState: string;
  lastUpdate: number;
  marketTimestamp: number;
  fetchedAt: number;
}