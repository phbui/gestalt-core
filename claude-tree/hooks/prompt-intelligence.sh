#!/usr/bin/env bash
# UserPromptSubmit hook — Tier 2 per-prompt relevance injection.
# Queries Graphiti, gestalt_search, and Letta domain blocks concurrently.
# Falls back to static parallelism hints if backends are unavailable.

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
source "$SCRIPT_DIR/lib.sh" && gestalt_init

# Read stdin (hook payload from Claude Code)
STDIN_DATA=$(cat)

# Delegate to Python script
exec python3 "$SCRIPT_DIR/prompt-intelligence.py" \
    --gestalt-dir "$GESTALT_DIR" \
    --state-dir "$STATE_DIR" \
    --letta-url "${LETTA_URL:-http://localhost:8283/v1}" \
    --graphiti-url "${GRAPHITI_URL:-http://localhost:8200}" \
    --tier2-max-tokens "${GESTALT_TIER2_MAX_TOKENS:-700}" \
    --search-limit "${GESTALT_SEARCH_LIMIT:-3}" \
    --graphiti-timeout "${GESTALT_GRAPHITI_TIMEOUT:-0.4}" \
    --relevance-threshold "${GESTALT_RELEVANCE_THRESHOLD:-0.01}" \
    <<< "$STDIN_DATA"
