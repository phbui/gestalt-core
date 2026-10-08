# Gestalt Skill Index

Complete reference for all gestalt commands and skills: what each does, when to use it, and how commands chain together.

When the user runs `/help-gestalt`, present this reference. If they ask about a specific command, show only that command's section.

## Contents

- [Quick Reference](#quick-reference)
- [Detailed Guide](#detailed-guide)
- [Command Chains](#command-chains)
- [Shared References](#shared-references)

---

## Quick Reference

All slash commands are skills. They live as `.cursor/skills/<name>/SKILL.md` (and `.cursor/skills/<name>/SKILL.md`) and are invoked via `/<name>`.

| Command | Purpose | Type |
|---------|---------|------|
| `/help-gestalt` | This index — shows all commands and how to use them | Reference |
| `/consolidate [run\|status\|recall\|publish]` | Memory-layer operations: synthesis cycle, health dashboard, temporal-graph recall, shareable briefing | Memory |
| `/prompt <request>` | Optimize a raw request with gestalt context and execution strategy, confirm before executing | Memory |
| `/save` | Extract knowledge from conversation to gestalt | Knowledge |
| `/compact-resume` | Prep for context compaction/session end: secure perishables, write the canonical gestalt resume anchor, sync memory | Knowledge |
| `/learn` | Deep-scan a repo and create/update gestalt entries | Knowledge |
| `/review` | Verify gestalt entries against actual codebase | Knowledge |
| `/sync` | Pull from Notion, Linear, Slack into gestalt | Knowledge |
| `/investigate` | Deep-dive into a bug, concept, decision, or behavior | Investigation |
| `/discuss` | Rubber-duck session with evidence-backed critique | Investigation |
| `/research` | Web research with parallel agents and citations | Investigation |
| `/swarm-check` | Detect cross-repo ripple effects from a change | Investigation |
| `/vet` | Eval-loop review of a research document: dimension auditors + venue personas, rounds to convergence, cold-referee exit gates | Investigation |
| `/build` | General-purpose builder from any input | Build |
| `/audit` | Final quality gate after /implement or /build | Build |
| `/pr` | Multi-persona PR review with selectable fixes | Build |
| `/fix` | Parallel batch fixes from any source | Build |
| `/requirements <mode>` | Whole requirements lifecycle in one skill — modes: explain, pitch, system, srs, adr, sdd, promote, implement (full SRS→code pipeline) | Requirements |
| `/commit` | Interactive commit with detailed message, review/edit flow, optional push | Utility |
| `/mutate` | Create, edit, delete, or rename gestalt artifacts (commands, skills, rules, agents) | Utility |
| `/caveman` | Ultra-compressed communication mode (~75% fewer tokens); gestalt writes stay in standard register per caveman-safeguard | Utility |
| `/capture [idea\|pref\|commit] <text>` | Deterministic save to the capture inbox — always writes, never judges; commitments get due dates | Jarvis |
| `/weekly-review` | GTD review: memory-hygiene gate (/consolidate status) first, then inbox triage, commitment resolution, stuck/stale surfacing, top-3 next actions | Jarvis |

---

> **Not for reviewing the harness itself.** `/audit` requires `srs.md` +
> `sdd.md` for a specific pitch, and `/vet` takes a single research document.
> To review gestalt against the codebase use `/review`; for a code diff use
> `/pr`. This boundary is `/audit`'s own design decision, stated in its
> SKILL.md: "Unlike `/review` (which audits gestalt itself), this command
> audits the code and design artifacts produced for a specific pitch."

## Detailed Guide

### Memory System

The gestalt memory system has 4 layers that work together to maintain knowledge across sessions:

- **Curated** (`knowledge/*.md`) — Hand-crafted, authoritative entries about repos, systems, and conventions. The primary source of truth. Edited by `/save`, `/learn`, `/review`, and `/consolidate`.
- **Episodic** (Letta) — Session-level memory blocks. Captures what happened in recent conversations. `/consolidate` promotes durable facts from here into curated entries.
- **Temporal** (Graphiti) — Knowledge graph with time-aware edges. Tracks how facts evolve over time — decisions, architecture changes, learned behaviors. Queryable via `/consolidate recall`.
- **Semantic** (SQLite FTS5+vec) — Full-text and vector search index over all curated entries. Powers fast, fuzzy lookups across the entire knowledge base.

Everything is automatic: `SessionStart` injects relevant context at conversation start; `Stop` captures new knowledge at the end. You don't need to do anything — but you can use `/consolidate` to run a manual synthesis cycle and `/consolidate status` to check the health of all four layers.

#### `/consolidate [run|status|recall|publish]`
**What:** Memory-layer operations, four modes. `run` (default) synthesizes across all layers — promotes durable Letta facts into curated entries, cross-links, flags stale facts. `status` shows the live health of all 4 layers. `recall <query>` queries the Graphiti temporal graph for cross-session history and evolving facts. `publish` generates a condensed briefing for Claude Projects.
**When to use:**
- Weekly, or after a sprint, to keep curated entries fresh (`run`)
- To verify knowledge was captured or memory injection works (`status`)
- "What did we decide about X last week?" (`recall`)
- Before onboarding someone or setting up a Claude Project (`publish`)

**Example:** `/consolidate`
**Example:** `/consolidate status`
**Example:** `/consolidate recall "why did we switch from Kafka to NATS"`
**Example:** `/consolidate publish`

#### `/prompt <request>`
**What:** Takes a raw user request, enriches it with relevant gestalt context (knowledge entries, recent decisions, conventions), adds an execution strategy (which commands to chain, which agents to spawn), and presents the optimized prompt for confirmation before executing.
**When to use:**
- When you have a vague request and want gestalt to sharpen it
- Before running a complex command chain
- When you want to see what context gestalt would inject before committing

**Example:** `/prompt "set up monitoring for the new spatial service"`
**Example:** `/prompt "add real-time collaboration to the platform"`

---

### Knowledge Operations

#### `/compact-resume`
**What:** Prepares a long-running session for context compaction or handoff: secures uncommitted work and in-flight processes, then writes one canonical, self-contained resume block to gestalt so a fresh context resumes with zero loss.
**When to use:**
- Before /compact on a long-running workstream
- Before handing a session off to a fresh context

**Example:** `/compact-resume`

#### `/save`
**What:** Extracts knowledge from the current conversation and writes it to gestalt.
**When to use:**
- After learning something worth remembering across sessions
- After debugging sessions that revealed non-obvious behavior
- When the user corrects a mistake — capture the correct approach
- Auto-triggers when context is approaching its limit

**Example:** `/save` (after a conversation about spatial pipeline quirks)

#### `/learn`
**What:** Deep-scans a repository with 4 parallel agents (Infra, Code, Test/Docs, External) and creates/updates gestalt knowledge entries.
**When to use:**
- First time working with a repo that has no gestalt entry
- After major refactors or architecture changes in a repo
- When gestalt entries are flagged as stale by `/review`

**Example:** `/learn spatial` or `/learn platform "deployment pipeline only"`

#### `/review`
**What:** Verifies all gestalt entries against the actual codebase. Checks every claim, validates links, finds gaps.
**When to use:**
- Periodically (weekly/biweekly) to keep gestalt accurate
- After the staleness check flags entries as HIGH risk
- Before onboarding someone to ensure docs are correct

**Example:** `/review`

#### `/sync`
**What:** Pulls context from Notion, Linear, and Slack via MCP and reconciles with gestalt entries.
**When to use:**
- After team meetings or architecture decisions made outside the codebase
- When you need the latest product/business context
- Before `/publish` to ensure external sources are current

**Example:** `/sync`

---

### Investigation & Analysis

#### `/investigate`
**What:** Deep investigation or conceptual deep-dive. Spawns up to 4 parallel agents (Code Tracer, Docs Scanner, Web Researcher, MCP Scanner). Also handles "understand" requests.
**When to use:**
- Debugging: "investigate why X is broken"
- Understanding: "understand how the auth flow works"
- Decisions: "investigate whether to use X or Y"
- Implementation: "investigate how I'd add feature Z"

**Example:** `/investigate why snapshot messages drop during reconnect`
**Example:** `/investigate how does data flow from spatial to platform`

#### `/discuss`
**What:** Rubber-duck session with a sharp, skeptical counterpart. Gathers evidence from code and external sources, then challenges your assumptions.
**When to use:**
- Thinking through an architectural decision
- Evaluating trade-offs before committing to an approach
- When you want someone to poke holes in your idea

**Example:** `/discuss should we use Redis or in-memory caching for the session store?`

#### `/research`
**What:** Pure web research with parallel agents. Every claim is cited.
**When to use:**
- Evaluating a library or technology
- Understanding best practices for a pattern
- Comparing approaches with external evidence

**Example:** `/research tradeoffs between Qdrant and Pinecone for vector search`

#### `/swarm-check`
**What:** Detects cross-repo ripple effects when a change in one repo may break others.
**When to use:**
- After changing a shared API, schema, or data contract
- Before merging PRs that touch cross-service boundaries
- When GRAPH.md shows connections you're not sure about

**Example:** `/swarm-check "changed the spatial pipeline output schema"`

#### `/vet`
**What:** Eval-loop review of a research document (paper, fellowship application, dissertation chapter). Runs parallel dimension auditors (truth/claims, citations, cross-corpus consistency, math-by-code-execution, voice/AI-tells, submission hygiene) plus a venue-relevant persona panel each round; fixes and repeats until 2 consecutive zero-MAJOR rounds; then runs exit gates (4-way verification round, fresh cold referee on the built artifact, NEEDS-PHI queue).
**When to use:**
- Before submitting a paper, application essay, or proposal anywhere with stakes
- After heavy edits to a document that belongs to a larger consistent corpus
- When math, citations, and claims need independent re-verification

**Requires:** the target document; corpus siblings and venue are auto-discovered or passed via `--corpus` / `--venue`.
**Example:** `/vet docs/paper/paper.md --venue "TMLR"`
**Example:** `/vet ~/Documents/proposal/proposal.md --corpus ~/Documents/proposal/`

---

### Build Pipeline

#### `/build`
**What:** General-purpose builder. Works from any input — a description, spec, design doc, SDD, ADR, or just a plain request. Spawns parallel agents for independent workstreams.
**When to use:**
- Building a new feature or component from a description
- Implementing from an SDD or spiking an ADR
- Extending existing code with new functionality
- Any build task that doesn't need the full `/implement` pipeline

**Example:** `/build "Add a retry wrapper with exponential backoff to src/api/"`
**Example:** `/build @docs/pitches/c7-comms/sdd.md "Implement the NATS handler"`

#### `/audit`
**What:** Final quality gate. Spawns 4 specialist reviewers (Architecture, Code Quality, Performance, Requirements), then implements fixes in ordered batches.
**When to use:**
- After `/implement` or `/build` is complete
- Before marking a pitch as done
- When you want a thorough quality review of recent changes

**Requires:** `srs.md` and `sdd.md` in the target pitch/system folder.
**Example:** `/audit @docs/pitches/c7-comms`

#### `/pr`
**What:** Multi-persona PR review. Spawns 3 specialist agents (Infra Expert, Senior Engineer, Code Quality Specialist) to review a PR diff in parallel, then presents a centralized report with selectable fix options. No SRS/SDD required — works on any PR.
**When to use:**
- Reviewing a PR before merge
- Getting a thorough security + logic + quality assessment of changes
- When you want actionable findings you can selectively fix

**Example:** `/pr 868`
**Example:** `/pr feature/auth-rework "Focus on the middleware only"`
**Example:** `/pr` (reviews current branch vs main)

#### `/fix`
**What:** Parallel batch fixer. Triages a list of issues, partitions by file ownership, dispatches parallel agents, verifies results.
**When to use:**
- Multiple lint errors or type errors to fix
- Batch of code review comments to address
- TODO cleanup across a directory
- Any situation with 3+ fixes needed

**Example:** `/fix "Fix all type errors in src/api/"`

---

### Requirements Engineering

#### `/requirements <mode>`
**What:** The whole requirements-engineering lifecycle as one parameterized skill, git-subcommand style. Modes: `explain` (concepts Q&A or inspect a pitch/system), `pitch` (create/update a pitch folder), `system` (create/update a system folder + registry), `srs` (Software Requirements Specification), `adr` (Architectural Decision Record), `sdd` (Software Design Description, monolithic or modular), `promote` (move pitch docs to system docs during Cooldown), `implement` (full SRS → ADR → SDD → Build → QA orchestration with parallel agents).
**Prerequisites:** srs/adr/sdd/promote/implement modes check their document prerequisites and STOP with directions if missing (e.g. `srs` needs a pitch or system index; `implement` needs `srs.md`).
**When to use:**
- "What's the difference between SRS and SDD?" → explain
- Starting a new Shape Up bet → pitch, then srs
- Recording a significant decision → adr
- Cooldown after a completed pitch → promote
- "Take this spec all the way to working code" → implement

**Example:** `/requirements "when should I write an ADR?"`
**Example:** `/requirements srs @docs/pitches/c8-collab/index.md`
**Example:** `/requirements implement @docs/pitches/c7-comms/srs.md`

---

### Utility

#### `/caveman`
**What:** Ultra-compressed communication mode (~75% fewer output tokens, full technical accuracy). Code, commits, PRs, and gestalt writes stay in normal register per the caveman-safeguard rule.
**When to use:**
- Long sessions where chat verbosity is burning context
- When you want terse status updates but normal-quality artifacts

**Example:** `/caveman`

#### `/commit`
**What:** Interactive commit workflow. Analyzes all changes, drafts a detailed conventional-commit message, presents it for review/editing, stages explicitly, commits, then optionally pushes.
**When to use:**
- After finishing any piece of work
- When you want a well-structured commit message without writing it yourself
- When you want to review exactly what will be committed before it happens

**Example:** `/commit`

#### `/mutate`
**What:** Meta-command for creating, editing, deleting, or renaming gestalt artifacts — commands, skills, rules, agents, references. Handles dual-agent sync (Claude/Cursor), all doc index updates, and symlink verification automatically.
**When to use:**
- Creating a new command or skill
- Modifying an existing rule's behavior
- Adding a new agent definition
- Any time you need to touch gestalt's configuration layer

**Example:** `/mutate create command deploy "Interactive deployment workflow"`
**Example:** `/mutate edit rule proactive-parallelism`
**Example:** `/mutate create skill monitor "Production monitoring dashboard"`

---

## Command Chains

Common sequences of commands that work well together:

### New Feature (Full Pipeline)
```
/requirements pitch → srs → adr → sdd → implement → /audit
```

### Quick Implementation
```
/requirements srs (if missing) → sdd → /build → /audit
```

### Knowledge Refresh
```
/sync → /review → /learn (on flagged repos)
```

### Investigation → Implementation
```
/investigate → /discuss (if decision needed) → /requirements implement or /build
```

### Cross-Repo Change
```
/swarm-check → /fix (per affected repo) → /review (update gestalt)
```

### Research → Decision → Build
```
/research → /discuss → /requirements adr → sdd → /build
```

### Onboarding
```
/sync → /review → /consolidate publish → (share BRIEFING.md)
```

### Context Recovery (New Session)
```
/investigate "understand how X works" → /save (to persist findings)
```

### Memory Health Check
```
/consolidate status → /consolidate (if Letta blocks are building up) → /consolidate recall (to verify promoted facts)
```

### Vague Request → Optimized Execution
```
/prompt "raw idea" → (review optimized plan) → /build or /requirements implement
```

### PR Review → Fix → Merge
```
/pr {number} → (select fixes) → /commit → (push)
```

### Paper / Application Submission
```
(draft ready) → /vet {document} → (resolve NEEDS-PHI queue) → submit
```

### Build → Commit → Push
```
/build → /commit → (review message, push)
```

### Create New Command
```
/mutate create command <name> → (scaffold, sync, update docs)
```

---

## Shared References

Commands and skills use these shared reference files to stay DRY:

| Reference | Path | Used By |
|-----------|------|---------|
| Orchestration | `gestalt/.cursor/references/orchestration.md` | requirements (implement mode), fix, audit, pr, learn, review, sync |
| Discipline Block | `gestalt/.cursor/references/discipline-block.md` | every skill that generates worker prompts (canonical grounding contract — embed verbatim) |
| Scope Discipline | `gestalt/.cursor/references/scope-discipline.md` | every skill that generates code-writing worker prompts (unified completeness+minimalism contract — embed verbatim; reviewer corollary for verifier/reviewer/audit) |
| Verification | `gestalt/.cursor/references/verification.md` | requirements (implement mode), fix, audit, pr |
| Gestalt Grounding | `gestalt/.cursor/references/gestalt-grounding.md` | save, learn, review, investigate, discuss, consolidate (publish mode), swarm-check |
| Knowledge Write | `gestalt/.cursor/references/knowledge-write.md` | save, learn, review, sync |
| Collaborative Work | `gestalt/.cursor/references/collaborative-work.md` | requirements (srs/sdd/adr/promote modes) |
