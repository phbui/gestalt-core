---
pitch_id: unified-relevance-injection
document: sdd-index
version: 1.0
created: 2026-04-09
---

# Software Design Description: Unified Relevance Injection

## Component Map

| Component | SDD | Phase | Effort |
|-----------|-----|-------|--------|
| Letta Controls | [letta-controls.md](./letta-controls.md) | 1 | 25 min |
| Tier 1 Session Start | [tier1-session-start.md](./tier1-session-start.md) | 2 | 2 hrs |
| Tier 2 Prompt Injection | [tier2-prompt-injection.md](./tier2-prompt-injection.md) | 2 | 1 week |
| Post-Compaction | [post-compaction.md](./post-compaction.md) | 3 | 2 hrs |

## Architecture Overview

```
SESSION START
    |
    +-- gestalt-session-start.sh
    |   +-- [CHANGED] Inject Tier 1 blocks only (~1.1K tokens)
    |   +-- [CHANGED] Warm-up gestalt_search (background)
    |   +-- [NEW]     CWD → gestalt hint
    |   +-- [SAME]    Last 3 session summaries
    |   +-- [SAME]    Index staleness check
    |   +-- [SAME]    Consolidation nudge/trigger
    |
    +-- gestalt-session-start.sh
        [NEW] Write Tier 2 block cache to $STATE_DIR/tier2-cache.json
              (all non-Tier-1 blocks, for use by prompt-injection)

EVERY PROMPT
    |
    +-- prompt-intelligence.sh
        [REWRITTEN]
        1. Read prompt text from stdin JSON
        2. Fan-out 3 concurrent queries:
           a. Graphiti search_memory_facts
           b. gestalt_search (SQLite)
           c. Letta domain block keyword match (against tier2-cache.json)
        3. RRF merge across all three ranked lists
        4. Apply relevance threshold + token budget cap
        5. Output JSON { additionalContext: "<relevance context> + <parallelism hints>" }

AFTER COMPACTION
    |
    +-- post-compact-reinject.sh
        [ENHANCED]
        1. Re-inject Tier 1 blocks (re-read from Letta API)
        2. gestalt_search(inferred_task) → top 2 entry pointers
        3. Existing behavioral reminders

SESSION END (UNCHANGED)
    |
    +-- gestalt-stop.sh (no changes)
```

## Component Boundaries

| Component | Owns | Does NOT own |
|-----------|------|-------------|
| letta-controls | Letta API PATCH for block limits; core_directives text | Hook scripts |
| tier1-session-start | Session-start injection logic; Tier 2 block cache file | Tier 2 retrieval |
| tier2-prompt-injection | All per-prompt retrieval; RRF merge; token budget | Session state |
| post-compaction | Reinject after compact; task inference | Main session injection |

## Shared Data Structures

### Tier 2 Block Cache (`$STATE_DIR/tier2-cache.json`)

Written by `gestalt-session-start.sh`, consumed by `prompt-intelligence.sh`.

```json
{
  "timestamp": "2026-04-09T10:00:00Z",
  "blocks": [
    {
      "label": "project_context",
      "value": "Platform uses ArgoCD...",
      "keywords": ["argocd", "deploy", "platform", "kubernetes"]
    },
    {
      "label": "tool_guidelines",
      "value": "When using kubectl...",
      "keywords": ["kubectl", "kubernetes", "k8s", "pod"]
    },
    {
      "label": "service_api_notes",
      "value": "Data-API MCP scale test...",
      "keywords": ["data-api", "mcp", "scale", "pavement", "supa-gate"]
    }
  ]
}
```

Keywords are pre-extracted at session start (not per-prompt) to keep prompt-time processing minimal.

### Per-Prompt Injection Format

`additionalContext` field value:

```markdown
## Relevant Context

**Facts:** {top Graphiti facts as bullet list}
**Gestalt:** slug1, slug2 — {one-line summaries}
**Memory:** {Letta domain block excerpt if matched}

---

BEFORE RESPONDING: {existing parallelism hints}
```

## Key Design Decisions

| Decision | ADR | Summary |
|----------|-----|---------|
| Tier 1 always-on, Tier 2 per-prompt | [ADR-001](../adr-001-tiered-injection.md) | Behavioral context always present; domain context relevance-filtered |
| Fan-out with RRF | [ADR-002](../adr-002-fanout-rrf-merge.md) | Concurrent queries, score-agnostic merge |

## Progress Tracking

| Requirement | Component | Status | Notes |
|-------------|-----------|--------|-------|
| URI-FR-001 | letta-controls | ⏳ Not started | |
| URI-FR-002 | letta-controls | ⏳ Not started | |
| URI-FR-010 | tier1-session-start | ⏳ Not started | |
| URI-FR-011 | tier1-session-start | ⏳ Not started | |
| URI-FR-012 | tier1-session-start | ⏳ Not started | |
| URI-FR-013 | tier1-session-start | ⏳ Not started | |
| URI-FR-020 | tier2-prompt-injection | ⏳ Not started | Verify prompt text access first |
| URI-FR-021 | tier2-prompt-injection | ⏳ Not started | |
| URI-FR-022 | tier2-prompt-injection | ⏳ Not started | |
| URI-FR-023 | tier2-prompt-injection | ⏳ Not started | |
| URI-FR-024 | tier2-prompt-injection | ⏳ Not started | |
| URI-FR-025 | tier2-prompt-injection | ⏳ Not started | Blocking: verify hook stdin format |
| URI-FR-030 | tier2-prompt-injection | ⏳ Not started | |
| URI-FR-031 | tier2-prompt-injection | ⏳ Not started | |
| URI-FR-040–043 | tier2-prompt-injection | ⏳ Not started | |
| URI-FR-050 | post-compaction | ⏳ Not started | |
| URI-FR-051 | post-compaction | ⏳ Not started | |
| URI-FR-060 | all | ⏳ Not started | .env vars added across components |
| URI-NFR-001 | tier2-prompt-injection | ⏳ Not started | Verified by benchmark |
| URI-NFR-002 | tier1-session-start | ⏳ Not started | |
| URI-NFR-003 | tier2-prompt-injection | ⏳ Not started | |
| URI-NFR-004 | tier1-session-start | ⏳ Not started | |
| URI-NFR-005 | tier2-prompt-injection | ⏳ Not started | Add to settings.json |
