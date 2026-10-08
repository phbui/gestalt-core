#!/usr/bin/env python3
"""
Auto-promote knowledge from Letta blocks and promotion queue to gestalt KB entries.
Runs as a background process spawned by gestalt-session-start.sh when GESTALT_AUTO_CONSOLIDATE=true (default off).
It proposes. It never edits a knowledge entry (N11, 2026-10-06): proposals go to <state-dir>/promotion-proposals.md.
"""
import argparse, json, os, re, sys, time, subprocess
from pathlib import Path
from datetime import datetime


# =============================================================================
# 5.8 — Log helper
# =============================================================================

class Log:
    def __init__(self, path: Path):
        self.path = path
    def _write(self, level: str, msg: str):
        ts = datetime.now().isoformat(timespec='seconds')
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(f"{ts} {level}: [auto-promote] {msg}\n")
    def info(self, msg): self._write('INFO', msg)
    def warn(self, msg): self._write('WARN', msg)
    def error(self, msg): self._write('ERROR', msg)


# =============================================================================
# 5.3 — Letta Block Reader
# =============================================================================

def read_letta_facts(letta_url: str, state_dir: Path, log) -> list[dict]:
    """Read Letta blocks and extract fact-like statements."""
    import urllib.request

    agent_id_file = state_dir / 'letta-agent-id.txt'
    if not agent_id_file.exists():
        return []

    agent_id = agent_id_file.read_text(encoding="utf-8").strip()
    try:
        req = urllib.request.Request(f"{letta_url}/agents/{agent_id}", method='GET')
        with urllib.request.urlopen(req, timeout=5) as resp:
            agent = json.loads(resp.read())
    except Exception as e:
        log.warn(f"Letta unreachable: {e}")
        return []

    facts = []
    # Focus on blocks that contain project-specific knowledge
    # Personal knowledge base: never promote employer-owned memory blocks into
    # knowledge/ (see .claude/rules/gestalt.md Scope). Only generic blocks.
    target_labels = {'project_context', 'tool_guidelines'}

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


# =============================================================================
# 5.4 — Stability and Significance Checks
# =============================================================================

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


# =============================================================================
# 5.5 — Entry Update Logic
# =============================================================================

def find_target_entry(fact: dict, manifest: dict) -> str | None:
    """AKP-FR-022: Find existing entry for a fact, preferring update over creation."""
    text = fact['fact'].lower()

    # Slug matching only, and deliberately conservative. Tag matching used to live here
    # but is NOT safe to use: entry tags are single generic words (mcp, memory, cursor,
    # docker, voice), matched as bare substrings, so they misroute badly -- "invoice"
    # matches tag `voice`, and a fact naming <kb-entry> matches tag `mcp` on whichever
    # entry sorts first. The gestalt_search fallback below is score-thresholded and
    # strictly better at the ambiguous cases, so ambiguity defers to it rather than
    # guessing. (This branch never executed before 2026-08-10: read_manifest() parsed a
    # markdown-table format the generator never emitted, so it always returned {}.)
    for slug in manifest:
        for candidate in (slug, slug.replace('-', ' '), slug.replace('-', '_')):
            multiword = any(sep in candidate for sep in ('-', '_', ' '))
            if not (multiword or len(candidate) >= 7):
                continue  # too generic to match on alone
            # Word-boundary match so `game` does not fire inside `endgame`
            if re.search(rf'(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])', text):
                return slug

    # NO semantic fallback here, deliberately. gestalt_search returns a Reciprocal Rank
    # Fusion score, which encodes RANK, not similarity: the top result of any query is
    # ~1/(K+1) per leg it appears in, so with K=60 the ceiling is 2/61 = 0.0328 regardless
    # of whether the match is good. Measured 2026-08-10 against the live index:
    #
    #   "gestalt session start hook memory block cap"        -> gestalt        0.0328
    #   "We fixed an invoice rendering bug in the billing.."  -> <kb-entry>   0.0323
    #   "purple elephant tuba quarterly synchronized swim.."  -> research-project      0.0164
    #
    # An off-topic fact that merely shares common English words scores 0.0323 --
    # indistinguishable from a genuine hit. The score separates "some lexical overlap"
    # from "none at all" and nothing finer, so no threshold on it can express "relevant
    # enough to write". Earlier versions used 0.1 (above the ceiling, so the branch was
    # dead) and then 0.025 (below it, so it accepted the invoice fact and would have
    # appended it to <kb-entry>).
    #
    # This function feeds an UNATTENDED write into the curated source of truth, where a
    # wrong append is worse than no append. So promotion requires the fact to name its
    # target entry via the slug match above. Anything ambiguous returns None and stays in
    # the queue for a human or /consolidate. Re-introducing an automatic fallback needs a
    # real similarity signal (e.g. the vector leg's cosine distance), not an RRF score.

    return None


