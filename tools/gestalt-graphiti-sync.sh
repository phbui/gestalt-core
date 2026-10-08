#!/usr/bin/env bash
# One hash-gated path from knowledge/*.md to Graphiti. Replaces the three
# previously-separate write paths this repo used to have (per-session Stop hook
# POST, the never-scheduled tools/gestalt-capture.sh digest, and /save's ad-hoc
# add_memory call) per <kb-entry> F9 (^memory-triplication).
#
# Usage:
#   gestalt-graphiti-sync.sh                 # sync every knowledge/*.md whose
#                                             # sha1 changed since its last sync
#   gestalt-graphiti-sync.sh --all           # force-sync every entry
#   gestalt-graphiti-sync.sh --dry-run       # print what would sync; no network
#                                             # calls, no state file writes
#   gestalt-graphiti-sync.sh <slug>          # sync exactly one slug (what /save
#                                             # and any other single-entry writer
#                                             # should call after writing a file)
#   gestalt-graphiti-sync.sh --force <slug>  # re-send EVERY chunk of that slug
#   gestalt-graphiti-sync.sh --reconcile [slug ...] [--resend] [--dry-run]
#                                             # ask the graph which acknowledged chunks exist as
#                                             # Episodic nodes, mark those landed, print the rest
#                                             # as "missing<TAB>name". --resend re-sends only the
#                                             # missing ones (GESTALT_SYNC_RESEND_NAMES path).
#                                             # --dry-run prints and changes nothing. When the graph
#                                             # cannot be queried from this node it says so, exits 5
#                                             # and leaves the ledger alone.
#
# Exit codes: 0 done or nothing to do; 1 a real fault; 2 bad flag; 4 the ledger is corrupt (nothing sent);
# 5 --reconcile could not reach the graph; 75 graphiti was unhealthy and the run skipped its work, but ONLY when
# GESTALT_SYNC_EXIT75=1 (timer units set it, with SuccessExitStatus=75). Unset, a skip still exits 0 so the
# existing callers (/save, drain-runner, corpus-sequencer) behave as before. Every skip logs "reason=<why>".
# A run that cannot get the lock logs "reason=locked" and exits 0 without sending.
#
# Reconcile from a leaf: the default GESTALT_FALKORDB_CLI is a local `docker exec gestalt-falkordb redis-cli`,
# which only works on the hub. From another node point it at the hub over ssh, for example
#   GESTALT_FALKORDB_CLI="ssh hub docker exec -i gestalt-falkordb redis-cli" gestalt-graphiti-sync.sh --reconcile
# (the first word after the command may be any wrapper that ends in redis-cli arguments).
#
# State: $STATE_DIR/graphiti-sync.json -- {slug: sha1} plus a reserved "#chunks"
# key holding {chunk-episode-name: sha1}. The per-chunk hashes are the real gate. A chunk is written there the
# moment its POST is acknowledged (queued, not landed). An optional "#landed" key holds the names the graph was
# seen to hold; --reconcile maintains it. Ledger code lives in tools/graphiti_sync/ledger.py. Writes are atomic,
# runs are serialised by flock on $STATE_DIR/ledger.lock, and a corrupt ledger stops the run before any send.
#
# WHY PER CHUNK (2026-09-03). The gate used to be whole-file, and naming a slug
# bypassed it entirely, so `sync.sh <slug>` re-sent every chunk unconditionally.
# /save calls exactly that after every write, so seven routine /save runs in one
# evening re-queued 54 chunks and put 50 duplicate episodes into the graph: the
# duplicate count for an entry equalled the number of times it was synced,
# regardless of how little of it changed. A one-word edit cost a full re-send.
# Now an explicit slug NEVER implies --force; it only selects the entry, and each
# chunk is sent only when its own content hash changed, when the caller lists it
# in GESTALT_SYNC_RESEND_NAMES (the reconciliation path for chunks that were
# queued but never landed), or under --force. GESTALT_SYNC_ONLY_RESEND=1 sends the
# named chunks and nothing else (one chunk per invocation for a runner).
#
# A chunk whose CONTENT changed does get a new episode under the same name. That
# is Graphiti's model working as intended and is not a duplicate. A duplicate is
# same name AND same content hash, which is what a dedupe should collapse.
#
# --summaries: session-summary digest (what gestalt-capture.sh used to do) --
# merges $GESTALT_SESSIONS_DIR/*.md newer than the cursor into one episode.
# Implemented 2026-08-29 (F9 TODO closed); cursor advances only on full success.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
export GESTALT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# gestalt_init sources ~/.fleet/fleet.env, which SETS GRAPHITI_URL and therefore silently
# overwrites one the caller exported. That made the target un-redirectable: an integration
# test that set GRAPHITI_URL to a local stub still posted to the production graph, and on
# 2026-09-03 leaked three kb-fixture episodes into it before the cause was found. Preserve an
# explicitly-provided value across init; fleet.env remains the default when none was given.
_CALLER_GRAPHITI_URL="${GRAPHITI_URL:-}"
source "$GESTALT_DIR/.claude/hooks/lib.sh" && gestalt_init
[ -n "$_CALLER_GRAPHITI_URL" ] && GRAPHITI_URL="$_CALLER_GRAPHITI_URL"

