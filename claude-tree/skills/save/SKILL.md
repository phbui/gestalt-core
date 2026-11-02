---
name: save
description: "Extract knowledge from the current conversation and write it to gestalt. Also `/save capture [idea|pref|commit] <text>`: deterministic one-line append to the capture inbox, no judgment, for 'remember this', 'note that', 'I'll do X later'."
model: sonnet
user-invocable: true
argument-hint: "topic or knowledge to save | capture [idea|pref|commit] <text>"
---

# Save

Extract knowledge from the current conversation and write it to gestalt. Also triggered automatically when context is getting full.

## When to Run

- User invokes `/save` explicitly (manual override)
- The conversation is long and context is approaching its limit — save proactively before the chat gets summarized so nothing is lost
- **Note:** Knowledge is also captured automatically by the Stop hook at session end. Manual `/save` is only needed for mid-session captures.

## Process

### Step 1: Extract Everything Worth Keeping

Scan the full conversation for:

- Corrections the user made (mistakes to avoid)
- Conventions or patterns that were established
- How-tos that were figured out (setup, run, test, deploy)
- Architectural decisions or design rationale
- Debugging insights (what worked, what didn't)
- Data flow discoveries (what connects to what)
- Cross-repo relationships uncovered
- External source references (Notion pages, Linear issues, Slack threads cited)
- Anything the user explicitly asked to remember

### Step 2: Check Existing Coverage

Follow the gestalt grounding procedure in `gestalt/.claude/references/gestalt-grounding.md` (Quick Lookup). For each piece of knowledge extracted, check if an existing entry covers it. If so, you will update that entry. If not, create a new one.

### Step 3: Write or Rewrite

Follow the knowledge write conventions in `gestalt/.claude/references/knowledge-write.md` — use the frontmatter template, linking rules, and write philosophy defined there.

**For a rule** — create or update `gestalt/rules/<slug>.mdc` with `description`, `globs`, `alwaysApply: false`.

**For knowledge** — create or update `gestalt/knowledge/<slug>.md`.

### Step 4: Register External Sources

If the conversation referenced external sources (Notion pages, Linear issues, Slack discussions), record them in the entry's `## Data Flow` or `## Relationships` section and update `gestalt/SOURCES.md`.

### Step 5: Validate

Check that all `wikilinks` in new/updated entries reference existing entries (check MANIFEST.md). If a link target doesn't exist, either create the entry or remove the link.

### Step 6: Regenerate Indices

Execute the full Post-Write Sequence per `gestalt/.claude/references/knowledge-write.md` §Post-Write Sequence (indices + semantic index + Graphiti). All parts are mandatory.

### Step 6.5: Rebuild Semantic Index (if available)

(Covered by Post-Write Sequence §b above.)

### Step 6.6: Feed Temporal Graph (if available)

Run `tools/gestalt-graphiti-sync.sh <slug>` for each entry written this session (covered by Post-Write Sequence §c above — this is the same hash-gated call, not a separate `add_memory`).

### Step 7: Confirm

Tell the user what was saved/updated. If this was an auto-save before context limit, note: "Saved conversation knowledge to gestalt before context limit."

**Note:** The Stop hook (gestalt-stop.sh) also captures session knowledge at session end; it deduplicates against MANIFEST.md before writing, so a manual /save mid-session will not produce duplicates.

## Verification Gate

Execute every step above — none are optional.
Before finishing: confirm all steps completed and outputs match the required formats.

## Evals — see EVALS.md (dev-time only)

## Capture mode (`/save capture`)

Folded in from `/capture` on 2026-11-02. Deterministic memory write: invoked means saved, never a judgment call (per the-relevant-entry item 3). Use it for "remember this", "note that", "I'll do X later". Skip every extraction step above.

## Input

`/save capture [type] <text>` where type is one of `idea`, `pref`, `commit` (default: `idea` for statements, `commit` if the text is a first-person promise like "I'll…", "I need to…", "remind me to…").

## Process (all steps mandatory, no skipping)

1. Classify: explicit type wins; otherwise infer `commit` vs `idea`/`pref` from the text shape. A `pref` is a standing preference about how Phi works or wants the system to behave.
2. Append ONE line to `gestalt/knowledge/capture-inbox.md`:
   - idea/pref → under `## Inbox`, newest first: `- YYYY-MM-DD [type] <text>`
   - commit → under `## Commitments`: `- [ ] YYYY-MM-DD "<text>" (due: <date or none>)` — parse a due date from the text if one is stated ("by Friday" → resolve to an absolute date), else `none`.
3. Update the entry's `updated:` frontmatter date.
4. If the capture is a `pref` that contradicts or extends an existing gestalt rule or memory, ALSO flag that in your confirmation (do not silently let the inbox fork from curated knowledge).
5. Regenerate indices: `tools/gestalt rebuild` (MANIFEST/GRAPH pick up nothing new usually, but the search index build is what makes the capture findable): `uv run tools/gestalt-index-builder.py`.
6. Confirm in ONE line: `captured [type]: <text> (due …)` — no elaboration.

## Rules

- Never refuse, never editorialize, never summarize the text beyond light cleanup of dictation artifacts.
- Preserve Phi's wording — this is his idea log, not your paraphrase.
- Commitments are never deleted by this skill; only `/weekly-review` resolves or archives them.
