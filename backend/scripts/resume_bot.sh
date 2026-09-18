#!/bin/bash
# Clears the kill switch on the autonomous scheduler bot so it resumes on the next tick.
# Usage: bash backend/scripts/resume_bot.sh [bot_id] [service_name]
set -euo pipefail

BOT_ID="${1:-bot-nifty-options}"
SERVICE="${2:-finova-autonomous-scheduler}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Clearing kill switch: bot_id=$BOT_ID service=$SERVICE"
railway ssh --service "$SERVICE" "BOT_ID=$BOT_ID python3 -" < "$SCRIPT_DIR/_resume_bot.py"
