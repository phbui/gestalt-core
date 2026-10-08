---
pitch_id: auto-knowledge-promotion
document: sdd
version: 1.0
created: 2026-04-09
---

# Software Design Description: Auto-Knowledge Promotion

## 1. Introduction

### 1.1 Purpose

This SDD describes how knowledge is automatically promoted from Letta blocks and Graphiti into the curated gestalt KB, closing the write gap identified in the investigation.

### 1.2 Scope

Three components:
1. **Promotion queue writer** — lightweight addition to `gestalt-stop.sh` (Task D)
2. **Auto-consolidation trigger** — replaces the nudge in `gestalt-session-start.sh` section 8
3. **Promotion agent script** — `gestalt/tools/auto-promote.py`, runs `/consolidate` Step 1 logic

## 2. Architecture

```
SESSION END (gestalt-stop.sh)
    |
    +-- Task A: Letta write (existing, unchanged)
    +-- Task B: Session summary (existing, unchanged)
    +-- Task C: Graphiti write (existing, unchanged)
    +-- [NEW] Task D: Promotion queue writer
            |
            +-- Scan transcript for promotable facts
            +-- Append to $STATE_DIR/promotion-queue.json

NEXT SESSION START (gestalt-session-start.sh, section 8)
    |
    +-- [CHANGED] If COUNT % INTERVAL == 0:
            |
            +-- Spawn: nohup python3 auto-promote.py &
            +-- (replaces echo nudge)

AUTO-PROMOTE.PY (background, ~30-60s)
    |
    +-- 1. Sleep 15s (race guard — wait for previous Stop hook to finish)
    +-- 2. Read Letta blocks via API
    +-- 3. Read promotion-queue.json
    +-- 4. Read MANIFEST.md to find existing entries
    +-- 5. For each promotable fact:
    |       - Stability check (2+ sessions)
    |       - Significance check (architectural, not transient)
    |       - Existing entry? Update it. No entry? Create one.
    +-- 6. Query Graphiti for expired facts → flag stale entries
    +-- 7. Rebuild indices (MANIFEST, GRAPH, search index)
    +-- 8. Feed updated entries to Graphiti
    +-- 9. Send condensation message to Letta
    +-- 10. Clear processed queue entries
    +-- 11. Log results to health.log
```

## 3. Component: Promotion Queue Writer (Stop Hook Task D)

**Satisfies:** AKP-FR-010, AKP-FR-011

Added to `gestalt-stop.sh` after Task C (Graphiti write). Must complete in <3s (AKP-CONST-002).

### 3.1 Implementation

Insert after the Graphiti write block (after Task C completes):

```bash
# --- Task D: Flag promotable facts for promotion queue ---
QUEUE_FILE="${GESTALT_PROMOTE_QUEUE_PATH:-$STATE_DIR/promotion-queue.json}"
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
```

**Key design choices:**
- Runs in background (`&`) — does not block the Stop hook's exit
- Pattern matching is regex-based, not LLM-based — keeps it under 3s
- Queue is append-only between promotion passes, capped at 100 entries
- Deduplication by fact text fingerprint (first 80 chars)

## 4. Component: Auto-Consolidation Trigger (SessionStart Section 8)

**Satisfies:** AKP-FR-001, AKP-FR-002, AKP-FR-003, AKP-FR-082

Replace the existing consolidation nudge in `gestalt-session-start.sh` section 8.

### 4.1 Current Code (to replace)

```bash
# --- 8. Consolidation nudge (every 5 sessions) ---
COUNTER_FILE="$STATE_DIR/session-counter"
COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNTER_FILE"
if [ $((COUNT % 5)) -eq 0 ] && [ "$COUNT" -gt 0 ]; then
    echo "<gestalt-consolidation-nudge>"
    echo "This is session #${COUNT}. Consider running /consolidate..."
    echo "</gestalt-consolidation-nudge>"
    echo "$(date -Iseconds) INFO: Consolidation nudge (session #$COUNT)" >> "$LOG"
fi
```

### 4.2 Replacement

```bash
# --- 8. Auto-consolidation (every N sessions) ---
COUNTER_FILE="$STATE_DIR/session-counter"
COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNTER_FILE"

AUTO_CONSOLIDATE="${GESTALT_AUTO_CONSOLIDATE:-true}"
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
                --graphiti-url "${GRAPHITI_URL:-http://localhost:8000}" \
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
```

## 5. Component: Auto-Promote Script

