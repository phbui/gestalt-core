#!/usr/bin/env bash
# PreToolUse hook for Bash: refuse `git push` inside an Overleaf clone unless a fetch or pull ran in the last 15 minutes.
# Enforces rules/overleaf-pull-first.md. Exit 2 blocks and feeds stderr to Claude. Any parse failure allows the call.
INPUT=$(cat)
CMD=$(jq -r '.tool_input.command // empty' <<< "$INPUT" 2>/dev/null)
CWD=$(jq -r '.cwd // empty' <<< "$INPUT" 2>/dev/null)
[ -z "$CMD" ] && exit 0
echo "$CMD" | grep -qE '(^|[;&|[:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push([[:space:]]|$)' || exit 0
DIR=$(echo "$CMD" | sed -nE 's/.*git[[:space:]]+-C[[:space:]]+([^[:space:]]+)[[:space:]]+push.*/\1/p' | head -1)
DIR="${DIR:-$CWD}"
DIR="${DIR/#\~/$HOME}"
[ -d "$DIR" ] || exit 0
URL=$(git -C "$DIR" remote get-url origin 2>/dev/null) || exit 0
echo "$URL" | grep -qi 'overleaf\.com' || exit 0
GITDIR=$(git -C "$DIR" rev-parse --absolute-git-dir 2>/dev/null) || exit 0
NOW=$(date +%s)
MT=$(stat -c %Y "$GITDIR/FETCH_HEAD" 2>/dev/null || echo 0)
AGE=$((NOW - MT))
if [ "$AGE" -gt 900 ]; then
  echo "BLOCKED: git push in an Overleaf clone with no fetch or pull in the last 15 minutes (last: ${AGE}s ago). Run 'git pull --rebase --autostash' in $DIR, reread the passage, then push. Rule: overleaf-pull-first." >&2
  exit 2
fi
exit 0
