---
pitch_id: unified-relevance-injection
document: sdd-component
component: tier2-prompt-injection
version: 1.0
created: 2026-04-09
---

# Component: Tier 2 Prompt Injection

**Satisfies:** URI-FR-020–025, URI-FR-030–031, URI-FR-040–043, URI-NFR-001, URI-NFR-003, URI-NFR-005

This is the core component. `prompt-intelligence.sh` is rewritten from 7 lines to a Python-backed shell script that performs concurrent retrieval from all three memory layers.

## 1. Blocking Prerequisite: Prompt Text Access

**Satisfies:** URI-FR-025

**Must verify before implementation:**

Claude Code's UserPromptSubmit hook receives a JSON payload on stdin. The schema must be confirmed:

```json
{
  "session_id": "...",
  "transcript_path": "...",
  "cwd": "...",
  "hook_event_name": "UserPromptSubmit",
  "prompt": "the user's message text"  // ← does this field exist?
}
```

If `prompt` is not in the stdin JSON, the hook must extract it from the transcript file:

```bash
PROMPT_TEXT=$(python3 -c "
import sys, json
data = json.load(sys.stdin)
# Try direct field first
if 'prompt' in data:
    print(data['prompt'])
    sys.exit(0)
# Fall back: read last user message from transcript
transcript_path = data.get('transcript_path', '')
if transcript_path:
    with open(transcript_path) as f:
        lines = [l for l in f if l.strip()]
    for line in reversed(lines):
        try:
            entry = json.loads(line)
            if entry.get('type') == 'user':
                content = entry.get('content', '')
                if isinstance(content, list):
                    content = ' '.join(c.get('text','') for c in content if c.get('type')=='text')
                print(content[:500])  # cap at 500 chars
                sys.exit(0)
        except Exception:
            continue
")
```

**Design note:** The first 200-500 characters of the prompt are sufficient for retrieval. Full prompt text is not needed.

## 2. Hook Architecture

`prompt-intelligence.sh` is restructured as a thin shell wrapper around a Python script:

```bash
#!/usr/bin/env bash
# prompt-intelligence.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
gestalt_init

# Read stdin (hook payload)
STDIN_DATA=$(cat)

# Delegate to Python for the heavy lifting
python3 "$SCRIPT_DIR/prompt-intelligence.py" \
    --gestalt-dir "$GESTALT_DIR" \
    --state-dir "$STATE_DIR" \
    --letta-url "$LETTA_URL" \
    --graphiti-url "$GRAPHITI_URL" \
    --tier2-max-tokens "${GESTALT_TIER2_MAX_TOKENS:-700}" \
    --search-limit "${GESTALT_SEARCH_LIMIT:-3}" \
    --graphiti-timeout "${GESTALT_GRAPHITI_TIMEOUT:-400}" \
    --relevance-threshold "${GESTALT_RELEVANCE_THRESHOLD:-0.01}" \
    <<< "$STDIN_DATA"
```

The Python script handles: prompt extraction, fan-out, RRF merge, and output formatting. The shell wrapper handles: environment loading, path resolution, config passing.

## 3. Python Script: prompt-intelligence.py

**Location:** `gestalt/.claude/hooks/prompt-intelligence.py`

### 3.1 Entry Point

```python
#!/usr/bin/env python3
import sys, json, argparse, asyncio, time, re
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gestalt-dir', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--letta-url', default='http://localhost:8283/v1')
    parser.add_argument('--graphiti-url', default='http://localhost:8000')
    parser.add_argument('--tier2-max-tokens', type=int, default=700)
    parser.add_argument('--search-limit', type=int, default=3)
    parser.add_argument('--graphiti-timeout', type=float, default=0.4)  # seconds
    parser.add_argument('--relevance-threshold', type=float, default=0.01)
    args = parser.parse_args()

    stdin_data = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    prompt_text = extract_prompt(stdin_data)

    if not prompt_text:
        # No prompt text — fall back to static output
        print(static_context())
        return

    results = asyncio.run(retrieve_all(prompt_text, args))
    merged = rrf_merge(results, k=60)
    filtered = apply_threshold(merged, args.relevance_threshold)
    budgeted = apply_token_budget(filtered, args.tier2_max_tokens)
    deduplicated = deduplicate(budgeted)

    context = format_context(deduplicated)
    output = {
        "additionalContext": context + "\n\n---\n\n" + PARALLELISM_HINTS
    }
    print(json.dumps(output))
```

