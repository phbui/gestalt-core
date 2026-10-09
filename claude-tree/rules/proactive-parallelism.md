# Proactive Parallelism

Single agent is the default: most coding-shaped work (build a feature, fix a bug, edit related files) is sequential and cheaper done directly. Fan out only when subtasks are genuinely independent AND the value justifies the cost — multi-agent runs 3-15x the tokens of a single agent, and coding tasks have fewer truly parallelizable pieces than research does ([Anthropic: multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system); [when and how to use multi-agent systems](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them)). Breadth fan-out is endorsed for research, audit, and verification — independent lookups/checks across files, repos, or claims — where parallelism pays for itself.

## Decision Heuristic

Before starting any multi-step task, ask:

1. **Are there 2+ subtasks?** (If only 1, just do it yourself.)
2. **Are they independent?** (No subtask needs another's output to start.)
3. **Is each subtask non-trivial?** (Reading one file doesn't warrant an agent.)
4. **Does the value justify 3-15x tokens?** (A cross-repo audit or N-way verification does; a single-file edit or quick lookup doesn't.)

If all four → spawn agents. If the task is coding-shaped and sequential, or you're unsure the value clears the cost bar → default to a single agent.

## Concurrency Limits

Up to 6 subagents per message (3-5 for Agent Teams builder+verifier work). Work exceeding 6 subtasks batches into sequential waves, passing context forward.

## Agent Selection and Patterns

`Explore` only for narrow lookups, `general-purpose` for anything that interprets, named gestalt agents by name. `sonnet` for judgment, writing and research, `haiku` only for deterministic lookups, `inherit` for orchestration. The strict type and model matrix is the `/prompt` skill, Step 4. Partition research and audit work by file group, repo or source, one agent each. Worker prompts follow `gestalt/.claude/references/orchestration.md`.

## Skill Suggestion

When the request closely matches a gestalt skill, suggest it — don't auto-invoke:

| Intent pattern | Skill |
|---|---|
| Deep investigation, "why does X", "trace how" | `/investigate` |
| Architecture trade-off, "should we use X" | `/discuss` |
| 3+ independent fixes, error lists, review comments | `/fix` |
| Build from spec/description, "create X" | `/build` |

Never auto-invoke skills costing >10K tokens without confirmation. For simple, scoped requests, just do the work directly.

## When NOT to Parallelize

- Subtasks have sequential dependencies (one's output feeds the next)
- Total work is a single simple step, or coding-shaped with few genuinely independent pieces
- You need interactive user input between steps, or the task is conversational

## Synthesis

After all agents return, you — the orchestrator — merge findings into a unified view, resolve conflicts between agent outputs, present a cohesive result, and act on findings (fix, write, update) yourself or via a second wave.
