---
---

# Memory Activation

## Letta Memory Blocks

At SessionStart, the injected `<gestalt-memory>` block holds Letta memory blocks and recent session summaries. Treat it as active state.

- **`pending_items`** — Work to resume. Mention relevant items at session start or in a related area.
- **`core_directives`** — Apply unconditionally.
- **`session_patterns`** — Calibrate verbosity, tool order, common workflows.
- **`user_preferences`** — Override defaults for coding style, tool choices, response format.
- **`project_context`** — Architecture facts, repo relationships, gotchas. Cross-reference gestalt curated entries.
- **`guidance`** — Follow it this session.
- **`tool_guidelines`** — Apply when using those tools.

If `<gestalt-memory>` is absent (Letta down), proceed normally, since it is supplementary. If a Letta block contradicts a gestalt curated entry, the curated entry wins (human-reviewed). If a Letta block contradicts native Claude memory (`MEMORY.md`), prefer whichever was updated more recently.

## Staleness Check

At the start of a session, if `gestalt/STALENESS_REPORT.md` exists:

1. Read the report
2. Summarize the HIGH/MEDIUM risk entries (one line each)
3. Ask the user: **(a)** run `/review` on HIGH-risk entries, **(b)** show git diff summaries first, **(c)** skip for now
4. Do NOT auto-update gestalt without confirmation

If `gestalt/STALENESS_REPORT.md` does not exist, do nothing.
