# Shared Verification Procedures

Standard verification steps run after code modifications. Skills reference this file for consistent post-fix verification.

## Post-Modification Hooks

After any phase that modifies code, run these checks on all modified files:

1. **Lint check**: Run `ReadLints` on every modified file. Fix any new lint errors introduced.
2. **Type check**: If the repo has a type checker, run it on changed files:
   - Python: check `pyproject.toml` for ruff, mypy, pyright config
   - TypeScript: check `tsconfig.json` → `tsc --noEmit`
   - Check `Taskfile.yaml` or `Makefile` for lint/check commands
3. **Test suite**: Run relevant test suites for the modified area:
   - Python: `pytest` on affected directories
   - TypeScript: `vitest`/`jest` on affected directories

## Verification Agent Pattern

For thorough verification, spawn two agents in parallel:

**Agent 1 — Automated Checks (shell):**
Run linters, type checkers, and test suites programmatically. Report all failures with file paths and error messages.

**Agent 2 — Exhaustive Verification (explore, readonly):**
Read modified files and verify changes against the original intent. Return a verification matrix:

| ID | Status | Notes |
|----|--------|-------|
| FIX-001 | PASS | Clean fix |
| FIX-002 | PARTIAL | Missing edge case |

Agent 2 RULES:
- You MUST verify EVERY requirement, not a sample. Your verification matrix MUST have one row per requirement/fix/item. Missing rows = incomplete verification.
- Before returning, count your matrix rows against the total items. If rows < total, you are NOT done.

## Reconciliation Loop

If verification surfaces issues:
- **Minor** (lint errors, missed edge cases): Fix and re-verify. Max 2 loops.
- **Major** (wrong approach, regression): Escalate to user before re-attempting.

## Verification Completeness Standard

Verification is exhaustive, never sampled. Every verification matrix MUST satisfy:

1. **Row coverage**: One row per requirement/fix/item. Count MUST equal total items.
2. **Status required**: Every row has PASS, FAIL, or BLOCKED (with reason).
3. **Evidence required**: Every PASS must cite the file:line that proves it. Every FAIL must cite what's wrong.
4. **No implicit passes**: Absence of a row is NOT a pass — it's an omission. Flag it.
5. **Completeness declaration**: Matrix ends with "Coverage: N/N items verified. Gaps: {list or 'none'}."