### 3.2 Prompt Extraction

```python
def extract_prompt(stdin_data: dict) -> str:
    """Extract user prompt text from hook payload."""
    # Try direct field
    if 'prompt' in stdin_data:
        return stdin_data['prompt'][:500]

    # Fall back to transcript
    transcript_path = stdin_data.get('transcript_path', '')
    if not transcript_path or not Path(transcript_path).exists():
        return ''

    with open(transcript_path) as f:
        lines = [l.strip() for l in f if l.strip()]

    for line in reversed(lines):
        try:
            entry = json.loads(line)
            if entry.get('type') == 'user':
                content = entry.get('content', '')
                if isinstance(content, list):
                    content = ' '.join(
                        c.get('text', '') for c in content
                        if c.get('type') == 'text'
                    )
                return str(content)[:500]
        except Exception:
            continue
    return ''
```

### 3.3 Fan-Out Concurrent Retrieval

**Satisfies:** URI-FR-020, URI-FR-021, URI-NFR-003

```python
async def retrieve_all(prompt: str, args) -> dict[str, list[dict]]:
    """Run all three retrievals concurrently. Each returns a ranked list."""
    tasks = {
        'graphiti': asyncio.create_task(
            retrieve_graphiti(prompt, args.graphiti_url,
                              args.graphiti_timeout, args.search_limit)
        ),
        'gestalt': asyncio.create_task(
            retrieve_gestalt(prompt, args.gestalt_dir, args.search_limit)
        ),
        'letta': asyncio.create_task(
            retrieve_letta_blocks(prompt, args.state_dir)
        ),
    }
    results = {}
    done, pending = await asyncio.wait(
        tasks.values(),
        timeout=max(args.graphiti_timeout, 0.5) + 0.1  # outer timeout
    )
    for name, task in tasks.items():
        if task in done:
            try:
                results[name] = task.result()
            except Exception as e:
                results[name] = []  # graceful degradation
        else:
            task.cancel()
            results[name] = []  # timed out
    return results
```

### 3.4 Graphiti Retrieval

**Satisfies:** URI-FR-020(a), URI-FR-041, URI-FR-043

```python
async def retrieve_graphiti(prompt: str, url: str,
                            timeout: float, limit: int) -> list[dict]:
    """Query Graphiti search_memory_facts via Streamable HTTP MCP."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Health check
            health = await client.get(f"{url}/health")
            if health.status_code != 200:
                return []

            # MCP initialize
            init_resp = await client.post(f"{url}/mcp",
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05",
                                 "capabilities": {},
                                 "clientInfo": {"name": "gestalt-hook"}}})
            session_id = init_resp.headers.get("Mcp-Session-Id", "")
            if not session_id:
                return []

            # Notify initialized
            await client.post(f"{url}/mcp",
                headers={"Mcp-Session-Id": session_id,
                         "Content-Type": "application/json"},
                json={"jsonrpc": "2.0",
                      "method": "notifications/initialized"})

            # Search
            resp = await client.post(f"{url}/mcp",
                headers={"Mcp-Session-Id": session_id,
                         "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "search_memory_facts",
                                 "arguments": {"query": prompt,
                                               "max_facts": limit,
                                               "group_id": "gestalt"}}})

            data = resp.json()
            if data.get("result", {}).get("isError"):
                return []

            content = data.get("result", {}).get("content", [])
            facts = []
            for i, item in enumerate(content):
                text = item.get("text", "")
                if text:
                    facts.append({
                        "source": "graphiti",
                        "rank": i,
                        "text": text[:300],
                        "type": "fact"
                    })
            return facts
    except Exception:
        return []
```

### 3.5 Gestalt KB Retrieval

**Satisfies:** URI-FR-020(b), URI-FR-042, URI-FR-043

```python
async def retrieve_gestalt(prompt: str, gestalt_dir: str,
                           limit: int) -> list[dict]:
    """Query gestalt_search via direct Python import."""
    import asyncio
    loop = asyncio.get_event_loop()

    def _search():
        import sys as _sys
        tools_dir = str(Path(gestalt_dir) / "tools")
        if tools_dir not in _sys.path:
            _sys.path.insert(0, tools_dir)
        try:
            from gestalt_mcp_server import gestalt_search
            results = gestalt_search(prompt, limit=limit)
            return [
                {
                    "source": "gestalt",
                    "rank": i,
                    "slug": r["slug"],
                    "heading": r.get("heading", ""),
                    "block_id": r.get("block_id", ""),
                    "text": r.get("content", "")[:200],
                    "type": "entry"
                }
                for i, r in enumerate(results)
            ]
        except Exception:
            return []

    return await loop.run_in_executor(None, _search)
```

