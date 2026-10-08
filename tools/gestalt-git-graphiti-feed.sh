#!/usr/bin/env bash
# Phase-2 git-change-detection feed (docs/pitches/gestalt-memory-expansion/sdd/hooks.md
# ^git-change-detection): iterate the repos under $WORKSPACE, find commits since the
# last time each repo was fed, and call add_memory (one episode per repo per run) with
# a bounded commit-log + diffstat summary. Wired as a backgrounded call from
# gestalt-session-start.sh, the same way that hook's staleness check is backgrounded.
#
# Usage:
#   gestalt-git-graphiti-feed.sh              # feed every repo under $WORKSPACE with
#                                              # commits since its last recorded sha
#   gestalt-git-graphiti-feed.sh --dry-run    # print "<repo>\t<from>\t<to>\t<bytes>"
#                                              # per repo with pending commits; zero
#                                              # network calls, zero state file writes
#   gestalt-git-graphiti-feed.sh <repo-name>  # feed exactly one repo (basename under
#                                              # $WORKSPACE), regardless of dry-run
#
# State: $STATE_DIR/git-graphiti-feed.json -- {repo_name: last_fed_sha}. A repo with no
# recorded sha is bootstrapped: its current HEAD is recorded as the baseline and nothing
# is fed for it this run (there is no "last session" yet to diff against, and feeding a
# whole repo's history as one episode on first sight would be a firehose, not a summary).
# Idempotent: a repo whose HEAD still matches its last recorded sha is skipped.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
source "$SCRIPT_DIR/../.claude/hooks/lib.sh" && gestalt_init

GRAPHITI_URL="${GRAPHITI_URL:-http://localhost:8200}"
GROUP="${GRAPHITI_GROUP_ID:-gestalt}"
STATE_FILE="${GESTALT_FEED_STATE:-$STATE_DIR/git-graphiti-feed.json}"
LOG="${GESTALT_FEED_LOG:-$STATE_DIR/git-graphiti-feed.log}"
# Bounded per repo: half the budget for `git log --oneline`, half for `git diff --stat`.
# Commits are far smaller than knowledge-entry prose (gestalt-graphiti-sync.sh's 12000
# default is for whole markdown entries), so a tighter default is plenty here.
SUMMARY_CHARS="${GESTALT_FEED_SUMMARY_CHARS:-6000}"
MAX_COMMITS="${GESTALT_FEED_MAX_COMMITS:-50}"

DRY_RUN=0
REPO_ARG=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -*) echo "ERROR: unknown flag $arg" >&2; exit 2 ;;
        *) REPO_ARG="$arg" ;;
    esac
done

mkdir -p "$STATE_DIR"

if [ ! -d "$WORKSPACE" ]; then
    echo "$(date -Iseconds) WARN: WORKSPACE $WORKSPACE does not exist, nothing to feed" >> "$LOG"
    exit 0
fi

