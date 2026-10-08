# Knowledge Write Conventions

Standard procedures for writing to gestalt. Referenced by `/save`, `/learn`, `/review`, `/sync`, and any command that creates or updates knowledge entries.

## Write Philosophy

- **Authoritative voice.** Entries read as if the correct answer was always known. Never frame entries as mistake logs.
- **Code is truth.** If gestalt says X and the code does Y, the code is right — update gestalt.
- **Rewrite, don't append.** When updating, rewrite the entire entry to integrate new knowledge seamlessly. Wrong approaches get a brief "Avoid" footnote at most.
- **Check before creating.** Read MANIFEST.md first. Update existing entries rather than duplicate.

## Knowledge Entry Frontmatter

```yaml
---
type: note | reference
title: "<descriptive title>"
repo: <repo-name>
tags: [<relevant tags>]
aliases: [<alternate names>]
created: YYYY-MM-DD
updated: YYYY-MM-DD
last-verified: YYYY-MM-DD
confidence: high | medium | low
---
```

## Required Sections and Block IDs

| Section | Block ID | Include When |
|---------|----------|-------------|
| What It Does | `^overview` | Always |
| Architecture | `^architecture` | Always |
| Setup | `^setup` | Repo entries |
| Run | `^run` | Repo entries |
| Test | `^test` | Repo entries |
| Deploy | `^deploy` | Repo entries |
| Conventions | `^conventions` | When notable |
| Data Flow | `^data-flow` | Pipelines, inter-service connections |
| Relationships | `^relationships` | Always |
| Service boundaries | `^{service}-input`, `^{service}-output` | When applicable |

Only include sections with real content. Skip empty sections.

## Linking Rules

- Use `wikilinks` inline for every cross-entry reference
- Place links next to the sentence that explains WHY they're related
- Every `## Data Flow` section uses structured format:
  - `function_name()` → output type `^{service}-output`
  - `other-entry` consumes {what}

## Size Limit

**500-line cap** per entry. If an entry would exceed 500 lines, split into focused sub-entries at write time (e.g., `platform.md` + `platform-deploy.md`). Each sub-entry gets its own frontmatter and `wikilinks` between them.

## Post-Write Sequence

Execute all three parts after creating or modifying any knowledge entry. All parts are mandatory.

### (a) Regenerate Indices

Regenerate all four indices by running `gestalt rebuild` (this regenerates MANIFEST.md, GRAPH.md, SOURCES.md, and BRANCHES.md). If the CLI is unavailable, regenerate each manually:

1. **MANIFEST.md** — all files, frontmatter, outgoing `links`, `^block-ids`
2. **GRAPH.md** — all `## Data Flow` sections, cross-entry connection index
3. **SOURCES.md** — external source references (update if new sources discovered)
4. **BRANCHES.md** — git branch tracking state (update if repo was scanned)

### (b) Rebuild Semantic Search Index

Run `tools/.venv/bin/python3 tools/gestalt-index-builder.py --force` to rebuild the semantic search SQLite database at `.search/gestalt.db`. Non-fatal — if it fails, log a warning and continue.

### (c) Feed New/Changed Entries to Graphiti

Run `tools/gestalt-graphiti-sync.sh <slug>` for each entry that was created or updated — one slug per invocation, no leading `knowledge/` or `.md`. This is the single hash-gated path from `knowledge/*.md` to Graphiti (F9, `the-relevant-entry.md` ^memory-triplication): it computes the file's sha1, compares it against `$STATE_DIR/graphiti-sync.json`, and only calls Graphiti's `add_memory` when the content actually changed, so re-running `/save` or any other writer never produces duplicate episodes. `tools/backfill-knowledge.sh` is the same script called with `--all` (every entry, ignoring the hash). If Graphiti is unreachable, the sync script logs a `WARN` to `health.log` and exits 0 — skip silently and move on.

## Skip Directories

When scanning repos, skip these generated/vendored directories — infer from lock/config files instead:

`node_modules/`, `.venv/`, `venv/`, `.env/`, `__pycache__/`, `.tox/`, `.mypy_cache/`, `.pytest_cache/`, `.next/`, `.nuxt/`, `dist/`, `build/`, `.turbo/`, `.cache/`, `.git/`, `.terraform/`, `vendor/`, `target/`, `*.egg-info/`
