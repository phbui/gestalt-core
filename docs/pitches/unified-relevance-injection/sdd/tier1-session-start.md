---
pitch_id: unified-relevance-injection
document: sdd-component
component: tier1-session-start
version: 1.0
created: 2026-04-09
---

# Component: Tier 1 Session Start

**Satisfies:** URI-FR-010, URI-FR-011, URI-FR-012, URI-FR-013, URI-NFR-002, URI-NFR-004

Modifies `gestalt-session-start.sh`. Changes are surgical — the existing flow is preserved; only the block injection and warm-up logic change.

## 1. Tier 1 Block Filtering

**Satisfies:** URI-FR-010, URI-FR-011

**Current code (lines 58-70, gestalt-session-start.sh):**

```python
python3 -c "
import sys, json
agent = json.load(sys.stdin)
blocks = agent.get('memory', {}).get('blocks', [])
for b in blocks:
    val = b.get('value', '').strip()
    if val:
        print(f\"## {b['label']}\n{val}\n\")
" <<< "$AGENT_DATA" > /tmp/letta-blocks.txt
LETTA_CONTEXT=$(cat /tmp/letta-blocks.txt)
```

**Replacement:**

```bash
# Load Tier 1 block list from .env (default if not set)
TIER1_BLOCKS="${GESTALT_TIER1_BLOCKS:-core_directives|user_preferences|session_patterns|guidance|pending_items}"

# Parse: Tier 1 injection + Tier 2 cache in one pass
python3 -c "
import sys, json, re, os

agent = json.load(sys.stdin)
blocks = agent.get('memory', {}).get('blocks', [])
tier1_pattern = os.environ.get('GESTALT_TIER1_BLOCKS',
    'core_directives|user_preferences|session_patterns|guidance|pending_items')

tier1_lines = []
tier2_cache = []

for b in blocks:
    val = b.get('value', '').strip()
    if not val:
        continue
    label = b['label']
    if re.fullmatch(tier1_pattern, label):
        tier1_lines.append(f'## {label}\n{val}')
    else:
        # Pre-extract keywords for per-prompt matching
        words = set(re.findall(r'[a-z]{3,}', val.lower()))
        tier2_cache.append({
            'label': label,
            'value': val,
            'keywords': sorted(words)[:50]  # cap at 50 keywords
        })

# Write Tier 1 to stdout (captured by LETTA_CONTEXT below)
print('\n\n'.join(tier1_lines))

# Write Tier 2 cache to file for prompt-intelligence.sh
import json as _json
cache_path = os.environ.get('GESTALT_TIER2_CACHE',
    os.path.expanduser('~/.claude/gestalt/tier2-cache.json'))
with open(cache_path, 'w') as f:
    _json.dump({'blocks': tier2_cache}, f)
" <<< "$AGENT_DATA" > /tmp/letta-tier1.txt
LETTA_CONTEXT=$(cat /tmp/letta-tier1.txt)
```

**Token impact:** Tier 1 blocks total ~1,100 tokens vs current ~4,600 for all blocks. Reduction: ~3,500 tokens at session start.

## 2. Tier 2 Block Cache

**Satisfies:** URI-FR-010 (domain blocks excluded from session start)

The Tier 2 cache (`$STATE_DIR/tier2-cache.json`) is written during the block parsing step above. It contains all non-Tier-1 blocks with pre-extracted keywords.

- **Location:** `$STATE_DIR/tier2-cache.json` (default: `~/.claude/gestalt/tier2-cache.json`)
- **Configurable:** `GESTALT_TIER2_CACHE` env var
- **Format:** `{"blocks": [{"label": "...", "value": "...", "keywords": [...]}]}`
- **Consumed by:** `prompt-intelligence.sh` for per-prompt keyword matching

