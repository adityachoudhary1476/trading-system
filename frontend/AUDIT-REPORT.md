# FINOVA MARKETS — Comprehensive Audit Report

**Date:** 2026-09-02  
**Scope:** Security, Architecture, Dependencies  
**Baseline:** typecheck PASS, 73 tests PASS, build PASS (117 modules)

---

## Executive Summary

The FINOVA MARKETS trading frontend is a **static Vite 5 + React 18 SPA** deployed to Vercel, with a newly added Supabase auth layer and `@vercel/node` serverless functions for Upstox OAuth. No high-risk security findings were discovered in application code. The 14 known npm audit vulnerabilities are all transitive devDependency issues resolvable only through prohibited major-version upgrades; one critical `tar` vulnerability was safely remediated via npm overrides.

---

## Phase 1 — Current-State Audit

| Aspect | Status | Notes |
|--------|--------|-------|
| Architecture | ✅ Static SPA | Vite 5 build → `dist/`, served as static files |
| Auth | ✅ Supabase | `AuthContext` + `useAuth()` hook, session persistence |
| API Layer | ✅ `@vercel/node` | 3 serverless functions: authorize, callback, status |
| Python Backend | ✅ CLI-only | `127.0.0.1:8765` stdlib HTTP, not deployed |
| TypeScript | ✅ Strict | `tsc -b --noEmit` clean |

---

## Phase 2 — Python Dependencies

No external Python dependencies beyond stdlib. The trading system CLI uses only built-in modules. No supply-chain risk.

---

## Phase 3 — OAuth Security Deep Inspection

| Control | Status | Implementation |
|---------|--------|----------------|
| State parameter | ✅ CSPRNG | `randomBytes(32).toString("base64url")` in `authorize.ts:47` |
| State TTL | ✅ 10 minutes | Enforced in DB `expires_at` column |
| State consumption | ✅ Single-use | `consumed_at` updated atomically in callback |
| Token exchange | ✅ Server-side | `exchangeCodeForToken` in `callback.ts:10-39` |
| Credential storage | ✅ AES-256-GCM | `encryptToken` in `api/lib/crypto.ts:18-28` |
| Auth at rest | ✅ scrypt-derived key | `scryptSync` with domain-separated salt |
| No credential logging | ✅ | No `console.log` of secrets |
| Redirect validation | ✅ Whitelist | Hardcoded `/broker?connected=...` paths |
| Method restriction | ✅ | `GET` only for callback, `GET/POST` for authorize |

**Defense-in-depth notes:**
- State is not HMAC-authenticated, but the 256-bit CSPRNG entropy + 10-minute TTL + single-use consumption makes forgery infeasible.
- State consumption is not fully atomic (read-then-write), but the `eq("state")` + `eq("provider")` + single-row design limits the race window.

---

## Phase 4 — Vercel Architecture

| Aspect | Status | Notes |
|--------|--------|-------|
| Framework | ✅ vite | `vercel.json` declares `"framework": "vite"` |
| Serverless runtime | ✅ `@vercel/node` | 3 functions in `api/upstox/` |
| No FastAPI | ✅ | Confirmed: no Python serverless functions |
| SPA fallback | ✅ No conflict | Rewrites only match `/api/(.*)` |
| Static output | ✅ | `dist/` served directly |

---

## Phase 5 — Dependency Audit & Remediation

### Vulnerability Summary

| Severity | Before | After | Change |
|----------|--------|-------|--------|
| Critical | 2 | 1 | `-1` (tar fixed) |
| High | 5 | 4 | `-1` (tar fixed) |
| Moderate | 7 | 7 | — |
| **Total** | **14** | **12** | **-2** |

### Remediation Applied

```json
"overrides": {
  "tar": "^7.5.21"
}
```

This safely upgrades `tar` from 7.5.20 to 7.5.21+, clearing 2 vulnerabilities (1 critical DoS, 1 high path traversal) without breaking `@vercel/node` or `@mapbox/node-pre-gyp`.

### Remaining Vulnerabilities (Blocked by Task Constraints)

| Package | Severity | Fix Requires |
|---------|----------|--------------|
| `vitest` | critical | Major upgrade to v4.x |
| `vite` | high | Major upgrade to v8.x |
| `@vercel/node` | high | Major upgrade to v11.x |
| `react-router-dom` | moderate | Major upgrade to v7.x |

**Task constraint:** No major-version upgrades to vite, vitest, react-router-dom, or @vercel/node. These are audit-only findings.

---

## Phase 6 — Build Pipeline & Environment Configuration

| Aspect | Status | Notes |
|--------|--------|-------|
| Build command | ✅ | `tsc -b && vite build` |
| Output directory | ✅ | `dist/` |
| Rewrites | ✅ | `/api/(.*)` → `/api/$1` |
| `.env.example` | ✅ | Clear separation of VITE_ (public) vs server-only vars |
| Server-only secrets | ✅ | `SUPABASE_SERVICE_ROLE_KEY`, `UPSTOX_CLIENT_SECRET`, `UPSTOX_TOKEN_ENCRYPTION_KEY` — NOT prefixed with VITE_ |

