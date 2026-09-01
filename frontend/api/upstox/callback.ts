import type { VercelRequest, VercelResponse } from "@vercel/node";
import { getServerSupabase } from "../lib/supabase.js";
import { encryptToken } from "../lib/crypto.js";
import { validateOAuthState } from "../../src/lib/oauth-state-machine.js";

const UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token";

export class TokenExchangeError extends Error {}

export async function exchangeCodeForToken(params: {
  code: string;
  clientId: string;
  clientSecret: string;
  redirectUri: string;
}): Promise<string> {
  const body = new URLSearchParams({
    code: params.code,
    client_id: params.clientId,
    client_secret: params.clientSecret,
    redirect_uri: params.redirectUri,
    grant_type: "authorization_code",
  });

  const resp = await fetch(UPSTOX_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });

  if (!resp.ok) {
    throw new TokenExchangeError(`Upstox token exchange failed: HTTP ${resp.status}`);
  }

  const json = (await resp.json()) as { access_token?: string; [key: string]: unknown };
  if (!json.access_token) {
    throw new TokenExchangeError("Upstox response missing access_token");
  }
  return json.access_token;
}

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    res.status(405).json({ error: "method_not_allowed" });
    return;
  }

  const code = typeof req.query.code === "string" ? req.query.code : "";
  const state = typeof req.query.state === "string" ? req.query.state : "";
  const upstoxError = typeof req.query.error === "string" ? req.query.error : "";

  const fail = (reason: string) => {
    res.redirect(302, `/broker?connected=error&reason=${encodeURIComponent(reason)}`);
  };

  const sb = getServerSupabase();

  const result = await validateOAuthState({
    state,
    code,
    upstoxError,
    getSession: async () => {
      const { data, error } = await sb
        .from("oauth_states")
        .select("id, user_id, expires_at, consumed_at")
        .eq("state", state)
        .eq("provider", "upstox")
        .single();
      return { row: data, error: error?.message ?? null };
    },
  });

  if (!result.ok) {
    fail(result.reason ?? "invalid_state");
    return;
  }

  const consumeRes = await sb
    .from("oauth_states")
    .update({ consumed_at: new Date().toISOString() })
    .eq("id", result.stateId!);
  if (consumeRes.error) {
    fail("state_consume_failed");
    return;
  }

  const clientId = process.env.UPSTOX_CLIENT_ID;
  const clientSecret = process.env.UPSTOX_CLIENT_SECRET;
  const redirectUri = process.env.UPSTOX_REDIRECT_URI;
  if (!clientId || !clientSecret || !redirectUri) {
    fail("server_misconfigured");
    return;
  }

  let accessToken: string;
  try {
    accessToken = await exchangeCodeForToken({ code: code!, clientId, clientSecret, redirectUri });
  } catch {
    fail("token_exchange_failed");
    return;
  }

  const encrypted = encryptToken(accessToken);
  const obtainedAt = new Date().toISOString();

  const upsertRes = await sb
    .from("broker_connections")
    .upsert(
      {
        user_id: result.userId!,
        provider: "upstox",
        access_token_encrypted: encrypted,
        obtained_at: obtainedAt,
      },
      { onConflict: "user_id,provider" },
    );

  if (upsertRes.error) {
    fail("connection_persist_failed");
    return;
  }

  res.redirect(302, "/broker?connected=success");
}
