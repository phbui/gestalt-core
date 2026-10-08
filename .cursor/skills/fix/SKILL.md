---
name: fix
description: "Spin up parallel agents to execute a batch of code fixes efficiently. Accepts fixes from linter errors, code review comments, issue lists, descriptions, or TODOs; triages, partitions by file ownership, and verifies results. Use for multiple fixes, errors, or changes across files."
disable-model-invocation: true
---
<Background>@gestalt-grounding</Background>

# Fix

Orchestrate parallel agents to execute a batch of code fixes efficiently. Uses the **planner-worker hierarchy** (see `gestalt/.cursor/references/orchestration.md`). You triage, partition, dispatch, and verify. Workers fix.

## Input

The user provides fixes in any form:
- A list of issues or errors (pasted text, linter output, test failures)
- A set of TODO items or GitHub/Linear issues
- A natural language description of multiple changes needed
- A file or directory to "clean up"
- Linter/type-checker output from a terminal

Example invocations:
```
/fix "Fix all the type errors in src/api/"
/fix @linter-output.txt
/fix "Apply these 8 changes from the code review: ..."
/fix "Clean up all TODO comments in platform/apps/pwa-backend/"
```

## Architecture

```
You (Planner)
├── Phase 1: Triage (1 explore agent, readonly)
│   └── Maps fixes → files, identifies dependencies, proposes partitions
├── Phase 2: Fix (up to 4 general-purpose agents, parallel)
│   ├── Fixer A — owns file cluster 1
│   ├── Fixer B — owns file cluster 2
│   ├── Fixer C — owns file cluster 3
│   └── Fixer D — owns file cluster 4
├── Phase 3: Verify (1 shell agent + 1 explore agent)
│   ├── Shell — runs lints, type checks, tests
│   └── Explorer — exhaustively verifies fixes against original issues
└── Phase 4: Reconcile (up to 4 agents if needed)
    └── Fix remaining issues from verification
```

## Process

### Phase 0: Parse Input

1. Read the user's fix list. Normalize into a structured list:

```
Fix List:
1. [FIX-001] {description} — {file(s) if known}
2. [FIX-002] {description} — {file(s) if known}
...
```

2. If the input is vague ("fix all type errors in src/"), note that triage will discover specifics.
3. If the input references external sources (Linear issues, linter output), read them first.

Present the parsed list:
```
Parsed {N} fixes. Launching triage...
```

### Phase 1: Triage (Explore Agent)

Spawn ONE readonly explore agent to map the problem space:

```
Task(subagent_type="explore", model="fast", readonly=true, prompt="""
You are triaging a batch of code fixes for parallel execution.

FIX LIST:
{normalized fix list from Phase 0}

SCOPE: {repo path or directory}

YOUR TASK:
1. For each fix, identify the EXACT files that need modification.
   Use grep, glob, and semantic search to locate relevant code.
   If a fix is vague, determine the concrete files and lines involved.

2. Build a FILE OWNERSHIP MAP — which files each fix touches:
   FIX-001 → [file_a.py, file_b.py]
   FIX-002 → [file_c.py]
   FIX-003 → [file_a.py, file_d.py]  ← shares file_a with FIX-001

3. Identify DEPENDENCIES between fixes:
   - FIX-003 depends on FIX-001 (shared file, FIX-001 changes API that FIX-003 uses)
   - FIX-005 depends on FIX-002 (type change propagates)

4. Propose PARTITIONS — group fixes into up to 4 non-overlapping clusters.
   Rules:
   - Fixes touching the same file(s) go in the SAME cluster
   - Dependent fixes go in the SAME cluster (or ordered across waves)
   - Balance cluster sizes roughly evenly
   - If >4 clusters needed, plan multiple waves

5. For each fix, note:
   - Estimated complexity: trivial / moderate / complex
   - Key context the fixer agent will need (imports, patterns, related code)

RETURN:
- File ownership map
- Dependency graph
- Proposed partitions (clusters with file lists)
- Wave plan (if batching needed)
- Per-fix context notes
- Total files mapped / total fixes mapped. Coverage: ALL fixes MUST be mapped.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**After triage returns:**

Review the partition plan. If fixes have complex dependencies that can't be cleanly partitioned, adjust:
- Merge overlapping clusters
- Split a wave into sequential sub-waves where cluster A runs before cluster B

Present the plan:
```
Triage complete:
- {N} fixes across {M} files
- Partitioned into {K} clusters ({W} wave(s))
- Wave 1: Clusters A({n} fixes), B({n} fixes), C({n} fixes), D({n} fixes)
- Wave 2: Cluster E({n} fixes) [depends on Wave 1]

