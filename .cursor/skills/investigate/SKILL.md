---
name: investigate
description: "Deep investigation or conceptual deep-dive using codebase and external research. Spawns parallel agents across code, gestalt, docs, and web; synthesizes findings with citations. Use to trace why something breaks, for bugs, technical decisions, or understanding how something works."
disable-model-invocation: true
---
<Background>@gestalt-grounding</Background>

# Investigate

Deep investigation of a specific issue or concept, grounded in both the codebase and external research. Spawns parallel agents to explore code, gestalt, documentation, and the web — then synthesizes a complete understanding with full citations.

Also handles **conceptual deep-dives** (previously `/understand`) — when the user wants to understand how something works rather than investigate a problem.

## Input

The user will specify something to investigate or understand. This could be:
- A bug or unexpected behavior ("investigate why snapshot messages drop during reconnect")
- An implementation question ("investigate how I'd add real-time presence to the PWA")
- A technical decision ("investigate whether we should use DuckDB or Polars for the transform layer")
- A system behavior ("investigate what happens when the agentic-backend hits the token limit")
- A concept to understand ("understand how the tool registry works", "understand how spatial results get into the platform")

## Persona

You are a senior engineer conducting a technical investigation. You combine deep codebase knowledge with external research to build a complete picture. You don't guess — you read code, trace execution paths, and cite sources. Your conclusions are evidence-based.

## Process

### Step 1: Frame the Investigation

Parse the user's request. Identify:
- **Subject** — what specific thing are we investigating or understanding?
- **Scope** — which repos, services, or layers are likely involved?
- **Question type** — categorize as one of:
  - `bug` — "why does X happen?"
  - `implementation` — "how would I do X?"
  - `decision` — "should I use X or Y?"
  - `behavior` — "what happens when X?"
  - `understand` — "how does X work?" (conceptual deep-dive)

Present the framing:
```
Investigating: {subject}
Scope: {repos/services involved}
Type: {bug | implementation | decision | behavior | understand}
Breadth: {narrow | standard | wide}

Launching investigation...
```

### Step 2: Ground in Gestalt (Orchestrator)

Follow the gestalt grounding procedure in `gestalt/.cursor/references/gestalt-grounding.md`. This establishes what you already know and identifies where to look in code.

**Breadth gate (before spawning):** Score the investigation's breadth before dispatching the roster below:
- **Narrow** (single file/function, one repo, answerable in <10 tool calls): collapse to 1 agent — Code Tracer only, or answer directly from gestalt grounding if sufficient. Skip agents whose scope is clearly empty.
- **Standard** (one service/subsystem): use the roster below as specified.
- **Wide** (cross-service, multi-repo, or landscape questions): split the Code Tracer by repo/service (up to 6 agents total per wave).
The mandatory-agent lists below apply to Standard breadth; Narrow and Wide override them. State the breadth score in the framing output.

### Step 3: Parallel Investigation (Up to 4 Agents)

Spawn agents based on the investigation type. Mandatory agents by investigation type:
- bug: Code Tracer + Docs Scanner + Web Researcher (3 agents)
- implementation: Code Tracer + Docs Scanner + Web Researcher (3 agents)
- decision: ALL 4 agents mandatory
- behavior: Code Tracer + Docs Scanner (2 agents minimum)
- understand: Code Tracer + Docs Scanner (2 agents minimum)

Before dispatching Agents 1-3 below, read `gestalt/.cursor/references/discipline-block.md` once and embed its Standard `<discipline>` Block verbatim at the top of each of their three prompts — subagents do not inherit this file, so the block must be copied in at dispatch time, not referenced by pointer. Each template below marks the spot with `[Embed the discipline-block.md Standard Block verbatim here]`.

