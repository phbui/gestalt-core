#!/usr/bin/env bash
set -uo pipefail

# --- Load shared library ---
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
source "$SCRIPT_DIR/lib.sh" && gestalt_init
SESSIONS_DIR="${GESTALT_SESSIONS_DIR:-$WORKSPACE/.claude/memory/sessions}"
TRANSCRIPTS_DIR="${GESTALT_TRANSCRIPTS_DIR:-$WORKSPACE/.claude/memory/transcripts}"
LETTA_URL="${LETTA_URL:-http://localhost:8283/v1}"
GRAPHITI_URL="${GRAPHITI_URL:-http://localhost:8200}"
GRAPHITI_GROUP="${GRAPHITI_GROUP_ID:-gestalt}"
LOCK_TIMEOUT="${GESTALT_LOCK_TIMEOUT:-60}"
MAX_MSGS="${GESTALT_LETTA_MAX_MESSAGES:-30}"
LOG="$STATE_DIR/health.log"
AGENT_ID_FILE="$STATE_DIR/letta-agent-id.txt"
LOCK_FILE="$STATE_DIR/stop-hook.lock"

mkdir -p "$SESSIONS_DIR" "$TRANSCRIPTS_DIR"

# --- Stderr capture (2026-10-06, roadmap X5) ---
# Commands whose failure matters send stderr to a scratch file, and _hlog_err copies the first
# 200 bytes to health.log with a timestamp and the command name. At most 5 lines per run, so a
# failing loop cannot flood the log. Hook stdout is a protocol, so nothing here prints to it.
_ERRF="$STATE_DIR/stop-hook.$$.err"
_ERRN=0
_hlog_err() {
    [ "$_ERRN" -ge 5 ] && return 0
    _ERRN=$((_ERRN + 1))
    local m; m=$(head -c 200 "$_ERRF" 2>/dev/null | tr '\n' ' ')
    echo "$(date -Iseconds) ERROR: $1 failed${m:+: $m}" >> "$LOG"
    : > "$_ERRF" 2>/dev/null
    return 0
}

# --- Cached hub-health probe (F3/F4, <kb-entry>) ---
# A dark hub previously cost 5.4s here (backlog curl + POST + backup.sh, all behind one
# wait). gestalt_hub_health (lib.sh) answers from the shared 90s cache after the first
# probe; when Letta is down, Task A (Letta send) and Task F (backup.sh) are skipped
# entirely below instead of paying their own timeouts.
gestalt_hub_health

# --- Disk space pre-check ---
AVAIL_KB=$(df -k "$WORKSPACE/.claude" 2>/dev/null | awk 'NR==2 {print $4}')
if [ "${AVAIL_KB:-0}" -lt 102400 ]; then
    echo "$(date -Iseconds) WARN: Low disk space (${AVAIL_KB}KB available)" >> "$LOG"
    ls -t "$SESSIONS_DIR"/*.md 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null || true
    ls -t "$TRANSCRIPTS_DIR"/*.jsonl 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null || true
fi


# --- Task 0: Clean up leftover agent worktree directories ---
# WorktreeRemove hook does not fire for Agent(isolation:worktree) (bug #36205).
# Clean up any /tmp/claude-worktree-* dirs left by worktree-create.sh.
for _wt_dir in /tmp/claude-worktree-*/; do
    [ ! -d "$_wt_dir" ] && continue
    # Only reap worktrees no live process is inside and older than 2h — the
    # unconditional rm -rf reaped OTHER sessions' active worktrees on every
    # Stop event (6 concurrent seats, 2026-08-29).
    _wt_real=$(readlink -f "$_wt_dir")
    _in_use=0
    for _cwd in /proc/[0-9]*/cwd; do
        case "$(readlink -f "$_cwd" 2>/dev/null)" in "$_wt_real"|"$_wt_real"/*) _in_use=1; break;; esac
    done
    if [ "$_in_use" = 0 ]; then
        _age_s=$(( $(date +%s) - $(stat -c %Y "$_wt_dir" 2>/dev/null || echo 0) ))
        [ "$_age_s" -lt 7200 ] && _in_use=1
    fi
    [ "$_in_use" = 1 ] && continue
    for _repo_dir in "$WORKSPACE"/*/; do
        [ ! -d "$_repo_dir/.git" ] && continue
        _wt_target="$_wt_dir/$(basename "$_repo_dir")"
        if [ -d "$_wt_target" ] && [ ! -L "$_wt_target" ]; then
            git -C "$_repo_dir" worktree remove --force "$_wt_target" 2>/dev/null || true
        fi
    done
    git -C "$WORKSPACE/gestalt" worktree remove --force "$_wt_real" 2>/dev/null || true
    rm -rf "$_wt_dir" 2>/dev/null
    echo "$(date -Iseconds) INFO: worktree-cleanup: $_wt_dir (idle, unreferenced)" >> "$LOG"
