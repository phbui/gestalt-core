---
name: sync
description: "Pull knowledge from external sources (Notion, Linear, Slack) via MCP tools and reconcile with existing gestalt entries. Spawns parallel syncer agents per source. Use to update gestalt with the latest external context."
disable-model-invocation: true
---

# Sync

Pull knowledge from external sources (Notion, Linear, Slack) and reconcile with existing gestalt entries. Updates entries with new information and refreshes the SOURCES.md registry.

## When to Run

- User invokes `/sync` explicitly
- Before a major project to ensure gestalt has the latest external context
- After team meetings or architecture decisions made outside the codebase

## Process

### Step 1: Git Pre-Flight (Mandatory)

Run the git pre-flight procedure from `gestalt/.cursor/references/orchestration.md` §Git Pre-Flight on each repo referenced in SOURCES.md or MANIFEST.md. Update BRANCHES.md with current state.

### Step 2: Read Source Registry

Read `gestalt/SOURCES.md`. This lists every gestalt entry's mapped external sources with last-sync timestamps. You MUST query ALL external sources listed in SOURCES.md. NEVER skip a source.

If SOURCES.md doesn't exist or is empty, scan MANIFEST.md and for each entry with a `repo:` field, infer external sources:
- Notion: search for docs mentioning the repo name
- Linear: search for projects/teams matching the repo name
- Slack: look for channels named after the repo (e.g., #spatial-dev)

### Step 3: Parallel External Scan

Spawn parallel syncer agents (see `.cursor/agents/syncer.md`). Each agent gets SOURCE type and the list of entry slugs with that source from SOURCES.md.

**Syncer template.** Each of the 3 agents below is a self-contained prompt — the orchestrator substitutes SOURCE and ENTRIES from the table into this template and dispatches all 3 as full, independent `Task()` calls in parallel (one message, 3 calls). Workers do not inherit this skill's instructions, so every substituted prompt must remain complete on its own:

```
Task(subagent_type="syncer", readonly=true, prompt="""
SOURCE: {SOURCE}
ENTRIES: {list of entry slugs with {SOURCE} references from SOURCES.md}

You MUST query ALL entries listed above. NEVER skip an entry. Document ALL findings per entry.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
- Entries queried: {N}/{N}. Updates found: {list}. No-change entries: {list}.
""")
```

| # | SOURCE |
|---|---|
| 1 | Notion |
| 2 | Linear |
| 3 | Slack |

### Step 4: Reconcile

Collect all syncer results. For each piece of new information:

1. **New context** — Information that enriches an existing entry but doesn't conflict. Queue for addition.
2. **Conflict** — External source says X, gestalt says Y. Flag for user review. Code is still truth for technical claims, but product/business context from Notion/Linear may supersede gestalt.
3. **Stale reference** — External source no longer exists. Flag for cleanup.
4. **New coverage** — External source covers a topic with no gestalt entry. Suggest running `/learn`.

### Step 5: Apply Updates

For each entry with new context:
- Rewrite entries following `gestalt/.cursor/references/knowledge-write.md` §Write Philosophy
- Add `wikilinks` for any new cross-references discovered
- Update `^block-ids` if new sections were added
- Include source attribution in the relevant section

For conflicts, present both versions to the user and wait for resolution.

### Step 6: Update SOURCES.md

Regenerate `gestalt/SOURCES.md` with:
- Updated last-sync timestamps
- New source mappings discovered during scan
- Removed stale references

### Step 7: Update Branch Tracking

Update `gestalt/BRANCHES.md` for every repo that was synced — record current commit hash and set last-synced timestamp.

### Step 8: Regenerate Indices

Execute the full Post-Write Sequence per `gestalt/.cursor/references/knowledge-write.md` §Post-Write Sequence (indices + semantic index + Graphiti). All parts are mandatory.

### Step 9: Report

Summarize with mandatory counts:
- **Sources queried: {N}/{N}.** ALL sources from SOURCES.md MUST be accounted for.
- **Updates found:** {list of entries with new information applied}
- **No-change sources:** {list of sources with no new information}
- **Conflicts flagged:** {list with both versions shown}
- **Stale references cleaned:** {list}
- **New coverage suggested:** {list of topics needing `/learn`}

COVERAGE: EVERY item in scope MUST be checked. Report: N/N checked.

## Verification Gate

Before completing this skill:
- Re-read all steps above. Confirm EVERY step was executed.
- Count: steps completed / total. If any skipped, state why.
- Verify all 3 source agents (Notion/Linear/Slack) returned results or explicit NO FINDINGS.
- Confirm updated entries were written and the full Post-Write Sequence ran.

## Evals — see EVALS.md (dev-time only)
