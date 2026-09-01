import { getSupabaseClient } from "@/lib/supabase";

export interface AuthorizeResponse {
  authorization_url: string;
  state: string;
  expires_at: string;
}

export interface ConnectionStatus {
  connected: boolean;
  provider?: string;
  obtained_at?: string;
}

export interface DisconnectResponse {
  disconnected: boolean;
}

async function authHeaders(): Promise<Record<string, string>> {
  const sb = getSupabaseClient();
  const { data } = await sb.auth.getSession();
  const token = data.session?.access_token;
  if (!token) throw new Error("Not authenticated");
  return { Authorization: `Bearer ${token}` };
}

export async function fetchConnectionStatus(): Promise<ConnectionStatus> {
  const headers = await authHeaders();
  const resp = await fetch("/api/upstox/status", { headers });
  if (resp.status === 401) {
    return { connected: false };
  }
  if (!resp.ok) {
    throw new Error(`Status check failed: ${resp.status}`);
  }
  return (await resp.json()) as ConnectionStatus;
}

export async function startUpstoxOAuth(): Promise<void> {
  const headers = await authHeaders();
  const resp = await fetch("/api/upstox/authorize", { method: "POST", headers });
  if (!resp.ok) {
    throw new Error(`Authorize failed: ${resp.status}`);
  }
  const json = (await resp.json()) as AuthorizeResponse;
  window.location.href = json.authorization_url;
}

export async function disconnectUpstox(): Promise<DisconnectResponse> {
  const headers = await authHeaders();
  const resp = await fetch("/api/upstox/disconnect", { method: "POST", headers });
  if (!resp.ok) {
    throw new Error(`Disconnect failed: ${resp.status}`);
  }
  return (await resp.json()) as DisconnectResponse;
}
