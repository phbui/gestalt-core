#!/usr/bin/env bash
# resolve-encrypted-conflict.sh <path> [--ours-ref REF] [--theirs-ref REF] [--base-ref REF]
#
# Resolve a merge/rebase conflict in a git-crypt encrypted file (knowledge/**, personal/**).
# Git sees ciphertext, calls the file binary, refuses to merge, and leaves NO conflict markers
# in the working tree — so the normal "edit the markers" workflow silently does nothing.
# This decrypts all three sides through `git-crypt smudge`, runs a real 3-way merge on the
# plaintext, and writes the result back (the clean filter re-encrypts on `git add`).
#
#   tools/resolve-encrypted-conflict.sh knowledge/<kb-entry>.md
#   # edit any <<<<<<< markers it reports, then:
#   git add knowledge/<kb-entry>.md && git rebase --continue   (or git merge --continue)
#
# During a rebase, "ours" is the upstream you are replaying onto (REBASE_HEAD is the commit
# being replayed = "theirs" in git's confusing rebase vocabulary); the defaults below handle
# both rebase and merge without you having to think about it.
set -euo pipefail
FILE="${1:-}"
[ -z "$FILE" ] && { echo "usage: $(basename "$0") <path> [--ours-ref REF] [--theirs-ref REF] [--base-ref REF]" >&2; exit 2; }
shift || true
OURS=""; THEIRS=""; BASE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ours-ref)   OURS="$2"; shift 2;;
    --theirs-ref) THEIRS="$2"; shift 2;;
    --base-ref)   BASE="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
cd "$(git rev-parse --show-toplevel)"
command -v git-crypt >/dev/null || { echo "git-crypt not on PATH" >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Prefer the index stages git already recorded (1=base, 2=ours, 3=theirs) — they exist for any
# real conflict and are correct for both merge and rebase. Fall back to refs if asked.
stage() { git cat-file -p ":$1:$FILE" 2>/dev/null | git-crypt smudge; }
if [ -z "$OURS$THEIRS$BASE" ] && git ls-files -u -- "$FILE" | grep -q .; then
  stage 1 > "$TMP/base.md"; stage 2 > "$TMP/ours.md"; stage 3 > "$TMP/theirs.md"
  echo "using index stages (1=base 2=ours 3=theirs)"
else
  OURS="${OURS:-HEAD}"
  THEIRS="${THEIRS:-$(git rev-parse --verify REBASE_HEAD 2>/dev/null || echo MERGE_HEAD)}"
  BASE="${BASE:-$(git merge-base "$OURS" "$THEIRS")}"
  git show "$BASE:$FILE"   | git-crypt smudge > "$TMP/base.md"
  git show "$OURS:$FILE"   | git-crypt smudge > "$TMP/ours.md"
  git show "$THEIRS:$FILE" | git-crypt smudge > "$TMP/theirs.md"
  echo "using refs: base=$BASE ours=$OURS theirs=$THEIRS"
fi

for f in base ours theirs; do
  head -c 8 "$TMP/$f.md" | grep -q GITCRYPT && { echo "ERROR: $f side is still ciphertext — is this clone unlocked? (git-crypt unlock)" >&2; exit 1; }
done

set +e
git merge-file -L ours -L base -L theirs -p "$TMP/ours.md" "$TMP/base.md" "$TMP/theirs.md" > "$TMP/merged.md"
RC=$?
set -e
cp "$TMP/merged.md" "$FILE"

if [ "$RC" -gt 0 ]; then
  echo "merged with $RC conflict hunk(s) — edit the <<<<<<< markers in $FILE, then:"
  grep -n '^<<<<<<<\|^=======$\|^>>>>>>>' "$FILE" || true
else
  echo "merged cleanly into $FILE"
fi
echo "then: ./tools/gestalt rebuild && git add $FILE MANIFEST.md MANIFEST-papers.md GRAPH.md && git rebase --continue"