done &

# --- Read stdin JSON for transcript_path and session_id ---
INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
    echo "$(date -Iseconds) WARN: No transcript at '$TRANSCRIPT_PATH'" >> "$LOG"
    exit 0
fi

# --- Copy transcript to persistent store ---
if [ -n "$SESSION_ID" ]; then
    cp "$TRANSCRIPT_PATH" "$TRANSCRIPTS_DIR/${SESSION_ID}.jsonl" 2>"$_ERRF" || _hlog_err "transcript save (cp)"
fi

# --- Acquire lock (macOS-compatible mkdir lock) ---
LOCK_DIR="${LOCK_FILE}.d"
_gestalt_unlock() { rm -rf "$LOCK_DIR" 2>/dev/null; rm -f "$_ERRF" "$_ERRF.send" 2>/dev/null; }
trap '_gestalt_unlock' EXIT

_deadline=$(($(date +%s) + LOCK_TIMEOUT))
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    if [ "$(date +%s)" -ge "$_deadline" ]; then
        # Check if holding process is still alive; stale lock → force acquire
        _pid_file="$LOCK_DIR/pid"
        if [ -f "$_pid_file" ]; then
            _old_pid=$(cat "$_pid_file" 2>/dev/null)
            if [ -n "$_old_pid" ] && ! kill -0 "$_old_pid" 2>/dev/null; then
                rm -rf "$LOCK_DIR" 2>/dev/null
                continue
            fi
        fi
        echo "$(date -Iseconds) WARN: Stop hook lock timeout" >> "$LOG"
        exit 0
    fi
    sleep 1
done
echo $$ > "$LOCK_DIR/pid" 2>/dev/null

