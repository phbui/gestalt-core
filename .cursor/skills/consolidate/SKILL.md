---
name: consolidate
description: "Memory-layer operations: consolidate (prune/merge/promote/cross-link across all four layers), status (health dashboard), recall (query the temporal graph for cross-session history), publish (condensed briefing for a shared Claude Project)."
disable-model-invocation: true
---

# Consolidate — Memory-Layer Operations

One skill, four modes over gestalt's memory layers. First argument selects the
mode; no argument (or a bare topic) means `run`.

| Mode | Was | Does |
|---|---|---|
| `run` (default) | /consolidate | Prune, merge, promote Letta learnings, cross-link |
| `status` | /gestalt | Health + content of all four layers at a glance |
| `recall <query>` | /recall | Query the temporal graph for evolving facts |
| `publish` | /publish | Condensed briefing for upload to a shared project |

## Mode: run (default)

Synthesize and connect knowledge across all four memory layers — gestalt's
equivalent of Claude Code's AutoDream. Runs manually or via the SessionStart
hook every 5 sessions (non-blocking).

### Step 1: Check Letta for Promotable Knowledge

Read the `<gestalt-memory>` block injected at SessionStart (per
memory-activation.md). If absent, Letta is offline — skip and note it in the
report. Look for facts in `project_context`, `pending_items`, or
`session_patterns` that MUST be permanent gestalt entries but aren't yet
(persistent architectural facts — not preferences, which stay in Letta). For
each: check MANIFEST.md for an existing entry; update it if the fact is
missing; create one only if the fact is architecturally significant and no
entry fits.

### Step 2: Check for Missing Cross-Links

Read MANIFEST.md. For each entry: unlinked references to repos/services that
have entries, entries describing the same data flow without mutual links,
incomplete `## Relationships` sections. Use `mcp__gestalt__gestalt_search` to
find semantically related but unlinked entries; if the server is down, fall
back to `grep -ri` across gestalt/knowledge/ and note the degraded mode.

### Step 3: Check for Stale Entries via Graphiti

1. `mcp__graphiti-memory__search_memory_facts(query="recently changed", group_ids=["gestalt"], max_facts=20)`
2. `mcp__graphiti-memory__search_memory_facts(query="superseded expired invalid", group_ids=["gestalt"], max_facts=10)`

For any fact with `expired_at` set, find the corresponding entry and flag it if
it still reflects the superseded information. If Graphiti is unreachable
(check `mcp__graphiti-memory__get_status()` first), skip and note it.

### Step 4: Rebuild if Changed

If entries changed in Steps 1-3: `gestalt rebuild`, rebuild the semantic
index, feed updated entries to Graphiti.

### Step 5: Report

```
## Consolidation Report
### Promoted from Letta → Gestalt
- {entry}: {what was added}
### New Cross-Links Added
- entry-a ↔ entry-b: {relationship}
### Stale Entries Flagged
- {entry}: {what changed, from Graphiti}
### Index Status
- MANIFEST: {N} entries | Search: {N} sections | Graphiti: {N} entities/{N} facts
```

## Mode: status

Show the health and content of all four memory layers.

### Step 1: Infrastructure Health

```bash
curl -sf http://localhost:8283/v1/health                                    # Letta
curl -sf http://localhost:8200/health                                       # Graphiti
docker inspect gestalt-falkordb --format='{{.State.Health.Status}}'         # FalkorDB
systemctl is-active gestalt-services                                        # systemd
```

### Step 2: Curated Layer

Entry count (`find gestalt/knowledge -maxdepth 1 -name '*.md' | wc -l`) and
entries modified in the last 7 days (`-mtime -7`).

### Step 3: Episodic Layer (Letta)

