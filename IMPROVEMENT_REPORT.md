# Trading System — Improvement Report

## Current Status

Everything is live and working:
- NIFTY options enabled (code + env var + DB)
- Scheduler running on `finova-autonomous-scheduler`, ticking every 30s
- Kill-switch Start/Stop working (DB-backed, polled every 30s)
- Git push → GitHub successful
- Upstox token updated and verified (no auth errors in logs)

---

## Pain Points Observed

### 1. Git push requires PAT workaround every time
- **Root cause:** `url."git@github.com:".insteadOf "https://github.com/"` in `.gitconfig` rewrites HTTPS URLs to SSH, but the SSH key (`~/.ssh/github`) is rejected by GitHub ("Permission denied (publickey)").
- **Workaround:** Must prefix every push with `git -c credential.helper= push https://x-access-token:<PAT>@github.com/...` — ugly and error-prone.

### 2. `railway ssh` is slow and limited
- ~90s to establish connection (every call).
- Can't read `/proc/1/environ` ("Permission denied") — can't verify live env vars on the running process.
- `railway logs` requires manually linking the service first (`railway service link`).

### 3. Kill-switch management is manual
- Clearing the kill switch requires a DB UPDATE via `railway ssh` (Python script piped to stdin).
- No CLI shortcut or API endpoint to resume the bot with one command.

### 4. No pre-deploy token validation
- The Upstox token validity is confirmed reactively (via scheduler logs after deploy), not proactively.
- A degraded/expired token isn't caught until the scheduler tries to use it.

### 5. `.env.example` secrets leakage
- Actual Upstox secrets (`UPSTOX_CLIENT_ID`, `UPSTOX_SERVICE_ACCOUNT_TOKEN`, `UPSTOX_TOKEN_ENCRYPTION_KEY`) keep appearing in `backend/.env.example` (tracked file), despite being placeholders.
- No pre-commit guard catches this.

### 6. No monitoring/alerts
- Scheduler silently skips ticks with `market_closed` — no way to distinguish "market closed" from "scheduler stuck."
- No alerting on kill-switch state, token expiry, or data staleness.

### 7. Submodule remnants
- The auto-generated commit `960aa656` removed 5 git submodules (`agency-agents`, `algo_trading_strategies_india`, `bhav`, `learn-trading-from-x`, `nifty-daily-moves-and-gaps`) that lacked `.gitmodules` and caused Railway snapshot failures.
- Need to verify none of these are still imported in the scheduler code.

---

## High-Impact Improvements

### A. Fix Git push (30 min)
```bash
# Check if the insteadOf rule is the culprit
git config --global --unset-all url."git@github.com:".insteadOf
# Or force HTTPS
git config --global url."https://github.com/".insteadOf "git@github.com:"
```
After this, `git push origin master` works normally — no PAT prefix needed.

### B. Add a one-command kill-switch reset (20 min)
Create `backend/scripts/resume_bot.sh`:
```bash
#!/bin/bash
# Clears the kill switch so the scheduler resumes on next tick
railway ssh --service finova-autonomous-scheduler "python3 -c \"
from sqlalchemy import create_engine, text
import os
e = create_engine(os.environ['MARKET_DATA_DB_URL'])
with e.begin() as c:
    c.execute(text(\"UPDATE autonomous_bots SET kill_switch_state='active', kill_switch_reason=NULL, kill_switch_halted_at=NULL, enabled='true', updated_at=NOW() WHERE bot_id='bot-nifty-options'\"))
    print('Kill switch cleared — scheduler will resume next tick')
\""
```
Run with: `bash backend/scripts/resume_bot.sh`

### C. Add a pre-deploy Upstox token check (15 min)
Add to `Dockerfile.worker` or a CI step:
```bash
# Quick probe — fails fast if token is invalid
python3 -c "
import os, requests
token = os.environ.get('UPSTOX_SERVICE_ACCOUNT_TOKEN')
if not token or len(token) < 20:
    raise SystemExit('UPSTOX_SERVICE_ACCOUNT_TOKEN not set or too short')
r = requests.get('https://api.upstox.com/v3/user/profile', headers={'Authorization': f'Bearer {token}'}, timeout=10)
if r.status_code != 200:
    raise SystemExit(f'Upstox token invalid: HTTP {r.status_code} — {r.text[:100]}')
print('Upstox token OK')
"
```

### D. Add a post-deploy verification script (20 min)
Create `backend/scripts/verify_deploy.py` — checks in one shot:
- DB: `options_enabled = true` for NIFTY bots, `kill_switch_state = 'active'`
- Env: `AUTONOMOUS_OPTION_UNDERLYINGS=NIFTY`, `UPSTOX_SERVICE_ACCOUNT_TOKEN` set
- Code: `options_enabled=True` in phase23 deployment.py
- Scheduler: not halted (polls kill switch)

Run via `railway ssh` after every deploy.

### E. Pre-commit hook: block secrets in `.env.example` (10 min)
Add `.pre-commit-config.yaml`:
```yaml
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets
        args: ["--baseline", ".secrets.baseline"]
        exclude: \.env$  # only scan tracked files
```
Or a simpler grep-based hook:
```bash
# .git/hooks/pre-commit
if git diff --cached --name-only | grep -q '\.env\.example' && \
   git diff --cached -- backend/.env.example | grep -E 'UPSTOX_(SERVICE|TOKEN_)'; then
    echo "ERROR: Real secrets found in .env.example — replace with placeholders"
    exit 1
fi
```

### F. Add uptime/alert monitoring (2-3 hours)
- **Option 1 (simple):** Add a Railway cron job that pings a health endpoint every 5 min, checks DB kill-switch state, and sends a Telegram/Discord webhook on failure.
- **Option 2 (robust):** Add a `/health` endpoint to the backend API that reports: scheduler alive, Upstox token valid, DB options enabled, kill switch state. Poll it externally (e.g., UptimeRobot).

### G. Migrate off Config as Code — 10 min
```bash
railway config migrate
```
Clears the deprecation warning (`railway.json` → `.railway/railway.ts`).

---

## Quick Win Checklist (do in order)

| Priority | Action | Time | Impact |
|---|---|---|---|
| P0 | Fix Git `insteadOf` config (A) | 30 min | Eliminates PAT workaround forever |
| P0 | Add `resume_bot.sh` script (B) | 20 min | One-command kill-switch reset |
| P1 | Pre-deploy Upstox token check (C) | 15 min | Catches bad token before deploy |
| P1 | Pre-commit secret guard (E) | 10 min | Prevents `.env.example` leakage |
| P2 | Post-deploy verification script (D) | 20 min | One-command deploy validation |
| P2 | Migrate Config as Code (G) | 10 min | Removes deprecation warning |
| P3 | Add uptime monitoring (F) | 2-3 hrs | Proactive alerting |
| P3 | Verify submodule imports removed (A.7) | 15 min | Prevents runtime import errors |
