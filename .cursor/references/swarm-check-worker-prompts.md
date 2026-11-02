# swarm-check: worker prompts

Verbatim worker prompts for `/swarm-check`, moved out of the skill body on 2026-11-02 so the skill loads smaller. Each section is dispatched exactly as written. Embed the Standard Block from `discipline-block.md` at the top of every prompt.

## W1. Step 3: Spawn Ripple Checkers

```
Agent(subagent_type="Explore", prompt="""
A change was made in {source_repo}:
- Files: {changed_files}
- Summary: {change_summary}

Your repo ({target_repo}) may be affected via: {connection_description}

Check:
1. Does {target_repo} import or reference anything that changed?
2. Are there shared data contracts (API types, DB schemas, message formats) that need updating?
3. Are there configuration references (env vars, service URLs, feature flags) that need updating?
4. Do tests in {target_repo} test against the changed interface?

For each issue found, report:
- File and line in {target_repo}
- What references the changed code
- Whether it will break, behave incorrectly, or just needs updating
- Suggested fix

Return: AFFECTED (with issues) or CLEAR (with evidence of no impact).
Document ALL checked files and contracts — not just affected ones.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```
