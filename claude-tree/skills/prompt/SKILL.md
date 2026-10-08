---
name: prompt
description: "Optimize a raw, vague, or underspecified request into a well-structured proper plan that leverages gestalt's full capabilities, then execute it after user confirmation."
model: sonnet
user-invocable: true
argument-hint: "raw request to optimize"
---

# Prompt

Optimize a raw request into a well-structured prompt that leverages gestalt's full capabilities, then execute it after user confirmation.

## Input

The user provides a raw request: `/prompt <their request>`

## Process

### Step 1: Analyze the Request

Parse the raw request. Identify:
- **Intent**: What does the user actually want? (build, investigate, fix, learn, discuss, etc.)
- **Scope**: Which repos, services, or systems are involved?
- **Complexity**: Single-step or multi-step? Independent subtasks?

### Step 2: Route, then Ground

`/prompt` is the general entry point to the whole system — the request arrives here
and leaves pointed at whichever skill, tool, or direct action actually fits. Routing
is therefore step one, not an afterthought.

Silently (without outputting to the user):

1. **Shortlist.** Call `gestalt_route(request)`. It returns
   `{skill, description, lines, rrf}` for the top candidates in ~1 ms, by lexical
   match over each skill's curated description and its indexed SKILL.md body.

   **A weak advisory signal, and you are the better router.** Measured 2026-08-11 on
   40 cases against the 21-skill catalog (`tests/test_routing.py`): lexical **28/40
   top-1, 31/40 top-3**; with `semantic=true`, **27/40 top-1, 34/40 top-3**. Prefer
   `semantic=true` here — /prompt's grounding step loads the embedding model via
   gestalt_search anyway, and top-3 is what you actually consume. Read the result as
   "have I forgotten a skill that fits?", then decide yourself from the descriptions —
   you can interpret intent and neither leg can. Roughly one in three top-1 answers
   is wrong, so never take the first row on faith.

   Residual failures are semantic-intent gaps ("poke holes in this plan" → discuss)
   that even dense embeddings miss. Do not spend effort tuning the router; spend it
   on reading the candidates.

   Never threshold on `rrf` — it is a rank-fusion number with no absolute meaning
   (see [[gestalt#^bm25-thresholds]]). Prefer direct execution whenever no skill
   genuinely earns its cost.

   If `gestalt_route` returns `[]` (index not built yet), fall back to reading
   `gestalt/.claude/references/skill-index.md`.

2. **Ground.** `gestalt_search` the request to find relevant knowledge entries — pass `semantic: true` (param added 2026-08-30): leaf nodes default to FTS-only, and a lexical-only grounding pass surfaces every entry sharing a proper noun with the query instead of the topically right ones (the campus VPN incident; falls back to FTS with a logged line when the local index has no vectors).
3. Read the `<gestalt-memory>` block (injected at session start) for relevant user preferences, pending items, or prior work on this topic
4. **Standing leverage — `research` and `investigate` are default-available**: treat these two gestalt skills as tools you reach for on your own judgment, not ones the user must request. If any part of the request would benefit from deep investigation ("why/how does X work", root-cause tracing) or external research (libraries, prior art, best practices, venues), fold that into the enhanced request or Team Manifest in Step 4 without being asked. The user should never need to say "use research/investigate as needed" — that instruction is now standing.

### Step 3: Detect Caveman Mode

Check if caveman mode is active. Indicators:
- User previously invoked `/caveman` this session
- User said "caveman mode", "less tokens", "be brief"
- The `<gestalt-memory>` user_preferences block mentions caveman preference

If caveman is active, set `caveman = true` for this prompt optimization.

### Step 4: Build Optimized Prompt

Construct an enhanced version of the request that includes:

**Context injection** (from gestalt):
- Relevant entry slugs the agent should read before starting
- Data flow connections from GRAPH.md if the task spans services
- Prior decisions or conventions from Letta memory

**Team Design** (dynamic — compose, don't preset):

**Gate first: is this direct-execution?** If the request is direct, mechanical, or single-step, state "Direct execution — no agents" and skip the rest of Step 4 entirely. Only proceed with scoring if multi-agent work is plausibly warranted. Spawning a team for trivial work wastes ~15x the tokens of a single call.

Reason about the task across 9 dimensions, then *derive* the team shape from the scores. Do not pick from a fixed strategy list.

Score the task:

| Dimension | Values | Drives |
|---|---|---|
| **Subtask predictability** | predictable / unknown | Static parallel plan vs orchestrator-workers |
| **Independence** | sequential / parallel-safe / mixed | Sequencing (waves vs parallel) |
| **Breadth** | 0 (direct) / 1 / 2-4 / 5-6 / >6 distinct angles | Agent count |
| **Reversibility** | reversible / irreversible | Whether to inject a verifier agent |
| **Code-write overlap** | none / single-file / multi-file | Worktree isolation |
| **Inter-agent collaboration** | one-shot / debate / handoff | Subagents vs Agent Team |
| **Hallucination risk** | low / medium / high | Discipline-block strength, CitationAgent injection, quote-extraction prerequisite, 1M-vs-200K context, effort level (see the-relevant-entry) |
| **Latency sensitivity** | blocking / async | Foreground vs `run_in_background`; whether to prefer fewer, faster agents |
| **Cost ceiling** | unconstrained / budget-capped | Model tier selection; agent count; whether to skip optional agents |

Derive team shape using these authoritative rules:

1. **Scale agent count to breadth** — 1 agent for fact-finding (3-10 tool calls), 2-4 for comparison/analysis, 5-6 for breadth research. Default 3-5; ceiling 6 subagents per wave. Above 6 → batch into sequential waves with context pass-through. (Anthropic multi-agent research system; Claude Code agent-teams)
2. **Pattern selection by predictability** — predictable subtasks → static parallel plan; unpredictable subtasks → orchestrator-workers (you decompose at runtime, possibly emitting a worker spec list). Always start simple — only escalate beyond a single agent when single-call underperforms.
3. **Subagents vs Agent Team** — subagents (default) for one-shot independent work that returns to you for synthesis; Agent Team only when teammates must message each other, debate, or share a task list.
4. **Agent type + model selection — strict rules** (this is where most hallucination originates; mismatched type/model produces fabricated constraints, see the-relevant-entry):

    **Agent type:**
    - `Explore` — ONLY for narrow targeted lookups answerable with grep + small reads (≤3 files, ≤200 lines each). Examples: "where is X defined", "which files reference Y", "does function A call B". NEVER assign to: code review, cross-file diff interpretation, multi-file consistency checks, error-trace synthesis, "read-and-interpret" work. Out-of-scope assignments cause Explore to fabricate scope constraints (e.g. "TEXT ONLY") and bail — observed 2026-05-19.
    - `general-purpose` — anything beyond narrow lookups: multi-file analysis, diff interpretation, web/MCP queries, code writing, synthesis. This is the default for non-trivial work.
    - Named gestalt agent (`builder`, `verifier`, `learner`, `researcher`, `designer`, etc.) — invoke by name; respect their declared model.

    **Model:**
    - `haiku` — ONLY for tasks with a deterministic answer where the model is not asked to decide anything: targeted grep, file listing, occurrence counting, "does X call Y? yes/no with file:line", mechanical verification. NEVER assign to: interpretation, synthesis, "explain why", multi-source merge, open-ended analysis. Haiku given judgment work fabricates excuses rather than attempting and failing.
    - `sonnet` — any task involving interpretation, synthesis, ambiguity, multi-step reasoning, or "explain". Default for all `general-purpose` agents unless the task is provably deterministic.
    - `opus` / `inherit` — orchestration of complex breadth, or workloads where Sonnet is documented to underperform. Anthropic measured 90.2% gain on breadth tasks with Opus-lead + Sonnet-workers vs single Opus.

    **Combined matrix:**
    | Task | Type | Model |
    |---|---|---|
    | "Where is X defined" / single-symbol lookup | Explore | (haiku-class default) |
    | "Grep for pattern, return matches" / mechanical count | general-purpose | haiku |
    | "Diff two branches, identify meaningful changes" | general-purpose | sonnet |
    | "Trace error through multiple files" | general-purpose | sonnet |
    | Web/MCP research with synthesis | general-purpose | sonnet |
    | Code writing | general-purpose | sonnet (+ worktree) |
    | Multi-source breadth research | general-purpose | sonnet (with opus orchestrator) |
5. **Isolation rule** — `isolation: worktree` whenever 2+ agents write code concurrently or any single agent makes irreversible multi-file changes; never for read-only research agents.
6. **Two-layer parallelism** — instruct each agent to also parallelize its internal tool calls (web fetches, file reads, MCP queries). Orchestrator parallelism alone leaves significant time on the table.
7. **Background mode** — `run_in_background: true` for any agent whose result you don't need before continuing; foreground for agents whose output blocks the next step.
8. **Verifier injection** — irreversible work (refactor, schema change, multi-file rewrite, gestalt index regeneration) gets a verifier agent at the end with separate context and no project memory.
9. **Partition by file, not concept** — when assigning scope, give each agent explicit file ownership to prevent overlap. Concept-based partitioning leads to overlap and gaps.
10. **Audit-type task detection** — if the task involves code review / audit / line-by-line examination AND (>20 files OR >3,000 added lines), the Team Manifest MUST follow the Large-Scale Code Audit pattern (`gestalt/.claude/references/orchestration.md` §Large-Scale Code Audit):
    - Cap each agent at ≤2,500 added lines (~25K tokens). Partition further if the diff is larger.
    - Tool-call budget: 3–15 per agent. Never set a target finding count.
    - Each agent prompt MUST require Two-Pass review (grep-then-read), CITATION-format evidence per finding, and `[verified]` / `[suspected]` confidence tags.
    - Each agent prompt MUST include the abstain rule: "Return `NO FINDINGS` if your scope is clean — preferred over speculation."
    - Forbidden phrasings (do NOT include in agent prompts): "target N findings", "be exhaustive", "find at least", "read every line" without a ceiling. These convert uncertainty into fabrication (primary research: Anthropic Multi-Agent Research System budgets tool calls, not findings).
    - For irreversible refactors driven by audit results, add a Disprove-the-Finding validator pass before fix batches.

Then emit a concrete **Team Manifest** with one row per agent:

```
Team Manifest:
- Agent N: <role> | type=<Explore|general-purpose|named> | model=<haiku|sonnet|opus|inherit>
  Scope: <files / topic boundary>
  Tools: <allowlist or "default">
  Isolation: <worktree | none>
  Mode: <foreground | background>
  Context: <200K | 1M>             ← default 200K; choose 1M only when scope demands >100K of input material
  Thinking: <adaptive | off>       ← adaptive for verification/synthesis on Opus-tier models; (re-verified on Opus 4.8/Fable 5, 2026-07-02) ← Opus-tier only; omit for Sonnet/Haiku agents
  Effort: <medium | high | xhigh>  ← xhigh for verifiers on Opus-tier models. Defaults are high (xhigh specifically for Opus 4.7 in Claude Code); Opus 4.8 and Fable 5 default to high on all surfaces. A medium-effort default regression shipped 2026-03-04 and was reverted 2026-04-07 — https://www.anthropic.com/engineering/april-23-postmortem — that is now historical context, not current state. Defaults have changed twice in 2026 — pin effort explicitly rather than relying on defaults.
  Discipline: <standard | strict>  ← strict = embed quote-extraction prerequisite + CitationAgent pass
  Returns: <expected output format, e.g., "numbered single-claim rows with file:line/URL citation slot per row">
- Agent N+1: ...

Sequencing: <all-parallel | sequential A→B→C | wave-1 [...] then wave-2 [...]>
Coordination: <subagents | Agent Team>
Rationale: <one line — which dimension scores drove this shape>
```

If 0 agents are warranted, state "Direct execution — no agents" and proceed inline. (The gate at the top of Step 4 should have caught this already.)

**Anti-pattern checklist** — before finalizing the manifest, verify NONE apply:

**HIGH — fabrication risk (never ship):**
- [ ] **Explore subagent assigned to multi-file diff, code review, or open-ended interpretation** — Explore declares this out-of-scope and fabricates constraints (e.g. "TEXT ONLY") to refuse. Use `general-purpose` + sonnet instead.
- [ ] **Haiku assigned to a task requiring judgment** (interpretation, synthesis, "explain why", ambiguity resolution) — Haiku fabricates excuses rather than attempting judgment work. Use sonnet.
- [ ] **Missing Discipline Block in generated worker prompts** — subagents do NOT inherit the parent system prompt (Anthropic Managed Agents docs); the orchestrator must embed grounding rules directly in each worker
- [ ] **Bullet-list RETURN for factual claims** — must be numbered single-claim rows with a per-item citation slot (bullet lists bypass per-claim verification; see the-relevant-entry)
- [ ] **"Target N findings" / "be exhaustive" / "find at least"** in any audit-agent prompt — quota framing produces fabricated findings; Anthropic budgets tool calls, not findings
- [ ] **Audit prompt without abstain permission** — every audit agent must be allowed to return `NO FINDINGS`
- [ ] **Audit task with >2,500 lines per agent** — exceeds context cliff (Stanford Lost-in-the-Middle); partition further

**MEDIUM — reliability/efficiency:**
- [ ] **1M context variant when 200K would suffice** — extended-RoPE regime degrades attention even at low fill; only choose 1M when scope demands >100K input tokens
- [ ] **Default `effort` on a verifier subagent on Opus-tier models** — defaults are high (xhigh specifically for Opus 4.7 in Claude Code; Opus 4.8 and Fable 5 default to high on all surfaces). A medium-effort default regression shipped 2026-03-04 and was reverted 2026-04-07 (historical context — https://www.anthropic.com/engineering/april-23-postmortem). Defaults have changed twice in 2026 — pin effort explicitly rather than relying on defaults; explicitly set `effort: "xhigh"` for verifiers and synthesizers
- [ ] **`tool_choice: any` or `{type: "tool"}` with thinking enabled** — returns a 400 error, not a silent disabling of thinking. `tool_choice: auto` and `tool_choice: none` are both compatible with thinking — use one of those for thinking-enabled agents
- [ ] Same-file writes across agents without worktree isolation
- [ ] Spawning a team for sequential or single-file work
- [ ] Multi-agent overhead without genuine parallelization payoff

**LOW — style/structure:**
- [ ] Fixed concurrency cap regardless of work size (size to actual breadth)
- [ ] Hardcoded role roster — every agent must trace to a dimension score
- [ ] Broadcast messaging in an Agent Team (partition explicitly by file/topic)
- [ ] Nesting Agent Teams (teammates can't spawn their own teams)
- [ ] Vague agent prompts — each must include objective, output format, tool guidance, scope boundary

**Constraints** (from user preferences in Letta):
- Terse responses, no trailing summaries
- Prefer efficiency — use appropriate model tiers (Haiku for scan, Sonnet for build, Opus for orchestration only)
- Follow existing codebase conventions

**Caveman propagation** (if `caveman = true`):
- Append caveman grammar rules to the enhanced prompt so subagents inherit the mode
- Include the caveman boundary rules (code normal, commits normal, PRs normal)
- Add gestalt write safeguard (see Step 4a)

### Step 4a: Gestalt Write Safeguard

If the execution strategy involves gestalt write operations, apply the `caveman-safeguard` rule (see `gestalt/.claude/rules/caveman-safeguard.md`). That rule classifies all operations as caveman-safe, caveman-suspended, or mixed — and defines the exact write register to use.

When a write operation is detected, include a brief safeguard note in the enhanced prompt:
```
GESTALT WRITES: caveman suspended per caveman-safeguard rule.
```

### Step 4b: Discipline Block (embedded in every worker prompt)

Subagents do **not** inherit the parent system prompt (Anthropic Managed Agents, beta `managed-agents-2026-04-01`). Parent-level hardening like CLAUDE.md rules, `cite-before-claim`, and `proactive-parallelism` are invisible to workers. The orchestrator MUST embed the grounding contract directly inside every generated worker prompt.

Read `gestalt/.claude/references/discipline-block.md` once and embed its Standard Block verbatim at the top of every generated worker prompt. Workers still receive the full block at dispatch — they don't inherit the parent prompt, so this is not optional boilerplate; it's the only grounding contract the worker will ever see.

For `Discipline: strict` (high hallucination risk), append:

```xml
<quote-extraction>
- BEFORE analyzing, extract word-for-word quotes from the provided materials that are
  relevant to your task. Number them.
- In your analysis, reference ONLY these numbered quotes by index.
- Any claim you cannot support with a numbered quote → mark [UNSUPPORTED] and exclude
  from the final return.
</quote-extraction>
```

### Step 4c: Hallucination Mitigations (decision matrix)

Map the **Hallucination risk** dimension score to concrete agent configuration:

| Risk | Discipline | RETURN format | Effort (Opus-tier models) | Context variant | CitationAgent pass |
|---|---|---|---|---|---|
| Low (single source, mechanical) | standard | numbered single-claim | medium (explicit — API default is high) | 200K | no |
| Medium (multi-source, synthesis) | standard | numbered single-claim | high | 200K | no |
| High (>20K input tokens OR cross-system factual claims) | strict (with quote-extraction) | numbered single-claim | xhigh | choose by scope | yes — separate downstream agent |

**Per-agent overrides:**
- Verifier-role agents on Opus-tier models: always `Effort: xhigh` regardless of risk score. Defaults are high (xhigh specifically for Opus 4.7 in Claude Code; Opus 4.8 and Fable 5 default to high on all surfaces) — a medium-effort default regression shipped 2026-03-04 and was reverted 2026-04-07 (historical context — https://www.anthropic.com/engineering/april-23-postmortem). Defaults have changed twice in 2026 — pin effort explicitly rather than relying on defaults; verifiers must opt in to xhigh explicitly.
- Synthesis-role agents that aggregate across 3+ workers: always inject a downstream **CitationAgent** whose sole job is to match every claim to a source location and return a claim-by-claim match table with `[VERIFIED]` / `[NO_SOURCE_FOUND — retract]` labels per claim (pattern: Anthropic Multi-Agent Research System).
- 1M-context selection: only when scope genuinely demands cross-document retrieval >100K tokens. Otherwise default to 200K — the 1M variant operates in an extended-RoPE positional regime that increases drift even at low fill (ref the-relevant-entry).

### Step 5: Present to User

Show the optimized prompt. If `caveman = true`, present in caveman style:

**Normal presentation:**
```
## Optimized Prompt

### Your Request
{original raw request}

### Context Added
- Read: entry-a, entry-b (relevant gestalt entries)
- Prior work: {from Letta memory, if any}
- Data flow: {from GRAPH.md, if cross-service}

### Team Manifest
{Concrete per-agent rows from Step 4: role | type | model | scope | tools | isolation | mode | returns}
{Sequencing: parallel | sequential | waves}
{Coordination: subagents | Agent Team}
{Rationale: one line — which dimension scores drove this shape}

### Enhanced Request
{The actual optimized prompt that will be executed}

Execute this? (y/n)
```

**Caveman presentation** (if `caveman = true`):
```
## Prompt

### Raw
{original}

### Context
- entry-a, entry-b
- Prior: {letta context, if any}
- Flow: {graph connections, if cross-service}

### Team
{compact manifest — one row per agent: role | type | model | scope}
Seq: <parallel|sequential|waves>
Coord: <subagents|team>
Why: <one-line rationale citing dimension scores>

### Safeguards
{if write ops detected:} Gestalt writes: caveman suspended for entry writes, active for chat
{if no write ops:} None needed — read-only

### Enhanced
{optimized prompt in caveman grammar}

Run? (y/n)
```

### Step 6: Execute or Revise

If the user confirms:
- If 0 agents (direct execution), execute the enhanced request inline
- If a single skill was recommended, invoke it with the enhanced context
- If a Team Manifest was emitted, spawn each agent per its row — type, model, scope, tools, isolation, foreground/background, sequencing — and respect the Coordination mode (parallel `Agent` calls in a single message for subagents; Agent Team setup if specified)
- If caveman is active and gestalt writes are involved, ensure the write safeguard is included in every agent prompt that touches gestalt files

If the user wants changes:
- Re-score the task across the 9 dimensions, regenerate the Team Manifest, then re-present

### Key Rules

- **Never auto-execute** — always confirm first
- **Don't add unnecessary complexity** — if the raw request is already clear and simple, say so and execute as-is
- **Compose, don't preset** — the team shape MUST be derived from task scores in Step 4, not picked from a fixed list. Every agent in the manifest must trace to a dimension score.
- **Gestalt lookup is silent** — the user MUST NOT see search/read operations, only the result
- **Preserve intent** — the optimized prompt MUST do what the user asked, not what you think they should ask
- **Be concise** — the optimization summary MUST be 5-10 lines, not a wall of text (the Team Manifest is allowed to expand this when the team is non-trivial)
- **Caveman safeguard** — follow `caveman-safeguard` rule for all gestalt interactions

ALL steps above are mandatory. Do not skip or abbreviate any step.

## Verification Gate

Before completing this skill:
- Re-read the task above. Confirm EVERY numbered step was addressed.
- Count: steps completed / total. If any skipped, state why explicitly.
- Verify all outputs have the required format and content.
- Verify the Team Manifest passes the anti-pattern checklist in Step 4.

## Evals — see EVALS.md (dev-time only)

## References

See `gestalt/.claude/references/agent-design-citations.md` for the external sources backing the team-design and hallucination-mitigation rules above.
