# Gestalt Grounding Procedure

Standard lookup procedure for commands and skills that need gestalt context before proceeding.

## Quick Lookup (Most Commands)

1. **Semantic discovery** — call `gestalt_search(query=<task description or key terms>)` to find relevant entries you might not know to look for by slug. Scan the top 5 results.
2. **Index lookup** — read `gestalt/MANIFEST.md` — find relevant entry slugs, their `links`, and `^block-ids`
3. **Read entries** — read each relevant `gestalt/knowledge/<slug>.md` entry (from both steps 1 and 2)

## Temporal Context (Investigation / Build / Audit Commands)

For commands that involve investigation, building, auditing, or multi-session work (`/investigate`, `/build`, `/audit`, `/discuss`, `/implement`, `/research`, `/fix`, `/swarm-check`), also query Graphiti for prior session knowledge:

1. Call `mcp__graphiti-memory__search_memory_facts(query=<task description>, group_ids=["gestalt"], max_facts=10)` — check what's already known from prior sessions
2. Call `mcp__graphiti-memory__search_nodes(query=<key entities>, group_ids=["gestalt"], max_nodes=5)` — find relevant entity nodes

If Graphiti is unreachable, skip silently — temporal context is supplementary.

## Cross-Service / Data Flow Questions

Also read `gestalt/GRAPH.md` when the task involves:
- Data flowing between services or repos
- Pipeline connections or dependencies
- Cross-repo impact assessment

## External Context Fallback

If gestalt knowledge is insufficient, read `gestalt/SOURCES.md` to find:
- Notion pages with architecture docs or product specs
- Linear projects/issues with prior work or decisions
- Slack channels with recent team discussions

Then query those sources via MCP tools.

## Branch-Aware Operations (Write Commands Only)

For commands that modify gestalt (`/save`, `/learn`, `/review`, `/sync`), also read `gestalt/BRANCHES.md` to check last-synced state per repo before proceeding.

## Linking Convention

When citing gestalt entries in responses, use:
- `slug` — link to `knowledge/slug.md`
- `slug` — link to specific section
- `slug` — link to heading
- "Per gestalt/knowledge/platform.md, the deploy command is..." — prose citation
