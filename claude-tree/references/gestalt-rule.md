# Gestalt rule — full text

This is the complete text of `rules/gestalt.md` as it stood before the 2026-10-17 stub cut. The rule keeps the lookup order, the link syntax, the personal-scope rule and the write duties; this file keeps the longer explanations, the MANIFEST and GRAPH detail, the linking rules and the agent-team note. `gestalt-grounding.md` has the per-command lookup procedure.

---

# Gestalt

`gestalt/` is a knowledge base at the workspace root. Learned conventions and reference knowledge that persist across sessions. Agent-agnostic — works with both Cursor and Claude Code.

## Always Reference Gestalt

Before answering, suggesting an approach, or starting a task, search gestalt (see Lookup) and read the entries it points at. Cite what you use: "Per gestalt/knowledge/platform.md, the deploy command is…". This applies to ALL tasks, not only when gestalt is mentioned.

## Lookup

Search first. Never read an index whole: `MANIFEST.md` is about 200 KB.

1. **Search** — `gestalt_search` with the question or topic finds the entry. `gestalt_route` finds the skill. Then read the one entry: `gestalt/knowledge/<slug>.md` or `gestalt/.claude/rules/<slug>.md`.
2. **MANIFEST.md** — slug-level index, for a slug's outgoing `links` and key `^block-ids`. Paper notes (`paper-*`) are indexed in `gestalt/MANIFEST-papers.md`, in the same format. Look up one slug with `gestalt_manifest('<slug>')`, which searches both files, or `grep -A4 '^### <slug>' gestalt/MANIFEST.md gestalt/MANIFEST-papers.md`. `gestalt_manifest('<few words>')` returns the matching entries, and with no argument it returns a short summary. Never read either file whole.
3. **GRAPH.md** — data-flow index. Read when the question is cross-cutting: "what feeds into X?", "trace data from A to B", "how do these services connect?". **One extra read.**
4. **SOURCES.md** — external source registry (Notion, Linear, Slack per entry). Read when gestalt lacks context, then query those via MCP.

### Resolving Links

- **Knowledge→rules exception (2026-08-30):** a `slug` with no `knowledge/<slug>.md` may reference a rule (`.claude/rules/<slug>.md`), e.g. `hub-invariants`, `[[no-silent-deferral]]`. Resolve on the rules tree before calling a link broken.

- `slug` → read `gestalt/knowledge/slug.md`
- `slug` → read `gestalt/knowledge/slug.md`, find the line containing `^block-id`
- `slug` → read `gestalt/knowledge/slug.md`, find the `## heading` section

Entries are per-repo (`knowledge/platform.md`) or cross-repo (`knowledge/deployment-pipeline.md`); check for cross-repo entries when a task spans repos.

## Scope: Personal Knowledge Base

`phbui/gestalt` is **personal**. Never write an entry — and never let auto-capture create one — for a repo or system owned by an employer or client, even when the workspace contains it and the entry would be useful: no internal repos, deploy topologies, API contracts, customer data models, access recipes, or incident writeups. Skip silently and say once: "Skipping gestalt capture — work-owned repo."

**Generic techniques** learned at work are fine (a library gotcha, a debugging pattern, a Claude Code behavior) provided they carry no internal hostnames, identifiers, credentials, or architecture. A work-system entry found in `knowledge/` gets removed, not redacted.

## Auto-Capture

Subject to the scope rule above. When you read a **personal** repo's README, Taskfile, or pyproject.toml and no gestalt entry exists for that repo, create one at `gestalt/knowledge/<repo>.md`. If one exists and is stale, rewrite it. Then regenerate indices. Confirm briefly: "Updated gestalt."

When the user corrects a mistake, update gestalt proactively. Confirm briefly: "Updated gestalt." Don't ask — just do it.

**Write philosophy:** see `gestalt-operations.md` §Write Philosophy — entries read as authoritative reference, never as mistake logs.

### Linking Rules

- Use inline `wikilinks` (not frontmatter `relates_to`), placed next to the sentence that explains *why* they relate
- Add `^block-id` anchors to key facts, service boundaries, data connection points
- Include `## Data Flow` sections for pipelines and inter-service connections

Before creating a new entry, check MANIFEST.md. Update existing entries rather than duplicate.

After any write, regenerate all indices per `gestalt-operations.md` §Index Regeneration.

## Agent Teams

Write-heavy operations (`/learn`, `/review`, `/sync`) fan out to parallel scanner agents with focused scopes, then you synthesize. Reads stay sequential (2-3 index reads). Ceiling and worker-prompt structure: `proactive-parallelism.md`, `references/orchestration.md`. Agent definitions: `claude-tree/agents/` (via the `gestalt/.claude` compat symlink, F10), `.cursor/agents/`.

## Gestalt Commands: Execution Rules

See `gestalt-operations.md` §Command Execution Rules — every step is mandatory, no spot checks.

## Sync Rule

`gestalt/.cursor/` is generated from `gestalt/claude-tree/` (reachable by that name or via the `gestalt/.claude` compat symlink, F10). Edit `claude-tree/` only. Then run `tools/sync-cursor-tree.py --write`, and never edit `.cursor/` by hand. Details: `dual-agent-sync.md`.

## Auto-Save

When context nears its limit, run `/save` proactively before the chat is summarized. Don't wait to be asked.

## Commands

Canonical command and skill list: `references/skill-index.md`.
