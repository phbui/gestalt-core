#!/usr/bin/env bash
# PostToolUse hook for Write|Edit to gestalt/ — Tier 5: content validation
# Validates frontmatter structure and index integrity after writes.
# Exit 2 feeds stderr to Claude as warning (file already written).

INPUT=$(cat)
FILE_PATH=$(jq -r '.tool_input.file_path // empty' <<< "$INPUT")
[ -z "$FILE_PATH" ] && exit 0

# no-unverified-claims layer 2: a "fixed/shipped/added X.py" claim that names a
# repo file which does not exist is the false-shipped pattern. Non-blocking warn
# (additionalContext), file-existence only (unambiguous, fast, low false-positive).
warn_missing_file_claims() {
  local f="$1" gd ref missing=""
  [ -f "$f" ] || return 0
  gd="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)" || return 0
  for ref in $(grep -oiE '(fixed|shipped|added|created|installed|wired)[^`]*`[a-zA-Z0-9_./-]+\.(py|sh|md|json|mdc)`' "$f" 2>/dev/null | grep -oE '`[^`]+`' | tr -d '`' | sort -u); do
    case "$ref" in *://*|/*|~*) continue ;; esac   # skip external/absolute paths
    if [ ! -e "$gd/$ref" ] && ! git -C "$gd" ls-files --error-unmatch "$ref" >/dev/null 2>&1; then
      missing="$missing $ref"
    fi
  done
  [ -n "$missing" ] && jq -n --arg m "$missing" '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":("no-unverified-claims: this entry claims a file was fixed/shipped/added but it is not in the repo:" + $m + " — verify with git log -S / confirm the path, or mark the claim uncommitted. Non-blocking.")}}'
  return 0
}

# ============================================================
# Knowledge entry validation (frontmatter structure)
# ============================================================

if echo "$FILE_PATH" | grep -qE '/knowledge/.*\.(md|mdc)$'; then
  [ ! -f "$FILE_PATH" ] && exit 0

  # Check non-empty
  if [ ! -s "$FILE_PATH" ]; then
    echo "Validation warning: $FILE_PATH is empty (0 bytes). This file should have content." >&2
    exit 2
  fi

  # Check frontmatter opening
  FIRST_LINE=$(head -1 "$FILE_PATH")
  if [ "$FIRST_LINE" != "---" ]; then
    echo "Validation warning: $FILE_PATH is missing YAML frontmatter (must start with ---). Add frontmatter with at least a title field." >&2
    exit 2
  fi

  # Check frontmatter closing
  CLOSE_LINE=$(awk 'NR>1 && /^---$/{print NR; exit}' "$FILE_PATH")
  if [ -z "$CLOSE_LINE" ]; then
    echo "Validation warning: $FILE_PATH has unclosed frontmatter (no closing ---). Add a closing --- after the YAML block." >&2
    exit 2
  fi

  # Check content after frontmatter
  TOTAL_LINES=$(wc -l < "$FILE_PATH")
  if [ "$TOTAL_LINES" -le "$CLOSE_LINE" ]; then
    echo "Validation warning: $FILE_PATH has frontmatter but no content body. Add content after the closing ---." >&2
    exit 2
  fi

  warn_missing_file_claims "$FILE_PATH"
  exit 0
fi

# Rules files intentionally have no frontmatter (always-loaded) — skip frontmatter check
if echo "$FILE_PATH" | grep -qE '/rules/.*\.(md|mdc)$'; then
  [ ! -f "$FILE_PATH" ] && exit 0

  # Check non-empty only
  if [ ! -s "$FILE_PATH" ]; then
    echo "Validation warning: $FILE_PATH is empty (0 bytes). This file should have content." >&2
    exit 2
  fi

  warn_missing_file_claims "$FILE_PATH"
  exit 0
fi

# ============================================================
# MANIFEST.md integrity check (entry count)
# ============================================================

if echo "$FILE_PATH" | grep -qE '/MANIFEST\.md$'; then
  [ ! -f "$FILE_PATH" ] && exit 0

  SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
  GESTALT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

  # Count knowledge files on disk
  FILE_COUNT=$(find "$GESTALT_DIR/knowledge" -name "*.md" -type f 2>/dev/null | wc -l)
  [ "$FILE_COUNT" -eq 0 ] && exit 0

  # Count entries referenced in MANIFEST (lines with [[ or table rows with |)
  # grep -c prints 0 AND exits 1 on no match, so `|| echo 0` appended a SECOND 0 and
  # the "$MANIFEST_COUNT" -lt test below died with "integer expression expected" —
  # silently disabling the truncation warning in the very case it guards.
  MANIFEST_COUNT=$(grep -cE '(\[\[|^\|.*\|)' "$FILE_PATH" 2>/dev/null | head -1)
  MANIFEST_COUNT=${MANIFEST_COUNT:-0}
  # Paper entries moved to MANIFEST-papers.md (2026-10-06), so count both files against the knowledge/ total.
  PAPERS_FILE="$(dirname "$FILE_PATH")/MANIFEST-papers.md"
  if [ -f "$PAPERS_FILE" ]; then
    PAPERS_COUNT=$(grep -cE '(\[\[|^\|.*\|)' "$PAPERS_FILE" 2>/dev/null | head -1)
    MANIFEST_COUNT=$(( MANIFEST_COUNT + ${PAPERS_COUNT:-0} ))
  fi

  # Warn if entries dropped by >30%
  THRESHOLD=$(( FILE_COUNT * 70 / 100 ))
  if [ "$MANIFEST_COUNT" -lt "$THRESHOLD" ]; then
    cat <<EOF
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"WARNING: MANIFEST.md lists ~${MANIFEST_COUNT} entries but knowledge/ has ${FILE_COUNT} files. Possible truncation — verify no entries were accidentally dropped."}}
EOF
  fi

  exit 0
fi

exit 0
