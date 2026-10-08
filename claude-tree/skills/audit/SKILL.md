---
name: audit
description: "Final quality gate for pitch implementations. Orchestrator that spawns specialist reviewers (Architecture, Code Quality, Performance, Requirements) then implements fixes in ordered batches. Use after /implement or /build is complete."
model: sonnet
user-invocable: true
argument-hint: "repo or directory to audit"
---

# Audit

Final quality gate for a pitch implementation. You are an **orchestrator agent** that reviews every change against architecture, engineering best practices, DRY, maintainability, and performance — then implements all fixes.

## Purpose

Run this after `/implement` or `/build` is complete. This is the last pass before a pitch's code is considered done. Unlike `/review` (which audits gestalt itself), this command audits the **code and design artifacts** produced for a specific pitch.

## Architecture

This command uses the **planner-worker hierarchy** (see `gestalt/.claude/references/orchestration.md`). You own all triage, prioritization, and fix-ordering decisions — never delegate these to workers.

```
You (Planner/Orchestrator)
├── Phase 1: Context Gathering (parallel)
│   ├── Scout Agent (explore) — git diff, file inventory, dependency graph
│   └── Orchestrator — reads SRS, SDD, ADRs, gestalt (simultaneous)
├── Phase 2: Deep Review (4 parallel specialist reviewers)
│   ├── Architecture & Integration Reviewer
│   ├── Code Quality & DRY Reviewer
│   ├── Performance & Optimization Reviewer
│   └── Requirements & Completeness Reviewer
├── Phase 3: Triage (orchestrator ONLY — no agents)
│   └── Dedup → Prioritize → Categorize → Plan fix order
├── Phase 4: Fix Implementation (ordered batches, up to 4 agents per batch)
│   ├── Batch 1: Structural fixes (architecture, integration, wiring)
│   ├── Batch 2: Quality fixes (DRY extraction, dead code, maintainability)
│   └── Batch 3: Optimization fixes (performance, rendering, data fetching)
├── Phase 5: Verification (2 parallel agents)
│   ├── Regression Verifier — confirm fixes, no new issues
│   └── Lint & Contract Checker — compile, lint, type-check, API contracts
├── Phase 6: Documentation (orchestrator)
│   └── Update SDD, Implementation Log, Progress Tracking
└── Phase 7: Report (orchestrator)
```

**Why ordered fix batches matter:** Structural fixes change WHERE code lives and HOW it connects. Quality fixes change WHAT the code looks like internally. Optimization fixes change HOW the code performs. If you optimize code that a structural fix later moves or rewires, the optimization is invalidated. Batch 1 → 2 → 3 is the only safe order.

Workers are stateless — they receive full context and return findings without coordinating with each other.

## Inputs

1. **Pitch folder** — Path to the pitch (required). Must contain at minimum `srs.md` and either `sdd.md` or `sdd/index.md`.
2. **Scope constraint** — Specific area to focus on (optional). Defaults to all changes.

Example invocations:
```
/audit @docs/pitches/example-pitch
/audit @docs/pitches/example-pitch "Focus on the api package only"
/audit @docs/systems/auth
```

## Process

### Phase 1: Context Gathering (Parallel)

Spawn a **Scout Agent** while you simultaneously read documents. Both run in parallel.

**Scout Agent (explore):**
```
Task(subagent_type="explore", prompt="""
Survey the full change footprint for this pitch implementation.

1. GIT DIFF: First detect the default branch: `git symbolic-ref refs/remotes/origin/HEAD --short` (fallback: main). Use it in place of `main` below.
   Run `git diff <default-branch> --name-only` in each affected repo.
   List every file created, modified, or deleted.
2. COMMIT HISTORY: Run `git log --oneline main..HEAD` to understand the
   sequence of changes.
3. DEPENDENCY GRAPH: For each changed file, identify what imports it and
   what it imports. Flag any new cross-package dependencies introduced.
4. FILE CATEGORIZATION: Group changed files by:
   - Frontend components / hooks / utilities
   - Backend endpoints / services / models
   - Types / interfaces / contracts
   - Configuration / build / deploy
   - Tests
   - Documentation

RETURN: structured inventory with file list, dependency edges, and
categorization. Include line counts for each changed file.

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**Orchestrator (simultaneous):** While the scout runs, read:
1. `index.md`, `srs.md`, `sdd.md` (or `sdd/index.md` + component SDDs), all `adr-*.md`
2. Gestalt entries for every repo touched (per MANIFEST.md)
3. Count requirements: functional, non-functional, constraints

**After both complete:** Merge into a master context document. Present:
```
Audit scope:
- Pitch: {name}
- Requirements: {N} functional, {M} non-functional, {K} constraints
- Files changed: {count} across {repos}
- New cross-package dependencies: {list or "none"}
- ADRs: {list with statuses}
- SDD progress: {✅ count} / {total}

