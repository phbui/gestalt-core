# Caveman Safeguard

When caveman mode (or any output compression skill) is active, these rules protect gestalt integrity.

## Gestalt Writes: Caveman Suspended

When writing to ANY file under `gestalt/` — knowledge entries, rules, indices, references — **suspend caveman compression** and use gestalt's standard writing register:

- Authoritative voice with complete technical fragments
- Preserve articles where needed for clarity ("consumes data from spatial" not "data spatial")
- Preserve Data Flow verb precision: "produces", "consumes", "feeds", "emits"
- Keep wikilink context sentences intact — the surrounding sentence explains *why* the link exists
- Keep frontmatter YAML untouched
- Keep `^block-id` section headings at full semantic length

Resume caveman for conversational output between writes.

## Classified Operations

**Caveman safe** (read-only, no gestalt writes):
`/investigate`, `/discuss`, `/research`, `/audit`, `/recall`, `/gestalt`, `/swarm-check`

**Caveman suspended for writes** (gestalt file mutations):
`/save`, `/learn`, `/review`, `/sync`, `/publish`, `/mutate`

**Mixed** (caveman OK for chat, suspended for any gestalt entry writes):
`/build`, `/implement`, `/fix`, `/refine`, `/commit`

## Never Compress Gestalt Files

Do not run `/caveman-compress` or any file-level compression tool on:
- `gestalt/knowledge/*.md`
- `claude-tree/rules/*.md` (+ `.cursor/rules/*.mdc` mirrors)
- `claude-tree/references/*.md` (+ `.cursor/references/*.md` mirrors)
- `gestalt/MANIFEST.md`, `GRAPH.md`, `SOURCES.md`, `BRANCHES.md` (created by /review's branch-tracking step)

## Subagent Propagation

When spawning subagents during gestalt operations, include the write safeguard in the agent prompt if the agent will write to gestalt files. Agents doing read-only work (scanners, verifiers) can inherit caveman mode freely.
