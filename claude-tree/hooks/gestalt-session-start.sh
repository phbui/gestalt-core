#!/usr/bin/env bash
set -uo pipefail

# --- Load shared library ---
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
source "$SCRIPT_DIR/lib.sh" && gestalt_init

# Resolve paths from .env (with defaults)
SESSIONS_DIR="${GESTALT_SESSIONS_DIR:-$WORKSPACE/.claude/memory/sessions}"
LETTA_URL="${LETTA_URL:-http://localhost:8283/v1}"
RETENTION="${GESTALT_SESSION_RETENTION_DAYS:-7}"
AGENT_ID_FILE="$STATE_DIR/letta-agent-id.txt"

mkdir -p "$STATE_DIR" "$SESSIONS_DIR"

# Rotate log if >1MB
[ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ] && : > "$LOG"

# --- 1. Health check Letta (shared, self-healing 90s cache -- see lib.sh
# gestalt_hub_health, <kb-entry> F3/F4 ^health-probe-cache) ---
gestalt_hub_health
LETTA_OK=false
if [ "${HUB_LETTA_OK:-0}" = "1" ]; then
    LETTA_OK=true
else
    echo "$(date -Iseconds) WARN: Letta unreachable at $LETTA_URL" >> "$LOG"
fi

# --- 2. Auto-restore if Letta agent is missing ---
if [ "$LETTA_OK" = true ] && [ -f "$AGENT_ID_FILE" ]; then
    _CHECK_ID=$(cat "$AGENT_ID_FILE")
    _CHECK_CODE=$(curl -sL -o /dev/null -w '%{http_code}' --max-time 2 "$LETTA_URL/agents/$_CHECK_ID" 2>/dev/null || echo "000")
    if ! ([ "$_CHECK_CODE" -ge 200 ] 2>/dev/null && [ "$_CHECK_CODE" -lt 300 ] 2>/dev/null); then
        echo "$(date -Iseconds) WARN: Agent $_CHECK_ID missing (HTTP $_CHECK_CODE), attempting restore..." >> "$LOG"
        RESTORE_SCRIPT="$GESTALT_DIR/tools/restore.sh"
        if [ -x "$RESTORE_SCRIPT" ]; then
            bash "$RESTORE_SCRIPT" 2>>"$LOG" || { echo "$(date -Iseconds) ERROR: restore.sh failed" >> "$LOG"; }
        fi
    fi
elif [ "$LETTA_OK" = true ] && [ ! -f "$AGENT_ID_FILE" ]; then
    RESTORE_SCRIPT="$GESTALT_DIR/tools/restore.sh"
    if [ -x "$RESTORE_SCRIPT" ]; then
        bash "$RESTORE_SCRIPT" 2>>"$LOG" || { echo "$(date -Iseconds) ERROR: restore.sh failed" >> "$LOG"; }
    fi
fi

