#!/bin/bash
set -euo pipefail

# Git Bash's MSYS runtime defaults to winsymlinks:deepcopy, so the `ln -s` calls
# below would silently deep-copy instead of linking. Inert on Linux/macOS.
# https://www.msys2.org/docs/symlinks/
export MSYS=winsymlinks:nativestrict

WORKSPACE="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
GESTALT_DIR="$WORKSPACE/gestalt"

CURSOR_RULE="$GESTALT_DIR/.cursor/rules/gestalt.mdc"
# F10: gestalt's own tree lives at claude-tree/ (gestalt/.claude is a compat symlink to it).
CLAUDE_RULE="$GESTALT_DIR/claude-tree/rules/gestalt.md"

if [ ! -f "$CURSOR_RULE" ]; then
    echo "Error: gestalt.mdc not found at $CURSOR_RULE"
    exit 1
fi

if [ ! -f "$CLAUDE_RULE" ]; then
    echo "Error: gestalt.md not found at $CLAUDE_RULE"
    exit 1
fi

count=0
for repo in "$WORKSPACE"/*/; do
    repo_name=$(basename "$repo")

    [ "$repo_name" = "gestalt" ] && continue
    [ "$repo_name" = "worktrees" ] && continue
    [ "$repo_name" = "results" ] && continue
    [ ! -d "$repo/.git" ] && [ ! -d "$repo/.cursor" ] && [ ! -d "$repo/.claude" ] && continue

    # Cursor rule
    cursor_dir="$repo/.cursor/rules"
    mkdir -p "$cursor_dir"
    cursor_target="$cursor_dir/gestalt.mdc"

    if [ -L "$cursor_target" ]; then
        echo "  cursor skip (symlink exists): $repo_name"
    elif [ -f "$cursor_target" ]; then
        echo "  cursor skip (file exists):    $repo_name"
    else
        ln -s "$CURSOR_RULE" "$cursor_target"
        echo "  cursor linked:                $repo_name"
        count=$((count + 1))
    fi

    # Claude Code rule
    claude_dir="$repo/.claude/rules"
    mkdir -p "$claude_dir"
    claude_target="$claude_dir/gestalt.md"

    if [ -L "$claude_target" ]; then
        echo "  claude skip (symlink exists): $repo_name"
    elif [ -f "$claude_target" ]; then
        echo "  claude skip (file exists):    $repo_name"
    else
        ln -s "$CLAUDE_RULE" "$claude_target"
        echo "  claude linked:                $repo_name"
        count=$((count + 1))
    fi
done

echo ""
echo "Done. Created $count new symlink(s)."
