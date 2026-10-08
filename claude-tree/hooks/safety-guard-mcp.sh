#!/usr/bin/env bash
# PreToolUse hook for mcp__* tools
# Blocks destructive SQL and dangerous k8s operations via MCP.

INPUT=$(cat)
TOOL_NAME=$(jq -r '.tool_name // empty' <<< "$INPUT")
[ -z "$TOOL_NAME" ] && exit 0

deny() {
  jq -n --arg reason "BLOCKED by safety-guard-mcp: $1. Ask the user to run this operation manually." \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
  exit 0
}

# ============================================================
# Destructive k8s MCP operations
# ============================================================

case "$TOOL_NAME" in
  mcp__kubernetes__pods_delete)
    deny "Pod deletion via MCP blocked" ;;
  mcp__kubernetes__resources_delete)
    deny "Resource deletion via MCP blocked" ;;
  mcp__kubernetes__resources_scale)
    REPLICAS=$(jq -r '.tool_input.replicas // empty' <<< "$INPUT")
    [ "$REPLICAS" = "0" ] && deny "Scale to 0 replicas blocked — would take service offline"
    ;;
  mcp__graphiti-memory__clear_graph)
    deny "Graphiti clear_graph blocked — would wipe all temporal knowledge" ;;
  mcp__graphiti-memory__delete_entity_edge)
    deny "Graphiti edge deletion blocked — ask user to confirm" ;;
  mcp__graphiti-memory__delete_episode)
    deny "Graphiti episode deletion blocked — ask user to confirm" ;;
esac

# ============================================================
# --- email-body-format gate ---
# Outbound email: block hard-wrapped prose in plain-text bodies.
# Gmail renders text/plain without format=flowed literally, so a body
# reflowed to ~70 columns reaches the recipient as ragged short lines.
# Phi's own Gmail-composed mail is one unbroken line per paragraph.
# ============================================================

case "$TOOL_NAME" in
  mcp__google-workspace__draft_gmail_message \
  | mcp__google-workspace__send_gmail_message \
  | mcp__claude_ai_Gmail__create_draft \
  | mcp__claude_ai_Gmail__update_draft \
  | mcp__day-ai__create_email_draft)

    EMAIL_BODY=$(jq -r '(.tool_input.body // .tool_input.bodyText // .tool_input.content // .tool_input.message // empty)' <<< "$INPUT")
    EMAIL_FMT=$(jq -r '(.tool_input.body_format // .tool_input.bodyFormat // "plain")' <<< "$INPUT")

    if [ -n "$EMAIL_BODY" ]; then
      WRAP_HITS=$(EMAIL_BODY="$EMAIL_BODY" EMAIL_FMT="$EMAIL_FMT" python3 -c '
import os, re, sys

body = os.environ.get("EMAIL_BODY", "")
fmt = os.environ.get("EMAIL_FMT", "plain").lower()

# HTML bodies collapse whitespace when rendered, so source newlines are harmless.
if fmt == "html" or re.search(r"<(br|div|p|table)\b", body, re.I):
    print(0)
    sys.exit(0)

lines = body.split("\n")
hits = 0
for i in range(len(lines) - 1):
    cur = lines[i].rstrip()
    nxt = lines[i + 1].strip()
    if not nxt:
        continue                      # paragraph break: correct
    if cur[:1] in (" ", "\t", ">"):
        continue                      # indented code block or quoted reply
    if nxt[:1] in (" ", "\t", ">"):
        continue
    if len(cur) >= 45:
        hits += 1                     # long line butted against more prose
print(hits)
' 2>/dev/null)

      if [ "${WRAP_HITS:-0}" -ge 2 ]; then
        jq -n --arg reason "BLOCKED by safety-guard-mcp: email body is hard-wrapped prose ($WRAP_HITS wrapped lines). Gmail renders text/plain literally, so this reaches the recipient as ragged ~70-column lines. Fix: one paragraph = ONE unbroken line, blank line between paragraphs, never reflow to a column width. Prefer body_format=\"html\" with <div dir=\"ltr\"> and <br><br> between paragraphs, which matches how Phi composes mail in Gmail. Indented code blocks and quoted lines are exempt." \
          '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
        exit 0
      fi
    fi
    ;;
esac

# ============================================================
# Destructive SQL in any MCP tool that accepts SQL/query input
# ============================================================

# Extract SQL from common field names
SQL=$(jq -r '(.tool_input.sql // .tool_input.query // .tool_input.statement // empty)' <<< "$INPUT")
[ -z "$SQL" ] && exit 0

# Normalize to uppercase for matching
SQL_UPPER=$(echo "$SQL" | tr '[:lower:]' '[:upper:]')

echo "$SQL_UPPER" | grep -qE '\bDROP\s+DATABASE\b' && deny "DROP DATABASE — destructive SQL"
echo "$SQL_UPPER" | grep -qE '\bDROP\s+TABLE\b' && deny "DROP TABLE — destructive SQL"
echo "$SQL_UPPER" | grep -qE '\bTRUNCATE\s+TABLE\b' && deny "TRUNCATE TABLE — destructive SQL"
echo "$SQL_UPPER" | grep -qE '\bALTER\s+TABLE\s+.*\bDROP\b' && deny "ALTER TABLE DROP — destructive schema change"

# DELETE without WHERE clause
if echo "$SQL_UPPER" | grep -qE '\bDELETE\s+FROM\b'; then
  echo "$SQL_UPPER" | grep -qE '\bWHERE\b' || deny "DELETE FROM without WHERE clause — would delete all rows"
  echo "$SQL_UPPER" | grep -qE '\bWHERE\s+(1\s*=\s*1|TRUE)\b' && deny "DELETE FROM with a tautological WHERE (1=1/TRUE) — would delete all rows"
fi

# UPDATE ... SET without WHERE clause
if echo "$SQL_UPPER" | grep -qE '\bUPDATE\b' && echo "$SQL_UPPER" | grep -qE '\bSET\b'; then
  echo "$SQL_UPPER" | grep -qE '\bWHERE\b' || deny "UPDATE SET without WHERE clause — would update all rows"
  echo "$SQL_UPPER" | grep -qE '\bWHERE\s+(1\s*=\s*1|TRUE)\b' && deny "UPDATE SET with a tautological WHERE (1=1/TRUE) — would update all rows"
fi

# TRUNCATE without TABLE keyword (bare PostgreSQL form)
if echo "$SQL_UPPER" | grep -qE '\bTRUNCATE\b'; then
  echo "$SQL_UPPER" | grep -qE '\bTRUNCATE\s+TABLE\b' || deny "TRUNCATE without TABLE keyword — bare TRUNCATE would destroy all rows"
fi

# DROP SCHEMA
echo "$SQL_UPPER" | grep -qE '\bDROP\s+SCHEMA\b' && deny "DROP SCHEMA — destructive SQL"

exit 0
