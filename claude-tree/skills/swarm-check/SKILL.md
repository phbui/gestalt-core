---
name: swarm-check
description: "Cross-repo ripple detection. When a change in one repo may affect others, uses GRAPH.md dependency map to spawn parallel agents checking each affected repo for broken contracts or required updates. Use when making changes that cross service boundaries."
model: sonnet
user-invocable: true
disable-model-invocation: true
argument-hint: "change to check for cross-repo ripple effects"
---

# Swarm Check

Detect and coordinate cross-repo ripple effects. When a change in one repo affects others via data flow connections, spawn parallel agents to check each affected repo.

## Process

### Step 1: Identify the Change

Determine:
- **Source repo** — where the change was made
- **Changed files** — what was modified (use `git diff --name-only` if needed)
- **Change summary** — API contract change, schema change, config change, etc.

### Step 2: Map the Ripple via Gestalt

Follow the gestalt grounding procedure in `gestalt/.claude/references/gestalt-grounding.md` (Cross-Service / Data Flow Questions). Specifically:
- Read `gestalt/GRAPH.md` for all data flow connections from the source repo
- Read relevant knowledge entries for each connected repo
- Map: direct consumers, indirect consumers, deployment dependencies

### Step 3: Spawn Ripple Checkers

For each affected repo (up to 6 in parallel), spawn an explore agent:

[Worker prompt W1: read `gestalt/.claude/references/swarm-check-worker-prompts.md` §W1 and dispatch it verbatim, substituting the placeholders.]

### Step 4: Synthesize

Collect all ripple checker results. Present a unified impact report. EVERY repo from GRAPH.md MUST appear in the table — no omissions.

```markdown
## Cross-Repo Impact Report

### Source Change
{source_repo}: {change_summary}

### Coverage
Repos checked: {N}/{N}. Affected: {list}. Unaffected: {list}.

### Affected Repos
| Repo | Status | Issues | Severity |
|------|--------|--------|----------|
| {repo} | AFFECTED | {count} | HIGH/MED/LOW |
| {repo} | CLEAR | 0 | — |
| {repo} | UNCHECKED | — | — (not on disk) |

### Required Changes
1. **{repo}** — {file}: {what needs to change}

### Deployment Order
{If changes need coordinated deployment, specify the order}
```

## Rules

- MUST read GRAPH.md before spawning checkers — NEVER guess at connections.
- You MUST check ALL repos in the workspace for ripple effects. NEVER skip a repo because it "probably isn't affected."
- Spawn checkers for ALL repos with data flow connections identified in GRAPH.md.
- If a repo isn't on disk, note it as UNCHECKED — but it still MUST appear in the report.
- Report both breaking changes and non-breaking updates.
- COVERAGE: EVERY item in scope MUST be checked. Report: N/N checked.

## Evals — see EVALS.md (dev-time only)
