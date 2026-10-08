---
pitch_id: unified-relevance-injection
document: adr
adr_id: URI-ADR-002
title: "Fan-Out Retrieval with Reciprocal Rank Fusion"
status: accepted
date: 2026-04-09
---

# ADR-002: Fan-Out Retrieval with Reciprocal Rank Fusion

## Context

The Tier 2 per-prompt injection system must query three memory backends:

1. **Graphiti** — temporal knowledge graph, returns entity-relationship facts with graph distance scores
2. **Gestalt KB** — curated entries via SQLite FTS5 + sqlite-vec, returns sections with hybrid BM25+cosine scores
3. **Letta domain blocks** — in-memory JSON, returns keyword match frequency

These backends use fundamentally different scoring systems. Graphiti returns graph traversal distances. Gestalt search returns RRF-merged BM25+cosine scores. Letta block matching returns keyword frequency counts. The scores are not comparable across backends.

The system needs to merge results from all three into a single relevance-ordered list, capped at ~700 tokens, within a ~500ms latency budget.

## Options Considered

### Option A: Sequential Query with Score Normalization

Query backends one at a time. Normalize scores to 0-1 range per backend. Sort merged results by normalized score.

- **Pro:** Simple control flow. Easy to debug.
- **Con:** Sequential latency: ~300ms + ~500ms + ~50ms = ~850ms. Exceeds 500ms target.
- **Con:** Score normalization is brittle. A backend returning one high-confidence result gets normalized differently than one returning ten moderate results. Min-max normalization distorts when result sets have different distributions.

### Option B: Fan-Out with Weighted Score Averaging

Query all three backends concurrently. Assign weights per backend (e.g., gestalt 0.5, Graphiti 0.3, Letta 0.2). Normalize and weight-average scores.

- **Pro:** Fan-out achieves max(latency) instead of sum(latency).
- **Pro:** Backend weights let you prioritize curated KB over ephemeral memory.
- **Con:** Requires score normalization — same brittleness as Option A.
- **Con:** Weight tuning is arbitrary without evaluation data. How do you know 0.5/0.3/0.2 is better than 0.4/0.4/0.2?

### Option C: Fan-Out with Reciprocal Rank Fusion (Chosen)

Query all three backends concurrently. Merge using RRF: `score(d) = sum(1/(K + rank_i))` where K=60 and rank_i is the document's rank in each backend's result list. RRF is score-agnostic — it uses only rank position, not raw scores.

- **Pro:** Fan-out achieves max(latency) ≈ 500ms P95.
- **Pro:** RRF is score-agnostic. No normalization needed. Works regardless of how each backend scores results.
- **Pro:** Already proven in the codebase: `gestalt-mcp-server.py` uses RRF to merge BM25 and vector results (lines 130-138). Same algorithm, one level up.
- **Pro:** A document appearing in multiple backends' top results naturally ranks higher — cross-backend agreement signals relevance.
- **Pro:** K=60 is a well-studied constant that works across diverse retrieval systems.
- **Con:** Rank-based merging loses absolute confidence signals. A backend's #1 result with 0.99 confidence is treated the same as a #1 with 0.51 confidence.
- **Con:** Small result sets (e.g., Letta returns 1 match) have less rank resolution than large ones.

### Option D: Reranker Model

Fan-out, collect all results, then pass through a cross-encoder reranker model to produce a unified relevance score.

- **Pro:** Highest quality ranking. Cross-encoders outperform bi-encoder + fusion approaches.
- **Con:** Adds 200-500ms for the reranker pass. Total would be ~700-1000ms, exceeding the budget.
- **Con:** Requires loading a reranker model — additional memory and cold-start latency.
- **Con:** Overkill for 3-9 results from 3 backends. Rerankers shine with 50+ candidates.

## Decision

**Option C: Fan-Out with RRF.**

RRF is the right tool for merging heterogeneous ranked lists when:
- Backends use incompatible scoring systems (our case)
- Result sets are small (3-9 results total)
- Latency is constrained (no time for a reranker)
- The pattern is already proven in the codebase (gestalt_search uses it internally)

The key tradeoff — losing absolute confidence signals — is acceptable because our token budget (700 tokens) means we're taking the top 3-5 results regardless. At that cut point, rank order matters more than confidence magnitude.

Fan-out concurrent execution is essential. Sequential querying would blow the latency budget. The three backends have no dependencies — they can run in parallel via Python `asyncio`, `concurrent.futures`, or shell background processes (`&`).

## Consequences

### Positive

- Latency reduced from ~850ms (sequential) to ~500ms (fan-out)
- No score normalization needed — RRF handles heterogeneous backends
- Reuses proven algorithm from gestalt_search internals
- Cross-backend agreement naturally boosts results that multiple layers agree are relevant

### Negative

- Rank-based merging is less precise than score-based merging for top-1 results
- Implementation requires concurrent execution in a shell hook (Python subprocess with asyncio, or parallel curl commands)
- Three network calls per prompt creates three failure points — each needs independent timeout handling

### Risks

- **Backend returning zero results:** RRF handles this gracefully — a backend with no results simply contributes nothing to the merged scores. No special handling needed.
- **All results below relevance threshold:** The relevance threshold (URI-FR-030) filters post-merge. If all merged results are below threshold, injection is empty — which is correct behavior (nothing relevant to inject).
- **Graphiti MCP session overhead:** Each Graphiti query requires MCP session initialization. Persistent session reuse across prompts would cut ~200ms. If not feasible, the 400ms per-backend timeout absorbs the init cost.

## Impact on Requirements

- **Adds:** URI-FR-021 (fan-out concurrent retrieval)
- **Adds:** URI-FR-022 (RRF merge specification)
- **Adds:** URI-NFR-003 (individual 400ms backend timeouts)
- **Constrains:** URI-NFR-001 — 500ms P95 target is achievable with fan-out but not with sequential execution
