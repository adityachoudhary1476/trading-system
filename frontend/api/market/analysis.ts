import type { VercelRequest, VercelResponse } from "@vercel/node";
import { getServerSupabase } from "../lib/supabase.js";

const PYTHON_BACKEND_URL = process.env.PYTHON_BACKEND_URL || "http://localhost:8000";

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

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    res.status(405).json({ error: "method_not_allowed" });
    return;
  }

  const symbol = typeof req.query.symbol === "string" ? req.query.symbol : "";
  const timeframe = typeof req.query.timeframe === "string" ? req.query.timeframe : "1d";

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

  try {
    const url = `${PYTHON_BACKEND_URL}/api/market/analysis?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`;
    const resp = await fetch(url, {
      headers: {
        Authorization: `Bearer ${bearer}`,
        Accept: "application/json",
      },
    });

    if (!resp.ok) {
      const errorBody = (await resp.json().catch(() => ({}))) as Record<string, unknown>;
      res.status(resp.status).json({
        error: "analysis_error",
        message: (errorBody.detail as string) || `Backend returned ${resp.status}`,
      });
      return;
    }

    const data = await resp.json();
    res.status(200).json(data);
  } catch (err) {
    console.error("Analysis proxy error:", err);
    res.status(502).json({ error: "backend_unavailable" });
  }
}
