#!/usr/bin/env bash
# PreToolUse hook for Write|Edit to gestalt/ — Tier 5: backup-before-write
# Copies the file before modification. Never blocks.

INPUT=$(cat)
FILE_PATH=$(jq -r '.tool_input.file_path // empty' <<< "$INPUT")
[ -z "$FILE_PATH" ] && exit 0

# Only backup gestalt knowledge entries and indices
case "$FILE_PATH" in
  */gestalt/knowledge/*.md|*/gestalt/MANIFEST.md|*/gestalt/MANIFEST-papers.md|*/gestalt/GRAPH.md|*/gestalt/SOURCES.md) ;;
  *) exit 0 ;;
esac

# Skip if file doesn't exist yet (new file)
[ ! -f "$FILE_PATH" ] && exit 0

# Derive backup path
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
GESTALT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKUP_DIR="$GESTALT_DIR/backups/knowledge"
BASENAME=$(basename "$FILE_PATH")

mkdir -p "$BACKUP_DIR" 2>/dev/null
cp "$FILE_PATH" "$BACKUP_DIR/${BASENAME}.$(date +%s).bak" 2>/dev/null || true

# Clean old backups (>30 days) in background
find "$BACKUP_DIR" -name "*.bak" -mtime +30 -delete 2>/dev/null &

exit 0
