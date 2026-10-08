#!/usr/bin/env bash
# WorktreeRemove hook — cleans up composite worktrees.
# Removes git worktrees properly (restoring .git/worktrees/ tracking)
# then deletes the directory.
#
# stdin:  JSON {session_id, cwd, hook_event_name, name}
# stdout: (none — cleanup only)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
source "$SCRIPT_DIR/lib.sh" && gestalt_init

INPUT=$(cat)
NAME=$(jq -r '.name // empty' <<< "$INPUT")
CWD=$(jq -r '.cwd // empty' <<< "$INPUT")

WORKTREE_PATH="/tmp/claude-worktree-${NAME}"
SOURCE="${CWD:-$WORKSPACE}"

if [ ! -d "$WORKTREE_PATH" ]; then
    exit 0
fi

# --- Remove git worktrees in parallel ---
for item in "$SOURCE"/*/; do
    [ ! -d "$item/.git" ] && continue
    dname="$(basename "$item")"
    target="$WORKTREE_PATH/$dname"

    if [ -d "$target" ] && [ ! -L "$target" ]; then
        git -C "$item" worktree remove --force "$target" 2>>"$LOG" &
    fi
done
wait

# --- Prune stale refs ---
for item in "$SOURCE"/*/; do
    [ ! -d "$item/.git" ] && continue
    git -C "$item" worktree prune 2>/dev/null &
done
wait

# --- Clean up ---
rm -rf "$WORKTREE_PATH" 2>>"$LOG"

echo "$(date -Iseconds) INFO: worktree-remove: $WORKTREE_PATH ($NAME)" >> "$LOG"
exit 0