Prefer the `<gestalt-memory>` block if injected; only fall back to
`curl -sf "http://localhost:8283/v1/agents/$(cat ~/.claude/gestalt/letta-agent-id.txt)"`.
If the curl fails, report "Letta unreachable" and continue. For each of the 8
blocks show label, char count, 1-line preview; highlight empty vs populated.
Also: session summary count (`ls ~/.claude/memory/sessions/*.md | wc -l`) and
transcript count (`ls ~/.claude/memory/transcripts/*.jsonl | wc -l`).

### Step 4: Temporal Layer (Graphiti)

`get_status()`, then `search_nodes(query="gestalt", group_ids=["gestalt"],
max_nodes=20)` for entity count/top names, `search_memory_facts(query="recent
changes", ...)` for newest facts, `get_episodes(group_ids=["gestalt"],
max_episodes=5)`. If unreachable: "Graphiti: DOWN — run `sudo systemctl
restart gestalt-services`" and skip.

### Step 5: Semantic Layer

```bash
sqlite3 gestalt/.search/gestalt.db "SELECT COUNT(*) FROM sections_meta"
sqlite3 gestalt/.search/gestalt.db "SELECT COUNT(DISTINCT slug) FROM sections_meta"
stat -c '%Y' gestalt/.search/gestalt.db
```

### Step 6: Present Summary

One report: Infrastructure table (service/status/details), Curated ({N}
entries, recent list), Episodic (block table + summary/transcript counts),
Temporal ({N} entities, newest facts), Semantic ({N} sections from {M}
entries, index age/size), and a Hooks table built by listing
`gestalt/.cursor/hooks/*.sh` cross-referenced with
`gestalt/.cursor/settings.json` (columns: hook file, event, matcher, script
exists).

## Mode: recall

Query the temporal knowledge graph for cross-session history: "what did I
learn about X last week", "what changed about platform deploy", superseded
facts, knowledge evolution.

1. **Parse** the query from the remaining arguments.
2. **Availability**: `mcp__graphiti-memory__get_status()`. On failure, write
   `/tmp/restart-gestalt.sh` containing `sudo systemctl restart
   gestalt-services`, `chmod +x` it, tell the user to run it, and stop. Never
   attempt sudo directly.
3. **Query** both in parallel:
   `search_memory_facts(query=..., group_ids=["gestalt"], max_facts=15)` and
   `search_nodes(query=..., group_ids=["gestalt"], max_nodes=10)`.
4. **Present** in three sections — Current Facts (`expired_at` null: text,
   created_at, source episodes), Superseded Facts (strikethrough, superseding
   fact, when), Related Entities (name + summary). If both queries are empty,
   say so and suggest `/learn`.
5. **Cross-reference** with the curated layer via MANIFEST.md: note
   consistent/contradicting entries; flag Graphiti facts with no curated entry
   as `/save` candidates.
6. **Suggest actions**: sparse knowledge → `/learn <repo>` or `/review`;
   superseded facts touching curated entries → `/review`.

## Mode: publish

Generate a condensed briefing for upload to a shared Claude Project. One-way
push from gestalt outward; a snapshot, never auto-synced.

1. **Read all indices** per
   `gestalt/.cursor/references/gestalt-grounding.md` (MANIFEST, GRAPH,
   SOURCES).
2. **Generate** `gestalt/BRIEFING.md`:
   System Overview (one paragraph) → Repository Map (per MANIFEST entry:
   name, one-liner, key relationships) → Architecture & Data Flow (condensed
   GRAPH.md) → Key Conventions (rules + notable entry conventions) → Active
   Context (from SOURCES.md, time-sensitive items).
3. **Size check**: MUST stay under 50K tokens (~35K words) to leave
   conversation room in a ~200K project. If over: prioritize overview > data
   flow > repo summaries > conventions > active context; truncate per-repo
   detail to one-liners; link full entries for depth.
4. **Output**: file location, token/word estimate, upload instructions
   (manual step), what was included vs truncated. Re-run after significant
   gestalt updates.

## Verification Gate

Execute every step of the selected mode — none are optional.
Before finishing: confirm all steps completed and outputs match the required
formats.

## Evals — see EVALS.md (dev-time only)
