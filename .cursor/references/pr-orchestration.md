# PR Review Orchestration

Phase-by-phase mechanics for `/pr`. Covers context gathering scouts, triage rules, fix-batch ordering, verification agents, and the final report contract. Persona definitions for Phase 1 live in `pr-personas.md`.

## Contents

- [Phase 0: Context Gathering (Parallel)](#phase-0-context-gathering-parallel)
- [Phase 2: Triage & Report (Orchestrator Only)](#phase-2-triage-report-orchestrator-only)
- [Phase 3: Fix Implementation (Parallel Fix Agents)](#phase-3-fix-implementation-parallel-fix-agents)
- [Phase 4: Verification (2 Parallel Agents)](#phase-4-verification-2-parallel-agents)
- [Phase 5: Final Report (Orchestrator)](#phase-5-final-report-orchestrator)
- [Orchestration Rules](#orchestration-rules)

## Phase 0: Context Gathering (Parallel)

Spawn **two scout agents** while you simultaneously read PR metadata. All three run in parallel.

### Scout Agent 1 — PR Scout

```
Agent(subagent_type="explore", prompt="""
You are surveying a pull request for a deep multi-persona review.

PR REFERENCE: {pr_number_or_branch}
REPO: {repo_path}

YOUR TASK:
1. PR DIFF: Get the full diff.
   - If a PR number: `gh pr diff {number}`
   - If a branch: `git diff main...{branch}`
   - If no reference: `git diff main...HEAD`
   List every file created, modified, or deleted.

2. COMMIT HISTORY: Get the commit log for this PR.
   - If a PR number: `gh pr view {number} --json commits --jq '.commits[].messageHeadline'`
   - If a branch: `git log --oneline main..{branch}`
   Understand the sequence and intent of changes.

3. PR METADATA (if PR number provided):
   - `gh pr view {number} --json title,body,labels,reviews,comments,baseRefName,headRefName`
   - Extract: title, description, linked issues, reviewer comments, labels.
   - Document ALL reviewer concerns already raised.

4. DEPENDENCY GRAPH: For each changed file, identify:
   - What imports it (callers/consumers)
   - What it imports (dependencies)
   - Flag any new cross-package or cross-service dependencies introduced.

5. FILE CATEGORIZATION: Group changed files by:
   - Frontend components / hooks / utilities / styles
   - Backend endpoints / services / models / middleware
   - Types / interfaces / contracts / schemas
   - Configuration / build / deploy / CI
   - Tests (unit, integration, e2e)
   - Documentation / comments
   Include line counts (additions/deletions) for each changed file.

6. CHANGE CLASSIFICATION: Categorize the overall PR as:
   - Feature (new capability)
   - Bugfix (correcting behavior)
   - Refactor (restructuring without behavior change)
   - Chore (deps, config, CI, docs)
   - Mixed (multiple types — list each)

RETURN: Structured inventory with file list, dependency edges, categorization,
commit history, PR metadata, and change classification. Be thorough — this
context feeds three specialist reviewers.
""")
```

### Scout Agent 2 — Repo Conventions Scanner

```
Agent(subagent_type="explore", prompt="""
You are scanning a repository to understand its conventions, patterns, and standards
so that PR reviewers can assess whether changes conform.

REPO: {repo_path}

YOUR TASK:
1. GESTALT: Read `gestalt/MANIFEST.md`. Find entries for this repo.
   Read the relevant gestalt knowledge entry. Extract:
   - Architecture patterns, module boundaries, data flow
   - Known conventions, naming patterns, coding standards
   - Known gotchas or anti-patterns to avoid

2. LINTER & FORMATTER CONFIG: Check for:
   - ESLint / Biome / Prettier config (JS/TS)
   - Ruff / Black / isort / mypy / pyright config (Python)
   - Taskfile.yaml, Makefile, or package.json scripts for lint/format/check commands
   - Pre-commit hooks (.pre-commit-config.yaml, .husky/)
   - CI lint/check steps (.github/workflows/)

3. CODING PATTERNS: Sample 3-5 existing files in the same directories as
   the changed files. Note:
   - Import ordering conventions
   - Error handling patterns (try/catch style, error types, Result types)
   - Logging patterns (logger usage, log levels, structured logging)
   - Test patterns (describe/it vs test(), assertion library, mock patterns)
   - Naming conventions (camelCase vs snake_case, file naming, component naming)
   - Comment style and density

4. ARCHITECTURE BOUNDARIES: Identify:
   - Package/module boundaries (what calls what, forbidden cross-dependencies)
   - API contract patterns (how types are shared between frontend/backend)
   - State management patterns (if frontend)
   - Dependency injection / service patterns (if backend)

5. SECURITY BASELINE: Check for:
   - Auth patterns (middleware, decorators, guards)
   - Input validation patterns (schemas, validators)
   - Secret management (.env patterns, vault usage, config injection)
   - CORS, CSP, or other security headers

RETURN: Structured conventions report covering all five areas.
Cite specific files as examples for each convention.
""")
```

### Orchestrator (Simultaneous)

While scouts run, read:
1. PR description and linked issues (if PR number provided)
2. Any ADRs or design docs in the repo that relate to the changed areas
3. Existing reviewer comments on the PR (if any — don't duplicate their concerns, build on them)
4. User's focus constraint (if provided)

### After All Complete

Merge into a master context document. Present a brief status:

```
PR Review: {title or branch}
- Type: {feature | bugfix | refactor | chore | mixed}
- Files changed: {count} ({additions}+ / {deletions}-)
- Categories: {frontend: N, backend: N, types: N, config: N, tests: N, docs: N}
- New cross-boundary deps: {list or "none"}
- Existing reviewer comments: {count or "none"}
- Focus: {user constraint or "full PR"}

Launching deep review with 3 personas...
```

## Phase 2: Triage & Report (Orchestrator Only)

**This phase is orchestrator work — do NOT spawn agents.** All synthesis and decision-making stays at the top.

Merge all three personas' findings into one centralized report.

### Step 1: Deduplicate

Same issue flagged by multiple personas → merge into one finding. Keep the most detailed description. Note which personas independently identified it (higher confidence signal). Use the finding ID from the most relevant persona.

### Step 2: Cross-Reference

- If the Infra Expert flags a non-standard pattern AND the Senior Engineer shows it causes a bug → elevate severity, combine evidence.
- If the Code Quality agent flags missing tests AND the Senior Engineer identifies an untested failure scenario → combine into one finding with both the scenario and the test recommendation.
- Note agreement and disagreement between personas explicitly.

### Step 3: Validate

For each CRITICAL or MAJOR finding, quickly verify it's real by reading the cited code. Discard false positives. Cross-reference against:
- ADR decisions (deliberate choices aren't violations)
- PR description (acknowledged trade-offs)
- Existing reviewer comments (already raised concerns)
- Repo conventions (established patterns)

### Step 4: Format the Selectable Report

Present findings in a format where the user can pick items to fix:

```
## PR Review: {title}

### Review Team
- Infra Expert: {N} findings ({breakdown by severity})
- Senior Engineer: {N} findings ({breakdown by severity})
- Code Quality: {N} findings ({breakdown by severity})
- Deduplicated: {N} total unique findings

### Overall Assessment
{2-3 sentences on the PR's overall quality, key risks, and whether it's
merge-ready, needs-work, or needs-rethink}

---

### CRITICAL ({count})

[ ] 1. {SEC-001}: {title}
       {severity} | {category} | `{file}:{lines}`
       {1-2 sentence problem summary}
       Fix: {concrete fix summary}

[ ] 2. {ENG-003}: {title}
       ...

### MAJOR ({count})

[ ] 3. {ENG-001}: {title}
       ...

[ ] 4. {QAL-002}: {title}
       ...

### MINOR ({count})

[ ] 5. {SEC-004}: {title}
       ...

### INFO ({count})
{List these inline — they're observations, not actionable fixes}
- {QAL-005}: {observation}
- {ENG-008}: {observation}

---

### Quick Actions
- **Fix all CRITICAL**: Reply `fix critical`
- **Fix all CRITICAL + MAJOR**: Reply `fix major`
- **Fix specific items**: Reply with numbers, e.g. `fix 1, 3, 5`
- **Fix all**: Reply `fix all`
- **Skip fixes**: Reply `skip` (report only)

### Personas agree on:
{List findings independently identified by 2+ personas — highest confidence}

### Personas disagree on:
{Any conflicting assessments with both sides stated}
```

**Wait for user selection before proceeding to Phase 3.**

If the user replies `skip`, jump to Phase 5 (Final Report) with no fixes.

## Phase 3: Fix Implementation (Parallel Fix Agents)

Based on the user's selection, group chosen fixes into file clusters and spawn **up to 4 parallel fix agents** on non-overlapping file sets. Use worktree isolation for parallel writes.

### Fix Batch Ordering

Same as `/audit` — structural before cosmetic:
1. **Structural fixes** — integration gaps, missing wiring, contract mismatches (from ENG/SEC findings)
2. **Code fixes** — logic bugs, security patches, error handling (from ENG/SEC findings)
3. **Quality fixes** — docs, logs, tests, naming (from QAL findings)

If all selected fixes are independent (different files), run them all in one wave. If some are dependent, batch accordingly.

### Fix Agent Prompt

```
Agent(subagent_type="general-purpose", model="sonnet", isolation="worktree", prompt="""
Implement PR review fixes.

ASSIGNED FIXES:
{list of finding IDs, files, line ranges, and exact fixes from the report}

FILE CLUSTER (you may ONLY modify these files):
{file_list_for_this_cluster}

REPO CONVENTIONS:
{relevant conventions from Phase 0}

RULES:
- Apply every assigned fix exactly as specified.
- Follow existing codebase conventions for any new code.
- For new tests: follow the repo's test patterns (framework, naming, location).
- For new docs: match existing doc style and density.
- For new logs: match existing log patterns (structured/printf, levels, fields).
- If a fix requires modifying files outside your cluster, STOP and report it.
- Run the project's linter on every modified file.
- Verify syntax and imports after every change.

RETURN:
- Files modified with description of changes
- Finding IDs resolved
- Any cross-cluster effects that need orchestrator attention
""")
```

**Between waves:** If any agent reports cross-cluster effects, resolve before starting the next wave.

## Phase 4: Verification (2 Parallel Agents)

After all fixes are applied, verify nothing is broken.

### Verification Agent 1 — Regression Checker

```
Agent(subagent_type="verifier", prompt="""
Verify PR review fixes were applied correctly with no regressions.

ORIGINAL FINDINGS: {selected_findings_list}
FIXES APPLIED: {fix_list_with_file_changes}
AFFECTED FILES: {file_list}

YOUR TASK:
1. For each CRITICAL fix: verify the fix is present and correct. Read the
   code and confirm the problem is resolved.
2. For each MAJOR fix: spot-check that the fix addresses the stated problem.
3. Check for regressions:
   - No new broken imports or missing dependencies
   - No removed-in-use code
   - No broken callers of modified functions
   - Integration between files still intact
4. If new tests were added: verify they test the right things and would
   actually catch the original issue.

RETURN: Verification matrix with PASS/FAIL per finding ID, plus any
regressions discovered.
""")
```

### Verification Agent 2 — Lint & Contract Checker

```
Agent(subagent_type="general-purpose", prompt="""
Verify compile health and contract integrity after PR review fixes.

AFFECTED FILES: {file_list}

YOUR TASK:
1. Run the project's linter on every modified file. Report any new lint errors.
2. Run the type checker if configured (tsc --noEmit, mypy, pyright).
3. For API boundaries: verify field names and types still match between
   frontend interfaces and backend models.
4. Run relevant test suites for the modified directories.
5. Check that all barrel files / index exports include modified modules.

RETURN: PASS or list of issues found with file paths and descriptions.
""")
```

**After both return:** If either reports issues, fix inline (orchestrator handles directly — max 1 retry). If issues persist, report to user.

## Phase 5: Final Report (Orchestrator)

```
## PR Review Complete: {title}

### Summary
- Findings: {total} ({critical} critical, {major} major, {minor} minor, {info} info)
- Fixed: {count} | Skipped: {count} | Deferred: {count}

### Fixes Applied
{Grouped by persona — what was found and how it was fixed}

#### Infra Expert Fixes
- {SEC-001}: {what was fixed}
- ...

#### Senior Engineer Fixes
- {ENG-001}: {what was fixed}
- ...

#### Code Quality Fixes
- {QAL-001}: {what was fixed}
- ...

### Verification
- Regression check: {PASS or issues}
- Lint & types: {PASS or issues}
- Tests: {PASS or issues}

### Remaining Items
{Any findings the user chose not to fix, or that couldn't be auto-fixed}
- {ID}: {summary} — {reason not fixed}

### Merge Readiness
{READY | READY WITH CAVEATS | NOT READY}
{Brief justification}
```

## Orchestration Rules

Follows shared orchestration patterns — read `gestalt/.cursor/references/orchestration.md` and `gestalt/.cursor/references/claude-orchestration.md` before proceeding.

1. **Evidence-based findings only.** Every finding must cite a specific file, line, and code snippet. No vague concerns.
2. **Concrete fixes only.** Every finding must include an exact fix — not "consider improving" but "change line 42 from X to Y".
3. **Respect PR intent.** Review what the PR does, not what you wish it did. Flag scope creep in INFO, not as findings.
4. **Respect ADR decisions.** Cross-reference findings against ADRs. Deliberate decisions aren't violations.
5. **One user selection gate.** Present the report in Phase 2. After selection, execute without further pauses unless a blocker emerges.
6. **Don't duplicate existing reviews.** If the PR already has reviewer comments, acknowledge them and build on them — don't re-raise the same concerns.
7. **Persona boundaries matter.** Each persona stays in their lane. Overlap is expected at edges but the core focus must be distinct. This prevents three agents all flagging the same obvious issue while missing domain-specific subtleties.
