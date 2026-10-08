---
pitch_id: gestalt-memory-expansion
document: srs
version: 1.0
created: 2026-04-01
---

# Software Requirements Specification: Gestalt Memory Expansion

## 1. Introduction

### 1.1 Purpose

This SRS defines verifiable requirements for expanding gestalt from a single-layer curated knowledge system into a four-layer memory architecture with automatic knowledge capture, temporal fact tracking, and semantic search.

### 1.2 Scope

The system adds three new layers (episodic, temporal, semantic) around gestalt's existing curated layer, with always-on infrastructure and zero-friction auto-setup. All four phases are covered.

### 1.3 Definitions

| Term | Definition |
|---|---|
| **Curated layer** | gestalt's existing `knowledge/*.md` entries with MANIFEST, GRAPH, SOURCES indices |
| **Episodic layer** | Session-scoped memory: what happened in this and recent conversations |
| **Temporal layer** | Cross-session fact graph with bi-temporal validity windows (Graphiti) |
| **Semantic layer** | Vector + full-text index over curated entries for similarity search |
| **Auto-save** | Automatic knowledge capture at session end via Claude Code hooks |
| **Memory block** | A labeled string in a Letta agent's core memory, always visible in its context |

### 1.4 References

- [Pitch: gestalt-memory-expansion](./index.md)
- [Claude Code Hooks Docs](https://code.claude.com/docs/en/hooks)
- [Letta Self-Hosting Docs](https://docs.letta.com/guides/selfhosting)
- [Graphiti MCP Server](https://github.com/getzep/graphiti/blob/main/mcp_server/README.md)
- [sqlite-vec](https://github.com/asg017/sqlite-vec) + [SQLite FTS5](https://www.sqlite.org/fts5.html)
- [FastMCP](https://modelcontextprotocol.io/docs/develop/build-server)

### 1.5 Constraints Discovered During Investigation

| Constraint | Impact |
|---|---|
| Anthropic has no embedding API | Letta and Graphiti both require a separate embedding provider. Use local model (nomic-embed-text-v1.5) or OpenAI API key. |
| Letta model name format | Use `anthropic/claude-sonnet-4-6` with `model_endpoint_type: 'anthropic'`. Opus was originally considered but Sonnet is confirmed working and preferred. |
| Graphiti MCP tool names differ from README | Actual names: `add_memory`, `search_memory_facts`, `search_nodes` (confirmed via live MCP server) |
| Milvus Lite does not support BM25/hybrid search | Replaced with SQLite FTS5 + sqlite-vec for hybrid retrieval |
| Graphiti MCP server is marked "experimental" | APIs may change between releases. Pin to a known-good version. |
| Letta processes messages sequentially per agent | No concurrent sends to the same agent. Stop hook must not race with other sessions. |
| SessionEnd hooks have a 1.5s default timeout | Must use Stop hook (600s default for command type), NOT SessionEnd |

---

## 2. Product Overview

### 2.1 User Classes

- **Phi (primary operator)** — Opens Claude Code in `~/Documents/GitHub/`, works across multiple repos. Never invokes `/save` manually.
- **Claude Code agents** — Subagents spawned by gestalt skills (`/learn`, `/review`, `/investigate`). Need cross-session context and fast knowledge retrieval.
- **Cursor agents** — Secondary tool. Needs MCP access to all layers via HTTP transport.

### 2.2 Operating Environment

- Ubuntu Linux (kernel 6.17+)
- Docker Engine (for Letta + Graphiti containers)
- systemd (for always-on service management)
- Claude Code CLI (with hooks support)
- Python 3.11+ (for MCP servers and tooling)
- Anthropic API key at `~/Documents/anthropic-key.txt` (unlimited tokens)

### 2.3 Design Constraints

- CONST-001: Gestalt markdown files remain the canonical source of truth for curated knowledge
- CONST-002: All derived indices (vector, graph) must be rebuildable from markdown files
- CONST-003: Auto-save must not require any manual user action
- CONST-004: Infrastructure must survive reboots without manual intervention
- CONST-005: Letta's episodic memory is complementary context, not a replacement for curated entries

---

## 3. Requirements

### 3.1 External Interfaces

#### 3.1.1 Software Interfaces

##### GME-INT-001: Letta REST API

- _Statement:_ The system shall communicate with the Letta server via its REST API at `http://localhost:8283/v1`.
- _Rationale:_ Hooks and scripts need to create agents, send messages, and read memory blocks programmatically.
- _Acceptance:_ `curl http://localhost:8283/v1/health` returns 200 when the system is operational.
- _Source:_ Pitch decision — self-hosted Letta.

##### GME-INT-002: Graphiti MCP Server

- _Statement:_ The system shall expose Graphiti's temporal knowledge graph to Claude Code and Cursor via an MCP server at `http://localhost:8000/mcp/`.
- _Rationale:_ MCP is the standard protocol for tool access from AI coding assistants.
- _Acceptance:_ Claude Code can call `add_memory` and `search_memory_facts` tools via the registered MCP server.
- _Source:_ Pitch Phase 2.

##### GME-INT-003: Gestalt Search MCP Server

- _Statement:_ The system shall expose a `gestalt_search` MCP tool via a custom FastMCP server using stdio transport.
- _Rationale:_ Enables semantic + full-text search over gestalt entries from Claude Code and Cursor.
- _Acceptance:_ `gestalt_search("deploy pattern")` returns relevant entry sections with slugs, block-ids, and text.
- _Source:_ Pitch Phase 3.

##### GME-INT-004: Anthropic API

- _Statement:_ The system shall use the Anthropic API key at `~/Documents/anthropic-key.txt` for all LLM inference (Letta memory formation, Graphiti entity extraction, auto-save reasoning).
- _Rationale:_ Unlimited Claude tokens available; single key simplifies management.
- _Acceptance:_ All services authenticate successfully with the key and use Claude Sonnet for inference.
- _Source:_ Pitch decision.

##### GME-INT-005: Embedding Provider

- _Statement:_ The system shall use `nomic-embed-text-v1.5` as the local embedding model for the gestalt semantic index (Phase 3). Graphiti (Phase 2) uses OpenAI `text-embedding-3-small` via the OpenAI API.
- _Rationale:_ Anthropic has no embedding API. The semantic search layer uses a local model to avoid external dependencies; Graphiti requires an OpenAI-compatible provider.
- _Acceptance:_ Semantic index embeddings are generated locally on CPU in under 500ms per section on CPU. Batch embedding of 500 sections completes in under 120 seconds. No external embedding API calls are made for Phase 3 operations.
- _Source:_ Investigation finding — Anthropic has no embedding support.

---

### 3.2 Functional Requirements — Phase 1: Episodic Layer + Auto-Save

##### GME-FR-001: Always-on Letta server

- _Statement:_ The system shall run a self-hosted Letta server as a systemd service that starts automatically on boot and restarts on failure.
- _Rationale:_ Zero-friction infrastructure — the server must always be available when Claude Code opens.
- _Acceptance:_ After a system reboot, `systemctl status gestalt-services` shows active. `curl http://localhost:8283/v1/health` returns 200 within 30 seconds of boot.
- _Source:_ Pitch decision — always-on systemd.

##### GME-FR-002: Letta agent auto-creation

- _Statement:_ On first use, the system shall automatically create a Letta "Subconscious" agent configured with 8 named memory blocks: `core_directives`, `user_preferences`, `project_context`, `session_patterns`, `pending_items`, `guidance`, `self_improvement`, `tool_guidelines`.
- _Rationale:_ The agent must exist before memory can be stored or retrieved.
- _Acceptance:_ After the first SessionStart hook fires, `GET /v1/agents/{id}/core-memory/blocks` returns all 8 blocks. The agent ID is persisted to `~/.claude/gestalt/letta-agent-id.txt`.
- _Source:_ Pitch Phase 1 — combined Path A + B.

##### GME-FR-003: Letta model configuration

- _Statement:_ The system shall configure the Letta agent to use `anthropic/claude-sonnet-4-6` for inference via `llm_config` with `model_endpoint_type: 'anthropic'`. The system shall also configure `nomic-embed-text-v1.5` for embeddings.
- _Rationale:_ Sonnet provides sufficient reasoning for memory formation at lower cost and latency than Opus. Local embeddings avoid external API dependencies.
- _Acceptance:_ Agent creation request specifies `anthropic/claude-sonnet-4-6` in `llm_config` with `model_endpoint_type: 'anthropic'`. Agent is created successfully.
- _Source:_ Investigation finding — Sonnet is the confirmed working model.

##### GME-FR-004: SessionStart hook — memory injection

- _Statement:_ When Claude Code starts a session in `~/Documents/GitHub/`, a SessionStart hook shall: (a) health-check the Letta server, (b) retrieve current memory blocks via `GET /v1/agents/{id}/core-memory/blocks`, (c) read the last 3 session summary files from `~/.claude/memory/sessions/`, and (d) inject all memory content into the session context via stdout.
- _Rationale:_ Agents need cross-session context to be useful from the first message.
- _Acceptance:_ (a) Hook stdout contains Letta memory blocks (if reachable) and last 3 session summaries. (b) Hook completes within 10 seconds. Verifiable by running the hook script manually and inspecting stdout.
- _Source:_ Pitch Phase 1.

##### GME-FR-005: SessionStart hook — health check graceful degradation

- _Statement:_ If the Letta server is unreachable, the SessionStart hook shall inject only the file-based session summaries and log a warning. It shall NOT block session start or produce an error visible to the user.
- _Rationale:_ Infrastructure failures must not prevent normal work.
- _Acceptance:_ With Letta server stopped, Claude Code starts normally with session summaries injected. A warning is written to `~/.claude/gestalt/health.log`.
- _Source:_ Design principle — graceful degradation.

##### GME-FR-006: Stop hook — session end processing

- _Statement:_ When a Claude Code session ends, a Stop hook (`type: 'command'`) shall: (a) copy the transcript to a persistent store at `~/.claude/memory/transcripts/{session_id}.jsonl`, (b) send the transcript to the Letta agent via `POST /v1/agents/{id}/messages/async` for memory formation, (c) write a session summary to `~/.claude/memory/sessions/{YYYY-MM-DD}-{session-id}.md`, and (d) feed Graphiti with a session episode (if available).
- _Rationale:_ A single `type: 'command'` hook handles all deterministic shell operations at session end. Letta's built-in memory formation handles episodic knowledge extraction; manual `/save` handles curated knowledge capture.
- _Acceptance:_ After a session, the transcript is copied, Letta has received the async message, a session summary file exists, and a Graphiti episode was added (if Graphiti is available).
- _Source:_ Pitch Phase 1 — revised architecture.

##### GME-FR-007: Automatic transcript-to-gestalt processing

- _Statement:_ **Deferred** — automatic gestalt entry writing from transcripts is not implemented. Letta handles episodic memory formation from transcripts. Manual `/save` is still required for curated knowledge capture to gestalt markdown entries.
- _Rationale:_ The `type: 'agent'` auto-save hook was removed (see GME-CONST-004). Letta's built-in memory formation provides episodic context; curated gestalt entries require human-guided `/save` for quality control.
- _Acceptance:_ N/A — deferred.
- _Source:_ Architecture decision — auto-save via agent hook removed.

##### GME-FR-008: Stop hook — session summary file

- _Statement:_ The Stop hook shall write a compressed session summary (under 2000 tokens) to `~/.claude/memory/sessions/{YYYY-MM-DD}-{session-id}.md`.
- _Rationale:_ File-based summaries provide a fallback when Letta is unavailable and a quick-read history of recent sessions.
- _Acceptance:_ After each session, a summary file exists at the expected path. The file is readable markdown with a date header, key topics, and decisions.
- _Source:_ Pitch Phase 1 — Path A.

##### GME-FR-009: Session summary cleanup

- _Statement:_ The SessionStart hook shall delete session summary files older than 7 days, AFTER injecting session summaries (GME-FR-004).
- _Rationale:_ Prevent unbounded disk usage. Older context lives in Letta's persistent memory.
- _Acceptance:_ After each SessionStart hook fires, `~/.claude/memory/sessions/` contains only files from the last 7 days.
- _Source:_ Pitch — episodic lifecycle.

##### GME-FR-010: Manual `/save` override

- _Statement:_ The `/save` command shall continue to work as a manual mid-session knowledge capture. It shall follow the same logic as the Stop hook's gestalt auto-save but operate on the current (in-progress) conversation.
- _Rationale:_ Users may want to force-capture knowledge before a session ends.
- _Acceptance:_ Running `/save` mid-session creates/updates gestalt entries immediately.
- _Source:_ Pitch Phase 1.

##### GME-FR-011: Hook configuration deployment

- _Statement:_ The system shall install hook configuration in `~/Documents/GitHub/.claude/settings.json` with SessionStart and Stop hooks pointing to the gestalt hook scripts.
- _Rationale:_ Hooks must be registered at the workspace level to fire for all repos under `~/Documents/GitHub/`.
- _Acceptance:_ Opening Claude Code in any subdirectory of `~/Documents/GitHub/` triggers both hooks.
- _Source:_ Pitch Phase 1.

##### GME-FR-012: Session lifecycle data flow

- _Statement:_ When the user opens Claude Code in `~/Documents/GitHub/`, the SessionStart hook shall inject Letta memory blocks and the last 3 session summaries into context via stdout. When the session ends, the Stop command hook shall copy the transcript to `~/.claude/memory/transcripts/{session_id}.jsonl`, send it to Letta async, write a session summary, and feed Graphiti. The Stop agent hook shall read the transcript copy and run auto-save to write gestalt entries. The session summary from session N shall be readable by session N+1's SessionStart hook.
- _Rationale:_ Individual requirements (GME-FR-004, 006, 007, 008) specify each step but not the data contracts between them.
- _Acceptance:_ After session N ends, session N+1's context includes session N's summary and updated Letta blocks.
- _Source:_ srs-flow-requirements rule.

##### GME-FR-013: Idempotent install script

- _Statement:_ The system shall provide an idempotent install script at `gestalt/tools/install.sh` that automates: verifying Docker and Python are available, installing Python dependencies into `gestalt/tools/.venv`, creating the env file from `~/Documents/anthropic-key.txt`, installing the systemd service, starting Letta and creating the agent, deploying hook scripts, merging hook config into `settings.json`, registering MCP servers, building the semantic index, and printing a success summary.
- _Rationale:_ Zero-friction setup — a single command takes a fresh system to fully operational without manual steps.
- _Acceptance:_ Running `install.sh` on a fresh Ubuntu system with Docker and Python produces a fully operational system. Running it again produces no errors.
- _Source:_ Pitch Phase 1 — zero-friction setup.

---

### 3.3 Functional Requirements — Phase 2: Temporal Knowledge Graph

##### GME-FR-020: Graphiti service deployment

- _Statement:_ The system shall run Graphiti MCP server and FalkorDB as additional containers in the gestalt Docker Compose stack, managed by the same systemd service as Letta.
- _Rationale:_ Single service management point. Adding Phase 2 is `docker compose up` with the expanded config.
- _Acceptance:_ `curl http://localhost:8000/health` returns 200. FalkorDB is reachable at `localhost:6379`.
- _Source:_ Pitch Phase 2.

##### GME-FR-021: Graphiti namespace isolation

- _Statement:_ The system shall configure Graphiti with `group_id: gestalt` as the default namespace. Per-repo namespacing (e.g., `gestalt-platform`, `gestalt-spatial`) shall be configurable via environment variable.
- _Rationale:_ Prevents cross-contamination between projects while allowing cross-project queries when needed.
- _Acceptance:_ Episodes added with default config are scoped to group `gestalt`. `search_memory_facts` with `group_ids: ["gestalt"]` returns only gestalt-scoped facts.
- _Source:_ Graphiti investigation — group_id scoping.

##### GME-FR-022: Graphiti LLM configuration

- _Statement:_ The system shall configure Graphiti to use `anthropic` as the LLM provider with Claude Sonnet for entity extraction, contradiction resolution, and community summarization.
- _Rationale:_ Sonnet provides sufficient reasoning for knowledge graph operations at lower cost and latency. Unlimited tokens.
- _Acceptance:_ Graphiti config.yaml specifies `provider: anthropic`, `model: claude-sonnet-4-6`. Entity extraction produces accurate nodes and edges from test episodes.
- _Source:_ Architecture decision — Sonnet used throughout.

##### GME-FR-023: Graphiti embedding configuration

- _Statement:_ The system shall configure Graphiti to use OpenAI's `text-embedding-3-small` as the embedding model, requiring OPENAI_API_KEY to be set in the environment.
- _Rationale:_ Graphiti's embedding layer requires an OpenAI-compatible provider. This is documented as a Phase 2 prerequisite (see GME-CONST-002).
- _Acceptance:_ Graphiti config.yaml specifies OpenAI as the embedding provider with text-embedding-3-small. If OPENAI_API_KEY is not set, Graphiti starts but embedding-dependent operations degrade gracefully.
- _Source:_ Investigation finding — embedding constraint.

##### GME-FR-024: Claude Code MCP registration for Graphiti

- _Statement:_ The system shall register the Graphiti MCP server with Claude Code using HTTP transport: `claude mcp add graphiti-memory --transport http http://localhost:8000/mcp/`.
- _Rationale:_ HTTP transport works with Docker-hosted servers. Simpler than stdio for containerized services.
- _Acceptance:_ Claude Code can invoke `add_memory`, `search_memory_facts`, and `search_nodes` tools.
- _Source:_ Graphiti investigation — MCP registration.

##### GME-FR-025: Auto-feed episodes from Stop hook

- _Statement:_ The Stop hook shall, after sending the transcript to Letta, also call Graphiti's `add_memory` tool with a compressed session summary as the episode body and `group_id: gestalt`.
- _Rationale:_ Every session's discoveries become searchable temporal facts.
- _Acceptance:_ After a session, `search_memory_facts("topic discussed in session")` returns relevant facts with valid timestamps.
- _Source:_ Pitch Phase 2.

##### GME-FR-026: Auto-feed episodes from `/learn`

- _Statement:_ After the `/learn` skill writes or updates gestalt knowledge entries, it shall call `add_memory` for each entry with the entry content as the episode body.
- _Rationale:_ Knowledge entries become nodes in the temporal graph, enabling cross-reference and staleness tracking.
- _Acceptance:_ After `/learn platform`, `search_nodes("platform")` returns an entity node with a summary derived from the entry.
- _Source:_ Pitch Phase 2.

##### GME-FR-027: Auto-feed episodes from `/review`

- _Statement:_ After the `/review` skill verifies or updates gestalt entries, it shall add episodes reflecting any changes found (corrections, outdated facts, new discoveries).
- _Rationale:_ Review findings become temporal facts — enabling "what changed" queries.
- _Acceptance:_ After `/review` corrects a stale fact, `search_memory_facts("corrected topic")` returns the new fact with `created_at` reflecting the review time, and the old fact has a non-null `expired_at`.
- _Source:_ Pitch Phase 2.

##### GME-FR-028: `/recall` command

- _Statement:_ The system shall provide a `/recall` command that queries Graphiti's `search_memory_facts` and `search_nodes` tools, returning temporal facts with validity windows.
- _Rationale:_ Users need to query cross-session history: "what did I learn about X?", "what changed recently?"
- _Acceptance:_ `/recall "platform deploy"` returns facts about platform deployment with timestamps. `/recall "what changed last week"` returns recently created or invalidated facts.
- _Source:_ Pitch Phase 2.

##### GME-FR-029: Automatic staleness via Graphiti

- _Statement:_ When the SessionStart hook detects that repos under `~/Documents/GitHub/` have new commits since the last session, it shall add an episode to Graphiti summarizing the changed files. Graphiti's contradiction detection shall automatically invalidate stale facts.
- _Rationale:_ Replaces the manual `bin/check-staleness.sh` with automatic, continuous staleness tracking.
- _Acceptance:_ After a git push that changes platform's deploy config, `search_memory_facts("platform deploy")` shows the old fact as expired and the new fact as current.
- _Source:_ Pitch Phase 2.

##### GME-FR-030: Graphiti graceful degradation

- _Statement:_ If the Graphiti MCP server is unreachable, all gestalt skills and hooks shall continue functioning without it. Graphiti calls shall be skipped with a logged warning, not raise errors.
- _Rationale:_ Infrastructure failures must not block normal work.
- _Acceptance:_ With Graphiti stopped, `/learn`, `/review`, `/save`, and Stop hooks complete successfully. Warnings appear in `~/.claude/gestalt/health.log`.
- _Source:_ Design principle — graceful degradation.

##### GME-FR-031: Knowledge write pipeline flow

- _Statement:_ When `/learn` writes a gestalt entry, the following shall occur in order: (1) entry written to `knowledge/{slug}.md`, (2) indices regenerated via `gestalt rebuild`, (3) semantic index rebuilt, (4) entry fed to Graphiti via `add_memory`. After `/learn` completes, the entry is readable via `gestalt_read`, `gestalt_search`, and `search_memory_facts`.
- _Rationale:_ Individual requirements (GME-FR-026, GME-FR-045) specify individual steps but not the ordering or data contracts between them.
- _Acceptance:_ After `/learn`, all three retrieval paths (`gestalt_read`, `gestalt_search`, `search_memory_facts`) return the new entry within 30 seconds.
- _Source:_ srs-flow-requirements rule.

##### GME-FR-032: Knowledge consolidation command

- _Statement:_ The system shall provide a `/consolidate` command that synthesizes knowledge across all four memory layers: (1) checks Letta memory blocks for facts that should be promoted to curated gestalt entries, (2) uses semantic search to discover related entries that are not cross-linked with `wikilinks`, (3) queries Graphiti for recently expired facts and flags corresponding stale gestalt entries, (4) rebuilds indices and feeds updates to Graphiti.
- _Rationale:_ Individual layers accumulate knowledge independently but don't cross-pollinate. Consolidation ensures Letta learnings become permanent curated knowledge and cross-links are maintained.
- _Acceptance:_ After running `/consolidate`, the report shows promotions made, cross-links added, and stale entries flagged. New `wikilinks` appear in updated entries.
- _Source:_ AutoDream equivalent — knowledge synthesis across memory layers.

##### GME-FR-033: Consolidation auto-nudge

- _Statement:_ The SessionStart hook shall count sessions via a counter file at `$STATE_DIR/session-counter`. Every 5th session, it shall inject a `<gestalt-consolidation-nudge>` into the session context via stdout suggesting the user run `/consolidate`.
- _Rationale:_ Consolidation should happen periodically but not every session. A nudge lets the agent decide whether to act on it.
- _Acceptance:_ On the 5th, 10th, 15th... session, the nudge appears in context. On other sessions, it does not.
- _Source:_ AutoDream equivalent — periodic maintenance trigger.

---

### 3.4 Functional Requirements — Phase 3: Semantic Search Layer

##### GME-FR-040: Gestalt semantic index

- _Statement:_ The system shall maintain a SQLite database at `gestalt/.search/gestalt.db` containing a FTS5 full-text index and sqlite-vec vector index over all gestalt knowledge entries.
- _Rationale:_ Enables hybrid BM25 + dense vector search over curated knowledge. SQLite FTS5 + sqlite-vec supports native hybrid search; Milvus Lite does not.
- _Acceptance:_ `gestalt/.search/gestalt.db` exists and contains both FTS5 and vector tables. File size is under 50MB for the current corpus.
- _Source:_ Investigation finding — Milvus Lite lacks hybrid search.

##### GME-FR-041: Section-level chunking

- _Statement:_ The index builder shall chunk knowledge entries by `##` heading boundaries, preserving `^block-id` anchors as metadata. Each chunk shall include: slug, heading, block-id (if present), and full text.
- _Rationale:_ Section-level granularity matches gestalt's block-id system and provides useful retrieval units.
- _Acceptance:_ An entry with 5 `##` sections produces 5 indexed chunks, each with correct slug and block-id metadata.
- _Source:_ Pitch Phase 3.

##### GME-FR-042: Embedding model

- _Statement:_ The index builder shall use `nomic-embed-text-v1.5` (768 dimensions, 8k context) for dense vector embeddings, running locally on CPU.
- _Rationale:_ Consistent with the embedding model used across all layers. 8k context handles long sections without truncation.
- _Acceptance:_ Embeddings are generated locally. No external API calls. Each section embeds in under 10ms on CPU.
- _Source:_ Investigation recommendation.

##### GME-FR-043: Hybrid search retrieval

- _Statement:_ The `gestalt_search` MCP tool shall perform hybrid retrieval: BM25 full-text search (via FTS5) combined with dense vector similarity (via sqlite-vec), merged using Reciprocal Rank Fusion (RRF).
- _Rationale:_ Hybrid search improves recall 15-30% over dense-only on technical documentation where exact term matching (function names, CLI flags, YAML keys) is as important as semantic similarity.
- _Acceptance:_ `gestalt_search("ANTHROPIC_API_KEY")` returns relevant sections via BM25 term matching even if the semantic embedding doesn't rank them first. `gestalt_search("how to deploy to production")` returns relevant sections via semantic similarity even if exact terms don't match.
- _Source:_ Investigation finding — hybrid advantage.

##### GME-FR-044: Gestalt search MCP server

- _Statement:_ The system shall expose a `gestalt_search` tool via a custom FastMCP server using stdio transport, registered with Claude Code.
- _Rationale:_ stdio transport is simpler for local Python processes (no Docker needed).
- _Acceptance:_ Claude Code can call `gestalt_search(query="...", limit=5)` and receive results with slug, heading, block-id, text, and relevance score.
- _Source:_ Pitch Phase 3.

##### GME-FR-045: Index rebuild on write

- _Statement:_ The semantic index shall be rebuilt automatically whenever gestalt indices are regenerated (after `/learn`, `/review`, `/save`, or auto-save).
- _Rationale:_ The vector index must stay in sync with the canonical markdown files.
- _Acceptance:_ After `/learn` adds a new entry, `gestalt_search` immediately returns sections from the new entry. Rebuild completes in under 10 seconds for the current corpus (~50 files, ~500 sections).
- _Source:_ Pitch Phase 3.

##### GME-FR-046: Index is disposable

- _Statement:_ The semantic index database (`gestalt/.search/gestalt.db`) shall be a derived artifact, excluded from git (added to `.gitignore`), and fully rebuildable from gestalt knowledge files via a single command.
- _Rationale:_ Design principle — markdown is canonical, indices are derived.
- _Acceptance:_ Deleting `gestalt.db` and running the rebuild command produces an identical, fully functional index.
- _Source:_ Design principle.

---

### 3.5 Functional Requirements — Phase 4: MCP Unification + Cursor Parity

##### GME-FR-050: Unified gestalt MCP server

- _Statement:_ The unified gestalt MCP server exposes four tools: `gestalt_read(slug)`, `gestalt_search(query)`, `gestalt_manifest()`, `gestalt_graph()`. Graphiti temporal tools (`search_memory_facts`, `search_nodes`, `add_memory`) are accessed directly via the `graphiti-memory` MCP server.
- _Rationale:_ Proxying Graphiti through the unified server was removed (see GME-FR-054). Four tools cover the curated and semantic layers; Graphiti tools are available directly.
- _Acceptance:_ All four tools are callable from Claude Code. Each returns structured results.
- _Source:_ Pitch Phase 4 — revised scope.

##### GME-FR-051: Gestalt read tool

- _Statement:_ `gestalt_read(slug)` shall return the full content of `gestalt/knowledge/{slug}.md`, including frontmatter.
- _Rationale:_ Direct file access via MCP, useful when the agent knows the exact entry it needs.
- _Acceptance:_ `gestalt_read("platform")` returns the full content of `knowledge/platform.md`.
- _Source:_ Pitch Phase 4.

##### GME-FR-052: Cursor HTTP transport

- _Statement:_ The unified MCP server shall support both stdio transport (for Claude Code) and HTTP transport (for Cursor), configurable at startup.
- _Rationale:_ Cursor's MCP client uses HTTP; Claude Code prefers stdio for local servers.
- _Acceptance:_ Cursor can call all four gestalt tools via `http://localhost:{port}/mcp/`. Claude Code calls the same tools via stdio. Graphiti temporal tools are accessed via the separate `graphiti-memory` MCP server.
- _Source:_ Pitch Phase 4.

##### GME-FR-053: Cursor MCP config sync

- _Statement:_ The system shall maintain a `.cursor/mcp.json` configuration file that mirrors the Claude Code MCP server registrations.
- _Rationale:_ Dual-tool sync rule — both tools must have equivalent configurations.
- _Acceptance:_ Both Claude Code and Cursor can access all gestalt MCP tools with equivalent behavior.
- _Source:_ Gestalt dual-agent-sync rule.

##### GME-FR-054: Proxy to Graphiti

- _Statement:_ **Not implemented.** Proxy to Graphiti was removed — Graphiti's Streamable HTTP transport cannot be proxied via httpx. Users call Graphiti MCP tools (`search_memory_facts`, `search_nodes`, `add_memory`) directly via the `graphiti-memory` MCP server.
- _Rationale:_ Technical constraint discovered during implementation. Direct access to Graphiti MCP tools is equivalent and simpler.
- _Acceptance:_ N/A — not implemented.
- _Source:_ Implementation finding — httpx proxy incompatible with Streamable HTTP transport.

---

### 3.6 Quality of Service

##### GME-NFR-001: SessionStart hook latency

- _Statement:_ The SessionStart hook shall complete within 10 seconds, including Letta health check, memory block retrieval, and session file injection.
- _Rationale:_ Users should not notice the hook running. 10 seconds is below the threshold of perceived delay for session startup.
- _Acceptance:_ Measured hook execution time is under 10 seconds in normal operation (Letta server warm).
- _Source:_ Pitch Phase 1.

##### GME-NFR-002: Stop hook background execution

- _Statement:_ The Stop hook's Letta message send shall use the async API (`POST /v1/agents/{id}/messages/async`) so it does not block session termination.
- _Rationale:_ Users should be able to close their terminal immediately. Memory formation happens in the background.
- _Acceptance:_ Session terminates within 5 seconds of the user's last message. Letta processes the transcript asynchronously (verified by checking memory blocks in the next session).
- _Source:_ Investigation finding — Letta async API.

##### GME-NFR-003: Stop hook timeout

- _Statement:_ **Deferred** — the `type: 'agent'` auto-save hook was removed (see GME-CONST-004). The `type: 'command'` Stop hook uses the default 600s timeout which is sufficient for all shell operations. No custom timeout override is required.
- _Rationale:_ Shell operations (curl, file writes) complete well within the default timeout. The LLM reasoning step that required 120s no longer exists in this hook.
- _Acceptance:_ N/A — deferred.
- _Source:_ Architecture decision — agent hook removed.

##### GME-NFR-004: Semantic search latency

- _Statement:_ `gestalt_search` shall return results within 500ms for the current corpus (~50 files, ~500 sections).
- _Rationale:_ Search must feel instantaneous to the agent. SQLite FTS5 + sqlite-vec achieves sub-5ms on this corpus size.
- _Acceptance:_ Measured P95 latency is under 500ms including embedding generation for the query.
- _Source:_ Pitch success criteria.

##### GME-NFR-005: Infrastructure restart resilience

- _Statement:_ All Docker services (Letta, FalkorDB, Graphiti) shall persist their data in named Docker volumes and restart automatically via systemd on system reboot or service failure.
- _Rationale:_ Zero manual intervention after reboot.
- _Acceptance:_ After `sudo reboot`, all three services are running within 60 seconds. Agent memory blocks, graph data, and vector indices survive the restart.
- _Source:_ Pitch decision — always-on systemd.

##### GME-NFR-006: Graceful degradation hierarchy

- _Statement:_ The system shall degrade gracefully if any infrastructure component is unavailable, following this hierarchy: (1) All services up → full functionality, (2) Graphiti down → curated + episodic layers work, temporal queries return empty, (2b) FalkorDB down → same as level 2 (Graphiti depends on FalkorDB), (3) Letta down → curated layer works, file-based session summaries provide episodic context, (3b) Letta down AND session summary write fails → curated layer only (level 4 for episodic layer), (4) All services down → gestalt files are still readable via standard file tools.
- _Rationale:_ The curated layer (markdown files) has zero infrastructure dependencies and must always work.
- _Acceptance:_ Each degradation level tested by stopping the relevant Docker container. No errors visible to the user; warnings logged.
- _Source:_ Design principle — markdown is canonical.

---

### 3.7 Design and Implementation Constraints

##### GME-CONST-001: Markdown canonical

- _Statement:_ Gestalt markdown files shall remain the canonical source of truth. All other data stores (Letta, Graphiti, SQLite) are derived or complementary.
- _Rationale:_ Human-readable, git-versioned knowledge is gestalt's core value proposition.
- _Acceptance:_ Deleting all Docker volumes and rebuilding produces a fully functional system from markdown files alone.
- _Source:_ Design principle.

##### GME-CONST-002: API key requirements

- _Statement:_ All LLM inference shall use the Anthropic API key at `~/Documents/anthropic-key.txt`. Graphiti (Phase 2) additionally requires an OpenAI API key for embeddings (text-embedding-3-small). The semantic search layer (Phase 3) uses local embeddings (nomic-embed-text-v1.5) only.
- _Rationale:_ Minimize credential management. Unlimited Anthropic tokens available. Graphiti's embedding layer requires OpenAI; the semantic index layer is self-contained with a local model.
- _Acceptance:_ Phase 1 and Phase 3 operate with only the Anthropic API key. Phase 2 additionally requires OPENAI_API_KEY documented in the setup guide.
- _Source:_ Pitch decision.

##### GME-CONST-003: No user-visible errors from hooks

- _Statement:_ Hooks shall never produce output that interrupts the user's workflow. Errors shall be logged to `~/.claude/gestalt/health.log`, not displayed.
- _Rationale:_ Invisible infrastructure — the user should forget the system exists.
- _Acceptance:_ With any single service down, Claude Code sessions start and stop without error messages.
- _Source:_ Design principle — zero friction.

##### GME-CONST-004: Hook type — single command hook

- _Statement:_ The Stop hook shall be a single `type: 'command'` hook. LLM-powered auto-save to gestalt entries is handled by Letta's memory formation, not by a separate Claude Code agent hook. The `type: 'agent'` approach was removed because it used Opus and was too slow.
- _Rationale:_ The `type: 'agent'` hook was evaluated and rejected — it invoked Opus at session end, adding unacceptable latency. Letta's async memory formation achieves the episodic goal without blocking session termination. Curated gestalt entry writing remains a manual `/save` operation.
- _Acceptance:_ Hook config specifies a single Stop hook entry with `"type": "command"`. No `type: 'agent'` Stop hook exists.
- _Source:_ Architecture decision — agent hook removed.

##### GME-CONST-005: Concurrent session serialization

- _Statement:_ When multiple Claude Code sessions end concurrently, the Stop command hook shall serialize using `flock` on `~/.claude/gestalt/stop-hook.lock`, waiting up to 60 seconds before logging a warning and exiting.
- _Rationale:_ Letta processes messages sequentially per agent. Concurrent Stop hooks racing to send transcripts can corrupt memory formation or produce duplicate summaries.
- _Acceptance:_ Two simultaneous session endings both produce summaries and Letta calls without corruption. Lock file is created at `~/.claude/gestalt/stop-hook.lock` when the hook runs.
- _Source:_ Investigation finding — Letta sequential processing constraint.

---

## 4. Verification

| Requirement | Verification Method |
|---|---|
| GME-INT-001 (Letta REST API) | `curl http://localhost:8283/v1/health` returns 200 |
| GME-INT-002 (Graphiti MCP server) | Call `add_memory` and `search_memory_facts` via MCP; inspect response |
| GME-INT-003 (gestalt_search MCP) | `gestalt_search("deploy pattern")` returns results with slugs and block-ids |
| GME-INT-004 (Anthropic API) | Confirm all services authenticate; check inference calls use Claude Sonnet |
| GME-INT-005 (embedding provider) | Run batch embed of 500 sections; verify <120s, no external API calls |
| GME-FR-001 (systemd service) | `systemctl status gestalt-services` after reboot |
| GME-FR-002 (agent auto-creation) | Check `~/.claude/gestalt/letta-agent-id.txt` exists; GET memory blocks; verify all 8 block names |
| GME-FR-003 (Letta model config) | Inspect agent creation request; verify model field; check fallback warning if unrecognized |
| GME-FR-004 (SessionStart injection) | Run hook script manually; inspect stdout for Letta blocks and session summaries |
| GME-FR-005 (SessionStart degradation) | Stop Letta; start Claude Code; verify session starts without user-visible error |
| GME-FR-006 (Stop dual-write) | End session; check Letta blocks updated and gestalt entry written |
| GME-FR-007 (auto-save quality) | Compare auto-saved entries against manual `/save` output for same conversation |
| GME-FR-008 (session summary file) | After session, check `~/.claude/memory/sessions/{date}-{id}.md` exists and is valid markdown |
| GME-FR-009 (session summary cleanup) | Create files >7 days old in sessions dir; run SessionStart; verify old files deleted |
| GME-FR-010 (manual `/save`) | Run `/save` mid-session; verify gestalt entries created/updated immediately |
| GME-FR-011 (hook config deployment) | Open Claude Code in any `~/Documents/GitHub/` subdirectory; confirm both hooks fire |
| GME-FR-012 (session lifecycle flow) | Run full session; verify session N+1 context includes session N summary and Letta blocks |
| GME-FR-013 (install script) | Run `install.sh` on fresh system; verify all components operational; run again, verify no errors |
| GME-FR-020 (Graphiti deployment) | `curl http://localhost:8000/health` returns 200; verify FalkorDB reachable at `localhost:6379` |
| GME-FR-021 (Graphiti namespace) | Add episode with default config; `search_memory_facts` with `group_ids: ["gestalt"]` returns it |
| GME-FR-022 (Graphiti LLM config) | Inspect Graphiti config.yaml; verify `provider: anthropic`, `model: claude-sonnet-4-6` |
| GME-FR-023 (Graphiti embedding config) | Inspect config.yaml; verify OpenAI embedding provider; test graceful degradation without OPENAI_API_KEY |
| GME-FR-024 (MCP registration) | `claude mcp list` shows graphiti-memory; invoke `add_memory`, `search_memory_facts`, `search_nodes` |
| GME-FR-025 (auto-feed from Stop hook) | End session; call `search_memory_facts("topic discussed")` in next session; verify results |
| GME-FR-026 (auto-feed from `/learn`) | Run `/learn platform`; call `search_nodes("platform")`; verify entity node exists |
| GME-FR-027 (auto-feed from `/review`) | Run `/review` on stale entry; call `search_memory_facts`; verify old fact expired, new fact current |
| GME-FR-028 (`/recall` command) | Run `/recall "test topic"` after sessions discussing that topic; verify timestamps |
| GME-FR-029 (automatic staleness) | Push git commit changing a config file; run SessionStart; verify episode added and stale fact expires |
| GME-FR-030 (Graphiti degradation) | Stop Graphiti; run `/learn`, `/review`, `/save`, Stop hook; verify all complete without error |
| GME-FR-031 (knowledge write pipeline) | Run `/learn`; within 30s call `gestalt_read`, `gestalt_search`, `search_memory_facts`; verify all return new entry |
| GME-FR-032 (consolidation command) | Run `/consolidate`; verify report shows promotions, cross-links, stale flags |
| GME-FR-033 (consolidation auto-nudge) | Check session counter; on 5th session verify nudge in context; on 4th verify no nudge |
| GME-FR-040 (semantic index) | Check `gestalt/.search/gestalt.db` exists after build; verify FTS5 and vector tables present |
| GME-FR-041 (section-level chunking) | Inspect index for 5-section entry; verify 5 chunks with correct slug and block-id metadata |
| GME-FR-042 (embedding model) | Run embedding; verify local process used, no external API calls, <10ms per section |
| GME-FR-043 (hybrid search) | Test with exact term query (BM25) AND semantic query; both return relevant results |
| GME-FR-044 (gestalt_search MCP server) | Call `gestalt_search(query="...", limit=5)` from Claude Code; verify response structure |
| GME-FR-045 (index rebuild on write) | Run `/learn`; call `gestalt_search` for new entry immediately; verify result appears |
| GME-FR-046 (index is disposable) | Delete `gestalt.db`; run rebuild command; verify fully functional index produced |
| GME-FR-050 (unified MCP) | All 4 gestalt tools callable from Claude Code; Graphiti tools via separate MCP server |
| GME-FR-051 (gestalt_read tool) | `gestalt_read("platform")` returns full content of `knowledge/platform.md` |
| GME-FR-052 (Cursor HTTP transport) | Call all 4 gestalt tools from Cursor via HTTP; verify equivalent results |
| GME-FR-053 (Cursor MCP config sync) | Inspect `.cursor/mcp.json`; verify all Claude Code MCP servers are mirrored |
| GME-FR-054 (proxy to Graphiti) | N/A — not implemented (see GME-FR-054) |
| GME-NFR-001 (SessionStart <10s) | `time` the hook script execution; verify under 10 seconds with warm Letta |
| GME-NFR-002 (Stop hook async) | End session; verify terminal closes within 5 seconds; check Letta blocks in next session |
| GME-NFR-003 (Stop hook timeout) | N/A — deferred (agent hook removed; command hook uses default 600s timeout) |
| GME-NFR-004 (search <500ms) | Benchmark 10 queries; check P95 latency including embedding generation |
| GME-NFR-005 (restart resilience) | `sudo reboot`; check all services running within 60 seconds; verify data persisted |
| GME-NFR-006 (graceful degradation) | Stop each service individually (Graphiti, FalkorDB, Letta); verify behavior at each level |
| GME-CONST-001 (markdown canonical) | Delete all Docker volumes; rebuild; verify fully functional system from markdown alone |
| GME-CONST-002 (API key requirements) | Verify Phase 1+3 work with Anthropic key only; verify Phase 2 documents OPENAI_API_KEY requirement |
| GME-CONST-003 (no user-visible errors) | Stop any single service; run Claude Code session; verify no error output to user |
| GME-CONST-004 (single command hook) | Inspect settings.json; verify single Stop hook entry with `"type": "command"`; confirm no `type: 'agent'` Stop hook |
| GME-CONST-005 (concurrent session serialization) | Simulate two concurrent session endings; verify both produce summaries without corruption |

---

## 5. Promotion Plan

When this pitch is complete, the following requirements become permanent gestalt system requirements:

| Requirement | Target System | Action |
|---|---|---|
| GME-FR-001 | gestalt | Add as system infrastructure requirement |
| GME-FR-004, GME-FR-006 | gestalt | Add as session lifecycle requirements |
| GME-FR-007 | gestalt | Replace manual `/save` requirement with auto-save |
| GME-FR-040–046 | gestalt | Add as search subsystem requirements |
| GME-FR-050–054 | gestalt | Add as MCP interface requirements |
| GME-NFR-001–006 | gestalt | Add as quality-of-service requirements |
| GME-CONST-001–005 | gestalt | Add as system constraints |
