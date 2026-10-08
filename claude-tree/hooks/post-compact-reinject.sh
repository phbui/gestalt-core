#!/usr/bin/env bash
# SessionStart hook (matcher: compact) — re-injects gestalt context after context compaction.
# Restores Tier 1 Letta blocks + infers current task for gestalt entry pointers.

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
source "$SCRIPT_DIR/lib.sh" && gestalt_init

TIER1_BLOCKS="${GESTALT_TIER1_BLOCKS:-core_directives|user_preferences|session_patterns|guidance|pending_items}"

# --- 1. Re-fetch Tier 1 Letta blocks (shared, self-healing 90s cache -- see lib.sh
# gestalt_hub_health, <kb-entry> F3/F4 ^health-probe-cache) ---
LETTA_CONTEXT=""
gestalt_hub_health
if [ "${HUB_LETTA_OK:-0}" = "1" ]; then
    AGENT_ID_FILE="$STATE_DIR/letta-agent-id.txt"
    if [ -f "$AGENT_ID_FILE" ]; then
        AGENT_ID=$(cat "$AGENT_ID_FILE")
        AGENT_DATA=$(curl -sf --max-time 5 "$LETTA_URL/agents/$AGENT_ID" 2>/dev/null || echo "")
        if [ -n "$AGENT_DATA" ]; then
            LETTA_CONTEXT=$(echo "$AGENT_DATA" | python3 -c "
import sys, json, re, os
try:
    agent = json.load(sys.stdin)
    blocks = agent.get('memory', {}).get('blocks', [])
    tier1 = os.environ.get('GESTALT_TIER1_BLOCKS',
        'core_directives|user_preferences|session_patterns|guidance|pending_items')
    lines = []
    for b in blocks:
        val = b.get('value', '').strip()
        if val and re.fullmatch(tier1, b['label']):
            lines.append(f\"## {b['label']}\n{val}\")
    print('\n\n'.join(lines))
except Exception:
    pass
" 2>/dev/null)
        fi
    fi
fi

# --- 2. Read stdin + task inference ---
STDIN_DATA=$(cat)
TASK_HINT=""

TRANSCRIPT_PATH=$(echo "$STDIN_DATA" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('transcript_path', ''))
except Exception:
    pass
" 2>/dev/null)

if [ -f "$TRANSCRIPT_PATH" ]; then
    INFERRED_TASK=$(TRANSCRIPT_PATH="$TRANSCRIPT_PATH" python3 << 'PYEOF'
import json, re, sys, os

transcript = os.environ.get('TRANSCRIPT_PATH', '')
if not transcript:
    sys.exit(0)
try:
    with open(transcript) as f:
        lines = [l.strip() for l in f if l.strip()]
except Exception:
    sys.exit(0)

user_msgs = []
for line in reversed(lines):
    try:
        entry = json.loads(line)
        if entry.get('type') == 'user':
            content = entry.get('content', '')
            if isinstance(content, list):
                content = ' '.join(c.get('text','') for c in content if c.get('type')=='text')
            user_msgs.append(str(content)[:200])
            if len(user_msgs) >= 5:
                break
    except Exception:
        continue

combined = ' '.join(user_msgs)
stopwords = {'the','and','for','with','that','this','are','was','has','have',
             'you','not','but','what','from','can','will','how','when','its','our','all'}
terms = re.findall(r'[a-z][a-z0-9_-]{2,}', combined.lower())
meaningful = [t for t in terms if t not in stopwords]
print(' '.join(meaningful[:20]))
PYEOF
)

    if [ -n "$INFERRED_TASK" ]; then
        TASK_HINT=$(INFERRED_TASK="$INFERRED_TASK" python3 << PYEOF2
import sys, os
sys.path.insert(0, '$GESTALT_DIR/tools')
try:
    from gestalt_mcp_server import gestalt_search
    task = os.environ.get('INFERRED_TASK', '')
    results = gestalt_search(task, limit=2)
    # No score threshold: the RRF ceiling 2/(K+1) sits below any useful constant,
    # so a gate here can never fire. See [[gestalt#^rrf-thresholds]].
    slugs = list(dict.fromkeys(r['slug'] for r in results))[:2]
    if slugs:
        print('Relevant gestalt: ' + ', '.join(f'[[{s}]]' for s in slugs))
except Exception as e:
    # F13, <kb-entry>: log what broke instead of
    # swallowing it silently -- the no-op fallback (empty TASK_HINT, hook still succeeds)
    # is unchanged; only the diagnostic is new.
    try:
        import datetime
        with open('$LOG', 'a') as _lf:
            _lf.write(
                datetime.datetime.now().astimezone().isoformat(timespec='seconds')
                + f' WARN: post-compact-reinject gestalt_search failed: {type(e).__name__}: {e}\n'
            )
    except Exception:
        pass
PYEOF2
)
    fi
fi

# --- 3. Build and output additionalContext ---
STATIC_REMINDERS="Session resumed after context compaction. Key reminders: (1) Always check gestalt/MANIFEST.md before answering questions — the knowledge base has curated repo entries. (2) Use parallel agents for 3+ independent subtasks (up to 6 per wave). (3) Available skills: /investigate, /discuss, /build, /fix, /audit, /save, /learn, /review, /sync. (4) If working on a task, check if prior context was lost and re-read relevant files."

ADDITIONAL=""
if [ -n "$LETTA_CONTEXT" ]; then
    ADDITIONAL="## Memory (restored after compaction)\n\n${LETTA_CONTEXT}\n\n"
fi
if [ -n "$TASK_HINT" ]; then
    ADDITIONAL="${ADDITIONAL}${TASK_HINT}\n\n"
fi
ADDITIONAL="${ADDITIONAL}${STATIC_REMINDERS}"

python3 -c "
import json, sys
text = sys.argv[1]
print(json.dumps({'additionalContext': text}))
" "$(printf '%b' "$ADDITIONAL")"
