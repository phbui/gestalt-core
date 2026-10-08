---
---

# Memory Activation

## Letta Memory Blocks

At SessionStart, the `<gestalt-memory>` block is injected into context containing Letta memory blocks and recent session summaries. Treat it as active state, not background flavor.

- **`pending_items`** — Work to resume. Mention relevant pending items at session start or when working in a related area.
- **`core_directives`** — Behavioral instructions. Apply unconditionally.
- **`session_patterns`** — Calibrate approach: verbosity, tool order, common workflows.
- **`user_preferences`** — Overrides defaults for coding style, tool choices, response format.
- **`project_context`** — Architecture facts, repo relationships, known gotchas. Cross-reference with gestalt curated entries.
- **`guidance`** — Active guidance for this session. Follow it.
- **`tool_guidelines`** — Tool-specific instructions. Apply when using those tools.

If `<gestalt-memory>` is absent (Letta down), proceed normally — it's supplementary, not blocking. If a Letta block contradicts a gestalt curated entry, the curated entry wins (human-reviewed). If a Letta block contradicts native Claude memory (`MEMORY.md`), prefer whichever was updated more recently.

## Staleness Check

At the start of a session, if `gestalt/STALENESS_REPORT.md` exists:

1. Read the report
2. Summarize which entries are HIGH/MEDIUM risk (one line each)
3. Ask the user: **(a)** run `/review` on HIGH-risk entries, **(b)** show git diff summaries first, **(c)** skip for now
4. Do NOT auto-update gestalt without user confirmation

If `gestalt/STALENESS_REPORT.md` does not exist, do nothing — the check-staleness script hasn't run yet or all entries are current.
