---
name: pr
description: "Multi-persona deep review of a pull request with specialist agents, centralized triage, and selectable fix options."
model: sonnet
user-invocable: true
argument-hint: "PR number or URL to review"
---

# PR Review

Multi-persona deep review of a pull request. You are an **orchestrator agent** that gathers PR context, dispatches three specialist persona agents to review from distinct angles, synthesizes findings into one centralized report with selectable fix options, then executes chosen fixes.

Designed to work on **any PR** — no SRS, SDD, or pitch artifacts required. If they exist, the reviewers will use them as additional context.

## Architecture

```
You (Orchestrator)
├── Phase 0: Context Gathering (parallel)
│   ├── PR Scout (explore) — diff, commits, file inventory, dependency edges
│   ├── Repo Conventions Scanner (explore) — gestalt, linters, patterns, style
│   └── Orchestrator — reads PR description, linked issues, ADRs if present
├── Phase 1: Deep Review (3 parallel persona agents)
│   ├── Infra Expert                → SEC-NNN findings
│   ├── Senior Engineer             → ENG-NNN findings
│   └── Code Quality Specialist     → QAL-NNN findings
├── Phase 2: Triage & Report (orchestrator ONLY)
│   └── Dedup → Cross-reference → Severity → Selectable fix menu
│   *** USER SELECTION GATE ***
├── Phase 3: Fix Implementation (parallel fix agents, worktree-isolated)
│   └── Up to 4 agents on non-overlapping file clusters
├── Phase 4: Verification (2 parallel agents)
│   ├── Regression Verifier — confirm fixes, no new issues
│   └── Lint & Contract Checker — compile, lint, type-check
└── Phase 5: Final Report (orchestrator)
```

Workers are stateless — they receive full context and return findings without coordinating with each other. The orchestrator owns all triage, prioritization, and synthesis.

## Input

The user provides a PR reference and an optional focus constraint:

```
/pr 868
/pr 868 "Focus on the streaming module only"
/pr feature/auth-rework
/pr https://github.com/org/repo/pull/868
/pr                                          ← reviews current branch vs main
```

If no PR number or branch is given, review the current branch's diff against `main` (or the repo's default branch).

Example invocations:
```
/pr 42
/pr 42 "Only look at the backend changes"
/pr feature/new-api
/pr
```

## Execution Overview

### Phase 0: Context Gathering (Parallel)

Spawn the **PR Scout** and **Repo Conventions Scanner** in parallel while you simultaneously read PR description, linked issues, and any relevant ADRs. After all complete, merge into a master context document and present a brief status (PR type, file counts, categories, focus). Then launch Phase 1.

For full scout agent prompts and the status template, see `gestalt/.claude/references/pr-orchestration.md` §Phase 0.

### Phase 1: Deep Review (3 Parallel Persona Agents)

Spawn three parallel review agents. Each embodies a distinct review persona. Every agent receives the full Phase 0 context. The user's focus constraint (if any) **MUST** be passed to all three.

| Persona | Lens | Output IDs |
|---------|------|------------|
| Infra Expert | Security, infrastructure patterns, operations, non-standard code | SEC-NNN |
| Senior Engineer | Logic correctness, data flow, error handling, integration, edge cases | ENG-NNN |
| Code Quality Specialist | Documentation, logging, tests, naming, maintainability | QAL-NNN |

Each persona returns findings sorted by severity (CRITICAL → MAJOR → MINOR → INFO) with a summary count.

For full persona prompts, criteria checklists, finding formats, and persona-boundary rules, see `gestalt/.claude/references/pr-personas.md`.

For domain-heavy PRs (DB migrations, frontend-heavy, ML pipelines, infra/DevOps), the orchestrator MAY add a 4th specialist persona — see `pr-personas.md` §Optional 4th Persona. Default is 3.

### Phase 2: Triage & Report (Orchestrator Only)

**This phase is orchestrator work — do NOT spawn agents.** All synthesis and decision-making stays at the top.

1. **Deduplicate** — same issue flagged by multiple personas → merge, keep most detailed description, note co-identification (confidence signal).
2. **Cross-reference** — combine evidence when two personas confirm the same root issue from different angles. Note agreement and disagreement explicitly.
3. **Validate** — for each CRITICAL/MAJOR, verify by reading the cited code. Discard false positives. Cross-reference against ADRs, PR description, existing reviewer comments, and repo conventions.
4. **Format the selectable report** — group by severity with checkboxes. Include review-team summary, overall assessment, quick-action shortcuts (`fix critical`, `fix major`, `fix all`, `fix 1, 3, 5`, `skip`), and persona agreement/disagreement sections.

**Wait for user selection before proceeding to Phase 3.** If the user replies `skip`, jump to Phase 5 with no fixes.

For the full report template (with all sections and exact format), see `gestalt/.claude/references/pr-orchestration.md` §Phase 2.

