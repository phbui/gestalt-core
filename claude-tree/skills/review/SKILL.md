---
name: review
description: "Verify the gestalt knowledge base against the actual codebase using parallel verifier agents — check that your notes still match the code. Checks every factual claim, validates links and block IDs, finds gaps."
model: sonnet
user-invocable: true
argument-hint: "gestalt entries to review (or 'all')"
---

# Review

Audit the gestalt knowledge base against the actual codebase. Code is the source of truth — docs, notes, and even gestalt entries may be stale. Uses parallel verifier agents for throughput.

## Process

### Step 1: Inventory

Read `gestalt/MANIFEST.md` and `gestalt/BRANCHES.md`. List every rule and knowledge entry. Read the full content of each file. Count total entries and note their types.

### Step 2: Staleness Pre-Flight (Mandatory)

Spawn the staleness-detector agent to prioritize which entries need verification most urgently:

```
Task(subagent_type="staleness-detector", model="haiku", readonly=true, prompt="""
Scan all repos referenced by gestalt knowledge entries. Compare git history against BRANCHES.md last-synced commits. Return a staleness report ranked HIGH/MEDIUM/LOW/NONE.
""")
```

Use the staleness report to order verification — HIGH risk entries first.

**Additionally (Phase 2):** If the Graphiti MCP server is available (call `mcp__graphiti-memory__get_status()`), query `mcp__graphiti-memory__search_memory_facts(query="recently expired changed", group_ids=["gestalt"], max_facts=15)` for recently expired facts (facts where `expired_at` is set within the last 7 days). Merge these with the staleness-detector's findings — Graphiti may surface staleness that git-based detection misses (e.g., an external API changed but no code was committed yet).

### Step 3: Git Pre-Flight (Mandatory)

Run the git pre-flight procedure from `gestalt/.claude/references/orchestration.md` §Git Pre-Flight on EACH repo referenced by a knowledge entry.

Compare with BRANCHES.md last-synced commit. If a repo's main branch has advanced since the last sync, flag the entry as potentially stale.

### Step 4: Parallel Verification

Split entries into batches and spawn **parallel verifier agents**. Use `subagent_type="reviewer"` for Agents 1–3 (batch verification and link checking — inherits `disallowedTools: [Write, Edit, NotebookEdit]`). Use `subagent_type="explore"` for Agent 4 (gap finding — different task). See `.claude/agents/reviewer.md` for verifier specifications.

**Verifier template (batches 1–2).** Split entries into 3 roughly equal batches; this template is invoked twice — once for batch 1 (entries 1..N/3) and once for batch 2 (entries N/3+1..2N/3) — as two full, independent `Task()` calls dispatched in parallel alongside Agents 3–4. Each is a self-contained prompt (workers don't inherit this skill's instructions), so substitute ENTRIES per call and send the complete prompt below both times:

