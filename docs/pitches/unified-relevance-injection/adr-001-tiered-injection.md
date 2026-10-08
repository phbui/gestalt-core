---
pitch_id: unified-relevance-injection
document: adr
adr_id: URI-ADR-001
title: "Tiered Memory Injection Over Always-In-Context"
status: accepted
date: 2026-04-09
---

# ADR-001: Tiered Memory Injection Over Always-In-Context

## Context

The gestalt memory stack uses Letta (MemGPT) for episodic memory. MemGPT's core design principle is that core memory blocks are **always in context** — injected into every prompt without filtering. This is an intentional analogy to CPU registers: the highest-priority data lives closest to the processor and is never paged to disk.

The current implementation follows this principle: `gestalt-session-start.sh` injects all 10 Letta blocks (~6.4K tokens) unconditionally at session start. This worked when the agent had 8 blocks totaling ~4.6K tokens. But the Letta agent autonomously created 2 additional domain-specific blocks (`service_api_notes`, `platform_arch_notes`) when `project_context` hit its 5K limit, growing injection to 6.4K with 100K-limit overflow blocks.

The workspace contains 35+ repos. Domain-specific blocks about data-api MCP scale tests are noise when the user is editing gestalt rules. The MemGPT assumption that all core memory is always relevant breaks in a multi-domain workspace.

Industry has converged on tiered injection as the solution (Google's context-aware multi-agent framework, OpenAI Agents SDK context personalization, Weaviate's context engineering guide). The pattern: always-in behavioral context + per-prompt relevance-filtered domain context.

## Options Considered

### Option A: Keep Always-In-Context (Status Quo)

Inject all Letta blocks unconditionally at session start.

- **Pro:** Zero implementation work. MemGPT's intended design. 6.4K tokens is only 3.2% of a 200K context window.
- **Pro:** No risk of false negatives (filtering out something relevant).
- **Con:** Token cost grows unbounded as the Letta agent creates more blocks. Already 10 blocks from a configured 8.
- **Con:** Domain-specific blocks are noise outside their domain. Attention dilution is real — production RAG literature shows context stuffing degrades answer quality.
- **Con:** Doesn't extend to other layers. Graphiti facts and gestalt KB entries can't use always-in-context (too much data).

### Option B: CWD-Aware Filtering

Inject blocks based on current working directory. In `platform/`, inject platform-related blocks; in `gestalt/`, inject gestalt blocks.

- **Pro:** Simple heuristic. CWD is always known.
- **Con:** Fragile. Multi-repo sessions are common (user starts in gestalt, switches to platform). CWD at session start doesn't predict mid-session work.
- **Con:** Doesn't help with Graphiti or gestalt KB — they need query-based filtering, not CWD-based.
- **Con:** Requires mapping block labels to repos — a brittle lookup table that breaks when blocks are renamed or created.

### Option C: Tiered Injection (Chosen)

Split blocks into two tiers:
- **Tier 1 (always-in):** Behavioral blocks that calibrate how the agent works — `core_directives`, `user_preferences`, `session_patterns`, `guidance`, `pending_items`. Injected at session start, unconditionally. ~1.1K tokens.
- **Tier 2 (per-prompt, relevance-filtered):** Domain blocks (`project_context`, `tool_guidelines`, self-created blocks) plus Graphiti facts and gestalt KB entries. Queried per-prompt based on the user's message, merged via RRF, capped at ~700 tokens.

- **Pro:** Preserves the MemGPT insight that behavioral context is too important to filter — it's always present.
- **Pro:** Extends naturally to all three layers. Same relevance gate for Letta domain blocks, Graphiti facts, and gestalt entries.
- **Pro:** Token-efficient. Session-start drops from 6.4K to 1.1K. Per-prompt adds ~700 tokens of high-relevance context.
- **Pro:** Matches industry consensus (Google, OpenAI, Weaviate pattern).
- **Con:** Adds ~400-500ms latency per prompt (fan-out retrieval).
- **Con:** Risk of false negatives — relevant domain context not injected because the query didn't match. Mitigated by low relevance threshold and CWD hint at session start.
- **Con:** More complex than status quo. Three concurrent queries, merge logic, token budget enforcement.

### Option D: Move Everything to Archival Memory

Migrate all Letta blocks to archival memory (vector-indexed passages with tag-based retrieval). Query archival per-prompt.

- **Pro:** True semantic filtering on all content.
- **Con:** Abandons core memory entirely — loses MemGPT's always-in behavioral context.
- **Con:** Archival retrieval is less reliable than core memory for behavioral calibration (retrieval can miss).
- **Con:** Graphiti already provides a temporal knowledge graph with semantic search — archival would be a second vector store doing overlapping work.
- **Con:** Significant migration effort for minimal gain over Option C.

## Decision

**Option C: Tiered Injection.**

The key insight is that not all memory is equal. Behavioral context (who the user is, how they work, what's pending) is always relevant — it calibrates the agent regardless of task. Domain context (architectural facts, project-specific notes) is relevant only when the task matches.

Tier 1 preserves MemGPT's core insight for the blocks that actually need it. Tier 2 applies the industry-standard relevance filtering pattern to everything else — including the two memory layers (Graphiti, gestalt KB) that were previously never injected at all.

The ~400-500ms per-prompt latency is acceptable. Users won't notice a half-second delay before a response that takes 5-30 seconds to generate. The token savings (6.4K → 1.1K at start, plus 700 tokens of targeted context per prompt) improve both cost and answer quality.

## Consequences

### Positive

- Session-start injection drops from ~6.4K to ~1.1K tokens (83% reduction)
- Graphiti facts are surfaced during regular work for the first time
- Gestalt KB entries are pointed to per-prompt without manual MANIFEST lookup
- Token budget is explicit and configurable, preventing unbounded growth
- Architecture extends cleanly: new memory layers get Tier 2 access automatically

### Negative

- Per-prompt latency increases by ~400-500ms P95
- False negatives possible: a relevant domain block could be missed if the query doesn't match
- System complexity increases: 3 concurrent queries, merge logic, timeout handling, fallback
- prompt-intelligence.sh goes from 7 lines to a substantial script with network calls

### Risks

- **Prompt text access:** The UserPromptSubmit hook may not receive the user's message text. If not, Tier 2 cannot extract query terms. Mitigation: verify Claude Code's hook API before implementation.
- **Cold start:** First gestalt_search call loads the embedding model (3-5s). Mitigation: warm-up call at session start (URI-NFR-002).
- **Cascade failure:** If all three backends are slow simultaneously, prompt latency exceeds 2s timeout. Mitigation: individual 400ms timeouts per backend, static fallback (URI-FR-043).

## Impact on Requirements

- **Adds:** URI-FR-010 through URI-FR-013 (Tier 1 session-start injection)
- **Adds:** URI-FR-020 through URI-FR-025 (Tier 2 per-prompt injection)
- **Adds:** URI-NFR-001 (500ms P95 latency target)
- **Modifies:** Existing session-start behavior (from "inject all blocks" to "inject Tier 1 only")
- **Constrains:** URI-CONST-004 (10K char hook output cap becomes a binding constraint for Tier 2)
- **Removes:** Unconditional injection of domain-specific Letta blocks at session start
