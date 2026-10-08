#!/bin/bash
# Discrimination test for GESTALT_SYNC_SKIP_NAMES (tools/gestalt-graphiti-sync.sh).
#
# The defect this pins: the python chunker read os.environ['_SKIP'], but the bash line
# that invokes it passed only _F _N _G _C _I. Nothing ever set _SKIP, so the skip set was
# always empty and every chunk of every slug was always re-sent. That re-landed known
# chunks as duplicate episodes and starved the one genuinely missing chunk, which in turn
# made the drain runner's name-count progress check read "no growth" and declare healthy
# work a stall.
#
# The harness replays the chunker block of the real script verbatim, so the defective bash
# line itself is under test rather than a paraphrase of it. Covers payload GENERATION,
# not the HTTP POST.
#
# Revert the fix (drop _SKIP= from the chunker's env prefix) and case 1 must FAIL.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO/tools/gestalt-graphiti-sync.sh"
ENTRY="$REPO/knowledge/large-entry.md"
[ -f "$ENTRY" ] || { echo "SKIP: fixture $ENTRY missing"; exit 0; }

HARNESS="$(mktemp)"; MANIFEST="$(mktemp)"; trap 'rm -f "$HARNESS" "$MANIFEST"' EXIT
# Locate the chunker by MARKER rather than by line number. This used to `sed -n 226,256p`,
# which quietly became the wrong span once the script grew and then reported an empty payload
# list as though the skip logic had broken.
# Anchor to the line START so this matches `payloads=$(` and NOT `s_payloads=$(`, the
# summaries chunker, which references DIGEST_FILE and dies under set -u in this harness.
START=$(grep -nE '^[[:space:]]*payloads=\$\(' "$SCRIPT" | head -1 | cut -d: -f1)
END=$(awk -v s="$START" 'NR>s && /^\)$/ {print NR; exit}' "$SCRIPT")
{ echo '#!/bin/bash'; echo 'set -euo pipefail'
  echo 'f="$1"; name="$2"; GROUP=gestalt; i=1'
  # The chunker reads shell variables the surrounding script sets. Supply them or it dies on
  # `set -u` and emits nothing, which is indistinguishable from the skip list eating everything.
  # Empty chunk state means every chunk counts as changed, which is what this test needs: it
  # measures the SKIP set, not the content-hash gate.
  echo "CHUNK_STATE_JSON='{}'; FORCE_CHUNKS=0; CHUNKS_TMP=$MANIFEST"
  sed -n "${START},${END}p" "$SCRIPT"
  echo 'printf "%s\n" "$payloads" | python3 -c "import sys,json;[print(json.loads(l)[\"params\"][\"arguments\"][\"name\"]) for l in sys.stdin if l.strip()]"'
} > "$HARNESS"; chmod +x "$HARNESS"

fail=0

# Derive the chunk count instead of hardcoding it. The fixture entry is a live knowledge file
# that grows, and the previous version asserted "9" until the entry became 11 chunks, at which
# point all three cases failed for a reason that had nothing to do with the skip logic under
# test. A content-dependent constant in an assertion is a test that rots on a schedule.
N=$(unset GESTALT_SYNC_SKIP_NAMES; "$HARNESS" "$ENTRY" kb-large-entry | grep -c .)
[ "$N" -ge 2 ] || { echo "SKIP: fixture chunks into $N part(s), need at least 2"; exit 0; }

# 1. RED->GREEN: with every chunk but the last named as already landed, only the last may be emitted.
skip_list=$(seq 1 $((N-1)) | sed 's/^/kb-large-entry#/')
got=$(GESTALT_SYNC_SKIP_NAMES="$skip_list" "$HARNESS" "$ENTRY" kb-large-entry | tr '\n' ' ' | xargs)
want="kb-large-entry#$N"
[ "$got" = "$want" ] && echo "PASS skip honored: $got" || { echo "FAIL skip ignored: want '$want' got '$got'"; fail=1; }

# 2. CONTROL: no skip list means the full send path is untouched.
n=$(unset GESTALT_SYNC_SKIP_NAMES; "$HARNESS" "$ENTRY" kb-large-entry | grep -c .)
[ "$n" = "$N" ] && echo "PASS control emits all $N" || { echo "FAIL control emitted $n, want $N"; fail=1; }

# 3. Empty skip list must behave as no skip list, not as "skip everything".
n=$(GESTALT_SYNC_SKIP_NAMES="" "$HARNESS" "$ENTRY" kb-large-entry | grep -c .)
[ "$n" = "$N" ] && echo "PASS empty skip emits all $N" || { echo "FAIL empty skip emitted $n, want $N"; fail=1; }

exit $fail