Proceeding with deep review.
```

### Phase 2: Deep Review (4 Parallel Specialists)

Spawn **four parallel review agents**. Each is a specialist with a distinct lens. Provide every agent with the full context from Phase 1 (SRS, SDD, ADRs, file list, dependency graph, gestalt knowledge).

#### Reviewer Evidence Protocol (embed verbatim in every Phase 2 agent prompt)

Subagents do not inherit this file. Each of the four reviewer templates below marks the spot with "[Embed the Reviewer Evidence Protocol block verbatim here]" — when you build the actual dispatched prompt, copy this whole block into that spot so the agent receives it in full.

```
EVIDENCE GROUNDING (mandatory for every finding):
- Apply the Two-Pass Audit Pattern from `gestalt/.claude/references/orchestration.md` §Large-Scale Code Audit.
- Pass 1: `rg -n <pattern>` over the diff. Record exact `file:line:matched_text`.
- Pass 2: `git diff <base>..HEAD -- <file>` for each Pass-1 hit. Read 5 lines of context.
- Every finding MUST include an Evidence block quoting the exact `+`-prefixed line from `git diff` output. Findings without a verbatim Evidence quote are fabrications — drop them.
- Tag each finding `[verified]` (Pass-1 + Pass-2 confirmed) or `[suspected]` (pattern-inferred). Suspected findings are flagged for orchestrator re-verification, not for direct fix-batch entry.
- Hard ceiling: do NOT exceed 2,500 added lines or 15 tool calls in your scope. If you cannot complete the scope within this budget, partition further — do not approximate.

ABSTAIN RULE:
- If after the two-pass review you find no issues, return `NO FINDINGS`. This is preferred over speculation.
- Never invent findings to hit an unstated quota. There is no minimum count.

