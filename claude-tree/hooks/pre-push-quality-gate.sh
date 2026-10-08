#!/usr/bin/env bash
# PreToolUse hook for Bash tool — quality gate on git push.
#
# A push must never leave the shell unless the repo's own CI gates pass
# locally. Memories and Stop hooks proved insufficient: memories are advisory,
# and the Stop-hook gate fires at end-of-turn — after a mid-turn push has
# already happened (observed 2026-07-10: a PR pushed with failing
# py:check/ts:check/bake CI). This hook is the mechanical enforcement.
#
# Behavior:
#   - Only engages when the command contains `git push` (deletions, dry runs,
#     and repos without a Taskfile pass through).
#   - Runs `task py:check` and `task ts:check` (whichever exist) in the target
#     repo. Any failure -> deny with the failing tail.
#   - Caches success per (repo, HEAD, clean-tree): a sentinel in .git/ lets
#     repeat pushes of an already-validated commit skip the ~3 min gate.
#   - Escape hatch: QUALITY_GATE_SKIP=1 in the environment (user-set only).

INPUT=$(cat)
CMD=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
CWD=$(jq -r '.cwd // empty' <<< "$INPUT")
[ -z "$CMD" ] && exit 0

# Path normalization — /usr/bin/git → git
CMD_NORM=$(sed 's|^/[^ ]*/s\?bin/git|git|' <<< "$CMD")

# Only engage on an actual push.
echo "$CMD_NORM" | grep -qE '\bgit\s+(-C\s+\S+\s+)?push\b' || exit 0

# Pass-throughs: branch deletion, dry runs (nothing lands on the remote).
echo "$CMD_NORM" | grep -qE '\bpush\b.*(--delete|--dry-run|\s-d\s|\s:refs/| :[a-zA-Z])' && exit 0

# Explicit user-approved skip.
[ "${QUALITY_GATE_SKIP:-}" = "1" ] && exit 0

deny() {
  jq -n --arg reason "BLOCKED by pre-push-quality-gate: $1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
  exit 0
}

# Resolve the repo being pushed. `git -C <dir> push` wins over cwd.
REPO_DIR="$CWD"
C_DIR=$(echo "$CMD_NORM" | sed -n 's/.*git[[:space:]]\+-C[[:space:]]\+\([^[:space:]]\+\).*/\1/p')
[ -n "$C_DIR" ] && REPO_DIR="$C_DIR"
ROOT=$(git -C "$REPO_DIR" rev-parse --show-toplevel 2>/dev/null) || exit 0

# ============================================================
# PHBUI-PERSONAL-REPO-ALLOWLIST-MARKER
# Personal repos never gate on CI (push family loosened per user request).
# ============================================================
_PHBUI_ORIGIN=$(git -C "$ROOT" remote get-url origin 2>/dev/null)
if echo "$_PHBUI_ORIGIN" | grep -qE '^(https://|git@|ssh://git@)?github\.com[:/]phbui/(gestalt|artifacts)(\.git)?/?$'; then
  exit 0
fi

# No Taskfile -> nothing to gate (docs repos etc.).
[ -f "$ROOT/Taskfile.yaml" ] || exit 0

# Which gates does this repo define?
GATES=()
TASKS=$(cd "$ROOT" && task --list-all 2>/dev/null || true)
echo "$TASKS" | grep -q 'py:check' && GATES+=("py:check")
echo "$TASKS" | grep -q 'ts:check' && GATES+=("ts:check")
[ ${#GATES[@]} -eq 0 ] && exit 0

# Sentinel: skip the gate if this exact HEAD already passed and the worktree
# is clean (a dirty tree could differ from the HEAD being pushed).
HEAD_SHA=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null)
DIRTY=$(git -C "$ROOT" status --porcelain 2>/dev/null | head -1)
SENTINEL="$ROOT/.git/.quality-gate-pass"
if [ -z "$DIRTY" ] && [ -f "$SENTINEL" ] && [ "$(cat "$SENTINEL" 2>/dev/null)" = "$HEAD_SHA" ]; then
  exit 0
fi

# Run the gates. Deny on the first failure with its tail for context.
for GATE in "${GATES[@]}"; do
  if ! OUT=$(cd "$ROOT" && task "$GATE" 2>&1); then
    TAIL=$(echo "$OUT" | grep -vE '^\s*$' | tail -8)
    deny "task $GATE FAILED in $ROOT — fix before pushing. Tail: $TAIL"
  fi
done

# All gates green: cache for this HEAD (clean tree means HEAD is exactly what
# was checked; the next push of the same sha skips the gate).
[ -z "$DIRTY" ] && echo "$HEAD_SHA" > "$SENTINEL" 2>/dev/null
exit 0
