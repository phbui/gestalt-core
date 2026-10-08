#!/usr/bin/env bash
# WorktreeCreate hook — composite worktree for multi-repo workspaces.
# Creates parallel git worktrees for all repos, symlinks everything else.
# This enables `isolation: "worktree"` when Claude Code opens at a non-git
# workspace root (e.g., ~/Documents/GitHub/) containing multiple git repos.
#
# stdin:  JSON {session_id, cwd, hook_event_name, name}
# stdout: absolute worktree path (ONLY output — diagnostics go to log)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
source "$SCRIPT_DIR/lib.sh" && gestalt_init

INPUT=$(cat)
NAME=$(jq -r '.name // empty' <<< "$INPUT")
CWD=$(jq -r '.cwd // empty' <<< "$INPUT")

if [ -z "$NAME" ]; then
    echo "$(date -Iseconds) ERROR: worktree-create: missing name" >> "$LOG"
    exit 1
fi

# Generate worktree path from agent name
WORKTREE_PATH="/tmp/claude-worktree-${NAME}"

# Use CWD from stdin, fall back to WORKSPACE from lib.sh
SOURCE="${CWD:-$WORKSPACE}"

# Single-repo case (2026-08-29): when the session's cwd IS a git repo (e.g.
# opened at gestalt/ instead of the workspace root), the composite loop below
# would symlink every subdirectory — none has its own .git — and hand the agent
# a fake "worktree" with no isolation and no git at all. Two agents hit that
# dead end before this branch existed. A repo SOURCE gets one real worktree.
if [ -e "$SOURCE/.git" ]; then
    git -C "$SOURCE" worktree prune 2>/dev/null
    # git-crypt cannot smudge in a linked worktree (key lookup resolves against
    # the worktree's private gitdir — upstream limitation), so a naive
    # `worktree add` dies at checkout. Detect encrypted paths, check out RAW
    # (smudge=cat), then mirror the decrypted working copies from the source
    # and mark them skip-worktree: agents read real content, `git status`
    # stays clean, and an accidental commit of plaintext is structurally
    # blocked (the index still holds the encrypted blobs).
    CRYPT_FILES=$(git -C "$SOURCE" ls-files | git -C "$SOURCE" check-attr --stdin filter 2>/dev/null | awk -F': ' '$3=="git-crypt"{print $1}')
    if [ -n "$CRYPT_FILES" ]; then
        ADD_CFG=(-c filter.git-crypt.smudge=cat -c filter.git-crypt.clean=cat -c filter.git-crypt.required=false)
    else
        ADD_CFG=()
    fi
    if git -C "$SOURCE" "${ADD_CFG[@]}" worktree add --detach --quiet "$WORKTREE_PATH" HEAD 2>>"$LOG"; then
        if [ -n "$CRYPT_FILES" ]; then
            while IFS= read -r f; do
                [ -f "$SOURCE/$f" ] || continue
                mkdir -p "$WORKTREE_PATH/$(dirname "$f")"
                cp -p "$SOURCE/$f" "$WORKTREE_PATH/$f"
            done <<< "$CRYPT_FILES"
            printf '%s\n' "$CRYPT_FILES" | git -C "$WORKTREE_PATH" update-index --stdin --skip-worktree 2>>"$LOG" \
                || printf '%s\n' "$CRYPT_FILES" | while IFS= read -r f; do git -C "$WORKTREE_PATH" update-index --skip-worktree -- "$f" 2>>"$LOG"; done
        fi
        echo "$(date -Iseconds) INFO: worktree-create: single-repo worktree $WORKTREE_PATH from $SOURCE ($NAME, $(printf '%s' "$CRYPT_FILES" | grep -c . ) crypt files mirrored)" >> "$LOG"
        echo "$WORKTREE_PATH"
        exit 0
    fi
    echo "$(date -Iseconds) ERROR: worktree-create: git worktree add failed for repo $SOURCE" >> "$LOG"
    exit 1
fi

mkdir -p "$WORKTREE_PATH"

# --- Git repos: parallel worktree creation ---
for item in "$SOURCE"/*/; do
    [ ! -d "$item" ] && continue
    name="$(basename "$item")"
    target="$WORKTREE_PATH/$name"

    if [ -d "$item/.git" ]; then
        (
            git -C "$item" worktree prune 2>/dev/null
            if git -C "$item" worktree add --detach --quiet "$target" HEAD 2>>"$LOG"; then
                exit 0
            else
                git -C "$item" worktree remove --force "$target" 2>/dev/null
                git -C "$item" worktree add --detach --quiet "$target" HEAD 2>>"$LOG" && exit 0
                ln -sfn "$item" "$target" 2>/dev/null
                echo "$(date -Iseconds) WARN: worktree-create: $name fallback to symlink" >> "$LOG"
                exit 1
            fi
        ) &
    else
        ln -sfn "$item" "$target" 2>/dev/null
    fi
done
wait

# --- Dotfiles/dotdirs (skip .git) ---
for item in "$SOURCE"/.[!.]*; do
    [ ! -e "$item" ] && continue
    dname="$(basename "$item")"
    [ "$dname" = ".git" ] && continue
    target="$WORKTREE_PATH/$dname"
    [ ! -e "$target" ] && ln -sfn "$item" "$target" 2>/dev/null
done

# --- Top-level files ---
for item in "$SOURCE"/*; do
    [ -f "$item" ] || continue
    fname="$(basename "$item")"
    target="$WORKTREE_PATH/$fname"
    [ ! -e "$target" ] && ln -sfn "$item" "$target" 2>/dev/null
done

echo "$(date -Iseconds) INFO: worktree-create: $WORKTREE_PATH ($NAME)" >> "$LOG"
echo "$WORKTREE_PATH"
