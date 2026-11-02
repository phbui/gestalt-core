# fix: worker prompts

Verbatim worker prompts for `/fix`, moved out of the skill body on 2026-11-02 so the skill loads smaller. Each section is dispatched exactly as written. Embed the Standard Block from `discipline-block.md` at the top of every prompt.

## W1. Phase 1: Triage (Explore Agent)

```
Agent(subagent_type="Explore", model="haiku", prompt="""
You are triaging a batch of code fixes for parallel execution.

FIX LIST:
{normalized fix list from Phase 0}

SCOPE: {repo path or directory}

YOUR TASK:
1. For each fix, identify the EXACT files that need modification.
   Use grep, glob, and semantic search to locate relevant code.
   If a fix is vague, determine the concrete files and lines involved.

2. Build a FILE OWNERSHIP MAP — which files each fix touches:
   FIX-001 → [file_a.py, file_b.py]
   FIX-002 → [file_c.py]
   FIX-003 → [file_a.py, file_d.py]  ← shares file_a with FIX-001

3. Identify DEPENDENCIES between fixes:
   - FIX-003 depends on FIX-001 (shared file, FIX-001 changes API that FIX-003 uses)
   - FIX-005 depends on FIX-002 (type change propagates)

4. Propose PARTITIONS — group fixes into up to 4 non-overlapping clusters.
   Rules:
   - Fixes touching the same file(s) go in the SAME cluster
   - Dependent fixes go in the SAME cluster (or ordered across waves)
   - Balance cluster sizes roughly evenly
   - If >4 clusters needed, plan multiple waves

5. For each fix, note:
   - Estimated complexity: trivial / moderate / complex
   - Key context the fixer agent will need (imports, patterns, related code)

RETURN:
- File ownership map
- Dependency graph
- Proposed partitions (clusters with file lists)
- Wave plan (if batching needed)
- Per-fix context notes
- Total files mapped / total fixes mapped. Coverage: ALL fixes MUST be mapped.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W2. Phase 2: Fix (Parallel Agents)

```
Agent(subagent_type="general-purpose", isolation="worktree", prompt="""
You are a code fixer. You own a specific set of files and fixes.

YOUR FIXES:
{list of FIX-IDs with descriptions for this cluster}

YOUR FILES (you may ONLY modify these):
{explicit file list for this cluster}

CONTEXT:
{per-fix context notes from triage — relevant imports, patterns, related code}
{codebase conventions if known from gestalt}

RULES:
- You must fix every issue in your assignment. No partial fixes. No TODOs.
- You MUST follow existing codebase conventions (naming, patterns, error handling).
- If a fix requires changing a file NOT in your list, STOP and report it —
  do not modify files outside your ownership.
- You MUST run the project's linter on every file you modify to catch new errors.
- If you discover additional issues while fixing, note them but only fix issues
  in your assignment unless the additional issue is in your owned files and trivial.
- Use the simplest fix that resolves each issue. Do NOT refactor surrounding code,
  add abstractions, or add handling for cases that cannot occur — an unrequested
  change is the same error as an unfixed issue: a scope mismatch.

RETURN FORMAT:
- Files modified: [list with brief description of changes]
- Fixes completed: [FIX-IDs with status]
- Unrequested changes: none, or [list with justification]
- Issues found: [any new issues discovered]
- Files that need changes outside your ownership: [if any]
- Confidence: high / medium / low (how confident you are the fixes are correct)

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W3. Agent 1 — Automated Checks (shell verification):

```
Agent(subagent_type="Explore", model="haiku", prompt="""
Run verification checks on the codebase after fixes were applied.

MODIFIED FILES:
{list of all files modified across all fix agents}

REPO PATH: {repo path}

YOUR TASK:
1. Run linters/type checkers if the project has them:
   - Python: look for pyproject.toml (ruff, mypy, pyright) or setup.cfg
   - TypeScript: look for tsconfig.json (tsc --noEmit)
   - Check Taskfile.yaml or Makefile for lint/check commands
2. Run relevant test suites if they exist:
   - Python: pytest on affected directories
   - TypeScript: vitest/jest on affected directories
3. Report ALL failures with file paths and error messages.

RETURN: structured list of check results (pass/fail with details).

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```

## W4. Agent 2 — Exhaustive Verification (explore, readonly):

```
Agent(subagent_type="Explore", model="haiku", prompt="""
Verify that fixes were applied correctly by exhaustively verifying the code.

ORIGINAL FIX LIST:
{full fix list with descriptions}

FIXES REPORTED AS COMPLETE:
{FIX-IDs and agent descriptions of what was changed}

YOUR TASK:
1. For EVERY fix, read the modified file and verify:
   - The original issue is actually resolved
   - No regressions were introduced in the same file
   - The fix follows codebase conventions
2. Flag any fix that looks incomplete, incorrect, or introduces new issues.

RULES:
- Your verification matrix MUST have one row per FIX-ID. Missing rows = incomplete.
- You MUST verify ALL fixes, not a sample.

RETURN: verification matrix
| FIX-ID | Status | Notes |
|--------|--------|-------|
| FIX-001 | PASS | Clean fix |
| FIX-002 | PARTIAL | Missing edge case handling |
...
Verified: {N}/{total} fixes checked.

[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
""")
```
