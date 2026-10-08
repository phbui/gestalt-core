---
pitch_id: auto-knowledge-promotion
document: adr
adr_id: AKP-ADR-001
title: "Background Agent at SessionStart for Auto-Promotion"
status: accepted
date: 2026-04-09
---

# ADR-001: Background Agent at SessionStart for Auto-Promotion

## Context

The gestalt memory stack writes to Letta and Graphiti automatically at session end (Stop hook). The curated KB (`knowledge/*.md`) is only written to manually via `/save`, `/learn`, or `/consolidate`. This creates a write gap where the most durable layer falls behind the two automatic layers.

The `/consolidate` skill already contains the promotion logic (Step 1: read Letta blocks, compare against KB entries, create/update entries). It just needs to run automatically. The question is: **when and how?**

The Stop hook currently takes ~25s (Letta 10s + Graphiti 15s) within a 30s timeout. The SessionStart hook takes ~3s (Letta health check + block retrieval + summary injection) within a 10s timeout.

## Options Considered

### Option A: Add to Stop Hook

Run promotion logic synchronously in `gestalt-stop.sh` after Tasks A-C.

- **Pro:** All write operations in one place. Conceptually clean: session ends → everything gets written.
- **Con:** Stop hook is already at ~25s of its 30s timeout. Adding promotion (30-60s for reading blocks, scanning MANIFEST, writing entries, rebuilding indices) would far exceed the timeout.
- **Con:** Promotion reads Letta blocks — but the Stop hook is simultaneously writing to Letta (Task A). Race condition: reading blocks while they're being updated produces stale state.
- **Con:** User has already closed the session. A 60s background task delays terminal cleanup.

### Option B: Separate Cron Job

Run promotion on a fixed schedule (e.g., every 6 hours) via cron or systemd timer.

- **Pro:** Completely decoupled from hooks. No latency impact on any session event.
- **Pro:** Fixed schedule is predictable.
- **Con:** Requires new infrastructure (cron job or systemd timer). Gestalt currently manages everything via Claude Code hooks — adding cron breaks the pattern.
- **Con:** Cron doesn't know about session cadence. Could run during a session (causing file conflicts) or miss a window entirely.
- **Con:** No access to Claude Code agent context. The cron job would need its own Claude API key and agent setup just to run `/consolidate` logic.

### Option C: Background Agent at SessionStart (Chosen)

On every Nth session start, spawn a background agent that runs `/consolidate` Step 1. The agent runs independently and does not block the session.

- **Pro:** Runs within the existing hook infrastructure. No new cron or systemd configuration.
- **Pro:** Session-cadence-aware: promotes every N sessions, which naturally adapts to usage patterns (heavy use = more frequent promotion).
- **Pro:** Has access to the full Claude Code environment: can read Letta via REST API, query Graphiti via MCP, write KB files, rebuild indices.
- **Pro:** Background execution means zero impact on session-start latency.
- **Pro:** Replaces the existing consolidation nudge (every 5 sessions) with actual execution — reuses the same trigger cadence.
- **Con:** Background process runs during the user's session. KB writes could cause minor git status noise.
- **Con:** Race condition with Stop hook from previous session. Mitigation: 15-second delay before reading Letta blocks (AKP-FR-004).
- **Con:** If the background agent crashes, promotion silently fails until the next scheduled pass.

### Option D: Auto-Invoke /consolidate After Nudge

Keep the nudge but auto-invoke `/consolidate` when the nudge fires, as an inline operation at session start.

- **Pro:** Reuses existing nudge trigger and the full `/consolidate` skill (all 4 steps).
- **Con:** `/consolidate` runs all 4 steps (promotion + cross-links + staleness + rebuild). Full consolidation takes 60-120s. Far too slow for session start.
- **Con:** Running inline would block the session for 1-2 minutes on every 5th session.
- **Con:** Cannot run as part of the 10s SessionStart hook timeout.

## Decision

**Option C: Background Agent at SessionStart.**

The core constraints are:
1. Must not add latency to any user-facing event (rules out A and D)
2. Must use existing infrastructure — Claude Code hooks, not cron (rules out B)
3. Must be session-cadence-aware (rules out B)
4. Must be lightweight enough for the SessionStart hook to spawn without blocking (rules out D)

Option C threads all four constraints. The SessionStart hook spawns a background process and immediately continues with session setup. The background agent runs at its own pace — reading Letta blocks (after a 15s race guard), scanning MANIFEST, writing entries if needed, and rebuilding indices.

The consolidation nudge is already triggered every 5 sessions. Option C replaces the nudge's `echo` statement with a `nohup ... &` background launch. Same trigger, actual execution instead of a suggestion.

Only Step 1 of `/consolidate` runs automatically (Letta → KB promotion). Steps 2-4 (cross-links, staleness, index rebuild) run as part of the promotion agent's post-write cleanup, but in a lightweight form — not the full `/consolidate` skill which is designed for manual deep-consolidation.

## Consequences

### Positive

- KB entries are kept current automatically, matching the automatic write behavior of Letta and Graphiti
- Zero latency impact on session start or session end
- Reuses existing infrastructure (hooks, Letta API, gestalt MCP)
- Cadence naturally adapts to usage patterns
- Replaces an ignored nudge with actual execution

### Negative

- Background agent runs during user's active session. KB file writes may appear in `git status`.
- Race condition window with previous session's Stop hook requires a 15-second delay before reading Letta blocks
- Silent failure if the background agent crashes — no immediate user notification
- Background process consumes system resources (CPU, memory for Python/embedding model) during the session

### Risks

- **Letta block stale read:** If the Stop hook from the previous session hasn't finished writing to Letta, the promotion agent reads stale blocks. Mitigation: 15-second delay (AKP-FR-004). The Stop hook's Letta write completes in ~10s.
- **Git conflicts:** If the user is manually editing a KB entry while the background agent writes to it, git will show conflicts. Mitigation: promotion writes are additive (appending facts to existing sections), not destructive. Conflicts are mergeable.
- **Resource contention:** The embedding model (nomic-embed-text-v1.5) is shared between the background agent (for gestalt_search during MANIFEST scan) and the main session (for Tier 2 per-prompt queries from the unified-relevance-injection pitch). Mitigation: the model is loaded once and shared via the MCP server process. Concurrent queries are serialized by the MCP server, adding minor latency but no crashes.

## Impact on Requirements

- **Adds:** AKP-FR-001 (replace nudge with execution)
- **Adds:** AKP-FR-003 (background execution)
- **Adds:** AKP-FR-004 (15-second race guard)
- **Modifies:** Existing consolidation nudge behavior (from echo to background launch)
- **Constrains:** AKP-CONST-001 — no session-start latency impact
- **Constrains:** AKP-CONST-002 — no Stop hook timeout regression