# --- 3. Retrieve Letta memory blocks (tier1 always injected; tier2 cached for on-demand recall) ---
LETTA_CONTEXT=""
if [ "$LETTA_OK" = true ] && [ -f "$AGENT_ID_FILE" ]; then
    AGENT_ID=$(cat "$AGENT_ID_FILE")
    HTTP_CODE=$(curl -sL -o /tmp/letta-agent-resp.json -w '%{http_code}' --max-time 3 "$LETTA_URL/agents/$AGENT_ID" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" -ge 200 ] 2>/dev/null && [ "$HTTP_CODE" -lt 300 ] 2>/dev/null; then
        AGENT_DATA=$(cat /tmp/letta-agent-resp.json)
    else
        AGENT_DATA=""
        echo "$(date -Iseconds) WARN: Letta agent $AGENT_ID returned HTTP $HTTP_CODE (agent may be deleted)" >> "$LOG"
    fi
    rm -f /tmp/letta-agent-resp.json
    if [ -n "$AGENT_DATA" ]; then
        LETTA_CONTEXT=$(echo "$AGENT_DATA" | python3 -c "
import sys, json, re, os
try:
    agent = json.load(sys.stdin)
    blocks = agent.get('memory', {}).get('blocks', [])
    tier1 = os.environ.get('GESTALT_TIER1_BLOCKS',
        'core_directives|user_preferences|session_patterns|guidance|pending_items')
    tier1_lines = []
    tier2_cache = []
    for b in blocks:
        val = b.get('value', '').strip()
        if not val:
            continue
        label = b['label']
        if re.fullmatch(tier1, label):
            tier1_lines.append(f'## {label}\n{val}')
        else:
            words = sorted(set(re.findall(r'[a-z]{3,}', val.lower())))[:50]
            tier2_cache.append({'label': label, 'value': val, 'keywords': words})
    print('\n\n'.join(tier1_lines))
    cache_path = os.path.join(os.environ.get('STATE_DIR',
        os.path.expanduser('~/.claude/gestalt')), 'tier2-cache.json')
    with open(cache_path, 'w') as f:
        json.dump({'blocks': tier2_cache}, f)
except Exception:
    pass
" 2>/dev/null || echo "")
    fi
fi

# --- 4. CWD-based repo hint (lightweight, no network/index dependency) ---
# Computed early so both the session digest (below) and the semantic gestalt
# hint (step 8) can use it without duplicating the relpath logic.
REPO_NAME=""
if [ "$PWD" != "$WORKSPACE" ]; then
    REPO_NAME=$(python3 -c "
import os
cwd = os.getcwd()
workspace = '$WORKSPACE'
try:
    rel = os.path.relpath(cwd, workspace)
    parts = rel.split(os.sep)
    if parts and parts[0] not in ('.', '..', ''):
        print(parts[0])
except Exception:
    pass
" 2>/dev/null)
fi

# --- 5. Compact session digest ---
# Replaces raw last-3-by-mtime concatenation (previously ~30KB of verbatim
# transcript prose — 76% of the 40KB injection cap, crowding out Letta
# memory and the CWD hint). Instead: pull a small candidate pool, prefer
# summaries that mention the current repo (REPO_NAME), and extract only
# salient lines (decisions/corrections/pending items) or a short fallback
# per file, capped in both line count and characters.
SESSION_CONTEXT=""
if [ -d "$SESSIONS_DIR" ]; then
    export _SESS_DIR="$SESSIONS_DIR" _REPO_HINT="$REPO_NAME"
    SESSION_CONTEXT=$(python3 -c "
import os, re, glob

sess_dir = os.environ.get('_SESS_DIR', '')
repo_hint = os.environ.get('_REPO_HINT', '').lower()

files = sorted(glob.glob(os.path.join(sess_dir, '*.md')), key=os.path.getmtime, reverse=True)
pool = files[:8]  # candidate pool considered for repo-relevance scoring

salient = re.compile(
    r'actually|decided|decision|correct|instead|discovered|realized|fix(?:ed)?|'
    r'pending|todo|next step|blocked|resolved',
    re.IGNORECASE,
)

scored = []
for f in pool:
    try:
        text = open(f, encoding='utf-8', errors='ignore').read()
    except Exception:
        continue
    score = 1 if (repo_hint and repo_hint in text.lower()) else 0
    scored.append((score, os.path.getmtime(f), f, text))

# Repo-relevant files first, most recent as tiebreak; cap at 3 files total.
scored.sort(key=lambda t: (-t[0], -t[1]))
selected = scored[:3]

MAX_LINES = 6
MAX_LINE_CHARS = 240
chunks = []
for _, _, f, text in selected:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    picked = [l for l in lines if salient.search(l)][:MAX_LINES]
    if not picked:
        # No salient lines matched — fall back to the first few lines,
        # skipping the title/message-count boilerplate.
        body = [l for l in lines if not l.startswith('#') and not l.startswith('**Messages:**')]
        picked = body[:MAX_LINES]
    picked = [l[:MAX_LINE_CHARS] for l in picked]
    if picked:
        chunks.append(f'### {os.path.basename(f)}\n' + '\n'.join(f'- {l}' for l in picked))

print('\n\n'.join(chunks))
" 2>/dev/null || echo "")
fi

# --- 7. Cleanup old session summaries ---
find "$SESSIONS_DIR" -name "*.md" -mtime +"$RETENTION" -type f -delete 2>/dev/null || true

# --- 7b. Cleanup old transcript copies ---
TRANSCRIPTS_DIR="${GESTALT_TRANSCRIPTS_DIR:-$WORKSPACE/.claude/memory/transcripts}"
find "$TRANSCRIPTS_DIR" -name "*.jsonl" -mtime +"$RETENTION" -type f -delete 2>/dev/null || true

# --- 8. Check if search index is stale ---
# gestalt D1 (<kb-entry>, knowledge/gestalt.md index section): ONE build,
# hash-gated (stamp = repo HEAD sha + builder schema hash), hub-served, with local fallback — so
# laptops stop re-embedding the same corpus every session. GESTALT_INDEX_BUILD_ROLE in
# ~/.fleet/fleet.env (sourced by lib.sh's gestalt_init, default "any" until the hub leg is
# verified live per ^hub-relaunch-checklist): "hub" = only the hub node ever runs the embedding
# build; every other node either fetches a matching artifact or falls back to an FTS-only local
# build, NEVER embeds. "any" = try a fetch first (cheap no-op when the stamp already matches
# HEAD), fall back to today's local incremental build otherwise.
INDEX_BUILDER="$GESTALT_DIR/tools/gestalt-index-builder.py"
FLEET_SYNC_BIN="$(command -v fleet-sync 2>/dev/null || echo "$HOME/.local/bin/fleet-sync")"
GESTALT_INDEX_BUILD_ROLE="${GESTALT_INDEX_BUILD_ROLE:-any}"
HUB_SHORT_NAME="${FLEET_HUB_NAME:-${FLEET_HUB:-node.example.ts.net}}"; HUB_SHORT_NAME="${HUB_SHORT_NAME%%.*}"
THIS_HOST="${FLEET_HOST:-$(hostname -s 2>/dev/null || hostname)}"
# Compare case-INSENSITIVELY. `hostname -s` returns "MSI" on the hub while the fleet's hub
# name is "hub", so this equality was false ON THE HUB ITSELF: index-publish never ran after a
# session-start rebuild, which is why artifact-for-head failed for 46df5b6c. The builder's own
# _role_forces_fts_only already lowercases both sides; this did not.
IS_HUB=0; [ "$(printf '%s' "$THIS_HOST" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$HUB_SHORT_NAME" | tr '[:upper:]' '[:lower:]')" ] && IS_HUB=1

STALE_STATE=missing
if [ -f "$GESTALT_DIR/.search/gestalt.db" ]; then
    NEWEST=$(find "$GESTALT_DIR/knowledge" -name '*.md' -newer "$GESTALT_DIR/.search/gestalt.db" 2>/dev/null | head -1)
    if [ -n "$NEWEST" ]; then STALE_STATE="stale — $(basename "$NEWEST") modified after last build"; else STALE_STATE=fresh; fi
fi

if [ "$STALE_STATE" != fresh ] && [ -f "$INDEX_BUILDER" ]; then
    echo "$(date -Iseconds) INFO: Search index $STALE_STATE" >> "$LOG"
    (
        fetched=1
        if [ -x "$FLEET_SYNC_BIN" ]; then
            "$FLEET_SYNC_BIN" index-fetch >> "$LOG" 2>&1 && fetched=0
        fi
        if [ "$fetched" = 0 ]; then
            echo "$(date -Iseconds) INFO: Search index installed via fleet-sync index-fetch (no local build)" >> "$LOG"
        elif [ "$GESTALT_INDEX_BUILD_ROLE" = hub ] && [ "$IS_HUB" != 1 ]; then
            # role=hub on a non-hub node: never embed here — FTS-only quick local build only.
            "${GESTALT_PYTHON:-python3}" "$INDEX_BUILDER" --force --fts-only >> "$LOG" 2>&1
            echo "$(date -Iseconds) INFO: Search index FTS-only build (role=hub, non-hub node, no artifact to fetch)" >> "$LOG"
        else
            "${GESTALT_PYTHON:-python3}" "$INDEX_BUILDER" --force >> "$LOG" 2>&1
            echo "$(date -Iseconds) INFO: Search index rebuild complete" >> "$LOG"
            if [ "$IS_HUB" = 1 ] && [ -x "$FLEET_SYNC_BIN" ]; then
                "$FLEET_SYNC_BIN" index-publish >> "$LOG" 2>&1 && echo "$(date -Iseconds) INFO: Search index published (fleet-sync index-publish)" >> "$LOG"
            fi
        fi
    ) >> "$LOG" 2>&1 &
    echo "$(date -Iseconds) INFO: Search index refresh triggered (background)" >> "$LOG"
fi

# --- 8b. Dual-agent sync drift check (normalized content) ---
# .claude/ and .cursor/ mirrors differ in two legitimate ways: tool-path
# self-references (.claude/ vs .cursor/, rules/*.md vs rules/*.mdc) and a few
# deliberate per-tool lines (e.g. each tool's own agent-concurrency cap).
# Byte-compare false-positives on both; the previous last-commit-timestamp
# compare was worse — a correct ONE-SIDED parity fix desyncs timestamps
# permanently, so reconciled pairs were flagged forever (observed 2026-08-29:
# all five flagged pairs were content-identical after translation).
# Now: diff after normalizing the translations; a non-empty diff is drift
# UNLESS its hash is recorded as an accepted per-tool divergence in
# references/.sync-accepted ("<name> <sha256-16 of normalized diff>").
# Editing either side of an accepted pair changes the hash and re-flags it.
DRIFT=""
SYNC_ACCEPT="$GESTALT_DIR/.claude/references/.sync-accepted"
_sync_norm() { sed -e 's|\.claude/|.TOOL/|g' -e 's|\.cursor/|.TOOL/|g' -e 's|\(rules/[A-Za-z0-9_-]*\)\.mdc|\1.md|g' "$1"; }
for f in "$GESTALT_DIR"/.claude/references/*.md; do
    [ -f "$f" ] || continue
    n=$(basename "$f")
    c="$GESTALT_DIR/.cursor/references/$n"
    if [ ! -f "$c" ]; then DRIFT="$DRIFT $n(missing)"; continue; fi
    d=$(diff <(_sync_norm "$f") <(_sync_norm "$c") 2>/dev/null) || true
    [ -z "$d" ] && continue
    h=$(printf '%s' "$d" | sha256sum | cut -c1-16)
    grep -qx "$n $h" "$SYNC_ACCEPT" 2>/dev/null && continue
    DRIFT="$DRIFT $n"
done
# --- 8c. Refresh repo-staleness report (background) ---
# BRANCHES.md-vs-actual-HEAD drift. Backgrounded like the index rebuild above: the
# consuming rule (rules/memory-activation.md §Staleness Check) reads
# STALENESS_REPORT.md at session start, so the refresh belongs here rather than on a
# wall-clock timer -- a timer firing between sessions guarantees nothing about
# freshness at the moment the report is actually read. The script writes a report only
# when a tracked repo has drifted and removes it otherwise, so absence means
# "checked, all current".
STALENESS_CHECK="$GESTALT_DIR/bin/check-staleness.sh"
if [ -f "$STALENESS_CHECK" ]; then
    bash "$STALENESS_CHECK" >> "$LOG" 2>&1 &
    echo "$(date -Iseconds) INFO: Staleness check triggered (background)" >> "$LOG"
fi

# --- 8d. Git change detection -> Graphiti (Phase 2, hooks.md ^git-change-detection) ---
GIT_FEED="$GESTALT_DIR/tools/gestalt-git-graphiti-feed.sh"
if [ -f "$GIT_FEED" ]; then
    bash "$GIT_FEED" >> "$LOG" 2>&1 &
    echo "$(date -Iseconds) INFO: Git graphiti feed triggered (background)" >> "$LOG"
fi

# --- 8b. (removed) embedding-model warmup ---
# Do not re-add: a model loaded in a subprocess dies with it, and searches are
# served by the long-lived MCP server's own singleton. See [[gestalt#^fts-path]].

# --- 8c. CWD-based semantic gestalt hint (uses REPO_NAME from step 4) ---
CWD_HINT=""
if [ -n "$REPO_NAME" ] && [ -f "$GESTALT_DIR/tools/gestalt-index-builder.py" ]; then
    CWD_HINT=$(python3 -c "
import sys
sys.path.insert(0, '$GESTALT_DIR/tools')
try:
    from gestalt_mcp_server import gestalt_search_fts
    # Lexical only: the hybrid path loads an embedding model and blew this hook's
    # 10 s timeout. See [[gestalt#^fts-path]].
    results = gestalt_search_fts('$REPO_NAME', limit=6)
    # No score gate: ranking scores are not calibrated relevance, so no constant
    # separates on- from off-topic. See [[gestalt#^bm25-thresholds]].
    # dict.fromkeys dedupes by slug while preserving rank order.
    slugs = list(dict.fromkeys(r['slug'] for r in results))[:3]
    if slugs:
        print('Relevant gestalt: ' + ', '.join(f'[[{s}]]' for s in slugs))
except Exception:
    pass
" 2>/dev/null)
fi

# --- 5b. Latest briefing (<kb-entry>) ---
# jarvis-briefing-run on the hub publishes each briefing as retained fleet/briefing; fleet-heartbeat
# caches it to ~/.fleet/briefing-latest.json on every node. Read the file only (same doctrine as
# hub-health: never a network call from a hook); inject when younger than 24 h, capped at 4000 chars.
BRIEF_CONTEXT=""
if [ -f "$HOME/.fleet/briefing-latest.json" ]; then
    BRIEF_CONTEXT=$(python3 -c "
import json, os, sys, time
try:
    d = json.load(open(os.path.expanduser('~/.fleet/briefing-latest.json')))
    ts = int(d.get('ts') or 0); text = (d.get('text') or '').strip()
    if text and time.time() - ts < 86400:
        age = int((time.time() - ts) / 60)
        print('## Latest briefing (%s %s, %d min ago)\n%s' % (d.get('mode') or '', d.get('date') or '', age, text[:4000]))
except Exception:
    pass
" 2>/dev/null)
fi

# --- 9. Inject via stdout ---
# Per-section budgets, then wrap. Ordering + budgets guarantee that
# operational content (session digest, CWD hint) always
# survives; Letta memory — the largest and most compressible section —
# absorbs whatever remains of the 40KB cap. The closing tag always
# survives because content is truncated before wrapping.
if [ -n "$LETTA_CONTEXT" ] || [ -n "$SESSION_CONTEXT" ] || [ -n "$CWD_HINT" ] || [ -n "$BRIEF_CONTEXT" ]; then
    # Command substitution strips trailing newlines, so section separators
    # are emitted OUTSIDE the captured parts (in _emit), never inside them.
    _CAP=16000
    _SESS_PART=""
    [ -n "$SESSION_CONTEXT" ] && _SESS_PART=$(printf '## Recent Sessions (compact)\n%s' "$SESSION_CONTEXT" | head -c 8000)
    _HINT_PART=""
    [ -n "$CWD_HINT" ] && _HINT_PART=$(printf '%s' "$CWD_HINT" | head -c 1000)
    _BRIEF_PART=""
    [ -n "$BRIEF_CONTEXT" ] && _BRIEF_PART=$(printf '%s' "$BRIEF_CONTEXT" | head -c 4200)
    _USED=$(( $(printf '%s' "$_SESS_PART" | wc -c) + $(printf '%s' "$_HINT_PART" | wc -c) + $(printf '%s' "$_BRIEF_PART" | wc -c) ))
    _LETTA_BUDGET=$(( _CAP - _USED ))
    [ "$_LETTA_BUDGET" -lt 0 ] && _LETTA_BUDGET=0
    _LETTA_PART=""
    [ -n "$LETTA_CONTEXT" ] && _LETTA_PART=$(printf '## Letta Memory Blocks\n%s' "$LETTA_CONTEXT" | head -c "$_LETTA_BUDGET")
    _emit() { [ -n "$1" ] && printf '%s\n\n' "$1"; }
    echo "<gestalt-memory>"
    _emit "$_SESS_PART"
    _emit "$_HINT_PART"
    _emit "$_BRIEF_PART"
    _emit "$_LETTA_PART"
    echo "</gestalt-memory>"
fi

# --- 9b. Resident-seat dispatcher policy (headless seats only) ---
# The resident seat has no human at the keyboard: an open AskUserQuestion menu
# freezes the session and queues every inbound cross-session message, with no
# remote way to answer it (resident-no-ask.sh is the hard backstop that denies
# the tool; this injects the behavioral half). <kb-entry>.
if [ -n "${TMUX_PANE:-}" ] && command -v tmux >/dev/null 2>&1; then
    _TMUX_S=$(tmux display-message -p -t "$TMUX_PANE" '#S' 2>/dev/null || true)
    if [ "$_TMUX_S" = "claude-resident" ]; then
        cat <<'RESIDENT'
<resident-seat-policy>
You are this node's headless resident seat. Work usually arrives via cross-session SendMessage and nobody is at this keyboard.
- NEVER call AskUserQuestion (a PreToolUse hook denies it). If a decision genuinely needs a human or the dispatching session, send the question back to the sender via SendMessage and continue other work or end the turn; otherwise decide autonomously and state the assumption in your reply.
- Act as a dispatcher, not a serial worker: run each dispatched task as background Agent-tool subagents (ceiling 6 per wave) so your main loop stays free to receive further messages. Relay results to the dispatcher via SendMessage when subagents report back.
- Hub caution still binds on the hub: size heavy jobs first, prefer foreground for anything large (hub-invariants).
- Never block on a peer's answer: cross-session delivery is unconfirmable in both directions. When you must ask, state your default in the same message, wait at most 30 minutes, take the default, record it in the queue item in knowledge/capture-inbox.md, and continue; re-ask in your next report. Pull before acting on a decision; the repo is authoritative when a message and the queue item disagree (<kb-entry>).
</resident-seat-policy>
RESIDENT
    fi
fi

# --- 10. Auto-consolidation (every N sessions) ---
COUNTER_FILE="$STATE_DIR/session-counter"
COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNTER_FILE"

# Default off (N11, 2026-10-06): auto-promote only appends proposals for review, so opt in with =true.
AUTO_CONSOLIDATE="${GESTALT_AUTO_CONSOLIDATE:-false}"
CONSOLIDATE_INTERVAL="${GESTALT_CONSOLIDATE_INTERVAL:-5}"

if [ $((COUNT % CONSOLIDATE_INTERVAL)) -eq 0 ] && [ "$COUNT" -gt 0 ]; then
    if [ "$AUTO_CONSOLIDATE" = "true" ]; then
        # Spawn background promotion agent (non-blocking)
        PROMOTE_SCRIPT="$GESTALT_DIR/tools/auto-promote.py"
        if [ -f "$PROMOTE_SCRIPT" ]; then
            nohup python3 "$PROMOTE_SCRIPT" \
                --gestalt-dir "$GESTALT_DIR" \
                --state-dir "$STATE_DIR" \
                --letta-url "$LETTA_URL" \
                --graphiti-url "${GRAPHITI_URL:-http://localhost:8200}" \
                --min-sessions "${GESTALT_PROMOTE_MIN_SESSIONS:-2}" \
                >> "$LOG" 2>&1 &
            echo "$(date -Iseconds) INFO: Auto-promotion agent spawned (session #$COUNT)" >> "$LOG"
        else
            echo "$(date -Iseconds) WARN: auto-promote.py not found at $PROMOTE_SCRIPT" >> "$LOG"
        fi
    else
        # Fallback: nudge when auto-consolidate is disabled
        echo "<gestalt-consolidation-nudge>"
        echo "This is session #${COUNT}. Consider running /consolidate to synthesize knowledge across memory layers."
        echo "This promotes Letta learnings to gestalt entries, adds missing cross-links, and flags stale facts."
        echo "</gestalt-consolidation-nudge>"
        echo "$(date -Iseconds) INFO: Consolidation nudge (session #$COUNT, auto-consolidate disabled)" >> "$LOG"
    fi
fi

exit 0
