---
pitch_id: unified-relevance-injection
document: srs
version: 1.0
created: 2026-04-09
---

# Software Requirements Specification: Unified Relevance Injection

## 1. Introduction

### 1.1 Purpose

This SRS defines verifiable requirements for replacing the current "dump everything at session start" memory injection with a tiered, relevance-filtered system that queries all three memory layers (Letta, Graphiti, Gestalt KB) per-prompt.

### 1.2 Scope

The system modifies three existing Claude Code hooks (`gestalt-session-start.sh`, `prompt-intelligence.sh`, `post-compact-reinject.sh`) and adds Letta agent configuration constraints. It does not add new services, modify the Stop hook write pipeline, or change the gestalt MCP server.

### 1.3 Definitions

| Term | Definition |
|---|---|
| **Tier 1** | Always-injected behavioral context: Letta blocks that calibrate *how* the agent works (preferences, directives, patterns). Present in every session regardless of task. |
| **Tier 2** | Relevance-filtered domain context: facts, entries, and blocks injected per-prompt based on query similarity. Drawn from all three memory layers. |
| **Fan-out** | Concurrent retrieval pattern: query all backends in parallel, merge results. Latency = max(slowest), not sum(all). |
| **RRF** | Reciprocal Rank Fusion: score-agnostic merge algorithm for combining ranked lists from different retrieval systems. Formula: `score(d) = sum(1/(K + rank_i))` where K=60. |
| **Domain block** | A Letta core memory block containing project-specific or task-specific knowledge (e.g., `project_context`, `tool_guidelines`, self-created blocks). |
| **Behavioral block** | A Letta core memory block containing user preferences, agent directives, or session calibration. Always relevant regardless of task. |

### 1.4 References

