#!/usr/bin/env bash
# PostToolUse hook for Write|Edit to .claude/ (claude-tree/) or .cursor/ — Tier 6: dual-sync check
# Warns when one side is modified without the counterpart existing.
# Advisory only — never blocks.
#
# F10 (<kb-entry>): the repo's own Claude Code tree physically
# lives at claude-tree/, exposed to the harness via a gestalt/.claude -> claude-tree
# compat symlink. An agent may spell an edit target either way, so every match/rewrite
# below recognizes both "claude-tree/" and ".claude/" as the same repo-internal tree.

INPUT=$(cat)
FILE_PATH=$(jq -r '.tool_input.file_path // empty' <<< "$INPUT")
[ -z "$FILE_PATH" ] && exit 0

# Only check .claude/, claude-tree/, and .cursor/ files
echo "$FILE_PATH" | grep -qE '(\.claude/|claude-tree/|\.cursor/)' || exit 0

# Skip settings files (no 1:1 counterpart)
echo "$FILE_PATH" | grep -qE 'settings(\.local)?\.json$' && exit 0

# Skip hooks (different mechanism per tool)
echo "$FILE_PATH" | grep -qE '/hooks/' && exit 0

# Determine counterpart
COUNTERPART=""
if echo "$FILE_PATH" | grep -qE '(\.claude|claude-tree)/'; then
  # .claude|claude-tree → .cursor (use # as sed delimiter — | is a regex operator here)
  COUNTERPART=$(echo "$FILE_PATH" | sed -E 's#(\.claude|claude-tree)/#.cursor/#')
  # Rules: .md → .mdc
  if echo "$FILE_PATH" | grep -qE '(\.claude|claude-tree)/rules/.*\.md$'; then
    COUNTERPART=$(echo "$COUNTERPART" | sed 's|\.md$|.mdc|')
  fi
elif echo "$FILE_PATH" | grep -qE '\.cursor/'; then
  # .cursor → .claude (compat spelling; also resolves via the claude-tree symlink)
  COUNTERPART=$(echo "$FILE_PATH" | sed 's|\.cursor/|.claude/|')
  # Rules: .mdc → .md
  if echo "$FILE_PATH" | grep -qE '\.cursor/rules/.*\.mdc$'; then
    COUNTERPART=$(echo "$COUNTERPART" | sed 's|\.mdc$|.md|')
  fi
fi

[ -z "$COUNTERPART" ] && exit 0

# Check if counterpart exists
if [ ! -f "$COUNTERPART" ]; then
  jq -n --arg path "$FILE_PATH" --arg counterpart "$COUNTERPART" \
    '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:("Dual-sync reminder: You modified " + $path + ". The counterpart " + $counterpart + " needs to be created or updated to stay in sync per the dual-agent-sync rule.")}}'
  exit 0
fi

# Counterpart exists: warn if it has no uncommitted change while the edited
# file does — the usual sign the mirror update was forgotten (existence alone
# proved insufficient; content drifts invisibly across fleet nodes otherwise).
REPO_DIR=$(cd "$(dirname "$FILE_PATH")" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)
if [ -n "$REPO_DIR" ]; then
  FILE_DIRTY=$(git -C "$REPO_DIR" status --porcelain -- "$FILE_PATH" 2>/dev/null)
  CP_DIRTY=$(git -C "$REPO_DIR" status --porcelain -- "$COUNTERPART" 2>/dev/null)
  if [ -n "$FILE_DIRTY" ] && [ -z "$CP_DIRTY" ]; then
    jq -n --arg path "$FILE_PATH" --arg counterpart "$COUNTERPART" \
      '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:("Dual-sync reminder: " + $path + " is modified but its counterpart " + $counterpart + " is untouched. Update the counterpart too per the dual-agent-sync rule (unless this change is genuinely one-sided).")}}'
  fi
fi

exit 0
