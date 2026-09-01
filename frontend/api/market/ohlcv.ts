import type { VercelRequest, VercelResponse } from "@vercel/node";
import { getServerSupabase } from "../lib/supabase.js";
import { decryptToken } from "../lib/crypto.js";
import { toUpstoxSymbol } from "../lib/symbol-map.js";

const UPSTOX_BASE = "https://api.upstox.com/v2";

const INTERVAL_MAP: Record<string, string> = {
  "1m": "1minute",
  "5m": "5minute",
  "15m": "15minute",
  "30m": "30minute",
  "60m": "1hour",
  "1h": "1hour",
  "1d": "day",
  "1D": "day",
  "1w": "week",
  "1M": "month",
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

async function getUpstoxAccessToken(userId: string): Promise<string | null> {
  const sb = getServerSupabase();
  const { data, error } = await sb
    .from("broker_connections")
    .select("access_token_encrypted")
    .eq("user_id", userId)
    .eq("provider", "upstox")
    .maybeSingle();

  if (error || !data?.access_token_encrypted) return null;
  return decryptToken(data.access_token_encrypted);
}

interface UpstoxCandleResponse {
  data: {
    candles: Array<[number, number, number, number, number, number, number]>;
  };
}

function formatDate(d: Date): string {
  return d.toISOString().slice(0, 10);
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
  const bars = Number.isFinite(barsRaw) && barsRaw > 0 ? Math.min(barsRaw, 500) : 160;

  if (!symbol || !symbol.includes(":")) {
    res.status(400).json({ error: "invalid_symbol" });
    return;
  }

  const interval = INTERVAL_MAP[timeframe];
  if (!interval) {
    res.status(400).json({ error: "invalid_timeframe" });
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

  const accessToken = await getUpstoxAccessToken(userId);
  if (!accessToken) {
    res.status(403).json({ error: "upstox_not_connected" });
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
    const endDate = new Date();
    const startDate = new Date();
    const days = interval === "day" ? bars : interval === "week" ? bars * 7 : interval === "month" ? bars * 30 : Math.ceil(bars / 390) + 1;
    startDate.setDate(startDate.getDate() - days);

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

    if (!resp.ok) {
      res.status(502).json({ error: "upstox_api_error" });
      return;
    }

    const json = (await resp.json()) as UpstoxCandleResponse;
    const candles = json.data?.candles ?? [];

    const ohlcv = candles.map((c) => ({
      time: c[0] * 1000,
      open: c[1],
      high: c[2],
      low: c[3],
      close: c[4],
      volume: c[5],
    }));

    res.status(200).json(ohlcv);
  } catch {
    res.status(502).json({ error: "upstox_request_failed" });
  }
}
