import type { VercelRequest, VercelResponse } from "@vercel/node";
import { randomBytes } from "node:crypto";
import { getServerSupabase } from "../lib/supabase.js";

const UPSTOX_AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog";
const STATE_TTL_MS = 10 * 60 * 1000;

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
  if (req.method !== "GET" && req.method !== "POST") {
    res.setHeader("Allow", "GET, POST");
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

  const clientId = process.env.UPSTOX_CLIENT_ID;
  const redirectUri = process.env.UPSTOX_REDIRECT_URI;
  if (!clientId || !redirectUri) {
    res.status(500).json({ error: "server_misconfigured" });
    return;
  }

  const state = randomBytes(32).toString("base64url");
  const expiresAt = new Date(Date.now() + STATE_TTL_MS).toISOString();

  const sb = getServerSupabase();
  const { error: insertErr } = await sb.from("oauth_states").insert({
    user_id: userId,
    state,
    provider: "upstox",
    expires_at: expiresAt,
  });
  if (insertErr) {
    res.status(500).json({ error: "state_persistence_failed" });
    return;
  }

  const params = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri,
    state,
  });
  const authorizationUrl = `${UPSTOX_AUTH_URL}?${params.toString()}`;

  res.status(200).json({
    authorization_url: authorizationUrl,
    state,
    expires_at: expiresAt,
  });
}
