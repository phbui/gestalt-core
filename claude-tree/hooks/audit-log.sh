#!/usr/bin/env bash
# PostToolUse async hook for Bash tool — audit logger
# Writes all executed commands to ~/.claude/audit.log in JSONL format.
# Runs with async:true so it never adds latency.

INPUT=$(cat)

LOG="$HOME/.claude/audit.log"

# Log rotation: truncate if >5MB
[ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ] && : > "$LOG"

# Write JSONL entry
jq -c '{
  ts: (now | strftime("%Y-%m-%dT%H:%M:%SZ")),
  session: .session_id,
  mode: .permission_mode,
  tool: .tool_name,
  cmd: (.tool_input.command // .tool_input.file_path // "unknown")
}' <<< "$INPUT" >> "$LOG" 2>/dev/null

exit 0
