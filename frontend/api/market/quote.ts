import type { VercelRequest, VercelResponse } from "@vercel/node";
import { getServerSupabase } from "../lib/supabase.js";
import { decryptToken } from "../lib/crypto.js";
import { toUpstoxSymbol } from "../lib/symbol-map.js";

const UPSTOX_BASE = "https://api.upstox.com/v2";

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

export interface UpstoxQuote {
  symbol: string;
  last_price: number;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  volume?: number;
  prev_close?: number;
}

interface UpstoxQuoteResponse {
  data: Record<string, UpstoxQuote>;
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    res.status(405).json({ error: "method_not_allowed" });
    return;
  }

  const symbol = typeof req.query.symbol === "string" ? req.query.symbol : "";
  if (!symbol || !symbol.includes(":")) {
    res.status(400).json({ error: "invalid_symbol" });
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
    const url = `${UPSTOX_BASE}/market-quote/quotes?symbol=${encodeURIComponent(upstoxSymbol)}`;
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

    const json = (await resp.json()) as UpstoxQuoteResponse;
    if (!json.data || Object.keys(json.data).length === 0) {
      res.status(404).json({ error: "symbol_not_found" });
      return;
    }
    // Try exact instrument key first, then colon-separated fallback,
    // then fall back to the first entry (Upstox returns data keyed by instrument_key)
    const upstoxKey = upstoxSymbol.replace("|", ":");
    const quote =
      json.data?.[upstoxSymbol] ??
      json.data?.[upstoxKey] ??
      Object.values(json.data)[0];
    if (!quote) {
      res.status(404).json({ error: "symbol_not_found" });
      return;
    }

    const price = quote.last_price ?? 0;
    const prevClose = quote.prev_close ?? quote.close ?? price;
    const change = price - prevClose;
    const changePct = prevClose !== 0 ? (change / prevClose) * 100 : 0;

    res.status(200).json({
      symbol,
      price,
      previousClose: prevClose,
      change: Math.round(change * 100) / 100,
      changePct: Math.round(changePct * 100) / 100,
      dayOpen: quote.open ?? price,
      dayHigh: quote.high ?? price,
      dayLow: quote.low ?? price,
      volume: quote.volume ?? 0,
      lastUpdate: Date.now(),
    });
  } catch {
    res.status(502).json({ error: "upstox_request_failed" });
  }
}