# Off-hub nodes reach graphiti via the fleet env (FQDN); localhost is only right ON the hub.
# Without this, a bare invocation on a laptop health-checked localhost, WARN'd into the log,
# and exited 0 — a silent no-op that looked like a successful sync (bit twice on 2026-08-30).
[ -z "${GRAPHITI_URL:-}" ] && [ -r "$HOME/.fleet/fleet.env" ] && GRAPHITI_URL="$(. "$HOME/.fleet/fleet.env" >/dev/null 2>&1; echo "${GRAPHITI_URL:-}")"
GRAPHITI_URL="${GRAPHITI_URL:-http://localhost:8200}"
GROUP="${GRAPHITI_GROUP_ID:-gestalt}"
KNOWLEDGE_DIR="${GESTALT_KNOWLEDGE_DIR:-$GESTALT_DIR/knowledge}"
STATE_FILE="${GESTALT_GRAPHITI_SYNC_STATE:-$STATE_DIR/graphiti-sync.json}"
# The log lives beside the ledger, so a test rig that points GESTALT_GRAPHITI_SYNC_STATE at a tmp dir
# also logs there: until 2026-09-06 the suite's dry runs appended "OK: kb-fixture" lines to the real
# ~/Documents/GitHub/.claude/gestalt/backfill.log, which reads exactly like a real send.
STATE_DIR="$(dirname "$STATE_FILE")"
LOG="$STATE_DIR/backfill.log"

FORCE_ALL=0
FORCE_CHUNKS=0
DRY_RUN=0
SUMMARIES=0
SLUG_ARG=""
MARK_SYNCED=""
RECONCILE=0
RESEND=0
RECON_SLUGS=()
for arg in "$@"; do
    case "$arg" in
        --reconcile) RECONCILE=1 ;;
        --resend) RESEND=1 ;;
        --all) FORCE_ALL=1 ;;
        --mark-synced=*) MARK_SYNCED="${arg#--mark-synced=}" ;;
        --force) FORCE_CHUNKS=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --summaries) SUMMARIES=1 ;;
        -*) echo "ERROR: unknown flag $arg" >&2; exit 2 ;;
        *) SLUG_ARG="$arg"; RECON_SLUGS+=("$arg") ;;
    esac
done

mkdir -p "$STATE_DIR"
LEDGER_PY=(env PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m graphiti_sync.ledger)
EXIT75="${GESTALT_SYNC_EXIT75:-0}"
# skip_exit: a skip exits 0 for every existing caller, or 75 where a timer unit opted in (X5, 2026-10-06).
skip_exit() { [ "$EXIT75" = "1" ] && exit 75; exit 0; }

# Serialise runs (N5, 2026-10-06). Two overlapping runs each read the ledger, then each wrote it back, so the
# later write erased the earlier one's chunk hashes. The lock is taken for any run that can write the ledger and
# is held on fd 9 until exit. flock where it exists, an atomic mkdir lock with a stale-pid check where it does
# not (macOS). Never delete the flock file: removing it while held breaks mutual exclusion.
LOCK_FILE="$STATE_DIR/ledger.lock"
LOCK_DIR=""
release_lock() {
    exec 9>&-
    [ -n "$LOCK_DIR" ] && rm -rf "$LOCK_DIR"
    LOCK_DIR=""
    return 0
}
acquire_lock() {
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE"
        flock -n 9 && return 0
    else
        local d="$LOCK_FILE.d" pid
        if mkdir "$d" 2>/dev/null; then LOCK_DIR="$d"; echo $$ > "$d/pid"; trap 'release_lock' EXIT; return 0; fi
        pid=$(cat "$d/pid" 2>/dev/null)
        if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
            rm -rf "$d"
            if mkdir "$d" 2>/dev/null; then LOCK_DIR="$d"; echo $$ > "$d/pid"; trap 'release_lock' EXIT; return 0; fi
        fi
    fi
    echo "$(date -Iseconds) SKIP: another gestalt-graphiti-sync run holds the lock reason=locked" | tee -a "$LOG" >&2
    skip_exit
}

