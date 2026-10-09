#!/usr/bin/env bash
# PreToolUse hook for Bash tool — Tiers 1, 2, 4
# Blocks dangerous commands: system destroyers, destructive git, deploy/infra.
# Allowlist checked first to prevent false positives on safe patterns.

INPUT=$(cat)
CMD=$(jq -r '.tool_input.command // empty' <<< "$INPUT")
[ -z "$CMD" ] && exit 0

# Path normalization — /usr/bin/git → git, /bin/rm → rm
CMD_NORM=$(sed 's|^/[^ ]*/s\?bin/\(git\|rm\|bash\|sh\)|\\1|' <<< "$CMD")

# CMD_SCAN -- CMD_NORM with quoted substrings' CONTENT blanked out (quote markers kept,
# content dropped). F14, <kb-entry>: several
# TIER 4 deploy/infra denials below match on bare English phrases with no anchor to real
# command syntax, so they fire on a quoted SEARCH STRING that merely mentions the phrase
# (e.g. a gestalt search for a rule name) as readily as on a real invocation. Phrase-only
# greps scan CMD_SCAN so quoted text can never trigger them; every other tier keeps
# scanning CMD_NORM unchanged, so a real unquoted invocation is denied exactly as before.
CMD_SCAN=$(printf '%s' "$CMD_NORM" | sed -E 's/"[^"]*"/""/g')
CMD_SCAN=$(printf '%s' "$CMD_SCAN" | sed -E "s/'[^']*'/''/g")

# --- Logging helper ---
log_block() {
  local SCRIPT_DIR GESTALT_DIR STATE_DIR LOG
  SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
  GESTALT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
  # F10: repo's own tree is claude-tree/ (gestalt/.claude is a compat symlink to it).
  STATE_DIR="${GESTALT_STATE_DIR:-$GESTALT_DIR/claude-tree/gestalt}"
  LOG="$STATE_DIR/health.log"
  mkdir -p "$STATE_DIR" 2>/dev/null
  echo "$(date -Iseconds) BLOCKED: $1 — $CMD" >> "$LOG" 2>/dev/null
}

deny() {
  log_block "$1"
  jq -n --arg reason "BLOCKED by safety-guard-bash: $1. Ask the user to run this command manually if genuinely needed." \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$reason}}'
  exit 0
}

# ============================================================
# ============================================================
# PHBUI-PERSONAL-REPO-ALLOWLIST-MARKER
# Loosens exactly six git verb families (push incl. main, pull,
# checkout/switch, merge/rebase, commit, stash) for the user's own
# personal repos. The set of repos comes from an extended regex matched against
# `git remote get-url origin`. The regex is read from GESTALT_PERSONAL_REPO_RE, or else from the
# first line of the file $GESTALT_DIR/.personal-repos. With neither, the default never matches,
# so no repo is loosened.
# All other guards (system destroyers, deploy/infra, other repos, and
# destructive-but-unlisted git ops like reset --hard / clean -f /
# push --force / branch -D) are untouched.
# ============================================================
PHBUI_REPO=0
# PHBUI-ANY-GIT: consider any git verb in the line (covers `cd <dir> && git …`), not only a leading one
if echo "$CMD_NORM" | grep -qE '(^|&&|;|\|)\s*git\s'; then
  _PHBUI_CWD_JSON=$(jq -r '.cwd // empty' <<< "$INPUT")
  _PHBUI_C_DIR=$(echo "$CMD_NORM" | sed -n 's/.*git[[:space:]]\+-C[[:space:]]\+\([^[:space:]]\+\).*/\1/p')
  # PHBUI-CD-PREFIX: a leading `cd <dir> &&` names the repo too (expand ~ and $HOME)
  _PHBUI_CD_DIR=$(echo "$CMD_NORM" | sed -n 's/^[[:space:]]*cd[[:space:]]\+\([^[:space:];&]\+\)[[:space:]]*&&.*/\1/p' | sed "s#^~#$HOME#; s#^\$HOME#$HOME#")
  _PHBUI_REPO_DIR="${_PHBUI_C_DIR:-${_PHBUI_CD_DIR:-${_PHBUI_CWD_JSON:-$PWD}}}"
  _PHBUI_ORIGIN=$(git -C "$_PHBUI_REPO_DIR" remote get-url origin 2>/dev/null)
  _PHBUI_ORIGIN_RE="${GESTALT_PERSONAL_REPO_RE:-}"
  if [ -z "$_PHBUI_ORIGIN_RE" ]; then
    _PHBUI_CFG="${GESTALT_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}/.personal-repos"
    [ -r "$_PHBUI_CFG" ] && IFS= read -r _PHBUI_ORIGIN_RE < "$_PHBUI_CFG"
  fi
  # 'x^x' cannot match: a caret in the middle of an ERE is an anchor.
  _PHBUI_ORIGIN_RE="${_PHBUI_ORIGIN_RE:-x^x}"
  if echo "$_PHBUI_ORIGIN" | grep -qE "$_PHBUI_ORIGIN_RE"; then
    PHBUI_REPO=1
  fi
