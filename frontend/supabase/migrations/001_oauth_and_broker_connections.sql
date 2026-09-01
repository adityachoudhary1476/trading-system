-- =====================================================================
-- Finova Markets — Upstox OAuth + broker connection storage
-- Run this in the Supabase SQL Editor against your project database.
-- =====================================================================

-- 1. Single-use OAuth state tokens (CSRF + replay protection)
CREATE TABLE IF NOT EXISTS public.oauth_states (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  provider     text NOT NULL DEFAULT 'upstox',
  state        text NOT NULL UNIQUE,
  expires_at   timestamptz NOT NULL,
  consumed_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_oauth_states_user
  ON public.oauth_states (user_id, provider);

CREATE INDEX IF NOT EXISTS idx_oauth_states_state
  ON public.oauth_states (state);

-- 2. Encrypted broker access tokens, one row per (user, provider)
CREATE TABLE IF NOT EXISTS public.broker_connections (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id               uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  provider              text NOT NULL DEFAULT 'upstox',
  access_token_encrypted text NOT NULL,
  obtained_at           timestamptz NOT NULL DEFAULT now(),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_broker_connections_user_provider
    UNIQUE (user_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_broker_connections_user
  ON public.broker_connections (user_id);

-- 3. Row Level Security: users touch only their own rows
ALTER TABLE public.oauth_states      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.broker_connections ENABLE ROW LEVEL SECURITY;

-- oauth_states policies
CREATE POLICY "oauth_states_select_own"
  ON public.oauth_states FOR SELECT
  USING  (user_id = auth.uid());

CREATE POLICY "oauth_states_insert_own"
  ON public.oauth_states FOR INSERT
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "oauth_states_update_own"
  ON public.oauth_states FOR UPDATE
  USING  (user_id = auth.uid());

-- broker_connections policies
-- IMPORTANT: There is NO SELECT policy for regular users.
-- The encrypted access token must NEVER be readable by the browser client.
-- The server-side API uses the service-role key (which bypasses RLS) to
-- retrieve and decrypt the token for Upstox API calls.
CREATE POLICY "broker_connections_insert_own"
  ON public.broker_connections FOR INSERT
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "broker_connections_update_own"
  ON public.broker_connections FOR UPDATE
  USING  (user_id = auth.uid());

CREATE POLICY "broker_connections_delete_own"
  ON public.broker_connections FOR DELETE
  USING  (user_id = auth.uid());