```
Task(subagent_type="reviewer", memory="project", readonly=true, prompt="""
CONTEXT: Auditing gestalt knowledge base against actual codebase. Code is truth.
ENTRIES: {list of entry slugs for this batch}

YOUR TASK:
1. Read EVERY entry assigned to you in full
2. For EACH entry, go to the referenced repo and verify ALL factual claims against current code
3. Document ALL discrepancies between gestalt entries and actual code state

RULES:
- You MUST verify EVERY entry assigned, not a sample
- EVERY claim checked MUST cite the code location that confirms or contradicts it
- Completeness marker: "Entries reviewed: {N}/{N}. Stale: {list}. Current: {list}."

RETURN:
- Per-entry verdict: CURRENT / STALE / PARTIAL with evidence
- Specific stale claims with code citations proving staleness
- Entries reviewed: {N}/{N}. Stale: {list}. Current: {list}.

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**Verifier Agent 3 — Link Checker:**
```
Task(subagent_type="reviewer", memory="project", readonly=true, prompt="""
CONTEXT: Auditing gestalt knowledge base link integrity.
ROLE: Link Checker
ENTRIES: {all entry slugs}

YOUR TASK:
1. Validate ALL wikilinks resolve to existing entries
2. Verify ALL ^block-ids referenced by other entries exist at their targets
3. Verify ALL GRAPH.md connections are valid and bidirectional

RULES:
- You MUST check EVERY link in EVERY entry, not a sample
- EVERY broken link MUST be reported with source file and line number
- Completeness marker: "Links checked: {N}/{N}. Broken: {list}. Valid: {list}."

RETURN:
- Broken wikilinks: {list with source:line}
- Orphan ^block-ids: {list with source:line}
- Invalid GRAPH.md connections: {list}
- Links checked: {N}/{N}. Broken: {list}. Valid: {list}.

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**Verifier Agent 4 — Gap Finder:**
```
Task(subagent_type="explore", model="haiku", readonly=true, prompt="""
CONTEXT: Auditing gestalt knowledge base for missing coverage.

YOUR TASK:
1. List ALL repos in workspace
2. Verify ALL repos against gestalt coverage — identify which have entries and which do not
3. Query external sources (Notion/Linear) for uncovered topics

RULES:
- You MUST check EVERY repo, not a sample
- EVERY uncovered repo MUST be reported
- Completeness marker: "Repos checked: {N}/{N}. Covered: {list}. Gaps: {list}."

RETURN:
- Repos with coverage: {list}
- Repos without coverage: {list}
- Topics found in external sources with no gestalt entry: {list}
- Repos checked: {N}/{N}. Covered: {list}. Gaps: {list}.

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

For EACH entry, verifiers check:
- **Rules:** Read the code the rule applies to. Does the rule still match the code? Are examples valid?
- **Knowledge (repo notes):** Go to the repo. Read Taskfile, pyproject.toml, entry points, test config. Do setup/run/test/deploy instructions match current state?
- **Knowledge (references):** Check that referenced files, functions, patterns still exist.

**Code is truth.** If a gestalt entry says "run with `task run`" but the Taskfile now uses `task dev`, the gestalt entry is wrong. Always trust the code over documentation.

Skip directories listed in `gestalt/.claude/references/orchestration.md` §Skip Directories.

### Step 5: Collect and Analyze Findings

Merge all verifier results. Verify ALL of the following categories — document ALL findings for each:

- **Stale entries** — knowledge that contradicts current code
- **Duplicates** — entries covering the same topic
- **Broken links** — `wikilinks` pointing to entries that don't exist
- **Orphan block IDs** — `^block-ids` referenced by other entries but no longer present
- **Promotion candidates** — knowledge entries that describe general conventions (should be rules)
- **Missing coverage** — repos or topics with no gestalt entry
- **Deprecated entries** — entries marked deprecated that could be removed
- **Cross-repo inconsistencies** — relationship descriptions that don't match code on both sides
- **Missing relationships** — repos that import from or deploy alongside each other but have no documented relationship
- **Unlinked entries** — entries that mention topics covered by other entries but don't use `wikilinks`
- **Missing data flow** — entries describing pipelines that lack a `## Data Flow` section
- **Missing block IDs** — key sections without `^block-id` anchors
- **Stale external sources** — SOURCES.md entries pointing to moved/deleted Notion pages or archived Linear projects

### Step 6: Build the Graph

Scan all `wikilinks` across all entries. For every pair of entries that reference each other's topics in body text but have no wikilink, propose adding one. The goal: any entry that references another entry's topic should be connected via `wikilinks` so agents and Obsidian can traverse the graph.

### Step 7: Split Oversized Entries (Mandatory)

Check file sizes against the 500-line cap in `gestalt/.claude/references/knowledge-write.md` §Size Limit.

For each oversized file:
1. Identify natural sections (by `##` headings, by service, by topic)
2. Propose a split plan
3. Get user approval
4. Create each sub-entry with its own frontmatter and `wikilinks` between them
5. Delete the original
6. Regenerate indices

### Step 8: Propose Changes

Present a structured list of proposed actions. For each, cite the specific code that proves the change is needed:

- **Update:** "knowledge/platform.md says `task run` but `Taskfile.yaml:12` now uses `task dev`"
- **Merge:** "knowledge/x.md and knowledge/y.md both cover S3 access — merge into one"
- **Promote:** "knowledge/uv-usage.md describes a general convention — create a rule"
- **Deprecate:** "knowledge/old-deploy.md references a pipeline that no longer exists"
- **Create:** "No gestalt entry for repo X — suggest running `/learn`"
- **Link:** "platform-deploy.md mentions Supabase but doesn't use platform-pwa-data wikilink"
- **Split:** "platform.md is 520 lines — split into platform.md + platform-deploy.md"
- **Add blocks:** "spatial.md architecture section has no ^block-id anchor"
- **Fix external:** "SOURCES.md references a deleted Notion page for platform"

**Wait for user approval before making changes.**

### Step 9: Execute Approved Changes

Apply each approved change. For updates, rewrite entries following `gestalt/.claude/references/knowledge-write.md` §Write Philosophy.

### Step 10: Update Branch Tracking

Update `gestalt/BRANCHES.md` for every repo that was verified — record current commit hash and set last-synced timestamp.

### Step 11: Regenerate Indices

Execute the full Post-Write Sequence per `gestalt/.claude/references/knowledge-write.md` §Post-Write Sequence (indices + semantic index + Graphiti). All parts are mandatory.

### Step 11.5: Feed Corrections to Temporal Graph

(Covered by Post-Write Sequence §c above.)

### Step 11.6: Rebuild Semantic Index

(Covered by Post-Write Sequence §b above.)

### Step 12: Report

Summarize: what was changed, what was split, what was left alone, link integrity stats, and overall health of the knowledge base.

## Verification Gate

Before completing this skill:
- Re-read all steps above. Confirm EVERY step was executed (not just planned).
- Count: steps completed / total. If any were skipped, state why explicitly.
- Verify all entries proposed for update/deprecation/split were presented to the user.
- Confirm the full Post-Write Sequence ran per gestalt/.claude/references/knowledge-write.md §Post-Write Sequence.

## Evals — see EVALS.md (dev-time only)
