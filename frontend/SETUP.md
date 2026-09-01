# Upstox OAuth — Production Setup

## Exact Production Redirect URI

Register this URI in the Upstox Developer Console (**App configuration > Redirect URL**):

```
https://tradingsystem-zeta.vercel.app/api/upstox/callback
```

This is the **only** production redirect URI. It is used identically for:
1. The `redirect_uri` query parameter in the Upstox authorization URL.
2. The `redirect_uri` form field in the authorization-code token-exchange POST.

Both values come from the single environment variable `UPSTOX_REDIRECT_URI`.

---

## Required Vercel Environment Variables

Set these in your Vercel project settings (not in source control):

| Variable | Scope | Purpose |
|---|---|---|
| `VITE_SUPABASE_URL` | Public (browser) | Supabase project URL |
| `VITE_SUPABASE_ANON_KEY` | Public (browser) | Supabase anon key |
| `SUPABASE_URL` | Server only | Supabase project URL (for Vercel functions) |
| `SUPABASE_SERVICE_ROLE_KEY` | Server only | Supabase service role key — **never expose to browser** |
| `UPSTOX_CLIENT_ID` | Server only | Upstox API key |
| `UPSTOX_CLIENT_SECRET` | Server only | Upstox API secret |
| `UPSTOX_REDIRECT_URI` | Server only | `https://tradingsystem-zeta.vercel.app/api/upstox/callback` |
| `UPSTOX_TOKEN_ENCRYPTION_KEY` | Server only | 32-byte base64 key for AES-256-GCM token encryption |

Generate the encryption key:

```bash
node -e "console.log(require('crypto').randomBytes(32).toString('base64'))"
```

---

## Supabase Setup

1. Create a Supabase project.
2. Run the SQL in `supabase/migrations/001_oauth_and_broker_connections.sql` in the Supabase SQL Editor.
3. Copy the project URL and keys into Vercel environment variables.
4. Enable email authentication in Supabase (Authentication > Providers > Email).

---

## OAuth Flow

1. User signs in via Supabase (email/password).
2. User navigates to `/broker` and clicks **Connect Upstox**.
3. Frontend calls `POST /api/upstox/authorize` with the Supabase JWT.
4. The function generates a 256-bit random `state`, stores it in `oauth_states` (10-min TTL, single-use), and returns the Upstox authorization URL.
5. Browser redirects to Upstox. User authenticates.
6. Upstox redirects to `https://tradingsystem-zeta.vercel.app/api/upstox/callback?code=...&state=...`.
7. The callback function validates the state (exists, unexpired, unconsumed, correct user), marks it consumed, exchanges the code for an access token (server-side, using `UPSTOX_CLIENT_SECRET`), encrypts the token with AES-256-GCM, and stores it in `broker_connections`.
8. Browser redirects to `/broker?connected=success`.
9. UI shows Upstox as connected. The access token never touches the browser.

---

## File Map

| File | Purpose |
|---|---|
| `api/upstox/authorize.ts` | Vercel function: generates state, returns auth URL |
| `api/upstox/callback.ts` | Vercel function: validates state, exchanges code, stores token |
| `api/upstox/status.ts` | Vercel function: reports connection status |
| `api/lib/supabase.ts` | Server-side Supabase client (service role) |
| `api/lib/crypto.ts` | AES-256-GCM token encryption |
| `src/lib/oauth-state-machine.ts` | Pure OAuth state validation logic (shared, tested) |
| `src/lib/supabase.ts` | Browser Supabase client (anon key) |
| `src/lib/upstox.ts` | Frontend OAuth helpers |
| `src/contexts/AuthContext.tsx` | Supabase auth session management |
| `src/pages/BrokerConnections.tsx` | Connections UI |
| `supabase/migrations/001_oauth_and_broker_connections.sql` | DB schema + RLS |
