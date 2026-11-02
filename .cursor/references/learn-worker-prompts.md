# learn: worker prompts

Verbatim worker prompts for `/learn`, moved out of the skill body on 2026-11-02 so the skill loads smaller. Each section is dispatched exactly as written. Embed the Standard Block from `discipline-block.md` at the top of every prompt.

## W1. Scanner template.** Each of the 4 agents below is a self-contained prompt — the orchestrator substitutes ROLE, SCOPE, and EXTRA from the table into this template and dispatches all 4 as full, independent `Task()` calls in parallel (one message, 4 calls). Workers do not inherit this skill's instructions, so every substituted prompt must remain complete on its own:

```
Agent(subagent_type="learner", model="haiku", prompt="""
CONTEXT: We are deep-scanning a repo to create/update gestalt knowledge entries.
ROLE: {ROLE}
REPO: {repo_path}
BRANCH DELTA: {changed_files if on feature branch, or "none"}

YOUR TASK:
1. Scan ALL {SCOPE} in your assigned scope.
2. Extract key facts, patterns, and conventions with file:line citations
3. Identify relationships to other repos/services ({EXTRA})

RULES:
- You MUST scan EVERY file in scope, not a representative sample
- EVERY fact MUST have a file:line citation
- Report: "Files scanned: {N}/{N}. Key facts: {N}."

RETURN:
- Structured findings with file:line citations
- Coverage: {N}/{N} files scanned
- Key patterns discovered
- Cross-repo relationships found

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```