# --- Load the last-fed-sha ledger (pure python: portable, independently testable) ---
LEDGER_JSON=$(STATE_FILE="$STATE_FILE" python3 -c "
import json, os
try:
    with open(os.environ['STATE_FILE']) as f:
        print(json.dumps(json.load(f)))
except Exception:
    print('{}')
")

# --- Compute per-repo pending work: (repo, from_sha_or_dash, to_sha, kind) ---
# from_sha "-" means bootstrap (no prior ledger entry) -- caller records the baseline
# and feeds nothing. A real, always-non-empty placeholder rather than an empty field:
# `read -r` with IFS set to a whitespace char (tab) collapses adjacent delimiters, so
# an empty middle field silently eats the next tab too and shifts every later field
# left -- caught in testing when a bootstrap row's `kind` came back empty because the
# genuinely-empty from-field swallowed its own trailing tab.
# Repos already up to date (HEAD == last_fed) are omitted entirely.
PENDING=$(WORKSPACE="$WORKSPACE" REPO_ARG="$REPO_ARG" LEDGER_JSON="$LEDGER_JSON" python3 -c "
import json, os, subprocess

workspace = os.environ['WORKSPACE']
repo_arg = os.environ.get('REPO_ARG', '')
ledger = json.loads(os.environ['LEDGER_JSON'])

try:
    entries = sorted(os.listdir(workspace))
except OSError as exc:
    print(f'__ERROR__\tcannot list WORKSPACE: {exc}')
    raise SystemExit

for name in entries:
    if repo_arg and name != repo_arg:
        continue
    path = os.path.join(workspace, name)
    if not os.path.isdir(os.path.join(path, '.git')) and not os.path.exists(os.path.join(path, '.git')):
        continue
    try:
        head = subprocess.run(['git', '-C', path, 'rev-parse', 'HEAD'],
                               capture_output=True, text=True, timeout=5)
    except Exception as exc:
        print(f'__ERROR__\t{name}: rev-parse failed: {exc}')
        continue
    if head.returncode != 0:
        continue  # no commits yet / not a real repo -- skip quietly
    head_sha = head.stdout.strip()
    last = ledger.get(name, '')
    if not last:
        print(f'{name}\t-\t{head_sha}\tbootstrap')
        continue
    if last == head_sha:
        continue  # up to date, idempotent skip
    # last_sha may no longer be reachable (rebase/force-push) -- fall back to bootstrap
    # rather than feeding a nonsensical or failing range.
    check = subprocess.run(['git', '-C', path, 'cat-file', '-e', last + '^{commit}'],
                            capture_output=True, timeout=5)
    if check.returncode != 0:
        print(f'{name}\t-\t{head_sha}\tbootstrap')
        continue
    print(f'{name}\t{last}\t{head_sha}\tincremental')
")

if printf '%s' "$PENDING" | grep -q '^__ERROR__'; then
    printf '%s\n' "$PENDING" | grep '^__ERROR__' | while IFS=$'\t' read -r _ msg; do
        echo "$(date -Iseconds) ERROR: $msg" >> "$LOG"
    done
    PENDING=$(printf '%s\n' "$PENDING" | grep -v '^__ERROR__')
fi

if [ -z "$PENDING" ]; then
    echo "$(date -Iseconds) OK: nothing to feed" >> "$LOG"
    exit 0
fi

# --- Build the bounded summary for one repo's incremental range ---
# Only ever called for kind=incremental (bootstrap feeds no episode -- both callers
# below short-circuit on kind=bootstrap before reaching this). Emitted as: first line
# = byte length, remaining lines = the episode body itself.
_build_summary() {
    local repo_path="$1" from_sha="$2" to_sha="$3"
    local range log_part stat_part body
    range="${from_sha}..${to_sha}"
    log_part=$(git -C "$repo_path" log --oneline -n "$MAX_COMMITS" "$range" 2>/dev/null | head -c $((SUMMARY_CHARS / 2)))
    stat_part=$(git -C "$repo_path" diff --stat "$range" 2>/dev/null | head -c $((SUMMARY_CHARS / 2)))
    body=$(printf '## Commits (%s)\n%s\n\n## Changes\n%s\n' "$range" "$log_part" "$stat_part")
    printf '%s' "$body" | wc -c
    printf '%s' "$body"
}

if [ "$DRY_RUN" = "1" ]; then
    while IFS=$'\t' read -r repo from to kind; do
        [ -z "$repo" ] && continue
        repo_path="$WORKSPACE/$repo"
        if [ "$kind" = "bootstrap" ]; then
            printf '%s\t-\t%s\tbootstrap\t0\n' "$repo" "$to"
            continue
        fi
        nbytes=$(_build_summary "$repo_path" "$from" "$to" | head -1)
        printf '%s\t%s\t%s\t%s\t%s\n' "$repo" "$from" "$to" "$kind" "$nbytes"
    done <<< "$PENDING"
    exit 0
fi

# --- Bootstrap entries need no network: persist their baseline immediately, before any
# hub-health gate. Doing this ahead of the gate (rather than inside the same loop that
# talks to graphiti) is the difference between "bootstrap-only runs are network-free and
# always succeed" and "a dark hub silently drops the baseline write too". ---
SYNCED_TMP="$STATE_DIR/git-graphiti-feed.$$.synced"
: > "$SYNCED_TMP"
BOOT_N=0
while IFS=$'\t' read -r repo from to kind; do
    [ -z "$repo" ] && continue
    [ "$kind" = "bootstrap" ] || continue
    BOOT_N=$((BOOT_N + 1))
    printf '%s\t%s\n' "$repo" "$to" >> "$SYNCED_TMP"
    echo "$(date -Iseconds) BOOTSTRAP: $repo @ $to (no episode fed)" >> "$LOG"
done <<< "$PENDING"

INCREMENTAL=$(printf '%s\n' "$PENDING" | awk -F'\t' '$4=="incremental"')

_persist_ledger() {
    SYNCED_FILE="$SYNCED_TMP" STATE_FILE="$STATE_FILE" python3 -c "
import json, os

state_file = os.environ['STATE_FILE']
synced_file = os.environ['SYNCED_FILE']

try:
    with open(state_file) as f:
        state = json.load(f)
except Exception:
    state = {}

with open(synced_file) as f:
    for line in f:
        line = line.rstrip('\n')
        if not line:
            continue
        repo, sha = line.split('\t', 1)
        state[repo] = sha

with open(state_file, 'w') as f:
    json.dump(state, f, indent=2, sort_keys=True)
"
    rm -f "$SYNCED_TMP"
}

if [ -z "$INCREMENTAL" ]; then
    _persist_ledger
    echo "$(date -Iseconds) OK: $BOOT_N bootstrap-only, 0 incremental (no network needed)" >> "$LOG"
    exit 0
fi

# --- Hub-health gate (F3/F4 pattern: shared 90s-cached probe, never a raw curl here) ---
# Only reached when there is at least one incremental repo actually needing add_memory.
gestalt_hub_health
if [ "${HUB_GRAPHITI_OK:-0}" != "1" ]; then
    _persist_ledger  # bootstrap baselines still land even though incrementals are deferred
    N=$(printf '%s\n' "$INCREMENTAL" | grep -c .)
    echo "$(date -Iseconds) WARN: graphiti unhealthy, skipping ($N repo(s) pending)" >> "$LOG"
    exit 0
fi

MCP_HEADERS=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
SESSION=$(curl -sL --max-time 5 -D /dev/stderr -X POST "$GRAPHITI_URL/mcp" \
    "${MCP_HEADERS[@]}" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"gestalt-git-graphiti-feed","version":"1.0.0"}}}' \
    2>&1 >/dev/null | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')