Note: `gestalt_search` is synchronous (SQLite calls). We run it in a thread pool executor to keep the event loop free for the other concurrent tasks.

### 3.6 Letta Domain Block Matching

**Satisfies:** URI-FR-020(c), URI-FR-040, URI-FR-043

```python
async def retrieve_letta_blocks(prompt: str, state_dir: str) -> list[dict]:
    """Keyword-match prompt against cached Tier 2 Letta blocks."""
    cache_path = Path(state_dir) / "tier2-cache.json"
    if not cache_path.exists():
        return []

    with open(cache_path) as f:
        cache = json.load(f)

    prompt_words = set(re.findall(r'[a-z]{3,}', prompt.lower()))
    results = []

    for block in cache.get("blocks", []):
        block_keywords = set(block.get("keywords", []))
        overlap = prompt_words & block_keywords
        if not overlap:
            continue

        # Score: fraction of block keywords matched
        score = len(overlap) / max(len(block_keywords), 1)
        results.append({
            "source": "letta",
            "rank": 0,  # will be set after sorting
            "label": block["label"],
            "text": block["value"][:300],
            "score": score,
            "type": "block"
        })

    # Sort by score descending, assign ranks
    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i

    return results[:3]  # cap at 3
```

### 3.7 RRF Merge

**Satisfies:** URI-FR-022

```python
def rrf_merge(results: dict[str, list[dict]], k: int = 60) -> list[dict]:
    """Merge ranked lists from all sources using Reciprocal Rank Fusion."""
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for source, result_list in results.items():
        for rank, item in enumerate(result_list):
            # Create a stable key for deduplication
            key = f"{item['source']}:{item.get('slug', item.get('label', item.get('text', '')[:50]))}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in items:
                items[key] = item

    # Sort by RRF score descending
    sorted_keys = sorted(scores, key=scores.__getitem__, reverse=True)
    merged = []
    for key in sorted_keys:
        item = items[key].copy()
        item["rrf_score"] = scores[key]
        merged.append(item)

    return merged
```

### 3.8 Relevance Threshold + Token Budget

**Satisfies:** URI-FR-023, URI-FR-030, URI-FR-031

```python
def apply_threshold(results: list[dict], threshold: float) -> list[dict]:
    """Remove results below minimum RRF score."""
    return [r for r in results if r.get("rrf_score", 0) >= threshold]


def apply_token_budget(results: list[dict], max_tokens: int) -> list[dict]:
    """Take results until token budget is exhausted."""
    budgeted = []
    total_chars = 0
    char_budget = max_tokens * 4  # ~4 chars per token

    for item in results:
        item_chars = len(item.get("text", "")) + 50  # 50 for formatting
        if total_chars + item_chars > char_budget:
            break
        budgeted.append(item)
        total_chars += item_chars

    return budgeted


def deduplicate(results: list[dict]) -> list[dict]:
    """Remove near-duplicate facts across sources (same text, different source)."""
    seen_text: set[str] = set()
    unique = []
    for item in results:
        # Fingerprint: first 100 chars, lowercased, whitespace-normalized
        fingerprint = re.sub(r'\s+', ' ', item.get("text", "")[:100].lower())
        if fingerprint not in seen_text:
            seen_text.add(fingerprint)
            unique.append(item)
    return unique
```

### 3.9 Output Formatting

**Satisfies:** URI-FR-024

