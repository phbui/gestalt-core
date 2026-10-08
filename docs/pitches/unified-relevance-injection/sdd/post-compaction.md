---
pitch_id: unified-relevance-injection
document: sdd-component
component: post-compaction
version: 1.0
created: 2026-04-09
---

# Component: Post-Compaction Enhancement

**Satisfies:** URI-FR-050, URI-FR-051

Modifies `post-compact-reinject.sh`. Currently outputs 7 lines of static JSON. Enhanced to re-inject Tier 1 blocks and task-inferred gestalt pointers.

## 1. Current State

```bash
#!/usr/bin/env bash
cat <<'EOF'
{"additionalContext":"Session resumed after context compaction. Key reminders: (1) Always check gestalt/MANIFEST.md..."}
EOF
```

~150 tokens. No live data. Stateless.

## 2. Enhanced Flow

```
post-compact-reinject.sh
    |
    +-- 1. Load environment (lib.sh, gestalt_init)
    +-- 2. Re-inject Tier 1 Letta blocks
    |       GET /v1/agents/{id} → filter to Tier 1 → format as markdown
    |       (same logic as session-start, same block list)
    +-- 3. Task inference
    |       Read last 5 messages from transcript → gestalt_search(terms, limit=2)
    |       → extract top 2 entry slugs
    +-- 4. Assemble additionalContext
    |       Tier 1 blocks + gestalt pointers + existing behavioral reminders
    +-- 5. Output JSON { "additionalContext": "..." }
```

## 3. Tier 1 Re-Injection

**Satisfies:** URI-FR-050

```bash
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
gestalt_init

TIER1_BLOCKS="${GESTALT_TIER1_BLOCKS:-core_directives|user_preferences|session_patterns|guidance|pending_items}"

# Re-fetch Tier 1 blocks from Letta
LETTA_CONTEXT=""
if curl -sf --max-time 3 "$LETTA_URL/health" > /dev/null 2>&1; then
    AGENT_ID=$(cat "$STATE_DIR/letta-agent-id.txt" 2>/dev/null || echo "")
    if [ -n "$AGENT_ID" ]; then
        AGENT_DATA=$(curl -sf --max-time 5 "$LETTA_URL/agents/$AGENT_ID" 2>/dev/null || echo "")
        if [ -n "$AGENT_DATA" ]; then
            LETTA_CONTEXT=$(python3 -c "
import sys, json, re, os
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
" <<< "$AGENT_DATA" 2>/dev/null)
        fi
    fi
fi
```

This re-uses the exact same filtering logic as `tier1-session-start.md`. Consider extracting to a shared function in `lib.sh`.

## 4. Task Inference

**Satisfies:** URI-FR-051

After compaction, the full conversation history is gone. To infer the current task, read the last 5 user messages from the compacted transcript (which is preserved):

```bash
TASK_HINT=""
STDIN_DATA=$(cat)
TRANSCRIPT_PATH=$(python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('transcript_path', ''))
" <<< "$STDIN_DATA" 2>/dev/null)

if [ -f "$TRANSCRIPT_PATH" ]; then
    INFERRED_TASK=$(python3 -c "
import json, re, sys

transcript = '$TRANSCRIPT_PATH'
with open(transcript) as f:
    lines = [l.strip() for l in f if l.strip()]

# Get last 5 user messages
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
# Extract meaningful terms (nouns, identifiers)
terms = re.findall(r'[a-z][a-z0-9_-]{2,}', combined.lower())
# Filter common words
stopwords = {'the','and','for','with','that','this','are','was','has','have'}
meaningful = [t for t in terms if t not in stopwords]
print(' '.join(meaningful[:20]))
" 2>/dev/null)

    if [ -n "$INFERRED_TASK" ]; then
        TASK_HINT=$(python3 -c "
import sys
sys.path.insert(0, '$GESTALT_DIR/tools')
try:
    from gestalt_mcp_server import gestalt_search
    results = gestalt_search('$INFERRED_TASK', limit=2)
    slugs = [r['slug'] for r in results if r.get('score', 0) > 0.05][:2]
    if slugs:
        print('Relevant gestalt: ' + ', '.join(f'[[{s}]]' for s in slugs))
except Exception:
    pass
" 2>/dev/null)
    fi
fi
```

## 5. Output Assembly

```bash
# Build additionalContext
ADDITIONAL=""

if [ -n "$LETTA_CONTEXT" ]; then
    ADDITIONAL="## Memory (restored after compaction)\n\n${LETTA_CONTEXT}\n\n"
fi

if [ -n "$TASK_HINT" ]; then
    ADDITIONAL="${ADDITIONAL}${TASK_HINT}\n\n"
fi

ADDITIONAL="${ADDITIONAL}Session resumed after context compaction. Key reminders: (1) Always check gestalt/MANIFEST.md before answering questions — the knowledge base has curated repo entries. (2) Use parallel agents for 3+ independent subtasks (up to 6 per wave). (3) Available skills: /investigate, /discuss, /build, /fix, /audit, /save, /learn, /review, /sync. (4) If working on a task, check if prior context was lost and re-read relevant files."

# Output JSON
python3 -c "
import json, sys
print(json.dumps({'additionalContext': sys.argv[1]}))
" "$ADDITIONAL"
```

## 6. Token Budget

Post-compaction injection is larger than the current 150 tokens but still bounded:

| Component | Tokens |
|-----------|--------|
| Tier 1 Letta blocks | ~1,100 |
| Task-inferred gestalt pointers | ~50 |
| Behavioral reminders | ~150 |
| **Total** | **~1,300** |

Well within the 10K char hook output cap. No explicit truncation needed for this component.

## 7. Decisions

### DEC-001: Re-fetch Letta at compaction, not from cache

The compaction event may occur hours after session start. The Letta blocks may have been updated by other sessions in the interim. Re-fetching via API ensures fresh state. Trade-off: ~100ms latency, acceptable since post-compaction is not time-critical (the user sees a compaction notice anyway).

### DEC-002: Transcript-based task inference, not conversation summary

The compaction event discards most conversation history but preserves the transcript file. Reading the last 5 user messages gives a reliable signal for the current task without requiring a separate summary or model call.

### DEC-003: Gestalt search score threshold at 0.05

Higher than the Tier 2 threshold (0.01) because post-compaction injection has less room for noise. Only inject gestalt pointers with meaningful match scores.
