# Claude Code Orchestration Patterns

Claude Code-specific patterns that extend the shared orchestration reference. These leverage features unique to Claude Code — Agent Teams, persistent memory, worktree isolation, lifecycle hooks, and fine-grained agent configuration.

Read `gestalt/.cursor/references/orchestration.md` first for shared patterns. This file adds Claude-specific enhancements.

## Contents

- [Agent Teams vs Subagents](#agent-teams-vs-subagents)
- [Worktree Isolation](#worktree-isolation)
- [Persistent Agent Memory](#persistent-agent-memory)
- [Lifecycle Hooks](#lifecycle-hooks)
- [Agent Configuration Best Practices](#agent-configuration-best-practices)
- [Opus-Tier Configuration Footguns](#opus-tier-configuration-footguns-re-verified-on-opus-48fable-5-2026-07-02-opus-47-footguns)
- [Concurrency](#concurrency)
- [Checkpointing Before Retry](#checkpointing-before-retry)
- [Background Agents](#background-agents)

## Agent Teams vs Subagents

Claude Code offers two orchestration modes. Choose based on the task.

| Dimension | Subagents (Agent tool) | Agent Teams |
|-----------|----------------------|-------------|
| Communication | Results return to orchestrator only | Teammates message each other directly via mailbox |
| Coordination | Orchestrator manages all work | Shared task list with self-claiming |
| Context | Results compressed into caller's window | Each teammate has a full independent context window |
| Concurrency | Max ~6 per wave (practical limit) | 3-5 teammates recommended; no hard limit |
| Isolation | Shared workspace (or `isolation: worktree`) | Each teammate is a full Claude Code session |
| Best for | Focused scan-and-return tasks | Complex work where agents need to iterate or debate |

### When to Use Agent Teams

- `/implement` Phase 4+5: Builder and verifier agents benefit from direct communication — the verifier can message the builder about a failed check without round-tripping through the orchestrator. The verifier MUST enumerate all requirements from the original prompt and verify each one independently. The verifier does NOT trust the builder's self-report — it reads the code directly.
- `/build` with 3+ parallel workstreams: Teammates can coordinate on shared interfaces.
- `/review` when entries have dense cross-references: Verifiers can share findings about linked entries.
- Any workflow where the orchestrator bottlenecks synthesis.

### When to Use Subagents

- `/learn` scanners: Pure scan-and-return. No inter-agent coordination needed.
- `/investigate` probes: Independent research on different angles.
- Staleness detection: Simple git comparison, no collaboration value.
- Any read-only reconnaissance or single-purpose task.

### Agent Teams Configuration

Start with **3-5 teammates, 5-6 tasks each**. Three focused teammates outperform five scattered ones.

Task list design:
- Each task has a clear deliverable and explicit file ownership
- Use `blocked_by` for dependency ordering
- Tasks auto-unblock when dependencies complete
- Teammates self-claim tasks — no assignment needed

A task is VERIFIED only when:
- The builder agent's output addresses ALL enumerated requirements
- The verifier agent's matrix has one row per requirement with PASS status
- The Stop hook passes (lint/typecheck clean)
All three conditions are mandatory. Missing any one = task NOT complete.

Example task list for `/implement` Phase 4:
```
Task 1: Build workstream A (files: src/a/*)        — no dependencies
Task 2: Build workstream B (files: src/b/*)        — no dependencies
Task 3: Integration wiring (files: src/index.ts)   — blocked_by: [1, 2]
Task 4: Verify workstream A against SRS FR-001–005 — blocked_by: [1]
Task 5: Verify workstream B against SRS FR-006–010 — blocked_by: [2]
Task 6: Integration test + final audit             — blocked_by: [3, 4, 5]
```

## Worktree Isolation

Use `isolation: worktree` when spawning subagents that write code in parallel. Each agent gets a temporary git worktree — a full isolated copy of the repo.

Benefits over logical file-cluster partitioning:
- No race conditions on shared files
- Each agent can run tests against its own changes
- Changes merge cleanly via git
- Worktree auto-cleaned if agent makes no changes

When to use:
- `/build` with parallel builder agents modifying different directories
- `/fix` with parallel fix agents across different file clusters
- `/implement` Phase 4 builder agents
- Any scenario where 2+ agents write to the same repo

When NOT to use:
- Read-only agents (explore, staleness-detector) — no benefit
- Single-writer scenarios — overhead not justified
- Agents modifying gestalt knowledge files — these don't benefit from git isolation

Example:
```
Agent(subagent_type="general-purpose", isolation="worktree", prompt="""
CONTEXT: ...
YOUR TASK: Build workstream A
You may modify any files in src/a/ and tests/a/.
""")
```

### Verify Worktree Writes Post-Merge

A worktree-isolated agent's success report describes its **intent**, not the merged state of the main repo. The worktree path is ephemeral — `cp` from a stale or already-cleaned worktree silently produces a no-op even when `cp` exits 0. Two failure modes seen in practice:

1. **Hallucinated edits.** The agent's transcript reads as though it ran `Edit`, but the file in the worktree never actually changed. Worktree gets cleaned. The orchestrator's `cp $worktree/file $repo/file` succeeds because the source file exists — it's just identical to the destination.
2. **Worktree cleanup races the merge.** Agent finishes, returns its `worktreePath`, but Claude Code's auto-cleanup (when the agent makes "no changes") triggers before the orchestrator's `cp`. Then the path is gone or stale.

After every wave of worktree-isolated writers, verify the merged state before moving on:

```bash
# Verify the expected files actually changed in the main repo
cd $REPO && git diff --stat -- file_a file_b file_c
```

If a file you expected to see modified is missing from `git diff --stat`, the worktree change did not land. Common recovery:
- Read the file directly in the worktree (if it still exists) and `cp` it explicitly
- If the worktree is gone, re-apply the change yourself with `Edit` from the agent's reported intent
- Spawn a fresh agent without worktree isolation as a fallback

**Rule:** worktree-agent reports + post-merge `git diff --stat` together = trust. Worktree-agent reports alone = don't trust. Treat the verifier wave as load-bearing, not ceremonial.

## Persistent Agent Memory

Add `memory: project` to agents that benefit from cross-session learning. This loads the first 200 lines of `.cursor/agent-memory/MEMORY.md` into the agent's context.

| Agent | Memory? | What It Remembers |
|-------|---------|-------------------|
| learner | `memory: project` | Repo-scanning heuristics, recurring patterns, skip-file lists |
| reviewer | `memory: project` | Previously verified/false claims, common staleness patterns |
| builder | `memory: project` | Implementation patterns, build tool quirks, test framework conventions |
| designer | `memory: project` | Design patterns that worked, component naming conventions |
| verifier | No | Each verification must be independent and skeptical |
| staleness-detector | No | Stateless by design — compares git state, no memory needed |
| syncer | No | External sources change frequently; stale memory would mislead |
| researcher | No | Each research question needs fresh, unbiased exploration |

Agents that verify or research should NOT have memory — it could introduce confirmation bias.

## Lifecycle Hooks

Use hooks for quality gates that should run automatically, not as manual agent steps.

### Builder Agent Stop Hook

Run lint + typecheck when a builder agent finishes, before returning results:

```json
{
  "hooks": {
    "Stop": [{
      "type": "command",
      "command": "cd $PROJECT_DIR && task lint 2>&1 | head -50; task typecheck 2>&1 | head -50",
      "timeout": 30000
    }]
  }
}
```

This catches quality issues at the source rather than in a separate verification wave.

### PreToolUse Guard for Read-Only Agents

Prevent read-only agents from accidentally modifying files:

Scanner and verifier agents should use `disallowedTools` in their agent definition rather than hooks — it's more explicit. But for dynamic guards (e.g., blocking writes to specific paths), use a `PreToolUse` hook.

## Agent Configuration Best Practices

### Least-Privilege Defaults

Every agent definition should specify the minimum capabilities needed:

```yaml
# Read-only scanner
disallowedTools: [Write, Edit, NotebookEdit, Bash]
permissionMode: plan

# Builder (needs write access)
permissionMode: acceptEdits

# Verifier (reads code, runs tests)
disallowedTools: [Write, Edit, NotebookEdit]
permissionMode: default
```

### Model Selection (Claude Code Specific)

Claude Code model options use semantic aliases: `haiku`, `sonnet`, `opus`, `inherit`. Each has a 200K base variant and a 1M-context variant (example IDs — check current model list; as of 2026-07 the lineup is Fable 5 / Opus 4.8 / Sonnet 4.6 / Haiku 4.5 — e.g. the Opus alias maps to the current Opus-tier model, the `[1m]` suffix selects the extended-context variant).

| Agent Role | Model | 1M variant? | Rationale |
|-----------|-------|---|-----------|
| Scanners (learner, staleness-detector) | `haiku` | no | High-volume reading, low complexity; bounded per-file scope |
| Verifiers (reviewer, verifier) | `haiku` | no | Checklist comparison, not creative work; bounded per-entry |
| Syncer | `haiku` | no | MCP queries and comparison |
| Builder | `sonnet` | only if cross-package SDD >100K tokens | Code generation requires capability |
| Designer | `sonnet` | only if multi-system SDD spans >100K tokens of inputs | Architectural reasoning |
| Researcher | `sonnet` | rarely — only when option evaluation needs cross-document retrieval >100K tokens | Option evaluation, trade-off analysis |
| Lead orchestrator (Agent Teams) | `inherit` | matches session | Uses the session's model (typically opus) |

**1M-variant selection rule:** Default to the 200K base. Only choose the `[1m]` variant when the worker genuinely needs cross-document retrieval or to hold a large codebase in context simultaneously. The 1M variants operate in an extended-RoPE positional regime that introduces position-dependent logit noise even at 10–20% fill (ref arXiv 2602.10959; the-relevant-entry). Cost is identical, but hallucination risk is higher.

## Opus-Tier Configuration Footguns (re-verified on Opus 4.8/Fable 5, 2026-07-02) ^opus-47-footguns

Originally documented for Opus 4.7 (released 2026-04-16); re-verified 2026-07-02 against Opus 4.8 and Fable 5 against primary sources (platform.claude.com / anthropic.com). Apply these rules when spawning Opus-tier or Fable 5 subagents — Fable 5 diverges on some points, called out inline.

### Thinking configuration

- **Manual `budget_tokens` returns 400 errors.** Adaptive thinking (`thinking: {type: "adaptive"}`) is the only supported mode. `thinking: {type: "enabled", budget_tokens: N}` returns a 400 on Opus 4.7, Opus 4.8, Fable 5, Mythos 5, and Sonnet 5 — deprecated but still functional on 4.6-era models (Opus 4.6, Sonnet 4.6). [VERIFIED] https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
- **Effort defaults have changed twice in 2026 — pin effort explicitly, don't rely on the default.** A regression introduced 2026-03-04 silently dropped the default effort to `medium`; it was reverted 2026-04-07. Current documented defaults: Opus 4.7 defaults to `xhigh` specifically in Claude Code; all other models/surfaces default to `high`. Opus 4.8 defaults to `high` on all surfaces; Fable 5 defaults to `high`. Effort levels were also recalibrated on 4.8: `medium` thinks somewhat more, `high` somewhat less, and `xhigh` substantially more than the equivalent 4.7-tier levels. Because the default has moved twice this year, set `output_config.effort` explicitly on every subagent spawn rather than trusting the default. [VERIFIED] https://www.anthropic.com/engineering/april-23-postmortem ; https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-8 ; https://platform.claude.com/docs/en/build-with-claude/effort
- **Thinking blocks default to `display: "omitted"`** (silent change from the 4.6-era default of `"summarized"`). This applies to Opus 4.7, Opus 4.8, Fable 5, Mythos 5, and Sonnet 5. If your orchestrator inspects thinking content for routing, audit, or debugging, set `display: "summarized"` explicitly — otherwise you receive empty strings. [VERIFIED] https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
- **Always round-trip the full thinking block (including `signature`)** in tool-result continuations. Stripping it breaks interleaved-thinking continuity and is a documented silent failure mode.

### Tool-choice footguns

- **Forcing tool use with thinking enabled returns a 400 error — it does not silently disable thinking.** `tool_choice: {type: "any"}` or `tool_choice: {type: "tool", name: "..."}` combined with `thinking` enabled errors out rather than quietly dropping the reasoning step. Only `tool_choice: {type: "auto"}` and `tool_choice: {type: "none"}` are compatible with thinking enabled. Use `tool_choice: auto` for any thinking-enabled agent that also has tools. [VERIFIED] https://platform.claude.com/docs/en/build-with-claude/extended-thinking

### Instruction-following change

- **The Opus lineage follows instructions more literally, particularly at lower effort levels.** This is documented for both Opus 4.7 and Opus 4.8 as a general characteristic, not a medium-effort-specific one — the model will not silently generalize an instruction from one item to another and will not infer requests you didn't make. For sub-agents that must execute a fixed sequence, list every step explicitly — do not imply categories. [VERIFIED] https://platform.claude.com/docs/en/about-claude/models/migration-guide ; https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8
- **Not documented for Fable 5.** This literal-instruction-following characterization is specific to the Opus lineage (4.7/4.8) in current documentation — do not extend it to Fable 5 subagents without separate verification.

### Fable 5 specifics

- **Adaptive thinking is always on and cannot be disabled.** Omitting `thinking` (or passing `{type: "adaptive"}` explicitly) runs adaptive thinking; an explicit `{type: "disabled"}` returns a 400. There is no thinking-off mode for Fable 5 subagents.
- **Effort defaults to `high`.** Per the docs, lower effort settings on Fable 5 still perform well and often exceed `xhigh`-level performance on prior models — a `low`/`medium` Fable 5 subagent is not necessarily a weaker one.
- **Thinking display defaults to `"omitted"`**, same as the 4.7/4.8 family (see Thinking configuration above).
- **The raw chain of thought is never returned**, under any `display` setting — `"summarized"` gives a readable summary; `"omitted"` gives an empty string. Do not build orchestrator logic that expects raw reasoning text from a Fable 5 subagent.

[VERIFIED] https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking ; https://platform.claude.com/docs/en/build-with-claude/effort

### Task budgets (beta `task-budgets-2026-03-13`)

For long agentic loops, consider passing `output_config: {task_budget: {type: "tokens", total: N}}`. The model sees the countdown and uses it to scope/prioritize — and a model that knows it has consumed 900K of a 1M budget is materially less likely to fabricate filling gaps than one with no countdown awareness. Caveat: too-tight budgets cause the model to complete tasks less thoroughly (Anthropic's own warning).

### Subagent context-prompt isolation

Per Anthropic Managed Agents (beta `managed-agents-2026-04-01`): each subagent "runs in its own session thread, a context-isolated event stream with its own conversation history. Tools and context are not shared." **The coordinator's system prompt does not propagate to workers.** Implication: any grounding contract, citation rule, or VERIFICATION GATE the orchestrator wants enforced on workers MUST be embedded in each generated worker prompt directly — not assumed-inherited from the parent. See `gestalt/.cursor/skills/prompt/SKILL.md` §Step 4b Discipline Block for the embedded contract template.

(Cross-reference the-relevant-entry for the full root-cause analysis and mitigation matrix.)

### Max Turns

Set `maxTurns` to prevent runaway agents:

| Agent | maxTurns | Rationale |
|-------|----------|-----------|
| Scanner/learner | 30 | Repo scan is bounded |
| Reviewer | 40 | May need to check many files |
| Builder | 50 | Implementation can be long |
| Verifier | 30 | Verification is bounded |
| Staleness-detector | 20 | Git operations are quick |

## Concurrency

The shared orchestration reference says max 4 (Cursor's limit). Claude Code can go higher.

**Claude Code practical limits:**
- Subagents: Up to 6 per wave (beyond this, diminishing returns and context management overhead)
- Agent Teams: 3-5 teammates (Anthropic's recommendation)

For most gestalt operations, 4 remains a good default. Raise to 6 for:
- `/review` with 25+ entries (bigger batches per verifier)
- `/learn` on a large monorepo (extra scanner roles)

## Checkpointing Before Retry

For long-running agents (builder, learner), save intermediate state before retry:

1. Agent writes findings to a checkpoint file: `.cursor/agent-memory/checkpoint-{task}.md`
2. Retry prompt includes: "Resume from checkpoint at {path}. Do not repeat completed work."
3. Checkpoint is deleted after successful completion.

This prevents full restarts on transient failures.

## Background Agents

Use `run_in_background: true` for non-blocking parallel work that doesn't need immediate results:

- Long-running builds while the user continues other work
- Verification passes that take time but aren't blocking
- External source scanning (MCP queries can be slow)

The orchestrator is notified when background agents complete. Do NOT poll — wait for the notification.