### Phase 3: Fix Implementation (Parallel Fix Agents)

Group the user's selected fixes into file clusters and spawn **up to 4 parallel fix agents** on non-overlapping file sets, with worktree isolation.

**Fix batch ordering** (same as `/audit` — structural before cosmetic):
1. Structural fixes — integration gaps, missing wiring, contract mismatches (ENG/SEC)
2. Code fixes — logic bugs, security patches, error handling (ENG/SEC)
3. Quality fixes — docs, logs, tests, naming (QAL)

If all selected fixes are independent (different files), run them in one wave. If dependent, batch accordingly. Between waves, resolve any cross-cluster effects before continuing.

For the fix agent prompt, see `gestalt/.claude/references/pr-orchestration.md` §Phase 3.

### Phase 4: Verification (2 Parallel Agents)

After all fixes are applied, spawn two verifiers in parallel:
- **Regression Checker** — confirms each CRITICAL/MAJOR fix is present and correct, checks for broken imports, removed-in-use code, broken callers, integration breaks.
- **Lint & Contract Checker** — runs the project linter, type checker, relevant test suites, and verifies API contract alignment between frontend/backend.

If either reports issues, fix inline (orchestrator handles directly — max 1 retry). If issues persist, report to user.

For both verifier prompts, see `gestalt/.claude/references/pr-orchestration.md` §Phase 4.

### Phase 5: Final Report (Orchestrator)

Present a structured final report grouping fixes by persona, listing verification results, deferred items, and overall merge readiness (READY | READY WITH CAVEATS | NOT READY). Full template in `gestalt/.claude/references/pr-orchestration.md` §Phase 5.

## Orchestration Rules

Follows shared orchestration patterns — read `gestalt/.claude/references/orchestration.md` and `gestalt/.claude/references/claude-orchestration.md` before proceeding.

1. **Evidence-based findings only.** Every finding must cite a specific file, line, and code snippet. No vague concerns.
2. **Concrete fixes only.** Every finding must include an exact fix — not "consider improving" but "change line 42 from X to Y".
3. **Respect PR intent.** Review what the PR does, not what you wish it did. Flag scope creep in INFO, not as findings.
4. **Respect ADR decisions.** Cross-reference findings against ADRs. Deliberate decisions aren't violations.
5. **One user selection gate.** Present the report in Phase 2. After selection, execute without further pauses unless a blocker emerges.
6. **Don't duplicate existing reviews.** If the PR already has reviewer comments, acknowledge them and build on them — don't re-raise the same concerns.
7. **Persona boundaries matter.** Each persona stays in their lane. Overlap is expected at edges but the core focus must be distinct.

Full per-phase orchestration mechanics live in `gestalt/.claude/references/pr-orchestration.md`.

## Error Handling

| Situation | Action |
|-----------|--------|
| No PR number and no branch diff | STOP — "No changes to review. Provide a PR number or ensure your branch has commits ahead of main." |
| PR not found (`gh pr view` fails) | Check if it's a branch name. Try `git diff main...{input}`. If that also fails, STOP with error. |
| Reviewer returns vague/unsupported findings | Discard during triage. Note discarded count in report. |
| Fix agent can't apply a fix | Log it, move to next fix, report in final summary. |
| Cross-cluster ripple effect | Orchestrator resolves between fix waves. |
| Verification finds regressions | Fix inline (max 1 retry), then report to user. |
| Scope too large (>200 changed files) | Warn user, suggest focusing on specific areas. Split into sub-reviews if user confirms. |
| PR is a draft | Note it in the status. Review normally — early feedback is valuable. |
| Merge conflicts present | Warn user. Review the code as-is but note that merge resolution may change things. |

## When NOT to Use

- **Post-build quality gate against specs** → use `/audit` (requires SRS/SDD, reviews against requirements)
- **Deep investigation of a specific issue** → use `/investigate` (single-topic deep dive)
- **Batch of known fixes to apply** → use `/fix` (parallel fix agents with triage)

## References

- `gestalt/.claude/references/pr-personas.md` — full persona prompts (Infra Expert, Senior Engineer, Code Quality Specialist) plus the optional 4th persona table
- `gestalt/.claude/references/pr-orchestration.md` — phase-by-phase mechanics: scout prompts, triage rules, report templates, fix agent prompt, verifier prompts, final report
- `gestalt/.claude/references/orchestration.md` — provider-neutral orchestration patterns
- `gestalt/.claude/references/claude-orchestration.md` — Claude-specific enhancements (Agent Teams, worktree isolation, hooks)

ALL phases above are mandatory. Do not skip or abbreviate any phase.

## Evals — see EVALS.md (dev-time only)

## Verification Gate

Before completing this skill:
- Re-read the task above. Confirm EVERY numbered step was addressed.
- Count: steps completed / total. If any skipped, state why explicitly.
- Verify all outputs have the required format and content.