**Satisfies:** AKP-FR-004, AKP-FR-005, AKP-FR-012, AKP-FR-020–023, AKP-FR-030–032, AKP-FR-040–041, AKP-FR-050, AKP-FR-060–062

**Location:** `gestalt/tools/auto-promote.py`

### 5.1 Entry Point

```python
#!/usr/bin/env python3
"""
Auto-promote knowledge from Letta blocks and promotion queue to gestalt KB entries.
Runs as a background process spawned by gestalt-session-start.sh.
"""
import argparse, json, re, sys, time, subprocess, os
from pathlib import Path
from datetime import datetime

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gestalt-dir', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--letta-url', default='http://localhost:8283/v1')
    parser.add_argument('--graphiti-url', default='http://localhost:8000')
    parser.add_argument('--min-sessions', type=int, default=2)
    args = parser.parse_args()

    log = Log(Path(args.state_dir) / 'health.log')
    log.info("Auto-promotion started")

    # Step 1: Race guard (AKP-FR-004)
    time.sleep(15)

    try:
        result = run_promotion(args, log)
        log.info(f"Auto-promotion complete: {result['promoted']} promoted, "
                 f"{result['skipped']} skipped, {result['flagged']} flagged")
    except Exception as e:
        log.error(f"Auto-promotion crashed: {e}")
        sys.exit(1)
```

### 5.2 Core Promotion Logic

```python
def run_promotion(args, log) -> dict:
    gestalt_dir = Path(args.gestalt_dir)
    state_dir = Path(args.state_dir)
    stats = {'promoted': 0, 'skipped': 0, 'flagged': 0, 'errors': 0}

    # Step 2: Read Letta blocks (AKP-FR-060 graceful degradation)
    letta_facts = read_letta_facts(args.letta_url, state_dir, log)

    # Step 3: Read promotion queue (AKP-FR-012)
    queue_path = state_dir / 'promotion-queue.json'
    queue_facts = read_queue(queue_path, log)

    # Merge candidates
    candidates = letta_facts + queue_facts
    if not candidates:
        log.info("No promotable facts found")
        return stats

    # Step 4: Read MANIFEST to find existing entries
    manifest = read_manifest(gestalt_dir)

    # Step 5: Evaluate each candidate
    promoted_entries = {}  # slug -> list of facts to add
    for fact in candidates:
        # Stability check (AKP-FR-020)
        if not is_stable(fact, queue_facts, args.min_sessions):
            stats['skipped'] += 1
            continue

        # Significance check (AKP-FR-021)
        if not is_significant(fact):
            stats['skipped'] += 1
            continue

        # Find target entry (AKP-FR-022)
        target_slug = find_target_entry(fact, manifest, gestalt_dir)
        if target_slug:
            promoted_entries.setdefault(target_slug, []).append(fact)
        else:
            # No existing entry — skip for now (creating new entries
            # requires more context than a background agent has)
            stats['skipped'] += 1
            log.info(f"No target entry for: {fact['fact'][:80]}...")

    # Write promoted facts to entries
    for slug, facts in promoted_entries.items():
        entry_path = gestalt_dir / 'knowledge' / f'{slug}.md'
        if not entry_path.exists():
            stats['errors'] += 1
            continue
        try:
            update_entry(entry_path, facts, log)
            stats['promoted'] += len(facts)
        except Exception as e:
            log.error(f"Failed to update {slug}: {e}")
            stats['errors'] += 1

    # Step 6: Graphiti staleness (AKP-FR-030, AKP-FR-061)
    flagged = check_graphiti_staleness(args.graphiti_url, gestalt_dir, log)
    stats['flagged'] = flagged

    # Step 7: Rebuild indices (AKP-FR-040)
    if stats['promoted'] > 0:
        rebuild_indices(gestalt_dir, log)

        # Step 8: Feed Graphiti (AKP-FR-041)
        for slug in promoted_entries:
            feed_graphiti(slug, gestalt_dir, args.graphiti_url, log)

        # Step 9: Condense Letta blocks (AKP-FR-050)
        condense_letta(promoted_entries, args.letta_url, state_dir, log)

    # Step 10: Clear processed queue entries (AKP-FR-011)
    clear_queue(queue_path, [f['fact'] for facts in promoted_entries.values()
                             for f in facts], log)

    return stats
```

### 5.3 Letta Block Reader

