#!/usr/bin/env bash
# letta-controls.sh — Phase 1: cap self-created block limits + update core_directives
# Usage: bash gestalt/tools/letta-controls.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
# gestalt/tools/ is two levels below gestalt root; GESTALT_DIR resolves to tools/../.. = workspace.
# Override GESTALT_WORKSPACE so STATE_DIR points to workspace/.claude/gestalt (where agent ID lives).
export GESTALT_WORKSPACE="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/../.claude/hooks/lib.sh" && gestalt_init
LETTA_URL="${LETTA_URL:-http://localhost:8283/v1}"

echo "=== Letta Controls: Phase 1 Setup ==="

# Check Letta health
if ! curl -sf --max-time 3 "$LETTA_URL/health" >/dev/null 2>&1; then
    echo "ERROR: Letta unreachable at $LETTA_URL"
    exit 1
fi
echo "OK: Letta reachable at $LETTA_URL"

# Read agent ID
if [ ! -f "$STATE_DIR/letta-agent-id.txt" ]; then
    echo "ERROR: Agent ID file not found at $STATE_DIR/letta-agent-id.txt"
    exit 1
fi
AGENT_ID=$(cat "$STATE_DIR/letta-agent-id.txt")
echo "Agent ID: $AGENT_ID"

# Step 1: List all blocks and find targets
echo ""
echo "--- Current blocks ---"
AGENT_DATA=$(curl -sf --max-time 5 "$LETTA_URL/agents/$AGENT_ID")
echo "$AGENT_DATA" | python3 -c "
import sys, json
agent = json.load(sys.stdin)
blocks = agent.get('memory', {}).get('blocks', [])
for b in blocks:
    print(f\"  {b['id'][:8]}... {b['label']:30s} limit={b.get('limit','?'):>8}  chars={len(b.get('value',''))}\")
"

# Step 2: Patch self-created blocks to cap their limits.
# Add one entry per agent-created block you want capped. Letta rejects a limit
# lower than the block's current content length, so size the cap accordingly.
# The former employer-specific blocks were removed with the personal fork.
echo ""
echo "--- Patching block limits ---"
declare -A BLOCK_LIMITS=()
for LABEL in "${!BLOCK_LIMITS[@]}"; do
    TARGET_LIMIT="${BLOCK_LIMITS[$LABEL]}"
    BLOCK_ID=$(echo "$AGENT_DATA" | python3 -c "
import sys, json
agent = json.load(sys.stdin)
for b in agent.get('memory', {}).get('blocks', []):
    if b['label'] == '$LABEL':
        print(b['id'])
        break
" 2>/dev/null)
    if [ -n "$BLOCK_ID" ]; then
        # Check current content length first — Letta rejects limit < content length (HTTP 422)
        CURRENT_LEN=$(echo "$AGENT_DATA" | python3 -c "
import sys, json
agent = json.load(sys.stdin)
for b in agent.get('memory', {}).get('blocks', []):
    if b['label'] == '$LABEL':
        print(len(b.get('value', '')))
        break
" 2>/dev/null)
        if [ -n "$CURRENT_LEN" ] && [ "$CURRENT_LEN" -gt "$TARGET_LIMIT" ] 2>/dev/null; then
            echo "  SKIP: $LABEL has $CURRENT_LEN chars > target limit $TARGET_LIMIT (trim content first)"
            continue
        fi
        HTTP=$(curl -sf --max-time 10 -o /tmp/letta-patch-resp.json -w '%{http_code}' -X PATCH \
            "$LETTA_URL/blocks/$BLOCK_ID" \
            -H "Content-Type: application/json" \
            -d "{\"limit\": $TARGET_LIMIT}" 2>/dev/null || echo "000")
        if [ "$HTTP" -ge 200 ] 2>/dev/null && [ "$HTTP" -lt 300 ] 2>/dev/null; then
            echo "  OK: Patched $LABEL ($BLOCK_ID) → limit=$TARGET_LIMIT"
        else
            echo "  WARN: Patch failed for $LABEL (HTTP $HTTP)"
        fi
    else
        echo "  SKIP: Block '$LABEL' not found (may not exist yet)"
    fi
done

# Step 3: Update core_directives via agent message
echo ""
echo "--- Updating core_directives ---"
DIRECTIVE="MEMORY MANAGEMENT: Do not create new core memory blocks. When project_context is full, condense and summarize existing content rather than creating overflow blocks. Project-specific investigation notes, scale test results, and architectural findings belong in gestalt knowledge entries (knowledge/*.md), not core memory. If you discover something worth remembering long-term, summarize it into an existing block or write GESTALT: <fact> so the auto-promotion system can identify it."

HTTP=$(curl -sf --max-time 10 -o /dev/null -w '%{http_code}' -X POST "$LETTA_URL/agents/$AGENT_ID/messages" \
    -H "Content-Type: application/json" \
    -d "{
        \"messages\": [{
            \"role\": \"user\",
            \"content\": \"Please append the following rule to your core_directives memory block using core_memory_append: '$DIRECTIVE'\"
        }]
    }" 2>/dev/null || echo "000")
if [ "$HTTP" -ge 200 ] 2>/dev/null && [ "$HTTP" -lt 300 ] 2>/dev/null; then
    echo "  OK: core_directives update sent (HTTP $HTTP)"
else
    echo "  WARN: core_directives update failed (HTTP $HTTP)"
    echo "  Manual action needed: add memory management directive to core_directives block"
fi

# Step 4: Verify final state
echo ""
echo "--- Final block state ---"
curl -sf --max-time 5 "$LETTA_URL/agents/$AGENT_ID" | python3 -c "
import sys, json
agent = json.load(sys.stdin)
blocks = agent.get('memory', {}).get('blocks', [])
for b in blocks:
    print(f\"  {b['label']:30s} limit={b.get('limit','?'):>8}  chars={len(b.get('value',''))}\")
"

echo ""
echo "=== Phase 1 complete ==="
