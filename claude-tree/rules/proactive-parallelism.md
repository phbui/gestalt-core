# Proactive Parallelism

Single agent is the default: coding-shaped work (build a feature, fix a bug, edit related files) is sequential and cheaper done directly. Fan out only when subtasks are genuinely independent AND the value justifies the cost, because multi-agent runs 3-15x the tokens of a single agent. Breadth fan-out pays for itself in research, audit and verification. Evidence links, the full text and the worker-prompt structure: `gestalt/.claude/references/proactive-parallelism.md` and `references/orchestration.md`.

## Decision Heuristic

Before any multi-step task, ask: (1) Are there 2+ subtasks? (2) Are they independent, with no subtask needing another's output to start? (3) Is each non-trivial, more than reading one file? (4) Does the value justify 3-15x tokens, as a cross-repo audit or N-way verification does and a single-file edit does not? If all four → spawn agents. If the task is coding-shaped and sequential, or you're unsure the value clears the cost bar → single agent.

## Limits and Selection

Up to 6 subagents per message (3-5 for Agent Teams builder+verifier work); work exceeding 6 subtasks batches into sequential waves, passing context forward. `Explore` only for narrow lookups, `general-purpose` for anything that interprets, named gestalt agents by name. `sonnet` for judgment, writing and research, `haiku` only for deterministic lookups, `inherit` for orchestration; the strict type and model matrix is the `/prompt` skill, Step 4. Partition research and audit work by file group, repo or source, one agent each.

## Skill Suggestion

When the request closely matches a gestalt skill, suggest it — don't auto-invoke: deep investigation, "why does X", "trace how" → `/investigate`; architecture trade-off, "should we use X" → `/discuss`; 3+ independent fixes, error lists, review comments → `/fix`; build from spec or description → `/build`. Never auto-invoke skills costing >10K tokens without confirmation. For simple, scoped requests, just do the work directly.

## When Not To, and Synthesis

Do not parallelize sequential dependencies, a single simple step, coding-shaped work with few independent pieces, or work that needs interactive input between steps. After all agents return, you merge findings into a unified view, resolve conflicts between agent outputs, present a cohesive result, and act on findings yourself or via a second wave.
