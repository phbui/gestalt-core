# investigate: worker prompts

Verbatim worker prompts for `/investigate`, moved out of the skill body on 2026-11-02 so the skill loads smaller. Each section is dispatched exactly as written. Embed the Standard Block from `discipline-block.md` at the top of every prompt.

## W1. Agent 1 — Code Tracer (general-purpose):

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

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W2. Agent 2 — Docs & Context Scanner (general-purpose):

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

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W3. Agent 3 — Web Researcher (general-purpose):

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

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W4. Agent 4 — MCP Context Scanner (general-purpose):

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

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```
