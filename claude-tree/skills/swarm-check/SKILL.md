---
name: swarm-check
description: "Cross-repo ripple detection. When a change in one repo may affect others, uses GRAPH.md dependency map to spawn parallel agents checking each affected repo for broken contracts or required updates. Use when making changes that cross service boundaries."
model: sonnet
user-invocable: true
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

```
Task(subagent_type="explore", prompt="""
A change was made in {source_repo}:
- Files: {changed_files}
- Summary: {change_summary}

Your repo ({target_repo}) may be affected via: {connection_description}

Check:
1. Does {target_repo} import or reference anything that changed?
2. Are there shared data contracts (API types, DB schemas, message formats) that need updating?
3. Are there configuration references (env vars, service URLs, feature flags) that need updating?
4. Do tests in {target_repo} test against the changed interface?

For each issue found, report:
- File and line in {target_repo}
- What references the changed code
- Whether it will break, behave incorrectly, or just needs updating
- Suggested fix

Return: AFFECTED (with issues) or CLEAR (with evidence of no impact).
Document ALL checked files and contracts — not just affected ones.

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

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
