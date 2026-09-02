import type { VercelRequest, VercelResponse } from "@vercel/node";
import { getServerSupabase } from "../lib/supabase.js";
import { decryptToken, TokenDecryptionError } from "../lib/crypto.js";
import { toUpstoxSymbol } from "../lib/symbol-map.js";

const UPSTOX_BASE = "https://api.upstox.com/v2";

/**
 * Maps the internal timeframe vocabulary to Upstox V2 historical-candle
 * interval values.
 *
 * Upstox V2 historical-candle API supports ONLY the following intervals:
 *   1minute, 30minute, day, week, month
 *
 * 5-minute, 15-minute, 1-hour, etc. are NOT exposed by V2 — callers asking
 * for those timeframes get a clear 400 "unsupported_timeframe" response
 * rather than being silently coerced to a different interval.
 */
const INTERVAL_MAP: Record<string, { interval: string; daysPerBar: number }> = {
  "1m": { interval: "1minute", daysPerBar: 0 },
  "1D": { interval: "day", daysPerBar: 1 },
  "1d": { interval: "day", daysPerBar: 1 },
  "1W": { interval: "week", daysPerBar: 7 },
  "1w": { interval: "week", daysPerBar: 7 },
  "1M": { interval: "month", daysPerBar: 30 },
  "1MO": { interval: "month", daysPerBar: 30 },
  "1mo": { interval: "month", daysPerBar: 30 },
};

const SUPPORTED_INTERVALS = new Set(Object.values(INTERVAL_MAP).map((v) => v.interval));

const UPSTOX_RANGE_DAYS: Record<string, number> = {
  "1minute": 31,
  "30minute": 366,
  day: 366,
  week: 366 * 10,
  month: 366 * 10,
};

function getBearer(req: VercelRequest): string | null {
  const h = req.headers.authorization;
  if (!h || !h.startsWith("Bearer ")) return null;
  return h.slice(7).trim();
}

async function resolveUserId(bearer: string): Promise<string | null> {
  const sb = getServerSupabase();
  const { data, error } = await sb.auth.getUser(bearer);
  if (error || !data?.user) return null;
  return data.user.id;
}

interface BrokerRow {
  access_token_encrypted: string | null;
}

async function loadEncryptedToken(userId: string): Promise<string | null> {
  const sb = getServerSupabase();
  const { data, error } = await sb
    .from("broker_connections")
    .select("access_token_encrypted")
    .eq("user_id", userId)
    .eq("provider", "upstox")
    .maybeSingle();
  if (error) {
    throw new Error(`Failed to load broker connection: ${error.message}`);
  }
  if (!data) return null;
  const row = data as BrokerRow;
  if (!row.access_token_encrypted) return null;
  return row.access_token_encrypted;
}