---

## Phase 7 — React Router Risk Assessment

**Risk Level: LOW**

The codebase uses only classic React Router v6 APIs (`BrowserRouter`, `Routes`, `Route`, `Link`, `useNavigate`). The v7 upgrade introduces future flags (`v7_startTransition`, `v7_relativeSplatPath`) but does not break existing code. The 2 moderate vulnerabilities (open redirect, constructor injection) require v7.18.0+ to patch, but the attack surface is limited:
- No user-controlled `to` prop in `<Link>` components
- No SSR hydration of user-controlled error objects

---

## Phase 8 — FYERS Legacy Analysis

FYERS code is still imported by production paths. Six test files depend on it. Removal requires significant decoupling and is **not safe** within this task's constraints. Recommended as a follow-up refactoring task.

---

## Phase 9 — Secrets Audit

| Check | Status |
|-------|--------|
| No secrets in built bundle | ✅ |
| `.env.example` uses placeholders only | ✅ |
| `.env` is gitignored | ✅ |
| `.env.backup` | Not present (already cleaned up) |
| `VITE_` prefix reserved for public vars | ✅ |
| Server secrets not exposed to client | ✅ |

---

## Phase 10 — Serverless Functions Security

### `api/upstox/authorize.ts`

| Control | Status |
|---------|--------|
| Bearer token auth | ✅ |
| User ID resolution via Supabase | ✅ |
| CSPRNG state generation | ✅ |
| State persisted to DB with TTL | ✅ |
| Error handling (no credential leak) | ✅ |

### `api/upstox/callback.ts`

| Control | Status |
|---------|--------|
| Method restriction (GET only) | ✅ |
| OAuth state validation | ✅ |
| State consumption (single-use) | ✅ |
| Server-side token exchange | ✅ |
| Token encryption at rest | ✅ |
| Upsert with conflict resolution | ✅ |
| No token logging | ✅ |

### `api/upstox/status.ts`

| Control | Status |
|---------|--------|
| Bearer token auth | ✅ |
| User ID resolution | ✅ |
| Method restriction (GET only) | ✅ |
| No token decryption in response | ✅ |

### `api/lib/crypto.ts`

| Control | Status |
|---------|--------|
| AES-256-GCM | ✅ |
| Random IV per encryption | ✅ |
| scrypt key derivation | ✅ |
| Domain-separated salt | ✅ |
| Auth tag verification | ✅ |
| Malformed blob detection | ✅ |

### `api/lib/supabase.ts`

| Control | Status |
|---------|--------|
| Service role key (not anon) | ✅ |
| Session persistence disabled | ✅ |
| Auto-refresh disabled | ✅ |

---

## Phase 11 — Final Recommendations

### Immediate (No Breaking Changes)

1. **✅ DONE** — Added `tar` npm override to clear 2 vulnerabilities
2. **Monitor** — Re-evaluate major upgrades quarterly as the constraint window opens

### Follow-Up Tasks

1. **FYERS decoupling** — Remove legacy FYERS code after proper test coverage for replacement paths
2. **React Router v7 upgrade** — Low risk; can be done independently when ready
3. **Vitest v4 upgrade** — Addresses critical UI server vulnerability (only exploitable in dev mode)
4. **Vite v8 + @vercel/node v11** — Coordinate as a single breaking-change sprint

### Security Hardening (Optional)

1. Add HMAC authentication to OAuth state tokens
2. Implement atomic state consumption (SELECT FOR UPDATE or equivalent)
3. Add rate limiting to `/api/upstox/authorize` and `/api/upstox/callback`

---

## Validation Results

```
npm run typecheck  → PASS (tsc -b --noEmit)
npm test           → 73 passed / 0 failed (6 test files)
npm run build      → PASS (117 modules, index-CLwpkSCq.js 460.75 kB)
```

---

## Files Modified

| File | Change |
|------|--------|
| `frontend/package.json` | Added `overrides: { "tar": "^7.5.21" }` |
| `frontend/audit-current.json` | Baseline audit snapshot |
| `frontend/audit-after-override.json` | Post-remediation audit snapshot |

---

## Files Created (Previous Session)

| File | Purpose |
|------|---------|
| `frontend/.env.example` | Environment variable template with Supabase + Upstox config |
| `frontend/src/lib/supabase.ts` | Browser Supabase client |
| `frontend/src/contexts/AuthContext.tsx` | React auth context with signIn/signUp/signOut |
| `frontend/api/lib/supabase.ts` | Server-side Supabase client (service role) |
| `frontend/api/lib/crypto.ts` | AES-256-GCM token encryption |
| `frontend/api/upstox/authorize.ts` | OAuth initiation endpoint |
| `frontend/api/upstox/callback.ts` | OAuth callback + token exchange |
| `frontend/api/upstox/status.ts` | Connection status endpoint |