```python
def read_letta_facts(letta_url: str, state_dir: Path, log) -> list[dict]:
    """Read Letta blocks and extract fact-like statements."""
    import urllib.request

    agent_id_file = state_dir / 'letta-agent-id.txt'
    if not agent_id_file.exists():
        return []

    agent_id = agent_id_file.read_text().strip()
    try:
        req = urllib.request.Request(f"{letta_url}/agents/{agent_id}", method='GET')
        with urllib.request.urlopen(req, timeout=5) as resp:
            agent = json.loads(resp.read())
    except Exception as e:
        log.warn(f"Letta unreachable: {e}")
        return []

    facts = []
    # Focus on blocks that contain project-specific knowledge
    target_labels = {'project_context', 'tool_guidelines',
                     'service_api_notes', 'platform_arch_notes', 'misc_repos_notes'}

    for block in agent.get('memory', {}).get('blocks', []):
        label = block.get('label', '')
        if label not in target_labels:
            continue
        value = block.get('value', '').strip()
        if not value:
            continue

        # Split into sentences/statements
        statements = re.split(r'(?<=[.!?\n])\s+', value)
        for stmt in statements:
            stmt = stmt.strip()
            if len(stmt) > 30 and is_factual_statement(stmt):
                facts.append({
                    'fact': stmt,
                    'source': f'letta_block:{label}',
                    'confidence': 0.6,
                    'session_id': 'letta',
                })
    return facts


def is_factual_statement(text: str) -> bool:
    """Check if a statement contains architectural/factual content."""
    factual_indicators = [
        r'(?:uses?|deploys?|runs?|connects?|stores?|serves?)\s',
        r'(?:endpoint|api|service|database|table|schema)',
        r'(?:port|host|url|path|directory|config)',
        r'(?:kubernetes|docker|argocd|helm|nats|redis|postgres)',
        r'(?:must|shall|always|never|requires?)',
    ]
    return any(re.search(p, text.lower()) for p in factual_indicators)
```

### 5.4 Stability and Significance Checks

```python
def is_stable(fact: dict, queue_facts: list[dict], min_sessions: int) -> bool:
    """AKP-FR-020: Fact must appear in 2+ sessions."""
    # Letta block facts are inherently multi-session (Letta persists across sessions)
    if fact.get('source', '').startswith('letta_block:'):
        return True

    # Queue facts: count distinct session_ids
    fingerprint = fact['fact'][:80].lower()
    session_ids = {f['session_id'] for f in queue_facts
                   if f['fact'][:80].lower() == fingerprint}
    return len(session_ids) >= min_sessions


def is_significant(fact: dict) -> bool:
    """AKP-FR-021: Only architecturally significant facts."""
    text = fact['fact'].lower()

    # Exclude transient state
    transient = [
        r'currently (?:debugging|investigating|working on|looking at)',
        r'(?:todo|fixme|hack|workaround)',
        r'(?:trying|let me|i think|maybe|perhaps)',
        r'(?:this session|right now|at the moment)',
    ]
    if any(re.search(p, text) for p in transient):
        return False

    # Must match architectural patterns
    significant = [
        r'(?:deploys?|deployment|infrastructure)',
        r'(?:database|schema|migration|model)',
        r'(?:api|endpoint|interface|contract)',
        r'(?:service|pipeline|workflow|queue)',
        r'(?:configuration|environment|secret)',
        r'(?:convention|pattern|standard|rule)',
        r'(?:depends? on|requires?|connects? to)',
    ]
    return any(re.search(p, text) for p in significant)
```

### 5.5 Entry Update Logic

