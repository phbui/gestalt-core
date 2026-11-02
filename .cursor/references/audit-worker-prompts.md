# audit: worker prompts

Verbatim worker prompts for `/audit`, moved out of the skill body on 2026-11-02 so the skill loads smaller. Each section is dispatched exactly as written. Embed the Standard Block from `discipline-block.md` at the top of every prompt.

## W1. Scout Agent (explore):

```
Agent(subagent_type="Explore", prompt="""
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

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W2. Agent 1 — Architecture & Integration Reviewer:

```
Agent(subagent_type="general-purpose", prompt="""
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

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
- For EACH finding in your output, confirm an Evidence block with verbatim diff line is present. Drop any finding without one.
""")
```

## W3. Agent 2 — Code Quality & DRY Reviewer:

```
Agent(subagent_type="general-purpose", prompt="""
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

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
- For EACH finding in your output, confirm an Evidence block with verbatim diff line is present. Drop any finding without one.
""")
```

## W4. Agent 3 — Performance & Optimization Reviewer:

```
Agent(subagent_type="general-purpose", prompt="""
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

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
- For EACH finding in your output, confirm an Evidence block with verbatim diff line is present. Drop any finding without one.
""")
```

## W5. Agent 4 — Requirements & Completeness Reviewer:

```
Agent(subagent_type="general-purpose", prompt="""
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

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
- For EACH finding in your output, confirm an Evidence block with verbatim diff line is present. Drop any finding without one.
""")
```

## W6. For each batch:

```
Agent(subagent_type="builder", isolation="worktree", prompt="""
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

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W7. Verification Agent 2 — Lint & Contract Checker:

```
Agent(subagent_type="general-purpose", prompt="""
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

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```