function formatDate(d: Date): string {
  // Upstox V2 expects YYYY-MM-DD in UTC. Using UTC avoids local-timezone drift
  // around midnight.
  return d.toISOString().slice(0, 10);
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function parseCandle(raw: unknown): {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
} | null {
  if (!Array.isArray(raw) || raw.length < 6) return null;
  const [t, o, h, l, c, v] = raw;
  if (!isFiniteNumber(o) || !isFiniteNumber(h) || !isFiniteNumber(l) || !isFiniteNumber(c)) {
    return null;
  }
  // Upstox V2 example shows candle[0] as an ISO timestamp string, but the API
  // type-table lists it as a number. Accept both forms without fabricating.
  let time: number;
  if (typeof t === "string") {
    const parsed = Date.parse(t);
    if (!Number.isFinite(parsed)) return null;
    time = parsed;
  } else if (isFiniteNumber(t)) {
    // Heuristic: V1 numeric timestamps are seconds (10 digits) or
    // milliseconds (13 digits). Coerce seconds to ms.
    time = t < 1e12 ? Math.round(t * 1000) : Math.round(t);
  } else {
    return null;
  }
  return {
    time,
    open: o,
    high: h,
    low: l,
    close: c,
    volume: isFiniteNumber(v) ? v : 0,
  };
}

interface UpstoxCandleResponse {
  data?: {
    candles?: unknown[];
  };
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    res.status(405).json({ error: "method_not_allowed" });
    return;
  }

  const symbol = typeof req.query.symbol === "string" ? req.query.symbol : "";
  const timeframe = typeof req.query.timeframe === "string" ? req.query.timeframe : "1d";
  const barsRaw = typeof req.query.bars === "string" ? parseInt(req.query.bars, 10) : 160;
  const bars =
    Number.isFinite(barsRaw) && barsRaw > 0 ? Math.min(barsRaw, 500) : 160;

  if (!symbol || !symbol.includes(":")) {
    res.status(400).json({ error: "invalid_symbol" });
    return;
  }

  const mapping = INTERVAL_MAP[timeframe];
  if (!mapping || !SUPPORTED_INTERVALS.has(mapping.interval)) {
    res.status(400).json({ error: "unsupported_timeframe" });
    return;
  }

  const bearer = getBearer(req);
  if (!bearer) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }

  const userId = await resolveUserId(bearer);
  if (!userId) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }

  // Decrypt the stored Upstox access token. Any failure here is a
  // controlled, non-500 response: the user must reconnect Upstox to
  // re-encrypt the token with the current key.
  let accessToken: string;
  try {
    const encrypted = await loadEncryptedToken(userId);
    if (!encrypted) {
      res.status(403).json({ error: "upstox_not_connected" });
      return;
    }
    accessToken = decryptToken(encrypted);
  } catch (e) {
    if (e instanceof TokenDecryptionError) {
      // Upstox is "connected" in the database but the token cannot be
      // decrypted. Distinct error code so the frontend can show a
      // reconnect prompt instead of a generic server error.
      res.status(502).json({ error: "upstox_token_unreadable" });
      return;
    }
    const message = e instanceof Error ? e.message : "Unknown error";
    console.error("OHLCV token load failed:", { symbol, message });
    res.status(502).json({ error: "upstox_token_unavailable" });
    return;
  }

  let upstoxSymbol: string;
  try {
    upstoxSymbol = toUpstoxSymbol(symbol);
  } catch {
    res.status(400).json({ error: "invalid_symbol" });
    return;
  }

  try {
    const interval = mapping.interval;
    const maxRangeDays = UPSTOX_RANGE_DAYS[interval] ?? 366;
    const days = Math.min(
      maxRangeDays,
      Math.max(bars, 1) * Math.max(mapping.daysPerBar, 1) + 1,
    );
    const endDate = new Date();
    const startDate = new Date(endDate);
    startDate.setUTCDate(startDate.getUTCDate() - days);

    const path = `/historical-candle/${encodeURIComponent(upstoxSymbol)}/${interval}/${formatDate(endDate)}/${formatDate(startDate)}`;
    const url = `${UPSTOX_BASE}${path}`;

    const resp = await fetch(url, {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        Accept: "application/json",
      },
    });

    if (resp.status === 401 || resp.status === 403) {
      res.status(403).json({ error: "upstox_token_expired" });
      return;
    }
    if (resp.status === 429) {
      res.setHeader("Retry-After", resp.headers.get("retry-after") ?? "1");
      res.status(429).json({ error: "upstox_rate_limited" });
      return;
    }
    if (!resp.ok) {
      let errorDetail = `Upstox HTTP ${resp.status}`;
      try {
        const errorBody = (await resp.json()) as Record<string, unknown>;
        const msg = errorBody["message"] ?? errorBody["error_message"] ?? errorBody["info"];
        if (typeof msg === "string" && msg.length > 0) errorDetail = msg;
      } catch {
        // Response wasn't JSON; keep default errorDetail
      }
      console.error("OHLCV Upstox error:", {
        status: resp.status,
        symbol,
        upstoxSymbol,
        interval,
        error: errorDetail,
      });
      res.status(502).json({ error: "upstox_api_error", message: errorDetail });
      return;
    }

    let json: UpstoxCandleResponse;
    try {
      json = (await resp.json()) as UpstoxCandleResponse;
    } catch {
      res.status(502).json({ error: "upstox_malformed_response" });
      return;
    }
    const rawCandles = Array.isArray(json.data?.candles) ? json.data.candles : [];
    const ohlcv: Array<{
      time: number;
      open: number;
      high: number;
      low: number;
      close: number;
      volume: number;
    }> = [];
    for (const raw of rawCandles) {
      const candle = parseCandle(raw);
      if (!candle) continue;
      ohlcv.push(candle);
    }
    // Upstox returns candles newest-first. Return them oldest-first so the
    // chart's time axis is monotonic — but never invent candles, never
    // pad, never duplicate.
    ohlcv.sort((a, b) => a.time - b.time);
    res.status(200).json(ohlcv);
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    console.error("OHLCV request failed:", { symbol, upstoxSymbol, message });
    res.status(502).json({ error: "upstox_request_failed" });
  }
}

export const __TESTING__ = {
  INTERVAL_MAP,
  UPSTOX_RANGE_DAYS,
  parseCandle,
  formatDate,
};