# --mark-synced=NAME[,NAME...]: record the CURRENT planned hash of these chunks in the ledger without
# sending anything. For a name the graph already holds at current content while the ledger still says
# stale (a run killed after its episode went out, 2026-09-06 13:06: three such names would otherwise
# reappear in every dry run forever, and a re-send would only manufacture a duplicate node). All names
# must belong to one entry; the plan comes from the same chunker as a send, under --dry-run --force.
if [ -n "$MARK_SYNCED" ]; then
    first="${MARK_SYNCED%%,*}"; slug="${first#kb-}"; slug="${slug%%#*}"
    DRY_RUN=1; FORCE_CHUNKS=1; SLUG_ARG="$slug"
    export GESTALT_SYNC_MANIFEST_OUT="$STATE_DIR/graphiti-sync.$$.mark-manifest"
    : > "$GESTALT_SYNC_MANIFEST_OUT"
fi

# Take the lock for anything that can write the ledger. A plain dry run (and a reconcile dry run) only reads.
if [ "$MARK_SYNCED" != "" ] || { [ "$DRY_RUN" != "1" ] && [ "$SUMMARIES" != "1" ]; }; then
    acquire_lock
fi

# Reconcile mode (N4): runs before any planning and never sends, except through the resend re-exec below.
if [ "$RECONCILE" = "1" ]; then
    recon_args=(reconcile "$STATE_FILE" ${RECON_SLUGS[@]+"${RECON_SLUGS[@]}"})
    [ "$DRY_RUN" = "1" ] && recon_args+=(--dry-run)
    recon_out=$("${LEDGER_PY[@]}" "${recon_args[@]}"); rc=$?
    [ -n "$recon_out" ] && printf '%s\n' "$recon_out"
    [ $rc -ne 0 ] && { echo "$(date -Iseconds) ERROR: reconcile failed rc=$rc" >> "$LOG"; exit $rc; }
    [ "$RESEND" = "1" ] && [ "$DRY_RUN" != "1" ] || exit 0
    missing=$(printf '%s\n' "$recon_out" | awk -F'\t' '$1=="missing"{print $2}')
    [ -z "$missing" ] && exit 0
    release_lock   # the per-slug children below take it themselves
    worst=0
    for slug in $(printf '%s\n' "$missing" | sed -e 's/^kb-//' -e 's/#.*$//' | sort -u); do
        names=$(printf '%s\n' "$missing" | awk -v b="kb-$slug" '{n=$0; sub(/#[0-9]+$/,"",n); if (n==b) print}')
        GESTALT_SYNC_RESEND_NAMES="$names" GESTALT_SYNC_ONLY_RESEND=1 "$0" "$slug" || worst=$?
    done
    exit $worst
fi

# A corrupt ledger must stop the run before anything is sent (N5). A missing one is a normal first run.
if [ "$SUMMARIES" != "1" ] && ! "${LEDGER_PY[@]}" check "$STATE_FILE"; then
    echo "$(date -Iseconds) ERROR: ledger unreadable, run stopped before any send reason=ledger-corrupt" >> "$LOG"
    exit 4
fi

# --summaries (F9 TODO, implemented 2026-08-29): merge session-summary files newer
# than a cursor into ONE digest episode. Cursor = epoch mtime of the newest file
# already digested ($STATE_DIR/graphiti-summaries.cursor); same DRY_RUN/health/MCP
# flow as the knowledge path, size-capped by the same chunk logic downstream.
SESSIONS_DIR="${GESTALT_SESSIONS_DIR:-$WORKSPACE/.claude/memory/sessions}"
SUMMARY_CURSOR="$STATE_DIR/graphiti-summaries.cursor"
DIGEST_FILE=""
if [ "$SUMMARIES" = "1" ]; then
    # Cursor is a touchstone FILE compared with `find -newer` / `touch -r`:
    # full mtime precision, no epoch parsing. (`stat -c %Y` truncates to
    # seconds, so an epoch cursor re-matched the newest file forever.)
    if [ -f "$SUMMARY_CURSOR" ]; then
        NEW_FILES=$(find "$SESSIONS_DIR" -name '*.md' -type f -newer "$SUMMARY_CURSOR" 2>/dev/null | sort)
    else
        NEW_FILES=$(find "$SESSIONS_DIR" -name '*.md' -type f 2>/dev/null | sort)
    fi
    if [ -z "$NEW_FILES" ]; then
        echo "$(date -Iseconds) summaries: nothing newer than cursor" >> "$LOG"
        echo "summaries: up to date"
        exit 0
    fi
    if [ "$DRY_RUN" = "1" ]; then
        printf '%s\n' "$NEW_FILES"
        exit 0
    fi
    DIGEST_FILE=$(mktemp)
    NEWEST_FILE=""
    while IFS= read -r sf || [ -n "$sf" ]; do
        [ -f "$sf" ] || continue
        { [ -z "$NEWEST_FILE" ] || [ "$sf" -nt "$NEWEST_FILE" ]; } && NEWEST_FILE="$sf"
        printf '## %s\n\n' "$(basename "$sf" .md)" >> "$DIGEST_FILE"
        cat "$sf" >> "$DIGEST_FILE"
        printf '\n\n' >> "$DIGEST_FILE"
    done <<< "$NEW_FILES"
