---
name: compact-resume
description: "Prepare the session for context compaction or handoff by securing uncommitted work and in-flight processes, then writing one canonical, self-contained resume block to gestalt so a fresh context can resume with zero loss. Use before /compact or a session handoff on a long-running workstream."
model: sonnet
user-invocable: true
argument-hint: "(no arguments — run proactively before compaction or handoff)"
---

# Compact-Resume

Prepare the session for context compaction (or session end) so that a fresh
context — same session post-compact, or a brand-new session — can resume the
work with zero loss. Produces one canonical, self-contained resume block in
gestalt plus synchronized memory pointers, and secures every perishable
artifact and in-flight process.

## When to Use

- The user says "prep for compact", "prepare for compact and resume",
  "/compact-resume", or context is approaching its limit (see the gestalt
  Auto-Save rule — run this proactively, don't wait to be asked).
- Before any deliberate `/compact` or session handoff on a long-running
  autonomous workstream.

## Process

ALL steps are mandatory. The test for every line written: *could a fresh
context with NO memory of this conversation act on it without guessing?*

### Step 1: Inventory what dies with the context

Walk these categories and list every live item:

1. **Uncommitted work** — `git status` in every repo touched this session.
2. **Perishable files** — anything in the session scratchpad (`/tmp/...`)
   or other temp locations: analysis outputs, generated assets, data pulls,
   build templates.
3. **In-flight processes** — running/queued cloud jobs (sky clusters, CI,
   batch), background tasks, and their expected completion times.
4. **Session-only schedulers** — ScheduleWakeup loops and CronCreate jobs
   (these DIE with the session; a post-compact context keeps them, a new
   session does not — the resume block must make them reconstructible).
5. **Open decisions** — anything awaiting user sign-off, with the options
   as last presented.
6. **Access state** — credentials decrypted/authenticated this session and
   how to re-obtain them (never write secret VALUES anywhere).

### Step 2: Secure the perishables

- Commit uncommitted work per-repo (quality gates first — run the gate and
  check its REAL exit code, not a piped `tail`'s).
- Copy scratchpad artifacts worth keeping to a durable location. Respect
  data boundaries: customer/company data must not land in public or
  unrelated repos — use a local non-repo directory when in doubt, and say
  so in the resume block.
- Do NOT push, deploy, or otherwise publish — securing is local.

### Step 3: Write the canonical resume block (gestalt)

Write ONE anchored block to the relevant `gestalt/knowledge/<slug>.md`
entry (caveman suspended — full gestalt register).

**Replace, do not append. There is exactly one live resume block per workstream
per entry.** Before writing, find the previous `^compact-resume-*` block for this
workstream and DELETE it in the same edit. A superseded handoff is stale by
construction: its whole content is "current state and what to do next", and the
next thing has already happened. Keeping it costs tokens forever and — measured
2026-08-10 — actively degrades retrieval, because accumulated resume blocks grew
to 38% of all indexed content and began outranking real knowledge in search.

Before deleting the old block, lift out anything in it that is a DURABLE fact
rather than handoff scaffolding — a measured number, a decided tradeoff, a
deadline, a fix and the reason it bites — and move that into the entry's normal
prose or its own result anchor. Delete the scaffolding, never the finding.

- Anchor format: `^compact-resume-YYYY-MM-DD`. When an entry tracks several
  parallel workstreams, suffix it (`^compact-resume-YYYY-MM-DD-<workstream>`) and
  keep one live block per workstream — replace within a workstream, never across.
- Result/verdict anchors (`^r164-gate-failed`, `^c4-results-2026-07-20`) are NOT
  resume blocks. They are the durable record and are never pruned by this skill.
- Contents, in rough order: current state one-liner → what is DONE (with
  commits/URLs) → the ACTIVE workstream and its exact next actions →
  in-flight processes with pickup instructions (commands verbatim) →
  perishable-asset locations → headline numbers/facts a resume must not
  re-derive → access recipes → open decisions.
- Every command must be copy-pasteable; every artifact referenced by
  absolute path or URL; every number carried, not summarized away.
- Update `gestalt/MANIFEST.md`'s Blocks line for the entry with the new
  anchor.

### Step 4: Update native memory

- Update the relevant `memory/project_*.md` CURRENT STATE header to point
  at the new gestalt anchor (memory carries the pointer + durable strategy
  facts; gestalt carries the operational detail).
- Create a new memory file only if the session spawned a genuinely new
  ongoing project; add its line to `MEMORY.md`.
- Honor the dual-agent-sync rule for every memory file touched.

### Step 5: Re-arm or document schedulers

For each session-only wakeup/cron that matters: either (post-compact case)
leave it running and note it, or (new-session case) write its full prompt
and timing into the resume block so it can be re-created verbatim.

### Step 6: Report

Tell the user, compactly: what was committed (hashes), what was persisted
where, the resume anchor name, which processes are still running and when
they land, what is unpushed (never push — remind `! git push`), and the
single sentence a new session should start from.

## Verification Gate

Before completing:
- Re-read the resume block AS IF you were the fresh context: does every
  next-action have a verbatim command? Is any fact only in conversation
  history and nowhere durable? Fix gaps before reporting.
- Confirm MANIFEST Blocks line, memory pointer, and dual-sync all updated.
- Count steps completed / 6; state any skip explicitly with the reason.

## Evals — see EVALS.md (dev-time only)