FORBIDDEN:
- "Target N findings", "be exhaustive", "find at least", "read every line" without ceiling — none of these appear in your task. Do not act as if they did.
```

**Agent 1 — Architecture & Integration Reviewer:**
```
Task(subagent_type="general-purpose", prompt="""
ARCHITECTURE & INTEGRATION audit.

CONTEXT:
- SRS: {srs_path}
- SDD: {sdd_path}
- ADRs: {adr_paths}
- Changed files: {file_list}
- Dependency graph: {dependency_edges from scout}
- Gestalt knowledge: {relevant_gestalt_entries}

CRITERIA:
1. ARCHITECTURE FIT: Right layer/package? Dependencies correct direction? No circular deps? Module boundaries respected? Packages/exports registered?
2. INTEGRATION: Every component imported and used? No orphans? API contracts match producer/consumer? Feature flags/env/config threaded?
3. ADR COMPLIANCE: Implementation matches ADRs? Undocumented deviations?
4. CROSS-CUTTING: Error handling, auth/permissions, observability for new code.

OUTPUT: ID (ARCH-{NNN}), Severity, Category (STRUCTURAL/INFORMATIONAL), file:line, evidence, exact fix. Sorted by severity.

[Embed the Reviewer Evidence Protocol block verbatim here]

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
- For EACH finding in your output, confirm an Evidence block with verbatim diff line is present. Drop any finding without one.
""")
```

**Agent 2 — Code Quality & DRY Reviewer:**
```
Task(subagent_type="general-purpose", prompt="""
CODE QUALITY, DRY, MAINTAINABILITY audit.

CONTEXT:
- Changed files: {file_list}
- Codebase conventions: {gestalt_entries_for_repos}

CRITERIA:
1. DRY: Duplicated logic (extract shared abstraction)? Inline values → constants? Repeated types → shared? Similar components → parameterized? API patterns → shared client?
2. DEAD CODE: Unused imports/vars/functions/types? Unreachable branches? Debug logs? Commented-out code? TODO/FIXME — if work not done elsewhere, implement or flag.
3. MAINTAINABILITY: Functions >50 lines? Unclear naming? Missing error handling? Magic numbers? Complex conditionals → named predicates? Deep nesting (>3)?
4. TYPE SAFETY: `any` in TS? Missing null checks? Unjustified `as X`? Implicit any?

OUTPUT: ID (QUAL-{NNN}), Severity, Category (QUALITY/INFORMATIONAL), file:line, evidence, exact fix. Sorted by severity.

[Embed the Reviewer Evidence Protocol block verbatim here]

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
- For EACH finding in your output, confirm an Evidence block with verbatim diff line is present. Drop any finding without one.
""")
```

**Agent 3 — Performance & Optimization Reviewer:**
```
Task(subagent_type="general-purpose", prompt="""
PERFORMANCE & OPTIMIZATION audit.

CONTEXT:
- SRS: {srs_path} (NFRs)
- Changed files: {file_list}
- Dependency graph: {dependency_edges from scout}

CRITERIA:
1. RENDERING: Unnecessary re-renders? Large lists without virtualization? Expensive compute without useMemo? Handlers without useCallback (where it matters)? Cascading re-renders? Missing Suspense for lazy?
2. DATA FETCHING: N+1 patterns? Missing cache? Overfetching? Unbounded lists without pagination? Waterfall → parallel?
3. BUNDLE: Heavy deps for small features? Missing code splitting? Unoptimized assets?
4. BACKEND: Missing indices? Unbounded queries? Sync where async? Connection pooling/cleanup? Memory leaks?
5. NFR: Each SRS performance NFR satisfied? Cite NFR ID.

OUTPUT: ID (PERF-{NNN}), Severity, Category (OPTIMIZATION/INFORMATIONAL), file:line, evidence, exact fix, estimated impact. Sorted by severity.

[Embed the Reviewer Evidence Protocol block verbatim here]

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
- For EACH finding in your output, confirm an Evidence block with verbatim diff line is present. Drop any finding without one.
""")
```

**Agent 4 — Requirements & Completeness Reviewer:**
```
Task(subagent_type="general-purpose", prompt="""
REQUIREMENTS TRACEABILITY & COMPLETENESS audit.

CONTEXT:
- SRS: {srs_path}
- SDD: {sdd_path}
- ADRs: {adr_paths}
- Changed files: {file_list}

CRITERIA:
1. REQUIREMENTS: Per functional requirement — PASS/PARTIAL/MISSING. Orphan code (no requirement mapping)?
2. SDD: Reflects built state? Unbuilt design sections? Built but undocumented? Progress Tracking accurate?
3. ADR: All terminal state? Accidental implementation of unchosen options?
4. DOCS: Implementation Log complete? SRS statuses current? index.md links correct?

OUTPUT: ID (REQ-{NNN}), Severity, Category (STRUCTURAL/DOCUMENTATION), requirement ID if applicable, evidence, exact fix. Plus PASS/PARTIAL/MISSING counts table.

[Embed the Reviewer Evidence Protocol block verbatim here]

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
- For EACH finding in your output, confirm an Evidence block with verbatim diff line is present. Drop any finding without one.
""")
```

### Phase 3: Triage (Orchestrator Only)

**This phase is orchestrator work — do NOT spawn agents.** Decision-making stays at the top.

Merge all four agents' findings into a single master findings list:

1. **Deduplicate** — Same issue flagged by multiple agents gets merged. Keep the most detailed description and the most specific fix. Note which reviewers independently identified it (higher confidence).

2. **Validate** — For each CRITICAL/MAJOR finding, quickly verify it's real by reading the cited code. Discard false positives. Reviewers sometimes flag intentional patterns as issues — cross-reference against ADR decisions and gestalt conventions.

3. **Categorize into fix batches** (this ordering is mandatory):
   - **Batch 1 — STRUCTURAL:** Architecture violations, integration gaps, orphan files, missing wiring, broken contracts, missing feature code (from REQ PARTIAL/MISSING). These change the shape and connectivity of the codebase.
   - **Batch 2 — QUALITY:** DRY extractions, dead code removal, maintainability improvements, type safety fixes, TODO implementations. These change internal code without affecting structure.
   - **Batch 3 — OPTIMIZATION:** Performance fixes, rendering optimizations, data fetching improvements, bundle optimizations. These tune existing correct code.
   - **Documentation fixes** — Handled by orchestrator in Phase 6, not by fix agents.

4. **Estimate effort** — Tag each fix as Quick (< 5 min), Medium (5-15 min), or Large (> 15 min).

5. **Group within batches** — Within each batch, group fixes by file cluster so agents touch non-overlapping file sets.

Present the audit report:

```
## Audit Report: {pitch_name}

### Summary
- CRITICAL: {count}  |  MAJOR: {count}  |  MINOR: {count}
- Requirements: {PASS}/{PARTIAL}/{MISSING} out of {total}
- False positives discarded: {count}

### Batch 1 — Structural Fixes ({count} issues)
{id}. [{severity}] {issue} — `{file}:{lines}` — {fix summary}
...

### Batch 2 — Quality Fixes ({count} issues)
{id}. [{severity}] {issue} — `{file}:{lines}` — {fix summary}
...

### Batch 3 — Optimization Fixes ({count} issues)
{id}. [{severity}] {issue} — `{file}:{lines}` — {fix summary}
...

### Documentation Fixes ({count} issues)
{id}. [{severity}] {issue} — {fix summary}
...

