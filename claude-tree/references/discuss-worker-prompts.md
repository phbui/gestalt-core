# discuss: worker prompts

Verbatim worker prompts for `/discuss`, moved out of the skill body on 2026-11-02 so the skill loads smaller. Each section is dispatched exactly as written. Embed the Standard Block from `discipline-block.md` at the top of every prompt.

## W1. Agent 1 — Code Evidence (explore):

```
Agent(subagent_type="Explore", model="haiku", prompt="""
CONTEXT: The user is discussing an idea and we need codebase evidence to ground the critique.

YOUR TASK:
The user is discussing: '{idea_summary}'

Search the codebase for:
1. How does the current code handle this or similar problems?
2. What patterns are already in use?
3. What would need to change to implement this idea?
4. What existing code would be affected?

RULES:
- You MUST gather evidence from ALL specified sources before forming conclusions
- EVERY claim MUST cite evidence. Uncited claims MUST be marked [UNVERIFIED].
- Cite specific files and line numbers for ALL findings
- COVERAGE: EVERY item in scope MUST be checked. Report: N/N checked.

RETURN:
- Code evidence with file:line citations
- Patterns found: {list}
- Affected files: {list with file:line}
- Items checked: {N}/{N}

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W2. Agent 2 — External Evidence (general-purpose, optional):

```
Agent(subagent_type="general-purpose", prompt="""
CONTEXT: The user is discussing an idea and we need external evidence to ground the critique.

YOUR TASK:
Research external context for this idea: '{idea_summary}'

1. Search the web for prior art, best practices, known pitfalls
2. Query MCP sources:
   - Notion — company architecture docs, product specs
   - Linear — related issues, project context, prior attempts
   - Slack — recent team discussions
3. Find official docs and engineering blog posts

RULES:
- You MUST gather evidence from ALL specified sources before forming conclusions
- EVERY claim in the discussion MUST cite evidence. Uncited claims MUST be marked [UNVERIFIED].
- EVERY MCP source listed MUST be queried — do not skip any
- If an MCP source is unavailable or not configured in this session, record it as 'SKIPPED — {source} unavailable' in your return and continue — do not abort or fail the verification gate over a missing source.

RETURN:
- Findings with source URLs and MCP links
- Sources queried: {N}/{N}
- Uncited claims: {list marked [UNVERIFIED]}

[Embed the Task Completion Gate from gestalt/.claude/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```
