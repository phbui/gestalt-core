---
name: commit
description: "Create a detailed, well-structured commit with interactive review, analysis, and optional push."
model: sonnet
user-invocable: true
argument-hint: "optional commit message hint"
disable-model-invocation: true
---

# Commit

Create a detailed, well-structured commit with interactive review. Analyzes all changes, drafts a commit message, presents it for approval or edits, then optionally pushes.

## Process

### Step 1: Gather State

Run in parallel:
1. `git status` — see all staged, unstaged, and untracked files
2. `git diff` — unstaged changes
3. `git diff --cached` — staged changes
4. `git log --oneline -10` — recent commit style

### Step 2: Analyze Changes

Read every changed file. Categorize the changes:
- **What changed** — files added, modified, deleted
- **Why it changed** — infer intent from the diff (new feature, bug fix, refactor, docs, config, etc.)
- **Scope** — which system/component/module is affected
- **Risk** — flag any security-sensitive files (.env, credentials), large binary additions, or breaking changes

If there are unstaged changes that look like they belong with staged changes, mention it. If nothing is staged, ask what the user wants to commit.

### Step 3: Draft Commit Message

Write a commit message following these conventions:
- **Subject line**: `<type>(<scope>): <description>` (under 72 chars)
  - Types: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`, `perf`, `style`, `ci`
  - Scope: the affected module/component (e.g., `hooks`, `api`, `gestalt`)
- **Body**: Explain *why* the change was made, not just what changed. Include:
  - Motivation / context
  - Key decisions made
  - Anything non-obvious about the approach
- **Footer**: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` (only if Claude wrote or co-wrote the code; use the trailer format the harness system prompt specifies — the model name portion changes with model upgrades)

MUST adapt to the repo's existing commit style from the git log. If the repo doesn't use conventional commits, match what it does use.

### Step 4: Present for Review

Show the full commit message in a code block. Then ask:

```
Stage & commit this? (y) approve | (e) edit | (n) cancel
```

- **y**: Stage the relevant files and commit
- **e**: Ask what to change, revise the message, re-present
- **n**: Abort — do nothing

### Step 5: Stage & Commit

Stage files explicitly by name — never use `git add -A` or `git add .`. Skip files that:
- Look like secrets (.env, credentials, tokens)
- Are large binaries that shouldn't be in git
- Are unrelated to the commit's intent

Create the commit. Verify with `git status` that it succeeded.

### Step 6: Push Prompt

After a successful commit, ask:

```
Push to <remote>/<branch>? (y/n)
```

Show the remote and branch explicitly. If the branch has no upstream, mention that `-u` will be used.

- **y**: Push (with `-u` if needed). Show the result.
- **n**: Done — commit stays local.

## Rules

- Never auto-stage or auto-commit without explicit user approval
- Never force-push without explicit user request and confirmation
- Never commit files that likely contain secrets — warn instead
- If the working tree is clean, say so and exit
- Keep the interaction tight — don't over-explain between steps

## Verification Gate

Execute every step above — none are optional.
Before finishing: confirm all steps completed and outputs match the required formats.

## Evals — see EVALS.md (dev-time only)