fi

log_allow_phbui() {
  local SCRIPT_DIR GESTALT_DIR STATE_DIR LOG
  SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
  GESTALT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
  # F10: repo's own tree is claude-tree/ (gestalt/.claude is a compat symlink to it).
  STATE_DIR="${GESTALT_STATE_DIR:-$GESTALT_DIR/claude-tree/gestalt}"
  LOG="$STATE_DIR/health.log"
  mkdir -p "$STATE_DIR" 2>/dev/null
  echo "$(date -Iseconds) ALLOWED(phbui-personal-repo): $1 — $CMD" >> "$LOG" 2>/dev/null
}

# PROTECTED BRANCHES — checked before everything else
# Never push to main/master. No exceptions. Use a PR.
# ============================================================

if echo "$CMD_NORM" | grep -qE '\bgit\s+push\s+.*\b(main|master)\b'; then
  if [ "$PHBUI_REPO" = "1" ]; then
    log_allow_phbui "push-to-main/master (phbui personal repo)"
  else
    deny "Pushing to main/master is forbidden. Use a feature branch and open a PR"
  fi
fi

# ============================================================
# ALLOWLIST — checked first, these patterns are always safe
# ============================================================

# Safe git patterns
echo "$CMD_NORM" | grep -qE '^git\s+checkout\s+(-b|--orphan)\s' && exit 0
echo "$CMD_NORM" | grep -qE '^git\s+restore\s+--staged\b' && exit 0
echo "$CMD_NORM" | grep -qE '^git\s+clean\s+[a-z-]*(-n|--dry-run)' && exit 0
# --force-with-lease is the safe form and is allowed wherever the push sits in the command line (a leading `cd dir &&` used to defeat the ^ anchor, 2026-10-08).
echo "$CMD_NORM" | grep -qE '\bgit\s+push\s+[^|;&]*--force-with-lease\b' && exit 0

# Safe rm targets
echo "$CMD_NORM" | grep -qE '^rm\s+.*\s/tmp/' && exit 0
echo "$CMD_NORM" | grep -qE '^rm\s+.*\s/var/tmp/' && exit 0

# ============================================================
# TIER 1 — System destroyers (hard block)
# ============================================================

echo "$CMD_NORM" | grep -qE 'rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*[rf][a-zA-Z]*|--recursive|--force|-r\s+-f|-f\s+-r)\s+/' && deny "rm -rf with root path — catastrophic filesystem deletion"
# The root-path line above requires a literal leading /, so `rm -rf ~`, $HOME, `.` and `..`
# all sailed past it. Additive: nothing previously denied becomes allowed, and a trailing
# path (~/Downloads, ./build) stays permitted.
echo "$CMD_NORM" | grep -qE 'rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*[rf][a-zA-Z]*|--recursive|--force|-r\s+-f|-f\s+-r)\s+(~|\$HOME|\$\{HOME\})/?(\s|$)' && deny "rm -rf on the home directory — catastrophic data loss"
echo "$CMD_NORM" | grep -qE 'rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*[rf][a-zA-Z]*|--recursive|--force|-r\s+-f|-f\s+-r)\s+\.\.?/?(\s|$)' && deny "rm -rf on the current/parent directory — use an explicit path"
echo "$CMD_NORM" | grep -qE '\bmkfs\.' && deny "mkfs — filesystem format would destroy data"
echo "$CMD_NORM" | grep -qE '\bdd\s+.*if=.*of=/dev/' && deny "dd to device — raw disk write"
echo "$CMD_NORM" | grep -qE ':\(\)\s*\{.*:\|:' && deny "Fork bomb detected"
echo "$CMD_NORM" | grep -qE '(curl|wget)\s+.*\|\s*(ba)?sh' && deny "Pipe-to-shell — remote code execution risk"
echo "$CMD_NORM" | grep -qE '(curl|wget)\s+.*\|\s*zsh' && deny "Pipe-to-shell — remote code execution risk"
echo "$CMD_NORM" | grep -qE '\b(ba|z)?sh\s+<\(\s*(curl|wget)' && deny "Process-substitution shell exec — remote code execution risk"
echo "$CMD_NORM" | grep -qE '\bchmod\s+(-R\s+)?777\s+/' && deny "chmod 777 on root path — destroys file permissions"
echo "$CMD_NORM" | grep -qE '>\s*/dev/(sd[a-z]|nvme[0-9])' && deny "Raw device write — would destroy disk data"

# ============================================================
# TIER 2 — Destructive git (block, suggest alternatives)
# ============================================================