fi

# --- Compute the list of (slug, sha1) pairs to sync. Pure python: portable,
# and independently testable without a bash subshell. ---
TO_SYNC=$("${LEDGER_PY[@]}" plan "$STATE_FILE" "$KNOWLEDGE_DIR" "$SLUG_ARG" "$FORCE_ALL" 2>>"$LOG")

if [ -z "$TO_SYNC" ]; then
    echo "$(date -Iseconds) OK: nothing to sync" >> "$LOG"
    exit 0
fi

TOTAL=$(printf '%s\n' "$TO_SYNC" | grep -c . || true)

if [ "$DRY_RUN" = "1" ]; then
    # A dry run used to stop here and print whole-file candidates, which proved nothing about the chunks a fire
    # sends (<kb-entry> row 63; the same gap destroyed the graph on 2026-09-01). It now runs the
    # identical chunker and chunk gate below and prints one line per chunk that would be queued:
    #   would send<TAB>kb-<slug>#<k><TAB><chars>      unchanged<TAB>kb-<slug>
    # No network call, no state write, no summaries digest.
    echo "DRY RUN: $TOTAL entr$([ "$TOTAL" = 1 ] && echo y || echo ies) past the file gate; planning chunks" >&2
    MCP_HEADERS=(); SESSION="dry-run"; SUMMARIES=0; PLANNED=0
else
if ! curl -sf --max-time 3 "$GRAPHITI_URL/health" >/dev/null; then
    echo "$(date -Iseconds) WARN: graphiti unhealthy at $GRAPHITI_URL/health, skipping ($TOTAL pending) reason=graphiti-unhealthy" | tee -a "$LOG" >&2
    skip_exit
fi

MCP_HEADERS=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
SESSION=$(curl -sL --max-time 5 -D /dev/stderr -X POST "$GRAPHITI_URL/mcp" \
    "${MCP_HEADERS[@]}" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"gestalt-graphiti-sync","version":"1.0.0"}}}' \
    2>&1 >/dev/null | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')

if [ -z "$SESSION" ]; then
    echo "$(date -Iseconds) ERROR: MCP session init failed reason=mcp-init-failed" >> "$LOG"
    exit 1
fi

curl -sL --max-time 3 -X POST "$GRAPHITI_URL/mcp" \
    "${MCP_HEADERS[@]}" -H "Mcp-Session-Id: $SESSION" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
fi  # DRY_RUN

