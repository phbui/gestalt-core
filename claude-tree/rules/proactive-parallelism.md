# Proactive Parallelism

Fan out whenever a turn holds two or more independent subtasks, each more than one file read. Token cost is not a gate. Phi runs Max plus API credits and wants speed (2026-11-05). Breadth fan-out pays most in research, audit and verification. Evidence, full text and worker-prompt structure: `gestalt/.claude/references/proactive-parallelism.md` and `references/orchestration.md`.

## Decision Heuristic

Run this check on every turn, not only at task start. A follow-up on an existing artifact ("also add X", "update Y and Z") is a new task. Ask: (1) Are there 2+ subtasks? (2) Are they independent, with none needing another's output to start? (3) Is each more than reading one file? If all three, spawn agents and say so in one line. An earlier "direct execution" verdict binds only its own turn. The Agent tool's "when in doubt, don't spawn" is the harness default. This rule overrides it.

## Limits and Selection

Up to 6 subagents per message (3-5 for Agent Teams builder+verifier work). Beyond 6 subtasks, batch into waves and pass context forward. `Explore` only for narrow lookups, `general-purpose` for anything that interprets, named gestalt agents by name. `sonnet` for judgment, writing and research, `haiku` only for deterministic lookups, `inherit` for orchestration. The strict type and model matrix is the `/prompt` skill, Step 4. Partition research and audit work by file group, repo or source, one agent each.

## Skill Suggestion

When the request matches a gestalt skill, suggest it and do not auto-invoke: deep investigation, "why does X", "trace how" → `/investigate`; architecture trade-off, "should we use X" → `/discuss`; 3+ independent fixes, error lists, review comments → `/fix`; build from spec or description → `/build`. A `/skill` token typed inside a request is an invocation, not a hint.

## When Not To, and Synthesis

Do not parallelize sequential dependencies, a single simple step, or work that needs interactive input between steps. After all agents return, merge findings into one view, resolve conflicts, present one result, and act on findings yourself or via a second wave.