# --- Parse transcript ---
export _GESTALT_TRANSCRIPT="$TRANSCRIPT_PATH"
PARSED=$(python3 -c "
import json, sys, os

def extract_text(msg):
    content = msg.get('message', {}).get('content', '')
    if isinstance(content, str):
        return content[:1200]
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get('text', '')
                if text:
                    parts.append(text[:800])
            elif isinstance(block, str):
                parts.append(block[:800])
        return ' '.join(parts)[:1200]
    return ''

try:
    transcript_path = os.environ.get('_GESTALT_TRANSCRIPT', '')
    if not transcript_path:
        print(json.dumps([]))
        sys.exit(0)
    lines = open(transcript_path).readlines()
    msgs = []
    for line in lines:
        try:
            obj = json.loads(line.strip())
            if obj.get('type') in ('user', 'assistant'):
                if obj.get('isMeta'):
                    continue
                text = extract_text(obj)
                if text and len(text.strip()) > 5:
                    msgs.append({'role': obj['type'], 'text': text})
        except Exception:
            pass
    print(json.dumps(msgs))
except Exception:
    print(json.dumps([]))
" 2>"$_ERRF") || { _hlog_err "transcript parse (python)"; PARSED="[]"; }

MSG_COUNT=$(echo "$PARSED" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
USER_COUNT=$(echo "$PARSED" | python3 -c "
import sys, json
try:
    msgs = json.load(sys.stdin)
    print(sum(1 for m in msgs if m.get('role') == 'user'))
except Exception:
    print(0)
" 2>/dev/null || echo "0")

# --- Letta send cadence (F9, <kb-entry>):
# native auto-memory + gestalt knowledge entries are the primary stores now, so Letta
# transcript summarisation only needs to run every Nth session, and never for a
# session too short to hold anything worth summarising. GESTALT_LETTA_EVERY_N (default
# 5) and COUNTER_FILE track "session N of every-Nth"; the 4-user-turn floor is a cheap
# noise filter independent of cadence. Both gates are logged when they skip a send.
EVERY_N="${GESTALT_LETTA_EVERY_N:-5}"
COUNTER_FILE="$STATE_DIR/letta-session-counter"
PREV_COUNTER=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
case "$PREV_COUNTER" in ''|*[!0-9]*) PREV_COUNTER=0 ;; esac
SESSION_COUNTER=$((PREV_COUNTER + 1))
{ echo "$SESSION_COUNTER" > "$COUNTER_FILE"; } 2>"$_ERRF" || _hlog_err "session counter write"
SEND_DUE=0
[ $((SESSION_COUNTER % EVERY_N)) -eq 0 ] && SEND_DUE=1

# --- Task A: Send to Letta (skipped when the hub is dark, the session is below the
# user-turn floor, or the cadence counter is not due) ---
export _MAX_MSGS="$MAX_MSGS"
if [ "${HUB_LETTA_OK:-0}" = "1" ] && [ -f "$AGENT_ID_FILE" ] && [ "$MSG_COUNT" -gt 0 ] \
   && [ "${USER_COUNT:-0}" -ge 4 ] && [ "$SEND_DUE" = "1" ]; then
    AGENT_ID=$(cat "$AGENT_ID_FILE")
    SUMMARY=$(echo "$PARSED" | python3 -c "
import sys, json, os
max_msgs = int(os.environ.get('_MAX_MSGS', '30'))
msgs = json.load(sys.stdin)[-max_msgs:]
lines = []
for m in msgs:
    lines.append(f'{m[\"role\"].upper()}: {m[\"text\"][:800]}')
print('\n---\n'.join(lines))
" 2>/dev/null || echo "")

    if [ -n "$SUMMARY" ]; then
        LETTA_PAYLOAD=$(python3 -c "
import json, sys
content = sys.stdin.read()
print(json.dumps({'messages': [{'role': 'user', 'content': 'Session transcript. Update memory blocks with patterns, preferences, corrections, pending items, project context:\n\n' + content}]}))
" <<< "$SUMMARY" 2>"$_ERRF") || _hlog_err "letta payload build (python)"
        # Backlog guard: each run takes minutes; an unbounded producer builds a
        # months-deep queue whose stale-context runs clobber live blocks
        # (last-writer-wins). Skip the send when the agent is already behind.
        _BACKLOG=$(curl -sf --max-time 3 "$LETTA_URL/runs/active?agent_ids=$AGENT_ID" 2>/dev/null \
            | python3 -c "import sys,json; print(sum(1 for r in json.load(sys.stdin) if r.get('status')=='created'))" 2>/dev/null || echo 0)
        if [ "${_BACKLOG:-0}" -ge "${GESTALT_LETTA_MAX_BACKLOG:-3}" ] 2>/dev/null; then
            echo "$(date -Iseconds) WARN: Letta backlog ${_BACKLOG} queued runs — skipping transcript send" >> "$LOG"
            LETTA_PAYLOAD=""
        fi
        if [ -n "$LETTA_PAYLOAD" ]; then
            LETTA_HTTP=$(curl -sL --max-time 10 -o /dev/null -w "%{http_code}" -X POST "$LETTA_URL/agents/$AGENT_ID/messages" \
                -H "Content-Type: application/json" \
                -d "$LETTA_PAYLOAD" 2>"$_ERRF.send")
            if [ "$LETTA_HTTP" -ge 200 ] 2>/dev/null && [ "$LETTA_HTTP" -lt 300 ] 2>/dev/null; then
                echo "$(date -Iseconds) OK: Letta memory update sent (HTTP $LETTA_HTTP)" >> "$LOG"
            else
                echo "$(date -Iseconds) ERROR: Letta memory update failed (HTTP $LETTA_HTTP)$(head -c 200 "$_ERRF.send" 2>/dev/null | tr '\n' ' ' | sed 's/^./: &/')" >> "$LOG"
            fi &
        fi
    fi
elif [ "${HUB_LETTA_OK:-0}" != "1" ]; then
    echo "$(date -Iseconds) INFO: hub dark — skipping Letta send" >> "$LOG"
elif [ "${USER_COUNT:-0}" -lt 4 ]; then
    echo "$(date -Iseconds) INFO: skipping Letta send — only ${USER_COUNT:-0} user turns (<4, noise filter)" >> "$LOG"
elif [ "$SEND_DUE" != "1" ]; then
    echo "$(date -Iseconds) INFO: skipping Letta send — session $SESSION_COUNTER of every-${EVERY_N} cadence" >> "$LOG"
fi

# --- Task B: Write session summary ---
DATE=$(date +%Y-%m-%d)
export _DATE="$DATE"
SUMMARY_FILE="$SESSIONS_DIR/${DATE}-${SESSION_ID:0:8}.md"
echo "$PARSED" | python3 -c "
import sys, json, os
date = os.environ.get('_DATE', 'unknown')
msgs = json.load(sys.stdin)
print(f'# Session {date}\n')
print(f'**Messages:** {len(msgs)}\n')
if msgs:
    print('## Key Exchanges\n')
    for m in msgs[-40:]:
        role = m['role'].upper()
        text = m['text'][:800].replace('\n', ' ')
        print(f'- **{role}:** {text}')
else:
    print('_No messages parsed from transcript._')
" > "$SUMMARY_FILE" 2>"$_ERRF" || { _hlog_err "session summary write (python)"; printf "# Session %s\n\n_Parse error._\n" "$DATE" > "$SUMMARY_FILE"; }

# --- Task C: Graphiti ingestion is not per-session here (F9, ^memory-triplication) ---
# Per-session POST was retired: each episode cost 2-3 min graphiti-side, which
# pegged CPU and burned OpenAI credits. Session summaries accumulate as local
# files under $SESSIONS_DIR; knowledge/*.md reaches Graphiti through the single
# hash-gated tools/gestalt-graphiti-sync.sh (run via /save's Post-Write Sequence,
# or manually with --all). There is deliberately no scheduled digest of session
# summaries — see the --summaries TODO in gestalt-graphiti-sync.sh.

# --- Task D: Flag promotable facts for promotion queue ---
QUEUE_FILE="${GESTALT_PROMOTE_QUEUE_PATH:-$STATE_DIR/promotion-queue.json}"
export SESSION_ID QUEUE_FILE
if [ -n "$PARSED" ] && [ "$MSG_COUNT" -gt 3 ]; then
    python3 -c "
import json, sys, os, re
from datetime import datetime

parsed = json.loads(sys.stdin.read())
queue_path = os.environ.get('QUEUE_FILE', '')
session_id = os.environ.get('SESSION_ID', 'unknown')

# Load existing queue
queue = []
if queue_path and os.path.exists(queue_path):
    try:
        with open(queue_path) as f:
            queue = json.load(f)
    except Exception:
        queue = []

# Scan for promotable patterns in user+assistant exchanges
correction_patterns = [
    r'(?:actually|correction|wrong|mistake|not (?:right|correct)|should be|instead of)',
    r'(?:the (?:real|actual|correct) (?:way|approach|pattern))',
    r'(?:discovered|found out|realized|turns out)',
]
architecture_patterns = [
    r'(?:deploys? (?:via|using|with)|uses? (?:argocd|kubernetes|helm|docker))',
    r'(?:database|schema|migration|table|column)',
    r'(?:api|endpoint|route|handler|middleware)',
    r'(?:service|microservice|container|pod|namespace)',
    r'(?:pipeline|workflow|dag|step|stage)',
]

combined_pattern = '|'.join(correction_patterns + architecture_patterns)
new_facts = []

for i, msg in enumerate(parsed):
    text = msg.get('text', '')
    if not text or len(text) < 20:
        continue
    matches = re.findall(combined_pattern, text.lower())
    if matches:
        # Extract the sentence containing the match
        sentences = re.split(r'[.!?\n]', text)
        for sent in sentences:
            if any(re.search(p, sent.lower()) for p in correction_patterns + architecture_patterns):
                sent = sent.strip()
                if 20 < len(sent) < 500:
                    is_correction = any(re.search(p, sent.lower()) for p in correction_patterns)
                    new_facts.append({
                        'fact': sent,
                        'source': 'transcript',
                        'confidence': 0.8 if is_correction else 0.5,
                        'session_id': session_id,
                        'timestamp': datetime.now().isoformat(),
                    })

# Deduplicate against existing queue (by fact text fingerprint)
existing_fingerprints = {f['fact'][:80].lower() for f in queue}
for nf in new_facts:
    if nf['fact'][:80].lower() not in existing_fingerprints:
        queue.append(nf)

# Cap queue at 100 entries (FIFO eviction)
queue = queue[-100:]

if queue_path:
    with open(queue_path, 'w') as f:
        json.dump(queue, f, indent=2)
" <<< "$PARSED" 2>>"$LOG" &
fi

# --- Task E: BRANCHES.md staleness check ---
BRANCHES_FILE="$GESTALT_DIR/BRANCHES.md"
if [ -f "$BRANCHES_FILE" ]; then
    BRANCHES_MTIME=$(stat -c '%Y' "$BRANCHES_FILE" 2>/dev/null || stat -f%m "$BRANCHES_FILE" 2>/dev/null || echo "0")
    STALE_REPOS=""
    for repo_dir in "$WORKSPACE"/*/; do
        [ -d "$repo_dir/.git" ] || continue
        REPO_NAME=$(basename "$repo_dir")
        LATEST_COMMIT=$(git -C "$repo_dir" log -1 --format='%ct' 2>/dev/null || echo "0")
        if [ "${LATEST_COMMIT:-0}" -gt "$BRANCHES_MTIME" ]; then
            STALE_REPOS="$STALE_REPOS $REPO_NAME"
        fi
    done
    if [ -n "$STALE_REPOS" ]; then
        echo "$(date -Iseconds) INFO: BRANCHES.md stale — repos advanced:$STALE_REPOS" >> "$LOG"
    fi
fi
# --- Task F: Lightweight block backup (skipped when the hub is dark; backs up Letta blocks) ---
BACKUP_SCRIPT="$GESTALT_DIR/tools/backup.sh"
if [ "${HUB_LETTA_OK:-0}" = "1" ] && [ -x "$BACKUP_SCRIPT" ]; then
    bash "$BACKUP_SCRIPT" --blocks >> "$LOG" 2>&1 &
fi

# --- Task G: Family-check advisory over the just-written session summary ---
# Cheap local grep for the five recurring failure-class signature phrasings named
# in knowledge/<kb-entry>.md ^families (proxy-not-property,
# queued-not-landed, names-not-identities, silent-partial-success,
# transport-not-delivery). Advisory only: a hit just means the summary CONTAINS
# language shaped like one of these patterns, not that the session actually
# committed the failure - never blocks Stop, never fails the hook.
export SUMMARY_FILE
python3 -c "
import os, re
from datetime import datetime

summary_path = os.environ.get('SUMMARY_FILE', '')

try:
    with open(summary_path, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()
except Exception:
    text = ''

# One regex per family; each is a cheap substring/shape match, not a semantic
# classifier -- false positives are expected and acceptable for an advisory.
# No literal quote characters in these patterns on purpose: this whole script is
# embedded in a bash double-quoted python3 -c string, and word-boundary \b
# already matches a quoted word (a quote char is non-word) without needing one.
FAMILY_PATTERNS = [
    ('proxy-not-property',
     r'\b(?:as a proxy for|instead of (?:checking|verifying|confirming)|stand-in (?:signal|for)|proxy (?:signal|check))\b'),
    ('queued-not-landed',
     r'\bqueued\b(?![^.\n]{0,60}\b(?:landed|confirmed|verified|delivered|drained)\b)'),
    ('names-not-identities',
     r'\b(?:match(?:ed|ing)?|delete[ds]?|target(?:ed|ing)?) by (?:name|path|filename)\b'),
    ('silent-partial-success',
     r'\ball (?:\d+ )?(?:tests?|items?|files?|checks?|agents?|steps?) (?:passed|succeeded|completed|done)\b'),
    ('transport-not-delivery',
     r'\bsuccess\b\s*:?\s*true\b|\baccepted\b|\bdispatch(?:ed)?\b(?![^.\n]{0,60}\b(?:confirmed|verified|observed)\b)'),
]

hits = []
if text:
    for family, pattern in FAMILY_PATTERNS:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                hits.append(family)
        except Exception:
            continue

for family in hits:
    print(f'{datetime.now().astimezone().isoformat()} ADVISORY: possible {family} pattern in this sessions summary -- see no-unverified-claims/no-silent-deferral.')
" >> "$LOG" 2>&1 || true

# Wait for background tasks (Letta, promotion queue, backup) before releasing lock
wait

_gestalt_unlock
exit 0