**Agent 1 — Code Tracer (general-purpose):**
```
Agent(subagent_type="general-purpose", model="sonnet", prompt="""
[Embed the discipline-block.md Standard Block verbatim here]

You are tracing code related to an investigation.

SUBJECT: {subject}
SCOPE: {repos and likely directories}
GESTALT CONTEXT: {summary of relevant gestalt entries}

YOUR TASK:
1. Search the codebase for all code related to {subject}.
   Use semantic search, grep for key identifiers, and glob for related files.
2. Trace the execution path end-to-end:
   - Entry point (API route, event handler, CLI command, scheduled task)
   - Data transformations at each step (actual types and shapes)
   - Storage interactions (DB queries, file I/O, cache ops)
   - Error handling (try/catch, fallbacks, retries)
   - Exit points (responses, events emitted, side effects)
3. For bugs: identify where the expected behavior diverges from actual.
   For implementation questions: identify extension points and constraints.
   For decisions: identify current patterns that inform the choice.

CITATION RULES:
- Every code reference must include file path and line numbers.
- Quote actual code snippets for key logic.
- You MUST document ALL assumptions or ambiguities in the code.

RETURN: structured trace with file:line citations for every claim.
Tool-call budget: 3–15 tool calls. Prioritize the most relevant files; do not attempt exhaustive enumeration. If your scope has no relevant findings, return "NO FINDINGS for {scope}".

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**Agent 2 — Docs & Context Scanner (general-purpose):**
```
Agent(subagent_type="general-purpose", model="sonnet", prompt="""
[Embed the discipline-block.md Standard Block verbatim here]

You are scanning documentation and project context for an investigation.

SUBJECT: {subject}
SCOPE: {repos and likely directories}

YOUR TASK:
1. Search for documentation related to {subject}:
   - README files in relevant directories
   - ADRs and design docs in docs/
   - Pitch documents (SRS, SDD) if this was a planned feature
   - Code comments and docstrings
   - Test files (they document expected behavior)
   - Configuration files that affect this behavior
2. Check for prior art:
   - Git log for commits related to this area
   - TODO/FIXME/HACK comments near relevant code
3. You MUST document ALL discrepancies between docs and code — code is truth.

RULES:
- EVERY finding MUST include file:line citation.
- "No docs found" is a valid finding — but you MUST confirm by searching, not assuming.

RETURN: all relevant documentation findings with file:line citations.
Tool-call budget: 3–15 tool calls. Prioritize the most relevant files; do not attempt exhaustive enumeration. If your scope has no relevant findings, return "NO FINDINGS for {scope}".

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**Agent 3 — Web Researcher (general-purpose):**
```
Agent(subagent_type="general-purpose", prompt="""
[Embed the discipline-block.md Standard Block verbatim here]

You are researching external sources to support a codebase investigation.

SUBJECT: {subject}
TECHNICAL CONTEXT: {brief description of the system and stack}

YOUR TASK:
1. Search the web for information relevant to this investigation.
   Focus on:
   - Official documentation for libraries/frameworks involved
   - Known issues, bugs, or gotchas related to the subject
   - Best practices and recommended patterns
   - Stack Overflow / GitHub Issues with similar problems
   - Engineering blog posts covering this pattern
2. Try at least 3 different search queries to get diverse results.
3. For each useful source, fetch the page and extract key information.

CITATION RULES (mandatory):
- Every factual claim MUST have a source URL.
- Use inline citations: "FastAPI's dependency injection is request-scoped [1]"
- Collect all sources in a numbered reference list.
- If a claim has no source, mark it as [UNVERIFIED].

RETURN: findings with inline citations and numbered source list.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

**Agent 4 — MCP Context Scanner (general-purpose):**
Agent 4 is conditional: spawn it when the subject involves internal systems, internal decisions, or prior team work. When skipped, state "Agent 4 skipped — no internal context expected" in the framing output. (Conditional agents must be declared and their skip decision reported — see orchestration.md.)
```
Agent(subagent_type="general-purpose", prompt="""
You are scanning internal company sources for context on an investigation.

SUBJECT: {subject}

YOUR TASK — query available MCP sources:
1. **Notion** — search for architecture docs, product specs, design decisions
   mentioning {subject} or related concepts.
2. **Linear** — search for issues, bugs, or projects related to {subject}.
   Check for prior attempts, known limitations, or planned work.
3. **Slack** — search for recent team discussions about {subject}.

For each source found, extract:
- Key information relevant to the investigation
- Decision context (why was something done a certain way?)
- Known issues or planned changes
- Links to original sources

RULES:
- You MUST query ALL listed MCP sources (Notion, Linear, Slack).
- For each source, report findings OR "no relevant results found" — NEVER silently skip a source.

RETURN: structured findings organized by source (Notion/Linear/Slack)
with links to originals.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

### Step 4: Synthesize

Collect all agent outputs. Merge into a unified investigation:

1. **Code evidence** — what the code actually does (from Agent 1)
2. **Documentation context** — what docs say and where they diverge from code (from Agent 2)
3. **External knowledge** — relevant patterns, known issues, best practices (from Agent 3)
4. **Internal context** — company decisions, prior work, planned changes (from Agent 4)

Cross-reference all four sources. Where code contradicts docs, code wins. Where external best practice contradicts current implementation, note both.

### Step 4b: Citation Verification (high-risk investigations only)

Trigger this step when: the synthesis makes cross-system factual claims, OR total agent input exceeded ~20K tokens, OR the investigation type is `decision`. Spawn one verification agent:

```
Agent(subagent_type="general-purpose", model="sonnet", prompt="""
You are a citation verifier. You receive a draft investigation report and its source materials.

DRAFT: {synthesized report}
SOURCES: {agent outputs with their citations}

YOUR TASK:
1. For every factual claim in the draft, locate its supporting citation in SOURCES.
2. Return a claim-by-claim table: | claim | citation | verdict |
   Verdicts: [VERIFIED] (citation supports claim) | [WEAK] (citation exists but doesn't fully support) | [NO_SOURCE — retract]
3. Tool-call budget: 0-5 (verify against provided materials; only use tools to spot-check file:line citations).

RETURN: the verdict table plus a list of claims to retract or soften.
""")
```

Apply the verdicts before presenting: retract [NO_SOURCE] claims, soften [WEAK] ones.

### Step 5: Present the Investigation

Structure based on investigation type:

**For bugs:**
```
## Investigation: {subject}

### Root Cause
{What's actually happening and why, with code citations}

### Evidence
{Code trace showing the bug path — file:line references}

### External Context
{Known issues, related bugs in libraries, relevant docs [N]}

### Recommended Fix
{Specific changes with file paths and approach}
{Alternative approaches if applicable}

### Risk Assessment
{What could go wrong with the fix, what else it might affect}
```

**For implementation questions:**
```
## Investigation: {subject}

### Current State
{How the relevant system works today, with code citations}

### Implementation Approach
{Recommended approach, grounded in existing patterns and external best practices [N]}

### Integration Points
{Where new code connects to existing code — specific files, functions, types}

### Alternatives Considered
{Other approaches with trade-offs, citing external sources [N]}

### Risks & Gotchas
{Things to watch out for, edge cases, migration concerns}
```

**For decisions:**
```
## Investigation: {subject}

### Context
{Current system state and why this decision matters}

### Option A: {name}
{Strengths, weaknesses, evidence from code and external sources [N]}

### Option B: {name}
{Strengths, weaknesses, evidence from code and external sources [N]}

### Recommendation
{Your evidence-based recommendation with justification}
```

**For conceptual deep-dives (understand):**
```
## Understanding: {subject}

### What It Is
{One paragraph: purpose and role in the system}

### How It Works
{Full mechanical walkthrough. Cite code. Show the path from trigger to outcome.
Include actual types, function signatures, table schemas. Use file:line refs liberally.}

### Key Design Decisions
{Why is it built this way? What trade-offs were made? What alternatives exist?}

### Connections
{What other parts of the system does this touch? Use wikilinks for gestalt entries.}

### Gotchas
{Anything surprising, non-obvious, fragile, or undocumented. Edge cases.
Implicit assumptions. Things that would trip up someone modifying this code.}

### Open Questions
{Anything you couldn't fully resolve from the code alone.}
```

**All formats include:**
```
### Sources
**Code:**
- `{file}:{lines}` — {what it shows}
- ...

**External:**
[1] {title} — {url}
[2] {title} — {url}
...

**Internal:** (if applicable)
- Notion: {page title} — {link}
- Linear: {issue} — {link}
```

### Step 6: Offer Next Steps

Based on findings, offer:
- `/discuss` if the investigation surfaced a decision worth debating
- `/save` if the investigation produced reusable knowledge
- `/implement` or `/build` if the user wants to act on the findings
- Specific follow-up investigations if the scope expanded

## Evals — see EVALS.md (dev-time only)
