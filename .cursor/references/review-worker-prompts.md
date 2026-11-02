# review: worker prompts

Verbatim worker prompts for `/review`, moved out of the skill body on 2026-11-02 so the skill loads smaller. Each section is dispatched exactly as written. Embed the Standard Block from `discipline-block.md` at the top of every prompt.

## W1. Verifier template (batches 1–2).** Split entries into 3 roughly equal batches; this template is invoked twice — once for batch 1 (entries 1..N/3) and once for batch 2 (entries N/3+1..2N/3) — as two full, independent `Task()` calls dispatched in parallel alongside Agents 3–4. Each is a self-contained prompt (workers don't inherit this skill's instructions), so substitute ENTRIES per call and send the complete prompt below both times:

```
Agent(subagent_type="reviewer", prompt="""
CONTEXT: Auditing gestalt knowledge base against actual codebase. Code is truth.
ENTRIES: {list of entry slugs for this batch}

YOUR TASK:
1. Read EVERY entry assigned to you in full
2. For EACH entry, go to the referenced repo and verify ALL factual claims against current code
3. Document ALL discrepancies between gestalt entries and actual code state

RULES:
- You MUST verify EVERY entry assigned, not a sample
- EVERY claim checked MUST cite the code location that confirms or contradicts it
- Completeness marker: "Entries reviewed: {N}/{N}. Stale: {list}. Current: {list}."

RETURN:
- Per-entry verdict: CURRENT / STALE / PARTIAL with evidence
- Specific stale claims with code citations proving staleness
- Entries reviewed: {N}/{N}. Stale: {list}. Current: {list}.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W2. Verifier Agent 3 — Link Checker:

```
Agent(subagent_type="reviewer", prompt="""
CONTEXT: Auditing gestalt knowledge base link integrity.
ROLE: Link Checker
ENTRIES: {all entry slugs}

YOUR TASK:
1. Validate ALL wikilinks resolve to existing entries
2. Verify ALL ^block-ids referenced by other entries exist at their targets
3. Verify ALL GRAPH.md connections are valid and bidirectional

RULES:
- You MUST check EVERY link in EVERY entry, not a sample
- EVERY broken link MUST be reported with source file and line number
- Completeness marker: "Links checked: {N}/{N}. Broken: {list}. Valid: {list}."

RETURN:
- Broken wikilinks: {list with source:line}
- Orphan ^block-ids: {list with source:line}
- Invalid GRAPH.md connections: {list}
- Links checked: {N}/{N}. Broken: {list}. Valid: {list}.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W3. Verifier Agent 4 — Gap Finder:

```
Agent(subagent_type="Explore", model="haiku", prompt="""
CONTEXT: Auditing gestalt knowledge base for missing coverage.

YOUR TASK:
1. List ALL repos in workspace
2. Verify ALL repos against gestalt coverage — identify which have entries and which do not
3. Query external sources (Notion/Linear) for uncovered topics

RULES:
- You MUST check EVERY repo, not a sample
- EVERY uncovered repo MUST be reported
- Completeness marker: "Repos checked: {N}/{N}. Covered: {list}. Gaps: {list}."

RETURN:
- Repos with coverage: {list}
- Repos without coverage: {list}
- Topics found in external sources with no gestalt entry: {list}
- Repos checked: {N}/{N}. Covered: {list}. Gaps: {list}.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```