```python
def find_target_entry(fact: dict, manifest: dict, gestalt_dir: Path) -> str | None:
    """AKP-FR-022: Find existing entry for a fact, preferring update over creation."""
    text = fact['fact'].lower()

    # Try to match against known entry slugs
    for slug, info in manifest.items():
        # Check if the fact mentions the entry's repo or title
        if slug.replace('-', ' ') in text or slug.replace('-', '_') in text:
            return slug

        # Check tags
        for tag in info.get('tags', []):
            if tag.lower() in text:
                return slug

    # Fall back to gestalt_search if available
    try:
        tools_dir = str(gestalt_dir / 'tools')
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from gestalt_mcp_server import gestalt_search
        results = gestalt_search(fact['fact'][:200], limit=1)
        if results and results[0].get('score', 0) > 0.1:
            return results[0]['slug']
    except Exception:
        pass

    return None


def update_entry(entry_path: Path, facts: list[dict], log) -> None:
    """AKP-FR-023: Write facts in authoritative voice."""
    content = entry_path.read_text()

    # Find the best section to append to (prefer ## Architecture or ## Data Flow)
    append_section = None
    for section in ['## Architecture', '## Data Flow', '## Relationships']:
        if section in content:
            append_section = section
            break

    if not append_section:
        # Append before the last section
        append_section = '## Relationships'

    # Format facts in authoritative voice
    fact_lines = []
    for f in facts:
        # Strip investigation framing (AKP-FR-023)
        text = f['fact']
        text = re.sub(r'^(?:we (?:discovered|found|realized) (?:that )?)', '', text, flags=re.I)
        text = re.sub(r'^(?:it turns out (?:that )?)', '', text, flags=re.I)
        text = re.sub(r'^(?:after investigating,? )', '', text, flags=re.I)
        text = text[0].upper() + text[1:] if text else text
        fact_lines.append(f"- {text}")

    if not fact_lines:
        return

    # Insert before the target section's next section
    insertion = "\n### Auto-Promoted Facts\n\n" + "\n".join(fact_lines) + "\n"

    # Find insertion point: after the target section header, before the next ## header
    pattern = rf'({re.escape(append_section)}.*?)(\n## |\Z)'
    match = re.search(pattern, content, re.DOTALL)
    if match:
        insert_pos = match.end(1)
        content = content[:insert_pos] + "\n" + insertion + content[insert_pos:]
    else:
        content += "\n" + insertion

    # Update frontmatter timestamp
    content = re.sub(
        r'(updated:\s*)[\d-]+',
        f'\\g<1>{datetime.now().strftime("%Y-%m-%d")}',
        content
    )

    entry_path.write_text(content)
    log.info(f"Updated {entry_path.name}: +{len(facts)} facts")
```

### 5.6 Graphiti Staleness Check

```python
def check_graphiti_staleness(graphiti_url: str, gestalt_dir: Path, log) -> int:
    """AKP-FR-030, AKP-FR-031: Check for expired facts in Graphiti."""
    try:
        import httpx
    except ImportError:
        log.warn("httpx not available — skipping Graphiti staleness check")
        return 0

    try:
        import asyncio

        async def _check():
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Health check
                health = await client.get(f"{graphiti_url}/health")
                if health.status_code != 200:
                    return 0

                # MCP init
                init_resp = await client.post(f"{graphiti_url}/mcp",
                    headers={"Content-Type": "application/json",
                             "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "auto-promote"}}})
                session_id = init_resp.headers.get("Mcp-Session-Id", "")
                if not session_id:
                    return 0

                await client.post(f"{graphiti_url}/mcp",
                    headers={"Mcp-Session-Id": session_id,
                             "Content-Type": "application/json"},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})

                # Search for expired facts
                resp = await client.post(f"{graphiti_url}/mcp",
                    headers={"Mcp-Session-Id": session_id,
                             "Content-Type": "application/json"},
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "search_memory_facts",
                                     "arguments": {"query": "recently expired superseded invalid",
                                                    "max_facts": 20,
                                                    "group_id": "gestalt"}}})
                data = resp.json()
                content = data.get("result", {}).get("content", [])

                flagged = 0
                staleness_path = gestalt_dir / 'STALENESS_REPORT.md'
                report_lines = []

                for item in content:
                    text = item.get("text", "")
                    if "expired" in text.lower() or "superseded" in text.lower():
                        report_lines.append(f"- {text[:300]}")
                        flagged += 1

                if report_lines:
                    report = f"# Staleness Report\n\nGenerated: {datetime.now().isoformat()}\n\n"
                    report += "## Expired/Superseded Facts from Graphiti\n\n"
                    report += "\n".join(report_lines) + "\n"
                    staleness_path.write_text(report)
                    log.info(f"Wrote STALENESS_REPORT.md with {flagged} expired facts")

                return flagged

        return asyncio.run(_check())
    except Exception as e:
        log.warn(f"Graphiti staleness check failed: {e}")
        return 0
```

### 5.7 Index Rebuild + Graphiti Feed + Letta Condensation

