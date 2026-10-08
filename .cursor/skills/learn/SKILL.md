---
name: learn
description: "Deep-scan a repository or topic and create/update gestalt knowledge entries. Spawns 4 parallel scanner agents (Infra, Code, Test/Docs, External) for maximum throughput. Use when you need to learn about a repo or create gestalt coverage."
disable-model-invocation: true
---
<Background>@gestalt-grounding</Background>

# Learn

Deep-scan a repository (or a specific topic within one) and create or update its gestalt knowledge. Uses parallel scanner agents for maximum throughput.

## Input

The user will specify one of:
- A repo name (learn everything about it)
- A repo + topic (e.g., "learn about the deployment pipeline in repo-a")
- A cross-repo topic (e.g., "learn about how data flows from repo-a to repo-b")

## Process

### Step 1: Identify Scope

If the user gave a repo name, you are learning **everything** about that repo. If they gave a topic, focus on that topic but still ground it in the full repo context.

### Step 2: Git Pre-Flight (Mandatory)

Run the git pre-flight procedure from `gestalt/.cursor/references/orchestration.md` §Git Pre-Flight on the target repo.

**If on a feature branch (not main):** Also run `git diff main..HEAD --stat` and `git log main..HEAD --oneline`. Pass the branch delta to scanner agents so they focus extra attention on changed files.

### Step 3: Check Existing Coverage

Follow the gestalt grounding procedure in `gestalt/.cursor/references/gestalt-grounding.md` (Quick Lookup + Branch-Aware Operations). If an entry exists for this repo/topic, read it. You will REWRITE it with everything you learn.

Also read `gestalt/BRANCHES.md` to check last-synced state for this repo.

### Step 4: Parallel Deep Scan

Spawn **4 parallel scanner agents** using the learner agent (see `.cursor/agents/learner.md` or `.claude/agents/learner.md`). Each scanner gets a ROLE, REPO path, and optional BRANCH DELTA. If on a feature branch, pass the branch delta (changed files list) so scanners give extra attention to modified code.

**Scanner template.** Each of the 4 agents below is a self-contained prompt — the orchestrator substitutes ROLE, SCOPE, and EXTRA from the table into this template and dispatches all 4 as full, independent `Task()` calls in parallel (one message, 4 calls). Workers do not inherit this skill's instructions, so every substituted prompt must remain complete on its own:

```
Task(subagent_type="learner", model="fast", readonly=true, prompt="""
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

| # | ROLE | SCOPE | EXTRA |
|---|---|---|---|
| 1 | Infra Scanner | infrastructure files: Taskfile, Dockerfile, docker-compose, CI/CD configs, deploy scripts, Helm charts, terraform, k8s manifests, Makefile, pyproject.toml, package.json, tsconfig, etc. | imports, API calls, shared infra |
| 2 | Code Scanner | source code files: entry points, modules, services, models, routes, handlers, utils, etc. | imports, API calls, shared libraries |
| 3 | Test/Docs Scanner | test files, documentation, READMEs, ADRs, and doc comments | test fixtures referencing external services, doc references |
| 4 | External Scanner | external integration points: MCP configs, API clients, SDK usage, third-party service connections, environment variable references, secrets references | external dependencies |

Skip directories listed in `gestalt/.cursor/references/orchestration.md` §Skip Directories.

### Step 5: Synthesize Findings

Collect all scanner results. Merge into a comprehensive knowledge entry.

Follow the knowledge write conventions in `gestalt/.cursor/references/knowledge-write.md` — use the frontmatter template, required sections, linking rules, and size limit defined there. Create or rewrite `gestalt/knowledge/{slug}.md`.

### Linking Rules (Mandatory)

Follow the linking rules from `gestalt/.cursor/references/knowledge-write.md`. Key: every repo mention uses `wikilinks`, every key section has a `^block-id`, pipeline entries have `## Data Flow`.

### Step 6: Validate

VERIFY that ALL of the following are true (FAIL if any are not):
- All `wikilinks` reference existing entries (check MANIFEST.md)
- No duplicate `^block-ids` within the entry
- Data flow connections reference valid targets
- Entry stays under 500 lines; split if larger (see below)

Verify the 500-line cap per `gestalt/.cursor/references/knowledge-write.md` §Size Limit.

### Step 7: Update Branch Tracking

Update `gestalt/BRANCHES.md` for this repo:
- Record current branch name and commit hash
- Record main branch commit hash
- If on a feature branch, record the delta summary (changed files, commit count)
- Set last-synced timestamp to now
- If a previous branch entry exists and the branch was merged to main, remove it

### Step 8: Regenerate Indices

Execute the full Post-Write Sequence per `gestalt/.cursor/references/knowledge-write.md` §Post-Write Sequence (indices + semantic index + Graphiti). All parts are mandatory.

### Step 8.5: Rebuild Semantic Index

(Covered by Post-Write Sequence §b above.)

### Step 8.6: Feed Temporal Graph

(Covered by Post-Write Sequence §c above.)

### Step 9: Summarize

Tell the user what you learned. Call out:
- Anything surprising or non-obvious
- Gaps you couldn't fill
- Conflicts between docs and code (code wins)
- New cross-repo connections discovered
- External sources found and registered

## Verification Gate

Before completing this skill:
- Re-read all steps above. Confirm EVERY step was executed.
- Count: steps completed / total. If any skipped, state why.
- Verify all 4 scanner agents returned results (or explicitly reported NO FINDINGS).
- Confirm the gestalt entry was written and the full Post-Write Sequence ran.

## Evals — see EVALS.md (dev-time only)