echo "$CMD_NORM" | grep -qE '\bgit\s+reset\s+--hard' && deny "git reset --hard destroys uncommitted changes. Use 'git stash' first"
echo "$CMD_NORM" | grep -qE '\bgit\s+reset\s+--merge' && deny "git reset --merge can lose uncommitted changes"
echo "$CMD_NORM" | grep -qE '\bgit\s+clean\s+(-[a-zA-Z]*f[a-zA-Z]*|--force)' && deny "git clean -f permanently removes untracked files. Use 'git clean -n' to preview first"
echo "$CMD_NORM" | grep -qE '\bgit\s+push\s+.*--force\b' && deny "git push --force can destroy remote history. Use --force-with-lease"
echo "$CMD_NORM" | grep -qE '\bgit\s+push\b.*\s-[a-zA-Z]*f[a-zA-Z]*(\s|$)' && deny "git push -f can destroy remote history. Use --force-with-lease"
echo "$CMD_NORM" | grep -qE '\bgit\s+branch\s+-D\s' && deny "git branch -D force-deletes without merge check. Use -d for safety"
if echo "$CMD_NORM" | grep -qE '\bgit\s+stash\s+(drop|clear)\b'; then
  if [ "$PHBUI_REPO" = "1" ]; then
    log_allow_phbui "git stash drop/clear (phbui personal repo)"
  else
    deny "git stash drop/clear permanently deletes stashed work"
  fi
fi
if echo "$CMD_NORM" | grep -qE '\bgit\s+checkout\s+--\s'; then
  if [ "$PHBUI_REPO" = "1" ]; then
    log_allow_phbui "git checkout -- (phbui personal repo)"
  else
    deny "git checkout -- discards uncommitted changes. Use 'git stash' first"
  fi
fi
echo "$CMD_NORM" | grep -qE '\bgit\s+restore\s+[^-]' && deny "git restore discards working tree changes. Use 'git stash' or 'git restore --staged'"

# ============================================================
# ============================================================
# TIER 3 — Pre-push quality gate (platform repo)
# Runs biome ci before allowing git push from the platform repo.
# Catches formatting drift and unsorted imports that CI would reject.
# Skipped if biome is not configured or pnpm is not available.
# ============================================================

if echo "$CMD_NORM" | grep -qE '^\s*git\s+push\b'; then
  REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
  if [ -n "$REPO_ROOT" ] && [ "$(basename "$REPO_ROOT")" = "platform" ] && [ -f "$REPO_ROOT/biome.jsonc" ] && command -v pnpm >/dev/null 2>&1; then
    BIOME_OUTPUT=$(cd "$REPO_ROOT" && timeout 12 pnpm biome ci --enforce-assist=true --diagnostic-level=error 2>&1)
    if [ $? -ne 0 ]; then
      FIRST_FILE=$(echo "$BIOME_OUTPUT" | grep -oE '[a-z][a-z_-]*/[^[:space:]]*\.(tsx?|jsx?|css|jsonc?)' | head -1)
      deny "biome ci failed on platform/${FIRST_FILE:+ — first issue in $FIRST_FILE}. Run 'pnpm biome check --write' from platform/, stage the formatted files, then retry the push"
    fi
  fi
fi

# TIER 4 — Deploy/infra commands (block)
# ============================================================

echo "$CMD_SCAN" | grep -qE '\btask\s+.*deploy' && deny "Deploy task blocked — production deployment requires manual execution"
echo "$CMD_SCAN" | grep -qE '\btask\s+.*migrate' && deny "Migration task blocked — database migrations require manual execution"
echo "$CMD_SCAN" | grep -qE '\btask\s+secrets:' && deny "Secrets task blocked — credential operations require manual execution"
echo "$CMD_SCAN" | grep -qE '\bkubectl\s+delete\b' && deny "kubectl delete blocked — resource deletion requires manual execution"
echo "$CMD_SCAN" | grep -qE '\bkubectl\s+apply\b.*prod' && deny "kubectl apply to prod blocked — production changes require manual execution"
echo "$CMD_SCAN" | grep -qE '\bsupabase\s+db\s+push' && deny "supabase db push blocked — database push requires manual execution"
echo "$CMD_SCAN" | grep -qE '\bsupabase\s+migration' && deny "supabase migration blocked — migrations require manual execution"
echo "$CMD_SCAN" | grep -qE '\bterraform\s+destroy' && deny "terraform destroy blocked — infrastructure destruction requires manual execution"
echo "$CMD_SCAN" | grep -qE '\bterraform\s+apply' && deny "terraform apply blocked — infrastructure changes require manual execution"
echo "$CMD_SCAN" | grep -qE '\bdocker\s+system\s+prune' && deny "docker system prune blocked — container cleanup requires manual execution"



exit 0