```python
def rebuild_indices(gestalt_dir: Path, log) -> None:
    """AKP-FR-040: Regenerate MANIFEST, GRAPH, search index."""
    gestalt_cli = gestalt_dir / 'tools' / 'gestalt'
    if gestalt_cli.exists():
        subprocess.run(['bash', str(gestalt_cli), 'rebuild'],
                      capture_output=True, timeout=30)
        log.info("Indices rebuilt (gestalt rebuild)")

    index_builder = gestalt_dir / 'tools' / 'gestalt-index-builder.py'
    if index_builder.exists():
        subprocess.run(['python3', str(index_builder), '--force'],
                      capture_output=True, timeout=60)
        log.info("Search index rebuilt")


def feed_graphiti(slug: str, gestalt_dir: Path, graphiti_url: str, log) -> None:
    """AKP-FR-041: Send updated entry to Graphiti."""
    entry_path = gestalt_dir / 'knowledge' / f'{slug}.md'
    if not entry_path.exists():
        return

    content = entry_path.read_text()
    try:
        import httpx, asyncio

        async def _feed():
            async with httpx.AsyncClient(timeout=10.0) as client:
                health = await client.get(f"{graphiti_url}/health")
                if health.status_code != 200:
                    return

                init_resp = await client.post(f"{graphiti_url}/mcp",
                    headers={"Content-Type": "application/json",
                             "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "auto-promote"}}})
                session_id = init_resp.headers.get("Mcp-Session-Id", "")
                if not session_id:
                    return

                await client.post(f"{graphiti_url}/mcp",
                    headers={"Mcp-Session-Id": session_id,
                             "Content-Type": "application/json"},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})

                await client.post(f"{graphiti_url}/mcp",
                    headers={"Mcp-Session-Id": session_id,
                             "Content-Type": "application/json"},
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "add_memory",
                                     "arguments": {
                                         "name": f"promote-{slug}-{datetime.now().strftime('%Y%m%d')}",
                                         "episode_body": content[:5000],
                                         "group_id": "gestalt",
                                         "source": "text"}}})
                log.info(f"Fed {slug} to Graphiti")

        asyncio.run(_feed())
    except Exception as e:
        log.warn(f"Graphiti feed failed for {slug}: {e}")


def condense_letta(promoted: dict, letta_url: str, state_dir: Path, log) -> None:
    """AKP-FR-050: Tell Letta to condense promoted facts."""
    agent_id_file = state_dir / 'letta-agent-id.txt'
    if not agent_id_file.exists():
        return

    agent_id = agent_id_file.read_text().strip()
    promoted_summary = []
    for slug, facts in promoted.items():
        for f in facts:
            promoted_summary.append(f"- '{f['fact'][:100]}' → knowledge/{slug}.md")

    if not promoted_summary:
        return

    message = (
        "The following facts have been promoted to gestalt knowledge entries. "
        "Please condense or remove them from your memory blocks to free space:\n\n"
        + "\n".join(promoted_summary[:20])
    )

    try:
        import urllib.request
        payload = json.dumps({"messages": [{"role": "user", "content": message}]}).encode()
        req = urllib.request.Request(
            f"{letta_url}/agents/{agent_id}/messages",
            data=payload,
            headers={"Content-Type": "application/json"},
            method='POST'
        )
        urllib.request.urlopen(req, timeout=15)
        log.info("Letta condensation message sent")
    except Exception as e:
        log.warn(f"Letta condensation failed: {e}")
```

### 5.8 Queue Management + Helpers

```python
def read_queue(queue_path: Path, log) -> list[dict]:
    if not queue_path.exists():
        return []
    try:
        with open(queue_path) as f:
            return json.load(f)
    except Exception:
        return []


def read_manifest(gestalt_dir: Path) -> dict:
    """Parse MANIFEST.md into a dict of slug -> {tags, title}."""
    manifest_path = gestalt_dir / 'MANIFEST.md'
    if not manifest_path.exists():
        return {}
    content = manifest_path.read_text()
    entries = {}
    for line in content.split('\n'):
        # Match: | [slug](knowledge/slug.md) | tags | description |
        match = re.match(r'\|\s*\[([^\]]+)\]', line)
        if match:
            slug = match.group(1)
            entries[slug] = {'tags': [], 'title': slug}
            # Try to extract tags
            parts = line.split('|')
            if len(parts) > 3:
                tags_str = parts[2].strip()
                entries[slug]['tags'] = [t.strip() for t in tags_str.split(',') if t.strip()]
    return entries


def clear_queue(queue_path: Path, promoted_facts: list[str], log) -> None:
    """Remove promoted facts from queue."""
    if not queue_path.exists():
        return
    try:
        with open(queue_path) as f:
            queue = json.load(f)
        promoted_fps = {f[:80].lower() for f in promoted_facts}
        remaining = [e for e in queue if e['fact'][:80].lower() not in promoted_fps]
        with open(queue_path, 'w') as f:
            json.dump(remaining, f, indent=2)
        log.info(f"Queue: {len(queue)} → {len(remaining)} after clearing promoted facts")
    except Exception:
        pass


class Log:
    def __init__(self, path: Path):
        self.path = path
    def _write(self, level: str, msg: str):
        ts = datetime.now().isoformat(timespec='seconds')
        with open(self.path, 'a') as f:
            f.write(f"{ts} {level}: [auto-promote] {msg}\n")
    def info(self, msg): self._write('INFO', msg)
    def warn(self, msg): self._write('WARN', msg)
    def error(self, msg): self._write('ERROR', msg)


if __name__ == '__main__':
    main()
```

