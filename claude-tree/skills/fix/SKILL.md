---
name: fix
description: "Spin up parallel agents to execute a batch of code fixes efficiently. Accepts fixes from linter errors, code review comments, issue lists, descriptions, or TODOs; triages, partitions by file ownership, and verifies results. Use for multiple fixes, errors, or changes across files."
model: sonnet
user-invocable: true
argument-hint: "issues to fix (list, linter output, or description)"
---

# Fix

Orchestrate parallel agents to execute a batch of code fixes efficiently. Uses the **planner-worker hierarchy** (see `gestalt/.claude/references/orchestration.md`). You triage, partition, dispatch, and verify. Workers fix.

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

[Worker prompt W1: read `gestalt/.claude/references/fix-worker-prompts.md` §W1 and dispatch it verbatim, substituting the placeholders.]

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

> **Claude Code quality gate:** The `builder-quality-gate.sh` Stop hook automatically runs lint and type checks on all files modified by this agent. If checks fail, the agent is instructed to fix the issues before stopping.

[Worker prompt W2: read `gestalt/.claude/references/fix-worker-prompts.md` §W2 and dispatch it verbatim, substituting the placeholders.]

**After each wave completes:**
- Collect results from all agents
- Check for conflicts (agent reported needing files outside ownership)
- If Wave N+1 exists, dispatch it with updated context from Wave N

### Phase 3: Verify

After all fix waves complete, run verification in parallel:

**Agent 1 — Automated Checks (shell verification):**
[Worker prompt W3: read `gestalt/.claude/references/fix-worker-prompts.md` §W3 and dispatch it verbatim, substituting the placeholders.]

**Agent 2 — Exhaustive Verification (explore, readonly):**
[Worker prompt W4: read `gestalt/.claude/references/fix-worker-prompts.md` §W4 and dispatch it verbatim, substituting the placeholders.]

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

Follows shared orchestration patterns — read `gestalt/.claude/references/orchestration.md` before proceeding. Additionally:

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
- **Fixes requiring architectural decisions** — use `/requirements implement` or `/discuss` first
- **Exploratory "make it better"** — use `/audit` for systematic quality review
- **Fixes spanning multiple repos** — use `/swarm-check` to map impact first

## Evals — see EVALS.md (dev-time only)
