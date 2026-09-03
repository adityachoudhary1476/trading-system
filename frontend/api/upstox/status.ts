import type { VercelRequest, VercelResponse } from "@vercel/node";
import { getServerSupabase } from "../lib/supabase.js";

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

/**
 * Derive the Indian market session phase from a UTC timestamp.
 *
 * NSE cash market regular session: 09:15 – 15:30 IST (UTC+5:30).
 * * Pre-market:    09:00 – 09:15 IST
 * * Post-market:   15:30 – 16:00 IST
 * * Closed / weekend / holiday:  otherwise
 *
 * This is a best-effort, timezone-correct derivation — it does NOT
 * query a live exchange calendar.  For production accuracy the backend
 * FastAPI service (services.market_data) queries Upstox's WebSocket
 * feed status endpoint and is the source of truth for real-time phase
 * detection; this Vercel endpoint mirrors the same logic for the
 * browser-only chart page so it can render even without a backend
 * connection.
 */
function marketPhase(utcMs: number): "pre_market" | "regular" | "post_market" | "closed" {
  // India is UTC+5:30 (330 minutes).
  // Create a Date, get its UTC components, then apply the IST offset
  // to obtain the local IST wall-clock.
  const utc = new Date(utcMs);
  const utcHours = utc.getUTCHours();
  const utcMinutes = utc.getUTCMinutes();
  const totalUtcMinutes = utcHours * 60 + utcMinutes;
  const istTotalMinutes = totalUtcMinutes + 330; // +5:30

  // Wrap to 0-1439 (minutes in a day)
  const istMinutesInDay = ((istTotalMinutes % 1440) + 1440) % 1440;
  const istHours = Math.floor(istMinutesInDay / 60);
  const istMins = istMinutesInDay % 60;

  // Weekend check (Saturday=6, Sunday=0)
  const day = utc.getUTCDay();
  if (day === 0 || day === 6) {
    return "closed";
  }

  // Convert IST time to minutes-since-midnight for comparison
  const mins = istHours * 60 + istMins;

  if (mins < 9 * 60 + 0) return "closed";           // before 09:00 IST
  if (mins < 9 * 60 + 15) return "pre_market";       // 09:00 – 09:15
  if (mins < 15 * 60 + 30) return "regular";         // 09:15 – 15:30
  if (mins < 16 * 60 + 0) return "post_market";      // 15:30 – 16:00
  return "closed";                                   // after 16:00
}

/**
 * Compute the next market open and close (UTC epoch ms) for the IST
 * cash session.  Uses simple day-stepping from `now` — no exchange
 * holiday calendar.
 */
function nextMarketTimes(now: Date): { nextOpen: number; nextClose: number } {
  const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;
  let d = new Date(now.getTime());
  for (let i = 0; i < 10; i++) {
    // Shift to local IST and find the trading day boundary.
    const istMs = d.getTime() + IST_OFFSET_MS;
    const day = new Date(istMs).getUTCDay(); // 1=Mon … 5=Fri, 0=Sun, 6=Sat
    if (day >= 1 && day <= 5) {
      // 09:15 IST and 15:30 IST on this IST calendar day.
      const istMidnight = istMs - (istMs % 86400000);
      const openIST = istMidnight + (9 * 60 + 15) * 60000; // 09:15 IST
      const closeIST = istMidnight + (15 * 60 + 30) * 60000; // 15:30 IST
      const openMs = openIST - IST_OFFSET_MS; // convert back to UTC ms
      const closeMs = closeIST - IST_OFFSET_MS;
      if (openMs > now.getTime() && closeMs > now.getTime()) {
        return { nextOpen: openMs, nextClose: closeMs };
      }
      if (openMs <= now.getTime() && closeMs > now.getTime()) {
        return { nextOpen: openMs, nextClose: closeMs };
      }
    }
    d = new Date(d.getTime() + 86400000);
  }
  return { nextOpen: NaN, nextClose: NaN };
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

  const now = Date.now();
  const { nextOpen, nextClose } = nextMarketTimes(new Date(now));

  if (!data) {
    // User has no Upstox connection — still return market status so
    // the UI can show session info.
    res.status(200).json({
      connected: false,
      market: "NSE",
      phase: marketPhase(now),
      serverTime: now,
      nextOpen: Number.isFinite(nextOpen) ? nextOpen : null,
      nextClose: Number.isFinite(nextClose) ? nextClose : null,
    });
    return;
  }

  res.status(200).json({
    connected: true,
    provider: data.provider,
    obtained_at: data.obtained_at,
    // Market status derived from the Vercel function's own clock (UTC).
    // This mirrors the backend's logic for the browser-only page.
    market: "NSE",
    phase: marketPhase(now),
    serverTime: now,
    nextOpen: Number.isFinite(nextOpen) ? nextOpen : null,
    nextClose: Number.isFinite(nextClose) ? nextClose : null,
  });
}
