---
name: weekly-review
description: "GTD-style weekly review: triage the capture inbox, resolve/rollover commitments, surface stuck pending items, stale entries, and un-actioned NEEDS APPROVAL lines from recent sessions. Interactive by default, headless summary on a timer."
disable-model-invocation: true
---

# Weekly Review

The closing loop for the-relevant-entry items 7+8: nothing captured or promised silently goes stale.

## Process

0. **Memory-hygiene gate (prerequisite, instituted 2026-08-30)**: run `/consolidate status` first, every time. If the dashboard flags drift — duplicate lines in Letta blocks, orphaned entries or dangling wikilinks, graph-vs-curated contradictions, or a stale semantic index — run the full `/consolidate` before any triage below, so the review operates on consolidated memory rather than re-triaging noise. If status shows link rot or claim rot in the curated layer, RECOMMEND `/review` in the output (never auto-run it — it is the expensive pass and Phi decides). Headless mode: run status, report the verdict, run full consolidate only for mechanical fixes (index rebuild); leave judgment-requiring consolidation to the next interactive session.
1. **Commitments** (`gestalt/knowledge/capture-inbox.md` `^commitments`): table of all unchecked items — age, due date, status verdict (`do now` / `reschedule` / `drop?`). Interactive: ask Phi per stale item (batch into ONE AskUserQuestion, not many); mark `[x]` + move to `^archive` with a resolution note as directed. Headless: report only, change nothing.
2. **Inbox triage** (`^inbox`): for each item propose one disposition — promote to a real knowledge entry (do it, following entry conventions + index regen), fold into an existing entry, keep, or drop. Interactive: confirm the batch in one question; headless: propose only.
3. **Stuck pending work**: `pending_items` from `<gestalt-memory>` older than ~2 weeks, plus `NEEDS APPROVAL:` lines grep'd from the recent session summaries the SessionStart hook injects — list what was surfaced but never decided.
4. **Stale knowledge**: 5 entries with the oldest git-log touch dates that carry active-project tags; one line each — refresh, archive, or fine-as-is. Cross-reference the read-side signal: run `sh gestalt/tools/gestalt-hit-aggregate.sh` first — it merges every node's `~/.claude/gestalt/retrieval-hits.jsonl` into `retrieval-hits-fleet.jsonl` and NAMES any unreached peer (treat their hits as unknown, not absent; knowledge/gbrain.md ^transfer-list item 10). An entry that is both git-old AND absent from the hit log is the strongest archive candidate; an entry that is git-old but hit weekly is doing quiet load-bearing work — refresh it, don't archive it.
5. **Paper intake linking** (`gestalt/knowledge/the-relevant-entry.md`): entries created by `jarvis-zotero-ingest` arrive in the intake hub with no theme, because assigning one is a judgement about content that an ingester cannot make. Run ONE linker subagent over every entry listed there, with the wave-2 discipline: every claim quoted from the entry's own abstract, ABSTAIN rather than infer when the abstract does not support a claim, and never invent a connection to a Paper A/B/C scope item that the text does not carry. For each entry it writes the `## Notes` and Related sections, then moves that entry's line out of intake into the-relevant-entry, the-relevant-entry or the-relevant-entry. An entry that genuinely fits none of the three STAYS in intake, deliberately, and that is a real answer rather than a failure; append `, reviewed <today> fits none` to its line so the staleness check stops counting it, since it may legitimately live there forever. Headless: run it, since it is mechanical given the discipline, and report per-entry placement. `gestalt-optimal`'s `paper-hub-membership` FAILS on any entry that has sat in intake more than 14 days, so this step is what keeps that green; a growing intake means the review has not run, not that ingestion is broken.
6. **Week ahead**: from surviving commitments + pending items, propose the top 3 next actions.

## Output

One review document in chat (headless: also write `~/artifacts/jarvis/review-YYYY-MM-DD.md` and print the file:// link on its own line). Sections: Commitments table · Inbox dispositions · Stuck/undecided · Stale entries · Top 3 next actions.

## Rules

- Interactive edits to capture-inbox happen only after Phi's batched confirmation; headless mode NEVER writes to gestalt.
- After any interactive write: update `updated:` frontmatter, `tools/gestalt rebuild`, `uv run tools/gestalt-index-builder.py`.
- No motivational filler. Verdicts and evidence only.
