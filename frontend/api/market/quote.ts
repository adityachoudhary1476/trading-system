import type { VercelRequest, VercelResponse } from "@vercel/node";
import { getServerSupabase } from "../lib/supabase.js";
import { decryptToken, TokenDecryptionError } from "../lib/crypto.js";
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

export interface UpstoxQuote {
  symbol: string;
  last_price: number;
  open?: number;
  high?: number;
  low?: number;
  close?: number;
  volume?: number;
  prev_close?: number;
  ohlc?: { open?: number; high?: number; low?: number; close?: number };
}

interface UpstoxQuoteResponse {
  data: Record<string, UpstoxQuote>;
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function pickFinite(...candidates: unknown[]): number | null {
  for (const c of candidates) {
    if (isFiniteNumber(c)) return c;
  }
  return null;
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate, proxy-revalidate");
  res.setHeader("Pragma", "no-cache");
  res.setHeader("Expires", "0");
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
      res.status(502).json({ error: "upstox_token_unreadable" });
      return;
    }
    const message = e instanceof Error ? e.message : "Unknown error";
    console.error("Quote token load failed:", { symbol, message });
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
    // Upstox V2 full-market-quote uses ``instrument_key`` as a comma-separated
    // query parameter. The colon/pipe key choice does not affect Upstox — it
    // is normalized by the symbol mapper above.
    const url = `${UPSTOX_BASE}/market-quote/quotes?instrument_key=${encodeURIComponent(upstoxSymbol)}`;
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
      console.error("Quote Upstox error:", {
        status: resp.status,
        symbol,
        upstoxSymbol,
        error: errorDetail,
      });
      res.status(502).json({ error: "upstox_api_error", message: errorDetail });
      return;
    }

    let json: UpstoxQuoteResponse;
    try {
      json = (await resp.json()) as UpstoxQuoteResponse;
    } catch {
      res.status(502).json({ error: "upstox_malformed_response" });
      return;
    }
    if (!json.data || Object.keys(json.data).length === 0) {
      res.status(404).json({ error: "symbol_not_found" });
      return;
    }
    // Upstox keys responses by ``EXCHANGE:SYMBOL`` (colon) in current V2;
    // older variants keyed by ``EXCHANGE|SYMBOL`` (pipe). Try both, then
    // fall back to the only/first entry.
    const upstoxKey = upstoxSymbol.replace("|", ":");
    const quote =
      json.data?.[upstoxKey] ??
      json.data?.[upstoxSymbol] ??
      Object.values(json.data)[0];
    if (!quote) {
      res.status(404).json({ error: "symbol_not_found" });
      return;
    }

    // last_price is the only field that MUST exist for a usable quote.
    // If it is missing or non-finite, the provider did not supply a trade
    // value — surface this as a 404 so the frontend shows "—" instead of a
    // fabricated 0.
    const price = pickFinite(quote.last_price);
    if (price === null) {
      res.status(404).json({ error: "upstox_price_unavailable" });
      return;
    }

    // Previous close: prefer explicit prev_close, then close, then ohlc.close.
    // No fabrication — if every candidate is missing/non-finite, return null
    // and the frontend will render the field as unavailable.
    const prevClose = pickFinite(
      quote.prev_close,
      quote.close,
      quote.ohlc?.close,
    );

    // OHLC values: prefer top-level fields, then the nested ``ohlc`` object.
    const dayOpen = pickFinite(quote.open, quote.ohlc?.open);
    const dayHigh = pickFinite(quote.high, quote.ohlc?.high);
    const dayLow = pickFinite(quote.low, quote.ohlc?.low);
    const volume = pickFinite(quote.volume);

    // --- Canonical timestamp semantics (Phase 2 correction) ---
    // marketTimestamp: when the EXCHANGE says the price occurred (authoritative
    // freshness source).  Upstox V2 quote object may carry a ``timestamp``
    // field (epoch seconds).  If absent or invalid we fall back to the server
    // fetch time so the UI never claims a precise market time it doesn't have.
    const fetchedAt = Date.now();
    const rawMarketTs = pickFinite(quote.timestamp);
    const marketTimestamp =
      rawMarketTs !== null ? Math.round(rawMarketTs * 1000) : fetchedAt;

    // Derive change/changePct ONLY when prevClose is a genuine number. When
    // it is unavailable, the fields are emitted as null so the existing
    // defensive renderer can show "—".
    const change = prevClose !== null ? price - prevClose : null;
    const changePct =
      prevClose !== null && prevClose !== 0 ? (change! / prevClose) * 100 : null;

    const response: Record<string, unknown> = {
      symbol,
      price,
      marketTimestamp,
      fetchedAt,
      sessionState: "REGULAR",
      lastUpdate: marketTimestamp,
    };
    if (prevClose !== null) {
      response.previousClose = prevClose;
      response.change = change !== null ? Math.round(change * 100) / 100 : null;
      response.changePct =
        changePct !== null ? Math.round(changePct * 100) / 100 : null;
    }
    if (dayOpen !== null) response.dayOpen = dayOpen;
    if (dayHigh !== null) response.dayHigh = dayHigh;
    if (dayLow !== null) response.dayLow = dayLow;
    if (volume !== null) response.volume = volume;
    res.status(200).json(response);
  } catch {
    res.status(502).json({ error: "upstox_request_failed" });
  }
}