if [ -z "$SESSION" ]; then
    _persist_ledger
    echo "$(date -Iseconds) ERROR: MCP session init failed" >> "$LOG"
    exit 1
fi

curl -sL --max-time 3 -X POST "$GRAPHITI_URL/mcp" \
    "${MCP_HEADERS[@]}" -H "Mcp-Session-Id: $SESSION" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null

TOTAL=$(printf '%s\n' "$INCREMENTAL" | grep -c .)
echo "$(date -Iseconds) START: $TOTAL repo(s) pending -> graphiti group=$GROUP" >> "$LOG"

i=0
while IFS=$'\t' read -r repo from to kind; do
    [ -z "$repo" ] && continue
    i=$((i+1))
    repo_path="$WORKSPACE/$repo"

    body_out=$(_build_summary "$repo_path" "$from" "$to")
    body=$(printf '%s' "$body_out" | tail -n +2)
    name="git-${repo}-${to:0:12}"

    payload=$(_N="$name" _G="$GROUP" _B="$body" _I="$i" python3 -c "
import json, os
print(json.dumps({'jsonrpc': '2.0', 'id': 200000 + int(os.environ['_I']), 'method': 'tools/call', 'params': {
    'name': 'add_memory', 'arguments': {'name': os.environ['_N'], 'episode_body': os.environ['_B'],
    'group_id': os.environ['_G'], 'source': 'text', 'source_description': 'gestalt git change feed'}}}))
")

    # -d @- : payload via stdin -- a large diffstat as an argv item can exceed
    # MAX_ARG_STRLEN (128KB) and curl dies "Argument list too long" (the exact
    # 2026-08-18 backfill bug this pattern was ported from -- see
    # gestalt-graphiti-sync.sh). The <<< herestring scopes stdin to curl only.
    result=$(curl -sL --max-time 30 -X POST "$GRAPHITI_URL/mcp" \
        "${MCP_HEADERS[@]}" -H "Mcp-Session-Id: $SESSION" \
        -d @- <<< "$payload")
    if echo "$result" | grep -q '"isError":false\|Episode .* queued'; then
        printf '%s\t%s\n' "$repo" "$to" >> "$SYNCED_TMP"
        echo "  [$i/$TOTAL] OK: $repo ($from..$to)" >> "$LOG"
    else
        echo "  [$i/$TOTAL] FAIL: $repo -> $(echo "$result" | head -c 200)" >> "$LOG"
    fi
done <<< "$INCREMENTAL"

echo "$(date -Iseconds) DONE: processed $i repo(s)" >> "$LOG"

# --- Persist successful feeds (and the bootstrap baselines collected above) ---
_persist_ledger