if [ "$SUMMARIES" = "1" ]; then
    dn="session-digest-$(date +%Y-%m-%d)"
    echo "$(date -Iseconds) START: summaries digest $dn ($(wc -c < "$DIGEST_FILE") bytes) -> graphiti group=$GROUP" >> "$LOG"
    s_payloads=$(_F="$DIGEST_FILE" _N="$dn" _G="$GROUP" _C="${GESTALT_SYNC_CHUNK_CHARS:-12000}" _I=1 python3 - <<'SPYEOF'
import json, os, re

def _sub(text, limit):
    """Sub-split a section that is over the limit: `### ` boundaries, then paragraphs, then a hard
    character cap. Before this existed the packer emitted such a section whole, so 17 chunks in the
    corpus exceeded the limit and drove the truncation and 10-minute-timeout failures. The hard cap
    is not paranoia: gestalt prose is deliberately not hard-wrapped, so one paragraph is one line
    and a 19000-character line is normal here, which no paragraph splitter can help with."""
    def pack(ps):
        out, cur = [], ''
        for q in ps:
            if cur and len(cur) + len(q) > limit:
                out.append(cur); cur = ''
            cur += q
        if cur:
            out.append(cur)
        return out
    for ps in (re.split(r'(?m)^(?=### )', text), re.split(r'(?m)(?<=\n)\n(?=\S)', text)):
        o = pack(ps)
        if o and all(len(c) <= limit for c in o):
            return o
    out, cur = [], ''
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                out.append(cur); cur = ''
            out.append(line[:limit]); line = line[limit:]
        if cur and len(cur) + len(line) > limit:
            out.append(cur); cur = ''
        cur += line
    if cur:
        out.append(cur)
    return out or [text]

body = open(os.environ['_F'], encoding='utf-8').read()
name, group, limit, i = os.environ['_N'], os.environ['_G'], int(os.environ['_C']), int(os.environ['_I'])
def payload(n, text, k):
    return json.dumps({'jsonrpc': '2.0', 'id': 100000 * k + i, 'method': 'tools/call', 'params': {
        'name': 'add_memory', 'arguments': {'name': n, 'episode_body': text, 'group_id': group,
        'source': 'text', 'source_description': 'gestalt session-summary digest'}}})
if len(body) <= limit:
    print(payload(name, body, 0)); raise SystemExit
parts = [q for p in re.split(r'(?m)^(?=## )', body) for q in (_sub(p, limit) if len(p) > limit else [p])]
chunks, cur = [], ''
for p in parts:
    if cur and len(cur) + len(p) > limit:
        chunks.append(cur); cur = ''
    cur += p
if cur:
    chunks.append(cur)
for k, c in enumerate(chunks, 1):
    print(payload('%s#%d' % (name, k), c, k))
SPYEOF
)
    s_ok=1
    while IFS= read -r payload || [ -n "$payload" ]; do
        [ -z "$payload" ] && continue
        result=$(curl -sL --max-time 30 -X POST "$GRAPHITI_URL/mcp" \
            "${MCP_HEADERS[@]}" -H "Mcp-Session-Id: $SESSION" \
            -d @- <<< "$payload")
        if ! echo "$result" | grep -q '"isError":false\|Episode .* queued'; then
            s_ok=0; echo "  FAIL digest part -> $(echo "$result" | head -c 200)" >> "$LOG"
        fi
    done <<< "$s_payloads"
    rm -f "$DIGEST_FILE"
    if [ "$s_ok" = 1 ]; then
        touch -r "$NEWEST_FILE" "$SUMMARY_CURSOR"
        echo "$(date -Iseconds) DONE: summaries digest queued, cursor=$NEWEST_FILE" >> "$LOG"
        echo "summaries: digest queued"
        exit 0
    else
        echo "$(date -Iseconds) WARN: summaries digest had failures — cursor NOT advanced" >> "$LOG"
        exit 1
    fi
fi

echo "$(date -Iseconds) START: $TOTAL knowledge entries -> graphiti group=$GROUP" >> "$LOG"

SYNCED_TMP="$STATE_DIR/graphiti-sync.$$.synced"
: > "$SYNCED_TMP"
# Chunk hashes are only committed to state for slugs whose send fully succeeded, mirroring
# how the file-level hash is committed. A chunk whose POST failed must stay un-recorded or
# the next run would treat it as already sent.
# The per-entry manifest (name<TAB>sha1 of the chunk CONTENT, part header excluded)
# is written for every planned chunk whether or not it is sent, which makes it the
# only drift-free description of what the current chunker plans. It is deleted per
# entry, so set GESTALT_SYNC_MANIFEST_OUT=<file> to accumulate a copy; the by-content
# reconciliation needs it under --dry-run --force, where nothing else survives.
# Unset, behaviour is byte-for-byte what it was.
_drop_chunks_tmp() {
    [ -n "${GESTALT_SYNC_MANIFEST_OUT:-}" ] && [ -f "$CHUNKS_TMP" ] \
        && cat "$CHUNKS_TMP" >> "$GESTALT_SYNC_MANIFEST_OUT"
    rm -f "$CHUNKS_TMP"
    return 0
}

CHUNKS_ACCEPTED="$STATE_DIR/graphiti-sync.$$.chunks"
: > "$CHUNKS_ACCEPTED"
CHUNK_STATE_JSON=$("${LEDGER_PY[@]}" chunkstate "$STATE_FILE")