The cache is written at session start and remains valid for the session. If Letta is unreachable, the cache file is not written (or retains the previous session's content if the file already exists).

## 3. Gestalt Search Warm-Up

**Satisfies:** URI-NFR-002

The nomic-embed-text-v1.5 model takes 3-5 seconds to load on first invocation. Without warm-up, the first prompt's Tier 2 injection would experience this cold start.

Add after index staleness check:

```bash
# Warm up gestalt_search embedding model (background — does not block session start)
(python3 "$GESTALT_DIR/tools/gestalt-index-builder.py" --warmup \
    >> "$LOG" 2>&1) &
```

The `--warmup` flag triggers a dummy embedding call to load the model into memory without writing anything. The MCP server process keeps the model loaded for subsequent calls.

**If `--warmup` flag doesn't exist in gestalt-index-builder.py:** Add a lightweight alternative:

```bash
# Alternative: make a dummy gestalt_search call via Python import
(python3 -c "
import sys
sys.path.insert(0, '$GESTALT_DIR/tools')
try:
    from gestalt_mcp_server import gestalt_search
    gestalt_search('warm up embedding model', limit=1)
except Exception:
    pass
" >> "$LOG" 2>&1) &
```

This runs in background (`&`) so it doesn't add to the 10s SessionStart timeout.

## 4. CWD-Based Gestalt Hint

**Satisfies:** URI-FR-013

After the warm-up, detect the current working directory and inject relevant gestalt entry pointers:

```bash
# CWD hint — only if we're inside a known repo directory
CWD_HINT=""
if [ -n "$GESTALT_DIR" ] && [ "$PWD" != "$WORKSPACE" ]; then
    # Extract repo name from CWD (first path component under WORKSPACE)
    REPO_NAME=$(python3 -c "
import os, sys
cwd = os.getcwd()
workspace = '$WORKSPACE'
rel = os.path.relpath(cwd, workspace)
parts = rel.split(os.sep)
print(parts[0]) if parts else print('')
")
    if [ -n "$REPO_NAME" ] && [ "$REPO_NAME" != "." ] && [ "$REPO_NAME" != ".." ]; then
        # Search gestalt for matching entries
        CWD_HINT=$(python3 -c "
import sys
sys.path.insert(0, '$GESTALT_DIR/tools')
try:
    from gestalt_mcp_server import gestalt_search
    results = gestalt_search('$REPO_NAME', limit=3)
    slugs = [r['slug'] for r in results if r.get('score', 0) > 0.05][:3]
    if slugs:
        print('Relevant gestalt: ' + ', '.join(f'[[{s}]]' for s in slugs))
except Exception:
    pass
" 2>/dev/null)
    fi
fi
```

This runs after the warm-up background call, so the model is likely loaded. If not yet loaded, the warm-up's `&` background process and this foreground call may race — acceptable, as the CWD hint is a best-effort feature.

## 5. Updated Injection Block

The final `<gestalt-memory>` injection assembles all four components:

```bash
{
    echo "<gestalt-memory>"
    [ -n "$LETTA_CONTEXT" ] && echo "## Letta Memory Blocks" && echo "$LETTA_CONTEXT"
    [ -n "$SESSION_CONTEXT" ] && echo "## Recent Sessions" && echo "$SESSION_CONTEXT"
    [ -n "$CWD_HINT" ] && echo && echo "$CWD_HINT"
    echo "</gestalt-memory>"
} | head -c 40000  # Safety cap: ~10K tokens
```

The `head -c 40000` prevents runaway injection if session summaries are unexpectedly large.

## 6. Token Budget Verification (NFR-004)

After injection, log the estimated token count:

```bash
INJECTION_CHARS=$(wc -c < /tmp/gestalt-injection.txt)
ESTIMATED_TOKENS=$((INJECTION_CHARS / 4))
log "INFO" "Session-start injection: ~${ESTIMATED_TOKENS} tokens (${INJECTION_CHARS} chars)"
```

If `ESTIMATED_TOKENS > 2500`, log a WARNING. The warning is advisory — injection continues.

## 7. Decisions

### DEC-001: Single Python pass for Tier 1 + Tier 2 cache

Processing all blocks in one Python subprocess (rather than two separate calls) avoids re-parsing the `$AGENT_DATA` JSON. This keeps the session-start hook fast and the code coherent.

### DEC-002: Keywords pre-extracted at session start, not per-prompt

Extracting keywords from Letta blocks is O(n * text_length). Doing this once at session start (when we're already parsing blocks) avoids repeated work at every prompt. The cache file is small (~5-10KB) and fast to read.

### DEC-003: Warm-up via background subprocess, not MCP server ping

The MCP server may not be running during session start. Importing the Python module directly and calling gestalt_search triggers the model load without requiring a running MCP server process. This is more reliable than an HTTP ping.