def update_entry(entry_path: Path, facts: list[dict], log) -> None:
    """AKP-FR-023: Write facts in authoritative voice."""
    content = entry_path.read_text(encoding="utf-8")

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
        # HTML comment, not a visible tag: keeps the entry reading in authoritative voice
        # while making provenance (letta_block:<label> vs transcript, audit 2026-09-08)
        # recoverable by grep instead of unrecoverable once appended.
        fact_lines.append(f"- {text} <!-- promoted:{f.get('source', 'unknown')} -->")

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

    entry_path.write_text(content, encoding="utf-8")
    log.info(f"Updated {entry_path.name}: +{len(facts)} facts")


# =============================================================================
# 5.6 — Graphiti Staleness Check
# =============================================================================

async def _mcp_handshake(client, url: str, client_name: str = "auto-promote") -> str:
    # Health check + MCP initialize + notifications/initialized against a Graphiti
    # MCP server. Returns the Mcp-Session-Id, or "" if unhealthy or no session granted.
    health = await client.get(f"{url}/health")
    if health.status_code != 200:
        return ""
    init_resp = await client.post(f"{url}/mcp",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": client_name}}})
    session_id = init_resp.headers.get("Mcp-Session-Id", "")
    if not session_id:
        return ""
    await client.post(f"{url}/mcp",
        headers={"Mcp-Session-Id": session_id, "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return session_id


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
                session_id = await _mcp_handshake(client, graphiti_url)
                if not session_id:
                    return 0

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
                    staleness_path.write_text(report, encoding="utf-8")
                    log.info(f"Wrote STALENESS_REPORT.md with {flagged} expired facts")

                return flagged

        return asyncio.run(_check())
    except Exception as e:
        log.warn(f"Graphiti staleness check failed: {e}")
        return 0


# =============================================================================
# 5.7 — Index Rebuild + Graphiti Feed + Letta Condensation
# =============================================================================

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

    content = entry_path.read_text(encoding="utf-8")
    try:
        import httpx, asyncio

        async def _feed():
            async with httpx.AsyncClient(timeout=10.0) as client:
                session_id = await _mcp_handshake(client, graphiti_url)
                if not session_id:
                    return

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

    agent_id = agent_id_file.read_text(encoding="utf-8").strip()
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


# =============================================================================
# 5.8 — Queue Management + Helpers
# =============================================================================

def read_queue(queue_path: Path, _log) -> list[dict]:
    if not queue_path.exists():
        return []
    try:
        with open(queue_path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def read_manifest(gestalt_dir: Path) -> dict:
    """Parse MANIFEST.md and MANIFEST-papers.md into a dict of slug -> {tags, title}.

    MANIFEST.md is heading-based, as emitted by tools/gestalt regenerate_manifest():

        ### <slug>
        <type> | <title>
        Links: [[a]] [[b]]
        Blocks: ^x,^y,

    Tags are deliberately NOT written into MANIFEST.md, so they are read from each
    entry's own frontmatter, which is the source of truth for them.
    """
    # Paper entries live in MANIFEST-papers.md (roadmap L2, 2026-10-06). Same format, so read both.
    paths = [p for p in (gestalt_dir / 'MANIFEST.md', gestalt_dir / 'MANIFEST-papers.md') if p.exists()]
    if not paths:
        return {}
    entries: dict[str, dict] = {}
    slug = None
    for line in '\n'.join(p.read_text(encoding="utf-8") for p in paths).split('\n'):
        stripped = line.strip()
        if stripped.startswith('### '):
            slug = stripped[4:].strip()
            if slug:
                entries[slug] = {'tags': [], 'title': slug}
            continue
        if not slug or slug not in entries or not stripped:
            continue
        if stripped.startswith(('Links:', 'Blocks:')):
            continue
        # First remaining line under the heading is "<type> | <title>"
        if ' | ' in stripped and entries[slug]['title'] == slug:
            entries[slug]['title'] = stripped.split(' | ', 1)[1].strip()
    for slug in entries:
        entries[slug]['tags'] = _frontmatter_tags(gestalt_dir / 'knowledge' / f'{slug}.md')
    return entries


def _frontmatter_tags(entry_path: Path) -> list[str]:
    """Read the `tags: [...]` list from an entry's YAML frontmatter."""
    if not entry_path.exists():
        return []
    try:
        for line in entry_path.read_text(encoding="utf-8").split('\n')[:20]:
            if line.startswith('tags:'):
                raw = line.split(':', 1)[1].strip().strip('[]')
                return [t.strip() for t in raw.split(',') if t.strip()]
    except OSError:
        return []
    return []


def clear_queue(queue_path: Path, promoted_facts: list[str], log) -> None:
    """Remove promoted facts from queue."""
    if not queue_path.exists():
        return
    try:
        with open(queue_path, encoding='utf-8') as f:
            queue = json.load(f)
        promoted_fps = {f[:80].lower() for f in promoted_facts}
        remaining = [e for e in queue if e['fact'][:80].lower() not in promoted_fps]
        with open(queue_path, 'w', encoding='utf-8') as f:
            json.dump(remaining, f, indent=2)
        log.info(f"Queue: {len(queue)} → {len(remaining)} after clearing promoted facts")
    except Exception:
        pass


# =============================================================================
# 5.2 — Core Promotion Logic
# =============================================================================

PROPOSALS_FILE = 'promotion-proposals.md'


def append_proposals(path: Path, proposals: list[tuple[str, dict]], log) -> int:
    """Append one dated line per new proposal. Returns how many were new.

    A fact already in the file is skipped, so the same Letta block does not re-propose every few sessions."""
    existing = path.read_text(encoding='utf-8') if path.exists() else ''
    lines = []
    for slug, f in proposals:
        fact = ' '.join(f['fact'].split())
        if fact in existing:
            continue
        lines.append(f"- {datetime.now().date().isoformat()} [proposal] target [[{slug}]] | confidence {f.get('confidence', 0)} "
                     f"| source {f.get('source', '?')} | {fact}")
    if lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            if not existing:
                fh.write("# Promotion proposals\n\nAppended by tools/auto-promote.py. Review each line, then apply it by hand or delete it.\n\n")
            fh.write('\n'.join(lines) + '\n')
        log.info(f"Wrote {len(lines)} promotion proposal(s) to {path.name}")
    return len(lines)


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
        target_slug = find_target_entry(fact, manifest)
        if target_slug:
            promoted_entries.setdefault(target_slug, []).append(fact)
        else:
            # No existing entry — skip for now (creating new entries
            # requires more context than a background agent has)
            stats['skipped'] += 1
            log.info(f"No target entry for: {fact['fact'][:80]}...")

    # N11 (2026-10-06): never edit a knowledge entry. Append dated proposals for a person to review.
    # No `updated:` bump, no index rebuild, no Graphiti feed, no Letta condense: those only follow a
    # reviewed promotion. The proposals live under the state directory, off the encrypted corpus.
    proposals = [(slug, f) for slug, facts in promoted_entries.items() for f in facts]
    if getattr(args, 'dry_run', False):
        for slug, f in proposals:
            log.info(f"[dry-run] would propose for {slug}: {f['fact'][:80]}")
        stats['promoted'] = len(proposals)
        return stats
    fresh = append_proposals(state_dir / PROPOSALS_FILE, proposals, log)
    stats['promoted'] = fresh

    # Step 6: Graphiti staleness (AKP-FR-030, AKP-FR-061). Read-only against Graphiti.
    stats['flagged'] = check_graphiti_staleness(args.graphiti_url, gestalt_dir, log)

    # Step 10: Clear queue entries that are now recorded as proposals (AKP-FR-011)
    clear_queue(queue_path, [f['fact'] for _, f in proposals], log)

    return stats


# =============================================================================
# 5.1 — Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Auto-promote knowledge from Letta blocks and promotion queue to gestalt KB entries.')
    parser.add_argument('--gestalt-dir', required=True,
                        help='Path to the gestalt directory')
    parser.add_argument('--state-dir', required=True,
                        help='Path to the state directory (contains health.log, promotion-queue.json, etc.)')
    parser.add_argument('--letta-url', default=os.environ.get('LETTA_URL', 'http://localhost:8283/v1'),
                        help='Letta API base URL (default: http://localhost:8283/v1)')
    parser.add_argument('--graphiti-url', default=os.environ.get('GRAPHITI_URL', 'http://localhost:8200'),
                        help='Graphiti MCP server URL (default: http://localhost:8200)')
    parser.add_argument('--min-sessions', type=int, default=2,
                        help='Minimum sessions a fact must appear in before promotion (default: 2)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Log what would be proposed and write nothing')
    parser.add_argument('--no-wait', action='store_true',
                        help='Skip the 15 s race guard (tests and manual runs)')
    args = parser.parse_args()

    log = Log(Path(args.state_dir) / 'health.log')
    log.info("Auto-promotion started")

    # Step 1: Race guard (AKP-FR-004)
    if not args.no_wait:
        time.sleep(15)

    try:
        result = run_promotion(args, log)
        log.info(f"Auto-promotion complete: {result['promoted']} promoted, "
                 f"{result['skipped']} skipped, {result['flagged']} flagged")
    except Exception as e:
        log.error(f"Auto-promotion crashed: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