i=0
while IFS=$'\t' read -r slug sha; do
    [ -z "$slug" ] && continue
    i=$((i+1))
    f="$KNOWLEDGE_DIR/$slug.md"
    [ -f "$f" ] || continue
    name="kb-${slug}"
    CHUNKS_TMP="$STATE_DIR/graphiti-sync.$$.chunks.$i"
    : > "$CHUNKS_TMP"

    # Chunking (ported from backfill-knowledge.sh 2026-08-18, <kb-entry>): entries
    # over GESTALT_SYNC_CHUNK_CHARS (default 12000) are split on `## ` headings into
    # kb-<slug>#<n> episodes — a 37-51 KB entry sent whole to the local 14B model exceeded
    # Graphiti's 10-minute request timeout every time and burned the GPU for the full 10 min.
    # Previous-episode context (2026-09-06): graphiti builds every add_episode's dedup prompt from the TEN most
    # recent episodes (graphiti.py retrieve_episodes(last_n=RELEVANT_SCHEMA_LIMIT)) unless the caller passes
    # previous_episode_uuids; on a 21-part archive that block reached 116 KB and the answer overflowed the
    # token cap on every retry (<kb-entry> row 69). Entries with more than
    # GESTALT_SYNC_PREV_MAX_PARTS parts (default 0, i.e. every entry) therefore pass an explicit list: the uuids of the preceding
    # GESTALT_SYNC_PREV_N chunks (default 2) of the same entry, looked up by name in the graph through
    # GESTALT_FALKORDB_CLI (default: docker exec gestalt-falkordb redis-cli), or an empty list when they have
    # not landed yet (a bulk-queued wave) or the graph is not reachable from this node. Never the ten.
    payloads=$(_F="$f" _N="$name" _G="$GROUP" _C="${GESTALT_SYNC_CHUNK_CHARS:-12000}" _I="$i" _SKIP="${GESTALT_SYNC_SKIP_NAMES:-}" _CHUNKSTATE="$CHUNK_STATE_JSON" _RESEND="${GESTALT_SYNC_RESEND_NAMES:-}" _ONLY="${GESTALT_SYNC_ONLY_RESEND:-}" _FORCE="$FORCE_CHUNKS" _MANIFEST="$CHUNKS_TMP" _PREV_MAX_PARTS="${GESTALT_SYNC_PREV_MAX_PARTS:-0}" _PREV_N="${GESTALT_SYNC_PREV_N:-2}" _FALKORDB_CLI="${GESTALT_FALKORDB_CLI:-docker exec gestalt-falkordb redis-cli}" _GRAPH="${GESTALT_GRAPH:-gestalt}" python3 - <<'PYEOF'
import hashlib, json, os, re

def _sub(text, limit):
    """Sub-split a section that is over the limit: `### ` boundaries, then paragraphs, then a hard
    character cap. Before this existed the packer emitted such a section whole, so 17 chunks in the
    corpus exceeded the limit and drove the truncation and 10-minute-timeout failures. The hard cap
    is not paranoia: gestalt prose is deliberately not hard-wrapped, so one paragraph is one line
    and a 19000-character line is normal here, which no paragraph splitter can help with."""
    def pack(ps):
        out, cur = [], ''
        for q in ps:
            if cur and len(cur) + len(q) > limit:
                out.append(cur); cur = ''
            cur += q
        if cur:
            out.append(cur)
        return out
    for ps in (re.split(r'(?m)^(?=### )', text), re.split(r'(?m)(?<=\n)\n(?=\S)', text)):
        o = pack(ps)
        if o and all(len(c) <= limit for c in o):
            return o
    out, cur = [], ''
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                out.append(cur); cur = ''
            out.append(line[:limit]); line = line[limit:]
        if cur and len(cur) + len(line) > limit:
            out.append(cur); cur = ''
        cur += line
    if cur:
        out.append(cur)
    return out or [text]

body = open(os.environ['_F'], encoding='utf-8').read()
name, group, limit, i = os.environ['_N'], os.environ['_G'], int(os.environ['_C']), int(os.environ['_I'])
PREV_MAX_PARTS, PREV_N = int(os.environ.get('_PREV_MAX_PARTS', '0')), int(os.environ.get('_PREV_N', '2'))
import shlex, subprocess
def uuid_by_name(n):
    """The uuid of the newest landed episode with this name, or None: not landed, no graph from here, or the
    landed node is over the chunk cap (a pre-sub-split episode; kb-research-project#2 at 133,797 chars made a two-item
    context block of 144 KB on 2026-09-06 09:02 and the call was refused). Such a node is never context."""
    cli = shlex.split(os.environ.get('_FALKORDB_CLI', ''))
    if not cli: return None
    q = 'MATCH (e:Episodic {name: %s}) RETURN e.uuid, size(e.content) ORDER BY e.created_at DESC LIMIT 1' % json.dumps(n)
    try:
        out = subprocess.run([*cli, '--json', 'GRAPH.QUERY', os.environ.get('_GRAPH', 'gestalt'), q], capture_output=True, text=True, timeout=10).stdout
        rows = json.loads(out)[1]
        if not rows or not rows[0] or not rows[0][0]: return None
        size = int(rows[0][1] or 0) if len(rows[0]) > 1 else 0
        return rows[0][0] if size <= limit else None
    except Exception:
        return None
def previous_for(k, total):
    # Default 0: EVERY chunk gets an explicit list. The old threshold (6 parts) assumed short entries were safe,
    # but the risk is the size of the group's ten most recent episodes, not the entry: mid-wave those are the
    # run's own 10 to 20 KB chunks, and a 3-part entry drew a 234 KB prompt (2026-09-06 14:09, row 77).
    if total <= PREV_MAX_PARTS: return None          # graphiti's own window, only when the caller raises the threshold
    found = [u for u in (uuid_by_name('%s#%d' % (name, j)) for j in range(max(1, k - PREV_N), k)) if u]
    return found                                      # [] when nothing has landed: no context beats a 116 KB block
def payload(n, text, k, total=1):
    args = {'name': n, 'episode_body': text, 'group_id': group, 'source': 'text', 'source_description': 'gestalt knowledge entry'}
    prev = previous_for(k, total)
    if prev is not None: args['previous_episode_uuids'] = prev
    return json.dumps({'jsonrpc': '2.0', 'id': 100000 * k + i, 'method': 'tools/call', 'params': {'name': 'add_memory', 'arguments': args}})
skip = {n.strip() for n in os.environ.get('_SKIP', '').split('\n') if n.strip()}
try:
    chunk_state = json.loads(os.environ.get('_CHUNKSTATE') or '{}')
except Exception:
    chunk_state = {}
resend = {n.strip() for n in os.environ.get('_RESEND', '').split('\n') if n.strip()}
# GESTALT_SYNC_ONLY_RESEND=1 pins an invocation to the named chunks. Without it a named chunk rides along with
# every other chunk of the entry whose state hash is stale, which on a continuously edited entry is all of them:
# on 2026-09-06 13:04 naming kb-capture-inbox#4 queued 8 chunks (<kb-entry> row 76).
only_resend = os.environ.get('_ONLY') == '1'
force = os.environ.get('_FORCE') == '1'
manifest = open(os.environ['_MANIFEST'], 'a', encoding='utf-8')
def wanted(n, text):
    """Send only when this chunk's own content changed, the caller listed it for resend
    (queued once but never landed), or --force. Hash the CHUNK CONTENT and never the rendered
    body: the injected 'part k/N' header changes for every chunk whenever the chunk count
    changes, so hashing the payload would re-send a whole entry because one section was added."""
    h = hashlib.sha1(text.encode('utf-8')).hexdigest()
    manifest.write('%s\t%s\n' % (n, h))
    if n in skip:
        return False
    if only_resend:
        return n in resend
    return force or n in resend or chunk_state.get(n) != h
if len(body) <= limit:
    if wanted(name, body):
        print(payload(name, body, 0))
    manifest.close()
    raise SystemExit
m = re.search(r'^title:\s*(.+)$', body, re.M)
title = m.group(1).strip().strip('"') if m else name
# Chunks are packed to a limit that already excludes the `(part k/N)` header prepended below.
# Packing to the full limit and prepending afterwards overshot it by the header's own length,
# which is how three chunks came out at 12012-12042 against a 12000 cap.
limit = limit - len('# %s (part %d/%d)\n\n' % (title, 999, 999))
parts = [q for p in re.split(r'(?m)^(?=## )', body) for q in (_sub(p, limit) if len(p) > limit else [p])]
chunks, cur = [], ''
for p in parts:
    if cur and len(cur) + len(p) > limit:
        chunks.append(cur); cur = ''
    cur += p
if cur:
    chunks.append(cur)
for k, c in enumerate(chunks, 1):
    n = '%s#%d' % (name, k)
    if not wanted(n, c):
        continue
    head = '' if k == 1 else '# %s (part %d/%d)\n\n' % (title, k, len(chunks))
    print(payload(n, head + c, k, len(chunks)))
manifest.close()
PYEOF
)
    n_parts=$(printf '%s\n' "$payloads" | grep -c . || true)
    if [ "$n_parts" = 0 ]; then
        # Every chunk's hash already matches state: the entry is genuinely up to date.
        # Record the file hash so the whole-file gate stops selecting it, and say so.
        [ "$DRY_RUN" = "1" ] && { printf 'unchanged\t%s\n' "$name"; _drop_chunks_tmp; continue; }
        echo "  [$i/$TOTAL] SKIP: $name (all chunks unchanged)" >> "$LOG"
        printf '%s\t%s\n' "$slug" "$sha" >> "$SYNCED_TMP"
        cat "$CHUNKS_TMP" >> "$CHUNKS_ACCEPTED"
        _drop_chunks_tmp
        continue
    fi
    if [ "$DRY_RUN" = "1" ]; then
        printf '%s\n' "$payloads" | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    a = json.loads(line)["params"]["arguments"]
    prev = a.get("previous_episode_uuids")
    print("would send\t%s\t%d\tprev=%s" % (a["name"], len(a["episode_body"]), "auto" if prev is None else len(prev)))'
        PLANNED=$((PLANNED + n_parts))
        _drop_chunks_tmp
        continue
    fi
    label="$name"; [ "$n_parts" -gt 1 ] && label="$name (${n_parts} chunks)"
    ok=1
    while IFS= read -r payload || [ -n "$payload" ]; do
        [ -z "$payload" ] && continue
        # -d @- : payload via stdin — a large knowledge entry as an argv item
        # exceeds MAX_ARG_STRLEN (128KB) and curl dies "Argument list too long",
        # which this loop then swallowed as a WARN (why the 2026-08-18 backfill
        # never persisted). The <<< herestring scopes stdin to curl only.
        result=$(curl -sL --max-time 30 -X POST "$GRAPHITI_URL/mcp" \
            "${MCP_HEADERS[@]}" -H "Mcp-Session-Id: $SESSION" \
            -d @- <<< "$payload")
        if ! echo "$result" | grep -q '"isError":false\|Episode .* queued'; then
            ok=0; echo "  FAIL part of $name -> $(echo "$result" | head -c 200)" >> "$LOG"
        else
            # B-04: record this chunk the moment it is acknowledged, so a failure further on never re-sends it.
            printf '%s' "$payload" | "${LEDGER_PY[@]}" record "$STATE_FILE" "$CHUNKS_TMP" >>"$LOG" 2>&1 \
                || echo "  WARN: could not record an acknowledged chunk of $name" >> "$LOG"
        fi
    done <<< "$payloads"
    if [ "$ok" = 1 ]; then
        echo "  [$i/$TOTAL] OK: $label" >> "$LOG"
        # A pinned resend sends only the named chunks, so the entry's other stale chunks must NOT be committed
        # as sent. The acknowledged ones were already recorded one by one above.
        if [ "${GESTALT_SYNC_ONLY_RESEND:-}" != "1" ]; then
            printf '%s\t%s\n' "$slug" "$sha" >> "$SYNCED_TMP"
            cat "$CHUNKS_TMP" >> "$CHUNKS_ACCEPTED"
        fi
    else
        echo "  [$i/$TOTAL] WARN: $label (see FAIL lines above)" >> "$LOG"
    fi
    _drop_chunks_tmp
done <<< "$TO_SYNC"

if [ "$DRY_RUN" = "1" ]; then
    rm -f "$SYNCED_TMP" "$CHUNKS_ACCEPTED"
    echo "DRY RUN: ${PLANNED:-0} chunk(s) would be queued from $i entr$([ "$i" = 1 ] && echo y || echo ies); nothing sent, nothing written" >&2
    if [ -n "$MARK_SYNCED" ]; then
        "${LEDGER_PY[@]}" mark-synced "$STATE_FILE" "$GESTALT_SYNC_MANIFEST_OUT" "$MARK_SYNCED"
        rc=$?; rm -f "$GESTALT_SYNC_MANIFEST_OUT"; [ $rc -ne 0 ] && exit $rc
    fi
    exit 0
fi
echo "$(date -Iseconds) DONE: queued $i episodes" >> "$LOG"

# --- Persist successful syncs to the state file ---
"${LEDGER_PY[@]}" commit "$STATE_FILE" "$SYNCED_TMP" "$CHUNKS_ACCEPTED" || { echo "$(date -Iseconds) ERROR: final ledger commit failed" >> "$LOG"; exit 1; }
rm -f "$SYNCED_TMP" "$CHUNKS_ACCEPTED"
