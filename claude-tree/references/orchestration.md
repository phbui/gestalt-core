# Shared Orchestration Patterns

Patterns used by skills that spawn parallel agents. Skills reference this file instead of repeating these inline. These patterns are provider-neutral — they work in both Claude Code and Cursor.

For Claude Code-specific enhancements (Agent Teams, worktree isolation, memory, hooks), see `gestalt/.claude/references/claude-orchestration.md`.

## Contents

- [Planner-Worker Hierarchy](#planner-worker-hierarchy)
- [Agent Concurrency](#agent-concurrency)
- [File-Cluster Partitioning](#file-cluster-partitioning)
- [Model Selection](#model-selection)
- [Read-Only Agents](#read-only-agents)
- [Fix Batch Ordering](#fix-batch-ordering)
- [Large-Scale Code Audit](#large-scale-code-audit)
- [Orchestration Rules](#orchestration-rules)
- [Worker Prompt Structure](#worker-prompt-structure)
- [Prompt Hardening Rules](#prompt-hardening-rules)
- [Error Escalation](#error-escalation)
- [Git Pre-Flight](#git-pre-flight)
- [Skip Directories](#skip-directories)

## Planner-Worker Hierarchy

You (the orchestrator) are the planner. Workers are stateless specialists — they receive full context, produce findings, and return. They do not coordinate with each other. You handle all integration, triage, and prioritization. Never delegate decision-making to workers.

**Exception (Claude Code only):** When using Agent Teams, teammates coordinate via a shared task list and mailbox messaging. The orchestrator sets up the task list and team composition, then teammates self-coordinate. See `claude-orchestration.md` §Agent Teams for when to use this pattern.

## Agent Concurrency

Default: **max 4 concurrent agents** per wave. This is Cursor's hard limit and a safe default for Claude Code.

Claude Code can go up to **6 subagents per wave** or use **Agent Teams (3-5 teammates)** for higher parallelism. See `claude-orchestration.md` §Concurrency.

**Note on the three concurrency numbers in the system:**
- **4 agents** — Cursor's hard limit; safe cross-tool default for any skill targeting both providers
- **6 agents** — Claude Code subagent ceiling per wave; use when writing Claude Code-only skills
- **5 agents** — Agent Teams recommended roster size (Claude Code Agent Teams feature, peer-to-peer)

When writing a skill, use 4 as your default unless you know the skill is Claude Code-only. See `claude-orchestration.md` for Claude-specific guidance.

If work requires more than the per-wave limit:
- Batch into waves
- Process waves sequentially
- Pass updated context between waves

## File-Cluster Partitioning

When multiple agents modify code in parallel:
- **Partition by file, not by concept.** Each agent owns a distinct set of files.
- **Explicit file ownership** — every agent prompt lists the files it may modify. Include: "You may ONLY modify these files: [list]."
- **Shared-file rule:** Fixes touching the same file go in the same cluster.
- **Dependency rule:** Dependent fixes go in the same cluster or in ordered waves.
- **Balance** cluster sizes roughly evenly.

## Model Selection

- **Fast/cheap** — scanner, staleness-detector, learner agents (pure pattern matching, no cross-file synthesis)
  - Claude Code: `model: haiku` or use `subagent_type="explore"` (haiku by default)
  - Cursor: `model: fast`
- **Capable** — reviewer, verifier, syncer, builder, designer, researcher agents (require judgment, multi-file synthesis, or MCP reconciliation)
  - Claude Code: `model: sonnet` or `model: inherit`
  - Cursor: `model: inherit`
- Use `explore` for read-only reconnaissance and triage
- Use `generalPurpose` (Claude Code) or `Task()` (Cursor) for multi-step work that doesn't map to a named agent

## Read-Only Agents

For agents that only read, search, scan, or verify (no writes):

- **Cursor:** Add `readonly=true` to `Task()` calls to restrict file write tools
- **Claude Code:** Use named agents with `disallowedTools: [Write, Edit, NotebookEdit]` — see agent definitions in `.claude/agents/`
- Use `model: "fast"` (Cursor) or `model: haiku` (Claude) for scanner, reviewer, verifier, staleness-detector, learner, syncer agents

The `readonly` constraint blocks file write operations at the platform level. It does NOT restrict MCP tool calls — agents with MCP access can still call write-capable MCP tools (e.g., Slack post, Linear create). For MCP-accessing agents, add explicit instructions in the prompt to only use read-only MCP tools.

**When to restrict writes:**
- Investigation/exploration agents (Code Tracer, Docs Scanner, Gap Finder)
- Verification agents (Regression Checker, Spot-Check, Lint Checker)
- Scanner agents (Learner, Syncer, Staleness-Detector, Reviewer)
- Evidence-gathering agents (Web Researcher, MCP Context Scanner)

**When NOT to restrict:**
- Builder agents that implement code
- Designer agents that create SDDs
- Researcher agents that draft ADRs
- Fixer agents that apply code changes
- Shell agents that run diagnostic commands (shell agents have their own constraints)

## Fix Batch Ordering

When applying fixes from an audit or review:
1. **Structural** — architecture, integration, wiring (changes WHERE code lives)
2. **Quality** — DRY, dead code, maintainability (changes WHAT code looks like)
3. **Optimization** — performance, rendering, data fetching (changes HOW code performs)

Each batch completes before the next starts. Within a batch, agents work on non-overlapping file clusters.

## Large-Scale Code Audit

When an agent is asked to review added code (PR diff, refactor sweep, audit), hard ceilings are mandatory. Without them, agents fabricate findings — primary research shows fabrication rate of 30–50% when scope exceeds the cliff.

### Hard Ceilings (per agent, non-negotiable)

| Limit | Value | Source |
|---|---|---|
| Lines per agent | ≤2,500 added lines (~25K tokens) | Anthropic context engineering + Stanford Lost-in-the-Middle (arxiv 2307.03172) + Adobe RULER |
| Tool calls per agent | 3–15 by complexity (NEVER target finding count) | Anthropic Multi-Agent Research System |
| Files per agent | ≤20 unless each is small (≤100 lines) | Empirical (this repo, post-mortem of 16k-line audits) |

If the diff exceeds these limits, **partition into more agents** — never raise the ceiling. Partition by file ownership; agents that share files do not run in parallel.

### Two-Pass Audit Pattern (mandatory)

**Pass 1 — Mechanical sweep:** `rg -n <pattern>` over the diff. Record exact `file:line:matched_text` triples. No interpretation.

**Pass 2 — Context read:** For each Pass-1 hit, run `git diff <base>..HEAD -- <file>` and read 5 lines of context around the hit. Confirm the pattern is real bloat, not load-bearing code.

Agents skipping Pass 1 and going straight to "read every line" are the failure mode that produces fabricated findings.

### Proof-of-Work Rule

Every reported finding MUST include the exact `+`-prefixed diff line that triggered it, copied verbatim from tool output:

```
### <file>:<line_range>  [issue_type]  [confidence: verified|suspected]
**Evidence (from `git diff` output):**
```
+    <exact line copied from diff>
```
**Reason:** <one sentence>
**Action:** <remove | inline | replace>
```

Confidence tags:
- `[verified]` — Pass-1 grep confirmed AND Pass-2 context-read confirmed
- `[suspected]` — Pattern-inferred without grep confirmation; orchestrator must re-verify before acting

Findings without an Evidence quote are fabrications. Drop them.

### Permission to Abstain

Every audit prompt MUST include: "If after the two-pass review you find no issues in your scope, return `NO FINDINGS`. This is acceptable and preferred over speculation." (Anthropic Building Effective Agents — "give the LLM a way out").

### Forbidden Prompt Language

Never include these phrasings in audit-agent prompts — they convert uncertainty into fabrication:

- ❌ "Target 30-100+ findings" / "Find at least N issues"
- ❌ "Be exhaustive — find everything"
- ❌ "Read every line" without a line ceiling
- ❌ "If you find fewer than X across N lines, you didn't read deep enough"
- ❌ "List every instance of unnecessary code" without grep-first instruction

Replace with:
- ✅ "Apply the Two-Pass Audit Pattern. Report only findings with `[verified]` evidence."
- ✅ "If grep returns no hits for a pattern, omit it. Do not synthesize candidates."
- ✅ "Tool calls budget: 3–15. Spend them on grep + context reads, not fabrication."

### Disprove-the-Finding Validator (optional, high-stakes audits)

For irreversible refactors driven by audit findings, spawn a separate validator subagent per finding cluster. Its only job: attempt to disprove each finding by re-reading the cited line and checking whether the issue is real or a load-bearing pattern. Findings that survive disproof advance to fix batches; the rest are dropped. (Pattern: Claude Code code-review plugin.)

## Fan-Out Budget & Synthesis Discipline

Adopted 2026-08-30 from gbrain's $50 cost-explosion post-mortem (knowledge/gbrain.md ^cost-explosion) — their doctrine "every LLM-calling function needs a budget," translated to prose-level controls since the harness offers no BudgetTracker primitive. Prose is the control class that failed gbrain pre-incident, so treat these as review checkboxes, not guarantees:

- **State the fan-out budget before spawning.** Every multi-agent wave declares, in the Team Manifest or the message launching it, the agent count and per-agent tool-call ceiling — and the estimate must come from *actual data cardinality* (files counted, lines measured), never from a configured parameter alone. gbrain's 53x overrun was an estimator trusting `m=12` while the real cardinality was ~2,000.
- **Chunk synthesis at a fixed batch size.** When aggregating N worker outputs (or N findings) through a further LLM pass, never concatenate all N into one call — fix a batch size (~10-20 findings or ~2,500 lines per call, matching the audit caps above) and merge pairwise/hierarchically. Any single call's input must be bounded regardless of total volume.
- **Declare degraded results.** If any worker failed, abstained, was cut short, or a synthesis batch was dropped, the final result carries an explicit `DEGRADED: <what is missing>` line — never a silently complete-looking answer over partial inputs.
- **Checkpoint before synthesis.** Worker outputs are already on disk (task output files); never re-run completed workers to recover from a failed synthesis step — resume from the failed unit only.

## Orchestration Rules

- **Partition by file, not concept.** Each agent owns a distinct set of files. No overlap.
- **No silently-optional agents.** If a skill defines N agents for a task type, all N are mandatory UNLESS the skill explicitly declares an agent conditional — in which case the orchestrator MUST state the skip decision and reason in its output (e.g. 'Agent 4 skipped — no internal context expected'). Orchestrators MUST NOT reduce agent count based on unstated judgment.
- **Exhaustive, not sampled.** Verification covers ALL items, not a spot-check sample. Every requirement, every file, every step.

## Worker Prompt Structure

Agent prompts follow a consistent pattern:

```
CONTEXT:
- {relevant paths, docs, findings}

YOUR TASK:
1. {specific task}
2. {specific task}

RULES:
- {constraints}

RETURN:
- {structured output format}

VERIFICATION GATE (mandatory — agent must complete before returning):
- Re-read YOUR TASK above. For each numbered step, confirm completion.
- Count completed steps vs total steps. If any were skipped, you are NOT done.
- Verify your RETURN output has every required field populated.
- If you cannot complete a step, you MUST explain why — do not silently skip it.
- Scope check (code-writing tasks): is anything present that no requirement asked for? Remove it, or justify it against a specific requirement ID.
```

For code-writing workers, also embed the scope block from `scope-discipline.md` — it merges completeness and minimalism into one rule so they never compete as separate instructions — and the Code-Writing Ladder Block from `discipline-block.md` (stop at the first rung that holds: YAGNI → reuse → stdlib → native → dependency → one-liner → minimum code; never lazy about validation, error handling, security, accessibility).

For any worker that might produce something a human looks at — a render, plot, diagram, PDF, screenshot, report — also embed the **Artifact Block** from `discipline-block.md`. Subagents do not inherit the parent's `.claude/rules/`, so `artifact-links.md` is invisible to them unless you paste it.

## Prompt Hardening Rules

All worker prompts MUST follow these language rules to prevent shortcutting:

### Hard Language (mandatory)
- Use "MUST", "EVERY", "ALL", "NEVER" — not "should", "try to", "consider", "if possible"
- Use "You MUST fix EVERY issue" — not "Fix each issue completely"
- Use "ALL steps are mandatory" — not "Be thorough"
- Use "NEVER skip a step without explicit justification" — not "Note any gaps"

### Position-Aware Layout (anti-attention-decay)
Critical constraints go in TWO places — top and bottom of the prompt:
- Top: HARD RULES section before CONTEXT (most important constraints)
- Bottom: VERIFICATION GATE after RETURN (re-checks critical constraints)
Middle sections (CONTEXT, YOUR TASK) contain the work. Constraints bookend them.

### Completeness Markers
Every RETURN section MUST include:
- A count: "Total: N/N items addressed"
- A completeness declaration: "ALL requirements addressed: yes/no"
- If no: explicit list of what was skipped and why
- For code-writing workers: "Unrequested additions: none" (or list with justification) — completeness runs in both directions; see `scope-discipline.md`

### Anti-Satisficing
Never use language that permits partial completion:
- ❌ "Fix issues as needed" → ✅ "Fix ALL issues. None may remain."  
- ❌ "Note any findings" → ✅ "Document EVERY finding with file:line citation."
- ❌ "Use judgment" → ✅ "Follow this decision matrix: {table}"
- ❌ "Thoroughness level: very thorough" → ✅ "EVERY file in scope MUST be checked. Report coverage: N/N files."

## Error Escalation

| Severity | Action |
|----------|--------|
| Worker fails | Retry once, then report to orchestrator |
| Scope too large | Batch into waves, prioritize by impact |
| Cross-cluster effect | Orchestrator resolves between waves |
| Blocking conflict | STOP and escalate to user |
| Verification fails | Fix inline (max 1–2 retries), then escalate |
| Long-running agent fails | Save checkpoint before retry (Claude Code: see `claude-orchestration.md` §Checkpointing) |

## Git Pre-Flight

Before scanning or verifying a repo, check its git state:

1. `git -C <repo-path> branch --show-current` — current branch
2. `git -C <repo-path> rev-parse HEAD` — current commit hash
3. `git -C <repo-path> fetch --dry-run 2>&1` — check for remote updates
4. `git -C <repo-path> rev-list HEAD..@{u} --count 2>/dev/null` — commits behind remote

If behind remote, warn the user. If on a feature branch, also run `git diff main..HEAD --stat` and pass the delta to agents.

## Skip Directories

When scanning repos, skip these generated/vendored directories — infer from lock/config files instead:

`node_modules/`, `.venv/`, `venv/`, `.env/`, `__pycache__/`, `.tox/`, `.mypy_cache/`, `.pytest_cache/`, `.next/`, `.nuxt/`, `dist/`, `build/`, `.turbo/`, `.cache/`, `.git/`, `.terraform/`, `vendor/`, `target/`, `*.egg-info/`
