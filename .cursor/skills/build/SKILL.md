---
name: build
description: "General-purpose builder that implements features, components, or prototypes from any input — a description, spec, design doc, or existing SDD/ADR. Spawns parallel builder agents for independent workstreams. Use to build a feature straight from a design or spec."
disable-model-invocation: true
---
<Background>@gestalt-grounding</Background>

# Build

General-purpose builder. Takes any input — a description, a spec file, a design doc, an SDD, an ADR, or just a plain request — and builds it. Spawns parallel agents for independent workstreams when possible.

Unlike `/implement` (which requires an SRS and runs the full requirements-to-code pipeline), `/build` is flexible: it works with whatever context you have.

## Input

The user provides what to build in any form:
- A plain description ("build a retry wrapper for our API calls")
- A spec or design doc ("build per this spec: @docs/design.md")
- An SDD ("build @docs/pitches/c7-comms/sdd.md")
- An ADR to spike ("spike @docs/pitches/c7-comms/adr-001-streaming.md")
- Existing code to extend ("add WebSocket support to @apps/gateway/")
- A combination of the above

Example invocations:
```
/build "Add a retry wrapper with exponential backoff to src/api/"
/build @docs/pitches/c7-comms/sdd.md "Implement the NATS connection handler"
/build @docs/pitches/c7-comms/adr-001-streaming.md "Prototype both options and measure latency"
/build "Create a FastAPI service at apps/data-export/ with CSV and GeoJSON endpoints"
/build @design-sketch.md @apps/sensor-nats "Implement per the sketch"
```

## Architecture

```
You (Planner)
├── Step 1: Understand (read context, determine scope)
├── Step 2: Plan (break into workstreams, confirm with user)
├── Step 3: Execute
│   ├── Single workstream → build directly
│   ├── 2 workstreams → parallel subagents
│   └── 3+ workstreams → Agent Teams (teammates self-coordinate via task list)
├── Step 4: Verify (lint, type-check, test, spot-check)
└── Step 5: Report (summary, next steps, doc updates)
```

See `gestalt/.cursor/references/orchestration.md` for orchestration patterns.

## Process

### Step 1: Understand

Read everything the user provided. Determine what exists and what's needed:

