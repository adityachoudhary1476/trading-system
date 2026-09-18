#!/bin/bash
# Pre-commit hook: block real secrets from being committed in .env.example
# Rejects non-empty values for UPSTOX_SERVICE_ACCOUNT_TOKEN and UPSTOX_TOKEN_ENCRYPTION_KEY.
#
# Install:  cp backend/scripts/pre-commit.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
# Test:     echo "UPSTOX_SERVICE_ACCOUNT_TOKEN=eyJ0eX_fake" >> backend/.env.example && git add backend/.env.example && git commit -m "test"

if ! command -v git &> /dev/null; then
    exit 0
fi

# Only runs if .env.example is staged
if git diff --cached --name-only | grep -q '\.env\.example'; then
    # Lines starting with + (additions), excluding the +++ diff header
    # Then filter to UPSTOX secret lines, excluding empty/whitespace-only values
    secrets=$(git diff --cached -- backend/.env.example 2>/dev/null \
        | grep -E '^\+' \
        | grep -vE '^\+\+\+' \
        | grep -E 'UPSTOX_(SERVICE_ACCOUNT_TOKEN|TOKEN_ENCRYPTION_KEY)=' \
        | grep -vE 'UPSTOX_(SERVICE_ACCOUNT_TOKEN|TOKEN_ENCRYPTION_KEY)=[[:space:]]*$' \
        || true)

    if [ -n "$secrets" ]; then
        echo "ERROR: Real secret values found in .env.example:"
        echo "$secrets"
        echo ""
        echo "Replace with placeholders:"
        echo "  UPSTOX_SERVICE_ACCOUNT_TOKEN="
        echo "  UPSTOX_TOKEN_ENCRYPTION_KEY="
        exit 1
    fi
fi

echo "Pre-commit check passed (no secrets in .env.example)."
exit 0
