---
pitch_id: unified-relevance-injection
document: sdd-component
component: letta-controls
version: 1.0
created: 2026-04-09
---

# Component: Letta Controls

**Satisfies:** URI-FR-001, URI-FR-002

Phase 1 — 25 minutes. Two API calls and one config update. No hook changes.

## 1. Block Limit Caps

**Satisfies:** URI-FR-002

Patch the two self-created blocks via the Letta REST API.

```bash
# Read agent ID
AGENT_ID=$(cat ~/.claude/gestalt/letta-agent-id.txt)
LETTA_URL="http://localhost:8283/v1"

# Get the current agent to find block IDs
curl -s "$LETTA_URL/agents/$AGENT_ID" | python3 -c "
import sys, json
agent = json.load(sys.stdin)
for b in agent['memory']['blocks']:
    if b['label'] in ('service_api_notes', 'platform_arch_notes'):
        print(b['id'], b['label'], b.get('limit'))
"

# Patch each block (substitute actual block IDs)
curl -s -X PATCH "$LETTA_URL/blocks/{BLOCK_ID}" \
  -H "Content-Type: application/json" \
  -d '{"limit": 3000}'
```

Both blocks are patched to `limit: 3000`. The Letta agent will automatically condense content exceeding the new limit on its next memory update.

**Verification:** `GET /v1/agents/{id}` → both blocks show `"limit": 3000`.

## 2. Core Directives Update

**Satisfies:** URI-FR-001

Append the following to the `core_directives` block via the Letta agent message interface:

```
MEMORY MANAGEMENT: Do not create new core memory blocks. When project_context
is full, condense and summarize existing content rather than creating overflow
blocks. Project-specific investigation notes, scale test results, and architectural
findings belong in gestalt knowledge entries (knowledge/*.md), not core memory.
If you need to remember a specific discovery, prefix it with "GESTALT:" so the
auto-promotion system can identify it for KB promotion.
```

**Method:** Send a message to the agent instructing it to update `core_directives`. The Letta agent will append this via `core_memory_append`.

```bash
curl -s -X POST "$LETTA_URL/agents/$AGENT_ID/messages" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{
      "role": "user",
      "content": "Please update your core_directives block to add this memory management rule: Do not create new core memory blocks. When project_context is full, condense and summarize existing content rather than creating overflow blocks. Project-specific investigation notes, scale test results, and architectural findings belong in gestalt knowledge entries (knowledge/*.md), not core memory. If you need to remember a specific discovery, prefix it with GESTALT: so the auto-promotion system can identify it for KB promotion."
    }]
  }'
```

**Verification:** `GET /v1/agents/{id}` → `core_directives` block contains the memory management rule.

## 3. Prerequisite Verification Script

Create `gestalt/tools/letta-controls.sh` — a one-shot script to apply both changes:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../.claude/hooks/lib.sh"
gestalt_init

AGENT_ID=$(cat "$STATE_DIR/letta-agent-id.txt")

# Step 1: Get block IDs
echo "Fetching agent blocks..."
BLOCKS=$(curl -sf "$LETTA_URL/agents/$AGENT_ID" | python3 -c "
import sys, json
agent = json.load(sys.stdin)
for b in agent['memory']['blocks']:
    print(b['id'], b['label'], b.get('limit', 'no-limit'))
")
echo "$BLOCKS"

# Step 2: Patch limits
for LABEL in service_api_notes platform_arch_notes; do
    BLOCK_ID=$(echo "$BLOCKS" | awk -v l="$LABEL" '$2==l {print $1}')
    if [ -n "$BLOCK_ID" ]; then
        HTTP=$(curl -sf -o /dev/null -w "%{http_code}" -X PATCH \
            "$LETTA_URL/blocks/$BLOCK_ID" \
            -H "Content-Type: application/json" \
            -d '{"limit": 3000}')
        echo "Patched $LABEL ($BLOCK_ID): HTTP $HTTP"
    else
        echo "Block $LABEL not found — skipping"
    fi
done

# Step 3: Update core_directives
echo "Updating core_directives..."
curl -sf -X POST "$LETTA_URL/agents/$AGENT_ID/messages" \
    -H "Content-Type: application/json" \
    -d '{"messages": [{"role": "user", "content": "Please update your core_directives block to add this memory management rule at the end: MEMORY MANAGEMENT: Do not create new core memory blocks. When project_context is full, condense existing content. Project-specific findings belong in gestalt knowledge entries, not core memory."}]}'

echo "Done."
```

**Usage:** `bash gestalt/tools/letta-controls.sh`

## 4. Decisions

### DEC-001: PATCH via REST API, not agent message for block limits

Directly patching `limit` via `PATCH /v1/blocks/{id}` is immediate and reliable. Sending a message asking the agent to "set your limit" is unpredictable — the Letta agent doesn't have a tool for changing its own block limits.

### DEC-002: Append, not replace, for core_directives

Replacing `core_directives` risks overwriting behavioral rules added by previous sessions. Appending the memory management rule is additive and safe.
