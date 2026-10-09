---
name: capture
description: "Deterministically save an idea, preference, or commitment to the gestalt capture inbox — no judgment call, always writes. Use for 'remember this', 'note that', 'I'll do X later', or /capture <text>."
model: sonnet
user-invocable: true
argument-hint: "[idea|pref|commit] <text>"
---

# Capture

Deterministic memory write. The whole point (per the-relevant-entry item 3, mem0's Add-API philosophy) is that this NEVER decides whether something is worth saving — invoked means saved. Ambient extraction provably misses things; this is the bypass.

## Input

`/capture [type] <text>` where type is one of `idea`, `pref`, `commit` (default: `idea` for statements, `commit` if the text is a first-person promise like "I'll…", "I need to…", "remind me to…").

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