Dispatching fixers...
```

### Phase 2: Fix (Parallel Agents)

For each wave, spawn up to 4 agents in parallel. Each agent owns a cluster.

```
Task(subagent_type="general-purpose", prompt="""
You are a code fixer. You own a specific set of files and fixes.

YOUR FIXES:
{list of FIX-IDs with descriptions for this cluster}

YOUR FILES (you may ONLY modify these):
{explicit file list for this cluster}

CONTEXT:
{per-fix context notes from triage — relevant imports, patterns, related code}
{codebase conventions if known from gestalt}

RULES:
- You must fix every issue in your assignment. No partial fixes. No TODOs.
- You MUST follow existing codebase conventions (naming, patterns, error handling).
- If a fix requires changing a file NOT in your list, STOP and report it —
  do not modify files outside your ownership.
- You MUST run the project's linter on every file you modify to catch new errors.
- If you discover additional issues while fixing, note them but only fix issues
  in your assignment unless the additional issue is in your owned files and trivial.
- Use the simplest fix that resolves each issue. Do NOT refactor surrounding code,
  add abstractions, or add handling for cases that cannot occur — an unrequested
  change is the same error as an unfixed issue: a scope mismatch.

RETURN FORMAT:
- Files modified: [list with brief description of changes]
- Fixes completed: [FIX-IDs with status]
- Unrequested changes: none, or [list with justification]
- Issues found: [any new issues discovered]
- Files that need changes outside your ownership: [if any]
- Confidence: high / medium / low (how confident you are the fixes are correct)

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**After each wave completes:**
- Collect results from all agents
- Check for conflicts (agent reported needing files outside ownership)
- If Wave N+1 exists, dispatch it with updated context from Wave N

### Phase 3: Verify

After all fix waves complete, run verification in parallel:

**Agent 1 — Automated Checks (shell verification):**
```
Task(subagent_type="explore", prompt="""
Run verification checks on the codebase after fixes were applied.

MODIFIED FILES:
{list of all files modified across all fix agents}

REPO PATH: {repo path}

YOUR TASK:
1. Run linters/type checkers if the project has them:
   - Python: look for pyproject.toml (ruff, mypy, pyright) or setup.cfg
   - TypeScript: look for tsconfig.json (tsc --noEmit)
   - Check Taskfile.yaml or Makefile for lint/check commands
2. Run relevant test suites if they exist:
   - Python: pytest on affected directories
   - TypeScript: vitest/jest on affected directories
3. Report ALL failures with file paths and error messages.

RETURN: structured list of check results (pass/fail with details).

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**Agent 2 — Exhaustive Verification (explore, readonly):**
```
Task(subagent_type="explore", model="fast", readonly=true, prompt="""
Verify that fixes were applied correctly by exhaustively verifying the code.

ORIGINAL FIX LIST:
{full fix list with descriptions}

FIXES REPORTED AS COMPLETE:
{FIX-IDs and agent descriptions of what was changed}

YOUR TASK:
1. For EVERY fix, read the modified file and verify:
   - The original issue is actually resolved
   - No regressions were introduced in the same file
   - The fix follows codebase conventions
2. Flag any fix that looks incomplete, incorrect, or introduces new issues.

RULES:
- Your verification matrix MUST have one row per FIX-ID. Missing rows = incomplete.
- You MUST verify ALL fixes, not a sample.

RETURN: verification matrix
| FIX-ID | Status | Notes |
|--------|--------|-------|
| FIX-001 | PASS | Clean fix |
| FIX-002 | PARTIAL | Missing edge case handling |
...
Verified: {N}/{total} fixes checked.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

### Phase 4: Reconcile

If verification surfaces issues:

1. **Minor issues (lint errors, missed edge cases):** Spawn up to 4 fix agents for a second pass, using the same partition strategy.
2. **Major issues (wrong approach, regression):** Present to user for guidance before re-attempting.
3. **No issues:** Skip to summary.

**Max 2 reconciliation loops.** After 2 loops, present remaining issues to the user.

### Phase 5: Report

Present the final summary:

```
## Fix Team Report

Fixes completed: {N}/{total}

### Completed ({N}/{total})
| Fix | Description | Files | Status |
|-----|-------------|-------|--------|
| FIX-001 | {desc} | {files} | DONE |
| FIX-002 | {desc} | {files} | DONE |

### Verification
- Lints: {pass/fail count}
- Type checks: {pass/fail}
- Tests: {pass/fail count}
- Exhaustive verification: {N}/{total} verified

### Remaining Issues (if any)
| Fix | Issue | Recommendation |
|-----|-------|----------------|
| FIX-007 | {what went wrong} | {suggested next step} |

### Stats
- Agents spawned: {count}
- Waves: {count}
- Files modified: {count}
- Time: {approximate}
```

## Orchestration Rules

Follows shared orchestration patterns — read `gestalt/.cursor/references/orchestration.md` before proceeding. Additionally:

1. **Accept some error rate.** Verify after, don't demand perfection during.
2. **Max 2 reconciliation loops.** Then escalate to user.
3. **Never skip verification.** Even if all agents report success.

## Edge Cases

| Situation | Action |
|-----------|--------|
| All fixes in one file | Single agent, no parallelism needed |
| >16 fixes | Batch into 4+ waves, prioritize by impact |
| Fix requires cross-file refactor | Assign all related files to one agent |
| Agent reports file outside ownership | Re-partition and dispatch in next wave |
| Vague fix description | Triage agent clarifies; if still vague, ask user |
| No test suite available | Rely on lint + exhaustive verification; note reduced confidence |
| Fix conflicts with another fix | Merge into same cluster; if fundamental conflict, escalate |

## When NOT to Use

- **Single fix** — just do it directly, no orchestration overhead needed
- **Fixes requiring architectural decisions** — use `/implement` or `/discuss` first
- **Exploratory "make it better"** — use `/audit` for systematic quality review
- **Fixes spanning multiple repos** — use `/swarm-check` to map impact first

## Evals — see EVALS.md (dev-time only)
