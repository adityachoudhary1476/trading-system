#!/bin/bash
# Post-deploy verification — checks all systems are live after a deploy.
# Usage: bash backend/scripts/verify_deploy.sh [bot_id]
set -euo pipefail

BOT_ID="${1:-bot-nifty-options}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== 1. Env vars on Railway service ==="
railway variables list --service finova-autonomous-scheduler 2>&1 | grep -iE 'upstox|autonomous' || echo "  (could not list vars — check service link)"

echo ""
echo "=== 2. Database: kill switch + options_enabled ==="
BOT_ID="$BOT_ID" railway ssh --service finova-autonomous-scheduler "python3 -" < "$SCRIPT_DIR/_verify_deploy.py" 2>&1

echo ""
echo "=== 3. Live scheduler log (last 10 lines) ==="
railway logs --service finova-autonomous-scheduler --tail 10 2>&1