Fix order: Structural → Quality → Optimization → Documentation.
Shall I proceed?
```

**Wait for user confirmation before fixing.**

### Phase 4: Fix Implementation (Ordered Batches)

Execute fix batches **sequentially** (Batch 1 completes before Batch 2 starts). Within each batch, spawn **up to 4 parallel fix agents** working on non-overlapping file sets.

**For each batch:**

```
Task(subagent_type="builder", isolation="worktree", prompt="""
Implement audit fixes — Batch {N}: {STRUCTURAL | QUALITY | OPTIMIZATION}.

ASSIGNED FIXES:
{list of finding IDs, files, line ranges, and exact fixes}

FILE CLUSTER (no other agent touches these):
{file_list for this cluster}

RULES:
- You must apply every assigned fix. No skipped fixes.
- You MUST verify syntax and imports after each change.
- You MUST document ALL ripple effects outside cluster.

RETURN:
- Fixes applied: {N}/{N} with descriptions
- Ripple effects outside cluster: {list or "none"}
- Verification: syntax/imports checked for all modified files

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**Between batches:** Collect all agent returns. If any agent reports cross-cluster ripple effects, resolve them before starting the next batch. If any fix could not be applied, assess whether it blocks the next batch — if yes, resolve first; if no, defer to Phase 5.

### Phase 5: Verification (2 Parallel Agents)

After all fix batches complete, spawn **two parallel verification agents**:

**Verification Agent 1 — Regression Checker:**
```
Task(subagent_type="verifier", prompt="""
Verify audit fixes applied correctly, no regressions.

ORIGINAL FINDINGS: {master_findings_list with IDs}
FIXES APPLIED: {fix_list with finding IDs mapped to file changes}
AFFECTED FILES: {file_list}

You MUST verify each CRITICAL fix is present and correct. You MUST check ALL MAJOR fixes. Check no new orphans, broken imports, broken callers, removed-in-use code. Integration intact.

RETURN: Per-finding verification status (PASS/FAIL/PARTIAL) with evidence.

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**Verification Agent 2 — Lint & Contract Checker:**
```
Task(subagent_type="general-purpose", prompt="""
You are verifying compile health and contract integrity after audit fixes.

CONTEXT:
- All affected files: {file_list}
- API contracts from SDD: {contract_definitions}

YOUR TASK:
1. Run the project's linter on every modified file. Report any new lint errors.
2. For every API boundary (frontend <-> backend), verify field names and
   types still match between:
   - TypeScript interfaces and Python Pydantic models
   - API call sites and endpoint response shapes
3. Verify all barrel files / index exports include new or moved modules.
4. Check that package.json / pyproject.toml dependencies are correct
   (no missing deps from extracted utils, no unused deps from dead code
   removal).

RETURN: PASS or list of issues found (with file path and description).

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**After both return:** If either reports issues, fix them inline (orchestrator handles directly — no additional agent spawn). Maximum 1 retry: if issues persist after inline fix, present them to the user for manual resolution.

### Phase 6: Update Artifacts (Orchestrator)

The orchestrator updates SDD Implementation Log, Progress Tracking, SRS, and ADR statuses directly — no agents needed. Full templates: see `REFERENCE.md` §Phase 6.

### Phase 7: Final Report (Orchestrator)

Present the before/after issue counts, fixes by batch, verification results, and artifacts updated. Full template: see `REFERENCE.md` §Phase 7.

## Orchestration Rules

Follows shared orchestration patterns — read `gestalt/.claude/references/orchestration.md` before proceeding. Additionally:

1. **Evidence-based findings only.** Every issue must cite a specific file, line, and reason. Vague findings get discarded.
2. **Respect ADR decisions.** Do not "fix" deliberate ADR choices. Cross-reference findings against ADRs during triage.
3. **Preserve behavior.** Optimization and refactoring must not change functionality. Flag behavioral changes for user confirmation.
4. **Update docs last.** Artifacts reflect final state after all code fixes.
5. **One user confirmation gate.** Present the audit report in Phase 3. After confirmation, execute without further pauses unless a blocker emerges.

## Error Handling

| Situation | Action |
|-----------|--------|
| SRS or SDD missing | STOP, suggest `/srs` or `/sdd` |
| No git diff (no changes on branch) | STOP, inform user there's nothing to audit |
| Reviewer returns vague/unsupported findings | Discard during triage, note in report |
| Fix agent can't apply a fix | Log it, assess if it blocks next batch, defer or resolve |
| Cross-cluster ripple effect | Orchestrator resolves between batches |
| Verification finds regressions | Fix inline (orchestrator), max 1 retry |
| Issues persist after retry | Present to user for manual resolution |
| Scope too large (>100 changed files) | Split into sub-audits by package/service, run sequentially |

## Evals — see EVALS.md (dev-time only)
