import type { VercelRequest, VercelResponse } from "@vercel/node";
import { getServerSupabase } from "../lib/supabase";

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

  const sb = getServerSupabase();
  const { data, error } = await sb
    .from("broker_connections")
    .select("provider, obtained_at")
    .eq("user_id", userId)
    .eq("provider", "upstox")
    .maybeSingle();

  if (error) {
    res.status(500).json({ error: "query_failed" });
    return;
  }

  if (!data) {
    res.status(200).json({ connected: false });
    return;
  }

  res.status(200).json({
    connected: true,
    provider: data.provider,
    obtained_at: data.obtained_at,
  });
}