## 6. Configuration

**Satisfies:** AKP-FR-070

New `.env` variables (all optional, with defaults):

```bash
# Auto-promotion
GESTALT_AUTO_CONSOLIDATE=true          # Enable/disable
GESTALT_CONSOLIDATE_INTERVAL=5          # Sessions between passes
GESTALT_PROMOTE_MIN_SESSIONS=2          # Stability threshold
GESTALT_PROMOTE_QUEUE_PATH=             # Defaults to $STATE_DIR/promotion-queue.json
```

## 7. Graceful Degradation

| Failure | Behavior | Req |
|---------|----------|-----|
| Letta down | Skip Letta block scan, use queue only | AKP-FR-060 |
| Graphiti down | Skip staleness check + feed, promote from Letta+queue | AKP-FR-061 |
| Agent crash | No partial changes (entry writes are atomic per-file) | AKP-FR-062 |
| `auto-promote.py` missing | Log warning, skip | Defensive |
| Empty queue + no Letta facts | Log "No promotable facts", exit clean | AKP-FR-081 |

## 8. Decisions

### DEC-001: Regex-based transcript scanning, not LLM-based

The promotion queue writer (Task D) uses regex patterns to detect promotable facts. An LLM call would be more accurate but would add 5-10s latency (Sonnet inference) to the Stop hook, exceeding the 3s budget. Regex is fast (~50ms), runs in background, and catches the most common patterns (corrections, architectural statements). False negatives are acceptable — the Letta block scan catches what regex misses.

### DEC-002: Append "Auto-Promoted Facts" section, not inline integration

Auto-promoted facts are appended under a `### Auto-Promoted Facts` subsection rather than integrated inline. This makes promotions auditable (`grep "Auto-Promoted"`) and reversible. A human or `/review` pass can later integrate them into the entry's natural structure.

### DEC-003: No new entry creation, only updates

The background agent only updates existing entries. Creating new entries requires too much judgment (choosing a slug, writing frontmatter, structuring sections) for an unsupervised background process. If a fact has no target entry, it stays in the queue until a human runs `/save` or `/learn`.

## 9. Progress Tracking

| Requirement | Status | Notes |
|-------------|--------|-------|
| AKP-FR-001 | ⏳ Not started | |
| AKP-FR-002 | ⏳ Not started | |
| AKP-FR-003 | ⏳ Not started | |
| AKP-FR-004 | ⏳ Not started | 15s sleep |
| AKP-FR-005 | ⏳ Not started | Log class |
| AKP-FR-010 | ⏳ Not started | Task D in stop hook |
| AKP-FR-011 | ⏳ Not started | Queue JSON format |
| AKP-FR-012 | ⏳ Not started | |
| AKP-FR-020 | ⏳ Not started | Stability filter |
| AKP-FR-021 | ⏳ Not started | Significance filter |
| AKP-FR-022 | ⏳ Not started | Update over create |
| AKP-FR-023 | ⏳ Not started | Authoritative voice |
| AKP-FR-030 | ⏳ Not started | Graphiti staleness |
| AKP-FR-031 | ⏳ Not started | |
| AKP-FR-040 | ⏳ Not started | Index rebuild |
| AKP-FR-041 | ⏳ Not started | Graphiti feed |
| AKP-FR-050 | ⏳ Not started | Letta condensation |
| AKP-FR-060-062 | ⏳ Not started | Degradation |
| AKP-FR-070 | ⏳ Not started | .env config |
| AKP-FR-080-082 | ⏳ Not started | Flows |
