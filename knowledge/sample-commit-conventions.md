---
type: note
title: "Commit message conventions"
repo: gestalt-core
tags: [git, workflow]
aliases: [commit style]
created: 2026-10-08
updated: 2026-10-08
last-verified: 2026-10-08
confidence: high
---

## Subject line ^subject-line

Imperative mood, under 72 characters, no trailing period. It says what the change does. Good: "Cap injected memory at 2,000 characters". Poor: "Fixed some stuff".

## Body ^body

Explain why, not what. The diff already shows what. State the symptom, the cause and the choice made. Wrap nothing by hand. One paragraph is one line.

## Fixes need a failing test ^discriminating-test

A commit that fixes a bug carries a test that fails with the fix reverted. Run the test red without the fix and green with it, and quote both runs in the message. A test that passes either way proves nothing. If the change has no testable surface, say so in the message and give the reason.

## Attribution ^attribution

When an agent writes the change, the trailer names the agent and the session. The human who reviewed it stays the author of record.
