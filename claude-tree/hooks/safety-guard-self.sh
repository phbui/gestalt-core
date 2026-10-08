#!/usr/bin/env bash
# PreToolUse hook for Write|Edit — Tier 0: self-protection
# Prevents the agent from disabling its own safety hooks or settings.
# Uses exit 2 (hardest block) — stderr fed directly to Claude.
# This script is deployed BOTH in gestalt/claude-tree/hooks/ (exposed to the harness
# via the gestalt/.claude -> claude-tree compat symlink, F10) AND ~/.claude/hooks/

INPUT=$(cat)
FILE_PATH=$(jq -r '.tool_input.file_path // empty' <<< "$INPUT")
[ -z "$FILE_PATH" ] && exit 0

# ============================================================
# PROTECTED PATHS — the safety system itself
# ============================================================

# Settings files (hook configuration lives here). Matches both the compat-symlink
# spelling (gestalt/.claude/settings.json) and the real repo-tree spelling
# (gestalt/claude-tree/settings.json) — an agent could target either.
if echo "$FILE_PATH" | grep -qE '(\.claude|claude-tree)/settings\.json$'; then
  echo "BLOCKED: Cannot modify .claude/settings.json (claude-tree/settings.json) — this contains safety hook configuration. Ask the user to edit it manually." >&2
  exit 2
fi
if echo "$FILE_PATH" | grep -qE '(\.claude|claude-tree)/settings\.local\.json$'; then
  echo "BLOCKED: Cannot modify .claude/settings.local.json (claude-tree/settings.local.json) — this contains permission rules. Ask the user to edit it manually." >&2
  exit 2
fi

# All hook scripts (guards, lifecycle hooks, audit logger, shared library)
if echo "$FILE_PATH" | grep -qE '(\.claude|claude-tree)/hooks/.*\.sh$'; then
  echo "BLOCKED: Cannot modify hook scripts — these run at every session and tool use. Ask the user to edit them manually." >&2
  exit 2
fi

# Global hooks directory
if echo "$FILE_PATH" | grep -qE "$HOME/\.claude/hooks/"; then
  echo "BLOCKED: Cannot modify global hooks at ~/.claude/hooks/ — these are system-level safety. Ask the user to edit them manually." >&2
  exit 2
fi

# Global settings
if echo "$FILE_PATH" | grep -qE "$HOME/\.claude/settings\.json$"; then
  echo "BLOCKED: Cannot modify ~/.claude/settings.json — this contains global safety configuration. Ask the user to edit it manually." >&2
  exit 2
fi

exit 0
