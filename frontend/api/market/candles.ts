import type { VercelRequest, VercelResponse } from "@vercel/node";
import { getServerSupabase } from "../lib/supabase.js";

const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || "http://localhost:8000";

function getBearer(req: VercelRequest): string | null {
  const value = req.headers.authorization;
  return value?.startsWith("Bearer ") ? value.slice(7).trim() : null;
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    res.status(405).json({ error: "method_not_allowed" });
    return;
  }
  const bearer = getBearer(req);
  const symbol = typeof req.query.symbol === "string" ? req.query.symbol : "";
  const timeframe = typeof req.query.timeframe === "string" ? req.query.timeframe : "1d";
  const limit = typeof req.query.limit === "string" ? req.query.limit : "160";
  if (!bearer) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }
  const sb = getServerSupabase();
  const { data, error } = await sb.auth.getUser(bearer);
  if (error || !data?.user || !symbol.includes(":")) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }
  try {
    const url = `${PYTHON_BACKEND_URL}/api/market/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=${encodeURIComponent(limit)}`;
    const response = await fetch(url, {
      headers: { Authorization: `Bearer ${bearer}`, Accept: "application/json" },
      cache: "no-store",
    });
    const body = await response.json().catch(() => ({}));
    res.status(response.status).json(body);
  } catch {
    res.status(502).json({ error: "backend_unavailable" });
  }
}