```python
def format_context(results: list[dict]) -> str:
    """Format merged results as markdown for additionalContext."""
    if not results:
        return ""

    facts = [r for r in results if r["type"] == "fact"]
    entries = [r for r in results if r["type"] == "entry"]
    blocks = [r for r in results if r["type"] == "block"]

    sections = []

    if facts:
        fact_lines = "\n".join(f"- {r['text']}" for r in facts)
        sections.append(f"**Facts:** {fact_lines}")

    if entries:
        entry_refs = ", ".join(
            f"[[{r['slug']}{'#' + r['block_id'] if r.get('block_id') else ''}]]"
            + (f" — {r['heading']}" if r.get('heading') else "")
            for r in entries
        )
        sections.append(f"**Gestalt:** {entry_refs}")

    if blocks:
        block_lines = "\n".join(
            f"- `{r['label']}`: {r['text']}" for r in blocks
        )
        sections.append(f"**Memory:** {block_lines}")

    if not sections:
        return ""

    return "## Relevant Context\n\n" + "\n\n".join(sections)


PARALLELISM_HINTS = (
    "BEFORE RESPONDING: "
    "(1) Decompose this prompt into subtasks. If 2+ are independent, spawn parallel agents — "
    "up to 6 per wave. Use Explore agents for read-only work, general-purpose for writes/research. "
    "Even 2 parallel agents = 50% faster. "
    "(2) If the task matches a gestalt skill, suggest it: "
    "/investigate (deep research), /discuss (structured critique), /build (parallel workstreams), "
    "/fix (batch fixes), /audit (quality gate). "
    "Suggest before auto-invoking expensive skills."
)


def static_context() -> str:
    """Fallback: return existing static behavior when no prompt text available."""
    return json.dumps({"additionalContext": PARALLELISM_HINTS})
```

## 4. Hook Timeout Configuration

**Satisfies:** URI-NFR-005

Add to `~/.claude/settings.json` (under UserPromptSubmit hooks):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [{
          "type": "command",
          "command": "/home/user/.claude/hooks/prompt-intelligence.sh",
          "timeout": 2000
        }]
      }
    ]
  }
}
```

2000ms timeout = 2 seconds. This provides margin above the 500ms P95 target while preventing runaway execution if all backends hang.

## 5. Latency Breakdown

| Step | Expected P95 | Notes |
|------|-------------|-------|
| Prompt extraction | ~20ms | JSON parse + file read |
| Graphiti query | ~300ms | FalkorDB + embedding (dominant) |
| gestalt_search | ~200-500ms | Model warm after session start |
| Letta block match | ~10ms | In-memory JSON + regex |
| Fan-out total | ~500ms | max(Graphiti, gestalt) |
| RRF merge + filter | ~5ms | Pure Python |
| JSON output | ~1ms | |
| **Total** | **~500ms** | Meets URI-NFR-001 |

## 6. Graceful Degradation Matrix

**Satisfies:** URI-FR-040–043

| Letta | Graphiti | gestalt.db | Behavior |
|-------|----------|-----------|---------|
| Up | Up | Present | Full Tier 2 injection |
| Down | Up | Present | Graphiti + gestalt results only |
| Up | Down | Present | Letta blocks + gestalt results only |
| Up | Up | Missing | Graphiti + Letta blocks only |
| Down | Down | Missing | Static parallelism hints only |

Each backend wraps its entire call in `try/except` returning `[]`. The outer `retrieve_all` catches per-task exceptions. Static fallback fires when `format_context([])` returns `""`.

## 7. Decisions

### DEC-001: Python script, not pure bash

Pure bash cannot do async I/O or HTTP concurrently without complex process management. Python's `asyncio` gives clean fan-out with `asyncio.wait(timeout=...)`. The shell wrapper keeps the hook entry point as a bash script (matching existing convention) while delegating the heavy logic to Python.

### DEC-002: Direct Python import for gestalt_search, not MCP HTTP call

The gestalt MCP server uses stdio transport for Claude Code (not HTTP). Calling it via HTTP would require it to run as a separate process on a known port. Direct import of the module is simpler and avoids a port conflict. Trade-off: the module must be importable from the Python path (`$GESTALT_DIR/tools`).

### DEC-003: Transcript fallback for prompt text

If the UserPromptSubmit hook stdin JSON doesn't contain `prompt`, we read the last user message from the transcript file. This is the same file the Stop hook reads. The transcript is always present. Risk: the last line may not yet be flushed when the hook fires. Mitigation: read with a small retry (1-2 attempts, 50ms apart).

### DEC-004: Letta blocks from cache, not live API

Re-fetching Letta blocks on every prompt would add ~100ms per prompt and create unnecessary load on the Letta server. The session-start hook writes blocks to `tier2-cache.json` — this is stale only if Letta updates blocks mid-session (rare). Acceptable trade-off for latency.

### DEC-005: 200-char excerpt, not full block content

Domain block values can be up to 3K chars (after URI-FR-002 capping). Injecting 3K chars for a block match would consume the entire token budget. Truncating to 200 chars provides context for the agent to self-direct to the full block if needed.