1. **Read provided files** — specs, designs, SDDs, ADRs, existing code
2. **Check for requirements docs** — if `srs.md` or `sdd.md` exist in the context, use them as acceptance criteria and design guidance (but don't require them)
3. **Check gestalt** — follow `gestalt/.cursor/references/gestalt-grounding.md` to find relevant knowledge about the repo, conventions, and patterns
4. **Survey existing code** — if extending something, read the code being extended

Classify the build:

| Type | Indicator | Approach |
|------|-----------|----------|
| **Greenfield** | No existing code in target area | Build from scratch per description/spec |
| **Extension** | Existing code to add to | Read patterns, match conventions, extend |
| **SDD implementation** | SDD document provided | Follow the design, track progress |
| **Spike** | ADR provided, exploratory goal | Prototype options, measure, compare |

### Step 2: Plan

Present a build plan to the user:

```
Build plan:
- Type: {greenfield | extension | SDD implementation | spike}
- Target: {directory/files being created or modified}

Workstreams:
1. {workstream} — {what it builds} — {files it owns}
2. {workstream} — {what it builds} — {files it owns}
3. {workstream} — {what it builds} — {files it owns}

{If requirements docs exist:}
Requirements covered: {REQ-IDs}

Dependencies: {workstream ordering if any}

Shall I proceed?
```

For simple builds (single workstream, clear scope), keep the plan brief. For complex builds, be thorough.

### Step 3: Execute

**Single workstream (default):**

Build directly. For each step:
1. Write code following existing conventions and the plan
2. Run the project's linter on modified files, fix any new errors
3. Report progress before moving to next step

RULES:
- Scope = the stated requirements — all of them, and nothing else. A missing requirement and an unrequested addition are the SAME error. Implement EVERY requirement (no stubs, no TODOs, no placeholders) with the simplest mechanism that satisfies it; add a new abstraction, file, or dependency ONLY when a requirement cannot be met without it.
- You MUST run linter after each file modification.
- You MUST NOT skip any planned step.

RETURN:
- Files modified with descriptions
- Requirements completed: {N}/{N}
- Tests written and passing: {N}/{N}
- Skipped requirements: {list with reasons, or "none"}
- Unrequested additions: none, or {list with justification against a requirement ID}

**Two parallel workstreams:**

Spawn builder subagents in parallel. Each agent owns specific files — no overlap allowed:

```
Task(subagent_type="builder", prompt="""
CONTEXT:
- Build plan: {workstream description}
- Target files (you may ONLY modify these): {file list}
- Conventions: {codebase patterns from gestalt or existing code}
{- SDD section: {relevant design section} (if SDD exists)}
{- Requirements: {relevant REQ-IDs and acceptance criteria} (if SRS exists)}

YOUR TASK:
{specific build instructions for this workstream}

RULES:
- Scope = the stated requirements — all of them, and nothing else. A missing requirement and an unrequested addition are the SAME error. Build every requirement (no TODOs, no stubs, no placeholders) with the simplest mechanism that satisfies it; add a new abstraction, file, or dependency ONLY when a requirement cannot be met without it.
- You MUST follow existing codebase conventions.
- If you need to modify files outside your list, STOP and report it.
- You MUST run the project's linter on every file you modify.

RETURN:
- Files created/modified with descriptions
- What was built and why
- Requirements completed: {N}/{N}
- Unrequested additions: none, or {list with justification against a requirement ID}
- Issues found or decisions made
- Anything needing work outside your file cluster

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

After builders return, verify integration.

**Three or more parallel workstreams:**

Spawn builder agents per workstream, following `gestalt/.cursor/references/orchestration.md` for partitioning. Each builder gets a focused scope with non-overlapping file ownership. After all builders return, verify integration between all workstreams.

**During execution, if you encounter:**

| Situation | Action |
|-----------|--------|
| Unclear requirement | ASK the user — don't guess |
| Design gap (if working from SDD) | Propose addition, get approval |
| Significant architectural choice | Note it, suggest `/adr` if lasting impact |
| Scope creep | Note the additional work, stay on plan for now |

### Step 4: Verify

Run verification per `gestalt/.cursor/references/verification.md`:

1. **Lint check** — run the project's linter on all modified files
2. **Type check** — if the repo has a type checker configured
3. **Tests** — run relevant test suites for modified areas
4. **Spot-check** — for parallel builds, verify workstreams integrate correctly

If issues found: fix inline (max 2 retries), then escalate to user.

### Step 5: Report

```
## Build Summary

### What Was Built
- {description of what was created/modified}

### Files
- {file list with brief descriptions}

### Verification
- Lints: {pass/fail}
- Type checks: {pass/fail or N/A}
- Tests: {pass/fail or N/A}

{If SDD exists:}
### SDD Updates
- Progress Tracking: {requirements updated}
- Implementation Log: {entry added}

{If ADR spike:}
### Spike Findings
- {option}: {findings}
- Preliminary assessment: {which option looks better and why}

### Next Steps
- {what to work on next}
```

**If working from an SDD:** Update its Progress Tracking and Implementation Log sections. This is mandatory — the SDD is a living document.

**If spiking an ADR:** Update its Exploration Log and option pros/cons sections.

## When NOT to Use

- **Full requirements pipeline needed** → use `/implement` (SRS → ADR → SDD → Build → QA)
- **Batch of fixes/errors** → use `/fix` (parallel fix agents with triage)
- **Quality review of existing code** → use `/audit` (4 specialist reviewers)
- **Need to understand before building** → use `/investigate` first

## Evals — see EVALS.md (dev-time only)

## References

- Orchestration patterns: `gestalt/.cursor/references/orchestration.md`
- Verification procedures: `gestalt/.cursor/references/verification.md`