- [Pitch: unified-relevance-injection](./index.md)
- [Pitch: gestalt-memory-expansion](../gestalt-memory-expansion/index.md) — predecessor, phases 1-5
- [Claude Code Hooks Reference](https://code.claude.com/docs/en/hooks) — UserPromptSubmit, SessionStart, output caps
- [MemGPT paper (arXiv:2310.08560)](https://arxiv.org/abs/2310.08560) — always-in-context core memory design
- [Graphiti + FalkorDB Benchmarks](https://blog.getzep.com/graphiti-knowledge-graphs-falkordb-support/) — P95 latency characteristics
- [Context Engineering Best Practices (Weaviate)](https://weaviate.io/blog/context-engineering) — tiered injection, anti-stuffing

### 1.5 Constraints Discovered During Investigation

| Constraint | Impact |
|---|---|
| Claude Code caps `additionalContext` output at 10K chars | Per-prompt injection must stay under 10K chars (~2.5-3K tokens). Our 500-800 token target is well within. |
| `prompt-intelligence.sh` does not receive the user's message text via env/stdin | Must verify how UserPromptSubmit hooks access prompt content. May require reading from Claude Code's internal message passing. |
| `gestalt_search` first call loads nomic-embed-text-v1.5 (3-5s cold start) | Must warm the model at SessionStart to avoid first-prompt latency spike. |
| Graphiti MCP requires session initialization (init + notifications/initialized) before tool calls | Per-prompt calls need either a persistent MCP session or fast re-init (~200ms overhead). |
| Letta processes messages sequentially per agent | Block reads (GET /agents/{id}) are fast (~100ms) but must not collide with Stop hook writes. |
| `prompt-intelligence.sh` currently has no configured timeout | Must add explicit timeout in hooks config to prevent runaway queries from blocking prompts. |

---

## 2. Product Overview

### 2.1 User Classes

- **Phi (primary operator)** — Works across 35+ repos. Expects relevant memory surfaced automatically without manual `/recall` or MANIFEST lookups.
- **Claude Code agents** — Spawned by gestalt skills. Need domain context injected before they start work, not after they ask for it.
- **Cursor agents** — Secondary tool. Benefits indirectly: Tier 1 filtering reduces session-start payload; per-prompt injection not applicable (Cursor has no UserPromptSubmit hook equivalent).

### 2.2 Operating Environment

- Existing gestalt memory stack (Letta v0.16.6, Graphiti MCP, FalkorDB, SQLite FTS5+sqlite-vec)
- All services running via Docker Compose managed by systemd
- Claude Code CLI with hooks support (UserPromptSubmit, SessionStart events)
- Python 3.11+ for hook scripts

### 2.3 Design Constraints

##### URI-CONST-001: Preserve existing write pipeline

- _Statement:_ The system shall not modify the Stop hook (`gestalt-stop.sh`) write pipeline. All changes are to the read path only.
- _Rationale:_ The write pipeline (Letta transcript processing, Graphiti episode ingestion, session summary writing) works correctly and is proven.
- _Acceptance:_ `gestalt-stop.sh` is not modified by this pitch. Stop hook behavior is unchanged.
- _Source:_ Pitch scope decision.

##### URI-CONST-002: Markdown remains canonical

- _Statement:_ The system shall not change gestalt's file-based knowledge model. `knowledge/*.md` entries remain the source of truth for curated knowledge.
- _Rationale:_ Inherited from GME-CONST-001.
- _Acceptance:_ No changes to knowledge entry format, structure, or storage location.
- _Source:_ Inherited from gestalt-memory-expansion CONST-001.

##### URI-CONST-003: No new services

- _Statement:_ The system shall not add new Docker services, databases, or MCP servers. All retrieval uses existing infrastructure.
- _Rationale:_ The three-layer stack is already deployed. This pitch optimizes the read path, not the infrastructure.
- _Acceptance:_ `docker-compose.yml` is not modified. No new ports opened.
- _Source:_ Pitch scope decision.

##### URI-CONST-004: Hook output cap

- _Statement:_ All hook stdout output shall stay under 10,000 characters per invocation.
- _Rationale:_ Claude Code truncates hook output exceeding 10K chars to a file preview. Exceeding this silently degrades injection.
- _Acceptance:_ No hook invocation produces stdout exceeding 10,000 characters.
- _Source:_ Claude Code documentation.

---

## 3. Requirements

### 3.1 Letta Growth Control

##### URI-FR-001: Constrain Letta agent from creating new core blocks

- _Statement:_ The Letta agent's `core_directives` block shall contain an instruction preventing the agent from creating new core memory blocks. The directive shall instruct the agent to condense existing block content when full and direct overflow knowledge to gestalt knowledge entries.
- _Rationale:_ The Letta agent autonomously created 2 extra blocks (`service_api_notes`, `platform_arch_notes`) when `project_context` hit its 5K limit. Without a constraint, block count and total injection size grow unbounded.
- _Acceptance:_ After the directive is applied, no new core memory blocks are created by the Letta agent over 10+ sessions of normal use. Existing blocks are condensed rather than expanded.
- _Source:_ Investigation finding — agent self-created blocks with 100K limits.

##### URI-FR-002: Cap self-created block limits

- _Statement:_ The system shall reduce the `limit` field of `service_api_notes` and `platform_arch_notes` blocks from 100,000 to 3,000 characters via the Letta REST API (`PATCH /v1/blocks/{block_id}`).
- _Rationale:_ Even with URI-FR-001, a safety net prevents unbounded growth if the directive is ignored. 3K chars (~750 tokens) is sufficient for a domain summary.
- _Acceptance:_ Both blocks have `limit: 3000` in the Letta agent response. Content exceeding 3K is condensed by the Letta agent, not truncated.
- _Source:_ Investigation finding — blocks had 100K default limits.

### 3.2 Tier 1: Session Start Injection

##### URI-FR-010: Inject only Tier 1 blocks at session start

- _Statement:_ The `gestalt-session-start.sh` hook shall inject only Tier 1 Letta blocks at session start. Tier 1 blocks are: `core_directives`, `user_preferences`, `session_patterns`, `guidance`, `pending_items`. All other blocks (including `project_context`, `tool_guidelines`, and any self-created blocks) shall be excluded from session-start injection.
- _Rationale:_ Tier 1 blocks calibrate agent behavior and are relevant to every session. Domain blocks are only relevant when the task matches and should be injected per-prompt via Tier 2.
- _Acceptance:_ Session-start injection contains only the 5 named blocks. Total Tier 1 injection is under 1,500 tokens (measured). Domain blocks do not appear in `<gestalt-memory>`.
- _Source:_ Investigation finding — current 6.4K tokens includes ~1.1K behavioral + ~5.3K domain.

##### URI-FR-011: Configurable Tier 1 block list

- _Statement:_ The list of Tier 1 block labels shall be configurable via the `GESTALT_TIER1_BLOCKS` environment variable in `.env`. Default value: `core_directives|user_preferences|session_patterns|guidance|pending_items`.
- _Rationale:_ Block relevance may change as the Letta agent evolves. Configuration avoids code changes for tier adjustments.
- _Acceptance:_ Changing `GESTALT_TIER1_BLOCKS` in `.env` changes which blocks are injected at session start. Restart not required (hooks read `.env` on each invocation).
- _Source:_ Design decision — configurability over hardcoding.

##### URI-FR-012: Session summaries unchanged

- _Statement:_ The system shall continue injecting the last 3 session summaries at session start, unchanged from current behavior.
- _Rationale:_ Session summaries provide recent conversation context that is always relevant. They are already compact (~600 tokens total).
- _Acceptance:_ Last 3 session summaries appear in `<gestalt-memory>` after Tier 1 blocks.
- _Source:_ Preserving existing behavior.

##### URI-FR-013: CWD-based gestalt hint at session start

- _Statement:_ When the current working directory is within a known repository that has a gestalt knowledge entry, the session-start hook shall inject a one-line hint: `Relevant gestalt: slug` for up to 3 matching entries.
- _Rationale:_ Provides a lightweight signal for the agent to self-direct to relevant knowledge, without injecting full entries.
- _Acceptance:_ When CWD is `/home/user/Documents/GitHub/platform/`, session-start injection includes `Relevant gestalt: [[platform]]`. When CWD is the workspace root, no hint is injected.
- _Source:_ Design decision — CWD is a weak but free signal.

### 3.3 Tier 2: Per-Prompt Relevance Injection

##### URI-FR-020: Query all three memory layers per-prompt

- _Statement:_ The `prompt-intelligence.sh` hook shall query three memory backends on every `UserPromptSubmit` event:
  1. **Graphiti**: `search_memory_facts(query=<prompt_terms>, max_facts=3, group_id="gestalt")`
  2. **Gestalt KB**: `gestalt_search(query=<prompt_terms>, limit=3)` via the gestalt MCP server or direct Python import
  3. **Letta domain blocks**: keyword match of `<prompt_terms>` against non-Tier-1 block content (in-memory, from cached block JSON)
- _Rationale:_ All three layers contain different knowledge (temporal facts, curated entries, episodic context). Querying all three provides the most complete relevant context per prompt.
- _Acceptance:_ For a prompt containing "data-api pavement scores", the hook returns relevant Graphiti facts about data-api, gestalt entry pointers for `example-service-api`, and matching content from `project_context` or `service_api_notes` blocks.
- _Source:_ Core pitch requirement — unified retrieval.

##### URI-FR-021: Fan-out concurrent retrieval

- _Statement:_ The three retrieval queries (Graphiti, gestalt_search, Letta block match) shall execute concurrently, not sequentially. The hook shall wait for all three to complete (or timeout individually) before merging results.
- _Rationale:_ Sequential execution would sum latencies (~300ms + ~500ms + ~50ms = ~850ms). Fan-out reduces to max(~500ms).
- _Acceptance:_ Total hook execution time is less than or equal to the slowest individual query plus 100ms overhead, not the sum of all queries.
- _Source:_ Design decision — fan-out pattern from production RAG systems.

##### URI-FR-022: RRF merge across sources

- _Statement:_ Results from all three backends shall be merged using Reciprocal Rank Fusion (K=60). Each backend produces a ranked list; RRF combines them into a single relevance-ordered list without requiring score normalization across backends.
- _Rationale:_ Graphiti, gestalt_search, and Letta block matching use different scoring systems (graph distance, BM25+cosine, keyword frequency). RRF is score-agnostic and proven effective for heterogeneous retrieval fusion.
- _Acceptance:_ A fact appearing in both Graphiti and gestalt_search results ranks higher than a fact appearing in only one. Merge produces a single ordered list.
- _Source:_ Existing pattern — gestalt_search already uses RRF for BM25+vector fusion.

##### URI-FR-023: Token budget cap

- _Statement:_ Per-prompt injection shall be capped at a configurable token budget. Default: 700 tokens (~2,800 chars). The budget is enforced by truncating the merged result list after the cumulative token count exceeds the cap.
- _Rationale:_ Context stuffing degrades LLM answer quality. A hard cap ensures only the highest-relevance content is injected.
- _Acceptance:_ No per-prompt injection exceeds the configured token budget. Changing `GESTALT_TIER2_MAX_TOKENS` in `.env` adjusts the cap.
- _Source:_ Investigation finding — anti-stuffing best practice from Weaviate, production RAG guides.

##### URI-FR-024: Injection format

- _Statement:_ Per-prompt injection shall be output as a JSON object with an `additionalContext` field containing markdown-formatted text. The format shall be:
  ```
  ## Relevant Context
  **Facts:** <top Graphiti facts as bullet points>
  **Gestalt:** slug1, slug2 — <one-line summaries>
  **Memory:** <Letta domain block excerpt if matched>
  ```
  Followed by the existing parallelism hints.
- _Rationale:_ Structured markdown in `additionalContext` is parsed by Claude as system context. Including both the new relevance results and existing parallelism hints in one JSON output preserves current functionality.
- _Acceptance:_ The `additionalContext` field contains both relevance injection results and existing parallelism/skill hints. Output is valid JSON.
- _Source:_ Preserving existing prompt-intelligence.sh behavior while extending it.

##### URI-FR-025: Prompt text extraction

- _Statement:_ The hook shall extract the user's prompt text for use as the retrieval query. The extraction method shall be compatible with Claude Code's UserPromptSubmit hook interface (stdin JSON or environment variable).
- _Rationale:_ Without the prompt text, the hook cannot perform relevance filtering. The exact mechanism depends on Claude Code's hook API.
- _Acceptance:_ The hook correctly extracts the user's message text and uses it as the query for all three backends. Verified with prompts containing technical terms, natural language questions, and slash commands.
- _Source:_ Constraint — prompt text availability is a prerequisite for Tier 2.

### 3.4 Per-Prompt Retrieval Quality

##### URI-FR-030: Relevance threshold

- _Statement:_ Results below a minimum relevance score shall be excluded from injection, even if the token budget is not exhausted. The threshold is configurable via `GESTALT_RELEVANCE_THRESHOLD` (default: 0.01 RRF score, equivalent to appearing in at least one backend's top 10).
- _Rationale:_ Injecting low-relevance results wastes tokens and can mislead the agent. An empty injection is preferable to a noisy one.
- _Acceptance:_ A prompt about "kubernetes deployment" does not inject facts about "snapshot generation" even if the token budget has room.
- _Source:_ Investigation finding — quality over quantity for per-prompt context.

##### URI-FR-031: Deduplication across layers

- _Statement:_ When the same fact appears in multiple layers (e.g., a Graphiti fact that matches a gestalt entry section), the system shall inject it once (from the highest-ranked source) and not duplicate it.
- _Rationale:_ Letta, Graphiti, and Gestalt KB store overlapping information (~20-30% overlap per investigation). Deduplication saves tokens.
- _Acceptance:_ A fact about "data-api uses materialized views" appearing in both Graphiti and the `example-service-api` gestalt entry is injected once, not twice.
- _Source:_ Investigation finding — 20-30% content overlap between Letta blocks and Graphiti.

### 3.5 Latency and Performance

##### URI-NFR-001: Per-prompt latency target

- _Statement:_ The `prompt-intelligence.sh` hook shall complete in under 500ms at P95, measured from hook invocation to stdout completion.
- _Rationale:_ Based on Graphiti P95 (~300ms with FalkorDB + embedding), gestalt_search (~200-500ms warm), and fan-out execution. 500ms is within the range users perceive as instantaneous for a pre-prompt operation.
- _Acceptance:_ Over 100 consecutive prompts, 95th percentile hook execution time is under 500ms. Measured via timestamp logging in the hook.
- _Source:_ Investigation — Graphiti/gestalt_search latency benchmarks.

##### URI-NFR-002: Warm-up at session start

- _Statement:_ The `gestalt-session-start.sh` hook shall trigger a warm-up call to `gestalt_search` (with a dummy query) to pre-load the embedding model into memory, preventing a 3-5 second cold start on the first prompt.
- _Rationale:_ The nomic-embed-text-v1.5 model takes 3-5s to load on first invocation. Without warm-up, the first prompt's Tier 2 injection would timeout or be severely delayed.
- _Acceptance:_ First prompt after session start completes Tier 2 injection in under 500ms. The warm-up call runs in background during session-start and does not add to session-start latency.
- _Source:_ Code trace — gestalt-mcp-server.py lazy-loads model.

##### URI-NFR-003: Individual backend timeout

- _Statement:_ Each of the three backend queries shall have an independent timeout of 400ms. If a backend exceeds its timeout, the hook shall proceed with results from the backends that responded.
- _Rationale:_ A single slow backend should not block injection from the others. Graceful degradation per-backend.
- _Acceptance:_ When Graphiti is slow (>400ms), injection still includes gestalt_search and Letta block results. The timed-out backend's results are omitted, not waited for.
- _Source:_ Design decision — graceful degradation inherited from GME pattern.

##### URI-NFR-004: Session-start total budget

- _Statement:_ The combined Tier 1 injection (Letta blocks + session summaries + CWD hint) shall not exceed 2,500 tokens.
- _Rationale:_ Tier 1 currently measures at ~1.1K tokens (blocks) + ~600 tokens (summaries) + ~50 tokens (CWD hint) = ~1,750 tokens. A 2,500 cap provides headroom while preventing regression to the current 6.4K.
- _Acceptance:_ Session-start injection is measured and logged. Total is under 2,500 tokens.
- _Source:_ Investigation — measured block sizes.

##### URI-NFR-005: Hook timeout configuration

- _Statement:_ The `prompt-intelligence.sh` hook shall have an explicit timeout of 2 seconds configured in the hooks settings. This provides margin above the P95 target while preventing runaway execution.
- _Rationale:_ Currently prompt-intelligence.sh has no configured timeout. A runaway query (e.g., Graphiti hanging) would block prompts indefinitely.
- _Acceptance:_ Hook is killed after 2 seconds if not complete. Timeout is configured in settings.json.
- _Source:_ Code trace — no timeout currently configured.

### 3.6 Graceful Degradation

##### URI-FR-040: Letta unavailable

- _Statement:_ When the Letta server is unreachable at session start, the system shall skip Tier 1 block injection and log a warning. Per-prompt Letta block matching shall also be skipped. Graphiti and gestalt_search shall proceed independently.
- _Rationale:_ Letta downtime should not prevent session start or Tier 2 injection from other sources.
- _Acceptance:_ With Letta stopped, session starts successfully with session summaries only. Per-prompt injection includes Graphiti facts and gestalt entries.
- _Source:_ Inherited from GME graceful degradation pattern.

##### URI-FR-041: Graphiti unavailable

- _Statement:_ When Graphiti is unreachable, per-prompt injection shall proceed with gestalt_search and Letta block matching only. No error is surfaced to the user.
- _Rationale:_ Graphiti is the most latency-sensitive backend. Its absence degrades relevance but does not break the system.
- _Acceptance:_ With Graphiti stopped, per-prompt injection still includes gestalt entry pointers and Letta block matches. No error in Claude Code output.
- _Source:_ Inherited from GME graceful degradation pattern.

##### URI-FR-042: Gestalt search index unavailable

- _Statement:_ When `gestalt.db` is missing or corrupt, per-prompt injection shall proceed with Graphiti and Letta block matching only. The session-start hook shall trigger an index rebuild in background.
- _Rationale:_ The SQLite index is disposable and rebuildable from knowledge/*.md files.
- _Acceptance:_ With gestalt.db deleted, per-prompt injection includes Graphiti facts and Letta blocks. Index rebuild starts automatically. After rebuild, subsequent prompts include gestalt results.
- _Source:_ Inherited from GME — index is derived, not canonical.

##### URI-FR-043: All backends unavailable

- _Statement:_ When all three backends are unreachable, per-prompt injection shall fall back to the existing static parallelism hints only (current prompt-intelligence.sh behavior).
- _Rationale:_ Total backend failure should not break the user experience. The parallelism hints are always-available (hardcoded).
- _Acceptance:_ With Letta, Graphiti, and gestalt.db all unavailable, prompt-intelligence.sh outputs the same static JSON it outputs today.
- _Source:_ Design decision — static fallback.

### 3.7 Post-Compaction Recovery

##### URI-FR-050: Re-inject Tier 1 after compaction

- _Statement:_ The `post-compact-reinject.sh` hook shall re-inject Tier 1 Letta blocks (same 5 blocks as session start) after automatic context compaction, in addition to the existing behavioral reminders.
- _Rationale:_ Compaction may discard the session-start injection. Tier 1 blocks are small (~1.1K tokens) and always relevant.
- _Acceptance:_ After compaction, Claude's context includes Tier 1 blocks. Agent behavior calibration is preserved.
- _Source:_ Investigation — post-compact-reinject.sh currently injects only static reminders (~150 tokens).

##### URI-FR-051: Task-inferred gestalt pointers after compaction

- _Statement:_ After compaction, the hook shall infer the current task from recent session context and inject up to 2 gestalt entry pointers via `gestalt_search(query=<inferred_task>, limit=2)`.
- _Rationale:_ Compaction loses the context accumulated during the session. Re-pointing the agent to relevant gestalt entries helps it recover without re-reading MANIFEST.md.
- _Acceptance:_ After compaction during a platform debugging session, the hook injects `Relevant gestalt: [[platform]]`.
- _Source:_ Design decision — lightweight context recovery.

### 3.8 Configuration

##### URI-FR-060: Environment-based configuration

- _Statement:_ All configurable parameters shall be read from `gestalt/.env` at hook invocation time. The following variables shall be supported:

| Variable | Default | Description |
|---|---|---|
| `GESTALT_TIER1_BLOCKS` | `core_directives\|user_preferences\|session_patterns\|guidance\|pending_items` | Pipe-delimited list of always-injected block labels |
| `GESTALT_TIER2_MAX_TOKENS` | `700` | Maximum tokens for per-prompt Tier 2 injection |
| `GESTALT_SEARCH_LIMIT` | `3` | Max results per backend query |
| `GESTALT_GRAPHITI_TIMEOUT` | `400` | Per-backend timeout in milliseconds |
| `GESTALT_RELEVANCE_THRESHOLD` | `0.01` | Minimum RRF score for injection |

- _Rationale:_ Configuration in `.env` allows tuning without code changes. Hooks read `.env` via `lib.sh` on every invocation — no restart needed.
- _Acceptance:_ Changing any variable in `.env` takes effect on the next prompt (Tier 2) or next session start (Tier 1).
- _Source:_ Design decision — consistent with existing hook configuration pattern.

### 3.9 User Journey Flows

##### URI-FR-070: Normal session flow

- _Statement:_ When a user starts a Claude Code session in `~/Documents/GitHub/platform/`, the system shall:
  1. Inject Tier 1 Letta blocks (~1.1K tokens) + last 3 session summaries + CWD hint `Relevant gestalt: [[platform]]`
  2. On first prompt ("fix the deploy script"), inject Tier 2 context: Graphiti facts about platform deploys, gestalt entry pointer `[[platform#deploy]]`, and matching Letta block excerpt from `project_context` if it mentions deploys
  3. On subsequent prompts, inject fresh Tier 2 context matched to each prompt's content
  4. At session end, Stop hook writes to Letta + Graphiti as usual (unchanged)
- _Rationale:_ End-to-end flow ensures all components connect correctly.
- _Acceptance:_ Complete session from start to end demonstrates Tier 1 at start, Tier 2 per-prompt, and unchanged Stop behavior.
- _Source:_ SRS flow requirements rule.

##### URI-FR-071: Compaction recovery flow

- _Statement:_ When automatic context compaction occurs mid-session, the system shall:
  1. Detect compaction via the `compact` matcher on SessionStart
  2. Re-inject Tier 1 Letta blocks (~1.1K tokens)
  3. Inject task-inferred gestalt pointers (top 2 via gestalt_search)
  4. Include existing behavioral reminders (skills list, MANIFEST.md reminder)
  5. Resume normal Tier 2 per-prompt injection on subsequent prompts
- _Rationale:_ Compaction should not degrade the agent's memory access for the remainder of the session.
- _Acceptance:_ After compaction, the agent has access to behavioral calibration, relevant gestalt pointers, and per-prompt Tier 2 injection.
- _Source:_ SRS flow requirements rule.

##### URI-FR-072: Degraded session flow

- _Statement:_ When Graphiti is down and the gestalt search index is stale, the system shall:
  1. Inject Tier 1 Letta blocks at session start (Letta is still up)
  2. On each prompt, attempt all 3 backend queries. Graphiti times out (400ms). gestalt_search returns stale results (still useful).
  3. Inject available results from gestalt_search + Letta block matching. Omit Graphiti.
  4. Log Graphiti timeout to health.log. No error surfaced to user.
- _Rationale:_ Partial failure should result in partial injection, not no injection.
- _Acceptance:_ With Graphiti stopped, per-prompt injection still includes gestalt and Letta results. No visible error.
- _Source:_ SRS flow requirements rule.

---

## 4. Verification Matrix

| Req ID | Verification Method | Pass Criteria |
|---|---|---|
| URI-FR-001 | Run 10+ sessions, inspect Letta blocks | No new blocks created |
| URI-FR-002 | GET /v1/agents/{id}, check block limits | Both blocks show `limit: 3000` |
| URI-FR-010 | Read session-start stdout, count blocks | Only 5 Tier 1 blocks present |
| URI-FR-011 | Change GESTALT_TIER1_BLOCKS, restart session | Different blocks injected |
| URI-FR-013 | Start session in platform/, check stdout | `Relevant gestalt: [[platform]]` present |
| URI-FR-020 | Send prompt, inspect hook stdout | Results from all 3 backends |
| URI-FR-021 | Timestamp each backend call | max(times) + 100ms >= total, not sum(times) |
| URI-FR-022 | Send prompt matching 2+ backends | Cross-backend match ranks higher |
| URI-FR-023 | Send prompt with many matches | Injection under configured token cap |
| URI-FR-024 | Inspect hook stdout JSON | Valid JSON with additionalContext containing markdown sections |
| URI-FR-030 | Send irrelevant prompt | Empty or near-empty Tier 2 injection |
| URI-FR-031 | Send prompt matching overlapping fact | Fact appears once, not twice |
| URI-FR-040 | Stop Letta, start session | Session starts with summaries only |
| URI-FR-041 | Stop Graphiti, send prompt | Injection includes gestalt + Letta, no Graphiti |
| URI-FR-042 | Delete gestalt.db, send prompt | Injection includes Graphiti + Letta, rebuild starts |
| URI-FR-043 | Stop all backends, send prompt | Static parallelism hints only |
| URI-FR-050 | Trigger compaction, check reinject | Tier 1 blocks present post-compaction |
| URI-FR-051 | Trigger compaction during platform work | `[[platform]]` pointer in reinject output |
| URI-NFR-001 | Log timestamps over 100 prompts | P95 < 500ms |
| URI-NFR-002 | Check first prompt latency | Tier 2 completes < 500ms on first prompt |
| URI-NFR-003 | Delay Graphiti >400ms, check behavior | Other backends still return results |
| URI-NFR-004 | Measure session-start injection | Under 2,500 tokens |
| URI-NFR-005 | Check settings.json | prompt-intelligence timeout = 2000 |

---

## 5. Promotion Plan

After implementation and validation:

1. Promote URI-CONST-001 through URI-CONST-004 to system-level gestalt SRS
2. Promote URI-NFR-001 (latency target) and URI-NFR-004 (session-start budget) to system-level NFRs
3. Update the gestalt knowledge entry (`knowledge/gestalt.md`) hooks section to reflect tiered injection
4. Update `letta-activation.md` rule to reference Tier 1/Tier 2 block categorization
5. Mark `gestalt-memory-expansion` SRS as superseded for injection-related requirements
