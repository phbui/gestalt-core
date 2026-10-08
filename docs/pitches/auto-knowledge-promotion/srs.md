---
pitch_id: auto-knowledge-promotion
document: srs
version: 1.0
created: 2026-04-09
---

# Software Requirements Specification: Auto-Knowledge Promotion

## 1. Introduction

### 1.1 Purpose

This SRS defines verifiable requirements for automatically promoting knowledge from Letta and Graphiti into the curated gestalt knowledge base (`knowledge/*.md`), closing the write gap where the most durable memory layer depends on manual invocation.

### 1.2 Scope

The system modifies two existing hooks (`gestalt-session-start.sh`, `gestalt-stop.sh`), adds a promotion queue mechanism, and automates execution of `/consolidate` Step 1. It does not modify the KB entry format, add new services, or change the read path.

### 1.3 Definitions

| Term | Definition |
|---|---|
| **Promotion** | Moving a fact from Letta blocks or Graphiti entities into a curated `knowledge/*.md` entry |
| **Promotion queue** | A JSON file (`promotion-queue.json`) listing candidate facts flagged during session end for the next promotion pass |
| **Promotable fact** | A fact that is architecturally significant, stable (observed across 2+ sessions), and not already present in a KB entry |
| **Consolidation pass** | Execution of `/consolidate` Step 1 logic: read Letta blocks, compare against KB entries, create/update entries for missing facts |
| **Staleness propagation** | When Graphiti marks a fact as expired, checking whether the corresponding KB entry still contains the outdated information |

### 1.4 References

- [Pitch: auto-knowledge-promotion](./index.md)
- [Pitch: unified-relevance-injection](../unified-relevance-injection/index.md) — sibling pitch (read path)
- [Pitch: gestalt-memory-expansion](../gestalt-memory-expansion/index.md) — predecessor (built the stack)
- [/consolidate skill](../../../.claude/skills/consolidate/SKILL.md) — existing manual promotion logic
- [/save skill](../../../.claude/skills/save/SKILL.md) — existing manual knowledge capture

### 1.5 Constraints Discovered During Investigation

| Constraint | Impact |
|---|---|
| Stop hook has a 30s timeout and is already near budget (Letta ~10s + Graphiti ~15s) | Promotion detection in the Stop hook must be lightweight (<3s). Heavy work deferred to SessionStart. |
| `/consolidate` Step 1 involves reading all Letta blocks, scanning MANIFEST.md, and potentially writing entries | Full consolidation can take 30-60s. Must run as background agent, not inline in any hook. |
| Auto-save preference: user never runs `/save` manually | Auto-promotion must not require any user action. The nudge-based approach is insufficient. |
| Gestalt KB changes are git-tracked | All auto-promotions produce git-diffable changes. User can review via `git diff` or `git log`. |
| Letta processes messages sequentially per agent | The background consolidation agent must not send messages to Letta while the Stop hook is still writing. Race condition window: ~10s after session end. |

---

## 2. Product Overview

### 2.1 User Classes

- **Phi (primary operator)** — Benefits from knowledge persisting to KB without manual action. Reviews promotions via git diff.
- **Claude Code agents** — Better KB entries = better grounding. Auto-promotion keeps entries current.
- **Letta agent** — Source of promotable facts. After promotion, its blocks can be condensed (facts now live in KB).

### 2.2 Operating Environment

- Existing gestalt memory stack (Letta, Graphiti, SQLite FTS5+sqlite-vec)
- Claude Code hooks (SessionStart, Stop)
- Python 3.11+ for promotion logic
- Git for auditability

### 2.3 Design Constraints

##### AKP-CONST-001: No session-start latency impact

- _Statement:_ Auto-promotion shall not add latency to session start. All promotion work runs as a background agent.
- _Rationale:_ Session start is latency-sensitive (10s timeout). Promotion can take 30-60s.
- _Acceptance:_ Session-start hook completes within its existing 10s budget. Promotion agent runs in background.
- _Source:_ Pitch design decision.

##### AKP-CONST-002: No Stop hook timeout regression

- _Statement:_ Any addition to the Stop hook shall complete within 3 seconds and not increase the total Stop hook duration beyond its 30s timeout.
- _Rationale:_ Stop hook already uses ~25s (Letta + Graphiti). Adding heavy promotion logic would cause timeouts.
- _Acceptance:_ Stop hook total execution time remains under 30s with promotion detection added.
- _Source:_ Investigation — measured Stop hook latency.

##### AKP-CONST-003: KB entry format unchanged

- _Statement:_ Auto-promoted entries shall follow the existing gestalt entry format (YAML frontmatter, section anchors, wikilinks, 500-line cap).
- _Rationale:_ The KB format is established and consumed by the gestalt MCP server, MANIFEST rebuilds, and human readers.
- _Acceptance:_ All auto-promoted entries pass `gestalt rebuild` without errors.
- _Source:_ Inherited from GME-CONST-001.

##### AKP-CONST-004: Reuse /consolidate logic

- _Statement:_ Auto-promotion shall execute the existing `/consolidate` Step 1 logic, not reimplement it.
- _Rationale:_ The promotion logic (read Letta blocks, compare against MANIFEST, write/update entries) already exists and is tested. Reimplementation would create divergence.
- _Acceptance:_ The background agent invokes the same decision procedure as `/consolidate` Step 1.
- _Source:_ Pitch design decision — reuse over reimplementation.

---

## 3. Requirements

### 3.1 Auto-Consolidation at Session Start

##### AKP-FR-001: Replace nudge with execution

- _Statement:_ The `gestalt-session-start.sh` hook shall replace the existing consolidation nudge (every 5 sessions) with actual execution of a background promotion agent. When the session counter reaches the configured interval, the hook shall spawn a background agent that runs `/consolidate` Step 1 (Letta → KB promotion).
- _Rationale:_ The nudge is non-binding and rarely acted on. Automatic execution closes the promotion gap.
- _Acceptance:_ On every Nth session (default: 5), a background agent starts and runs `/consolidate` Step 1. No `<gestalt-consolidation-nudge>` tag is emitted. The agent produces git-diffable KB changes.
- _Source:_ Core pitch requirement.

##### AKP-FR-002: Configurable promotion cadence

- _Statement:_ The promotion interval shall be configurable via `GESTALT_CONSOLIDATE_INTERVAL` in `.env`. Default: 5 sessions. Setting to 0 disables auto-promotion.
- _Rationale:_ Different usage patterns may want more or less frequent promotion. Disabling is needed for debugging.
- _Acceptance:_ Setting `GESTALT_CONSOLIDATE_INTERVAL=3` triggers promotion every 3 sessions. Setting to 0 disables it entirely and restores the nudge behavior.
- _Source:_ Design decision — configurability.

##### AKP-FR-003: Background execution

- _Statement:_ The promotion agent shall run as a background process that does not block session start. The agent shall complete independently of the user's session.
- _Rationale:_ Promotion can take 30-60s (reading blocks, scanning MANIFEST, writing entries, rebuilding indices). This must not delay the user.
- _Acceptance:_ Session-start hook returns within its 10s timeout. Promotion agent runs in background. User can begin working immediately.
- _Source:_ AKP-CONST-001.

##### AKP-FR-004: Race condition guard

- _Statement:_ The promotion agent shall wait at least 15 seconds after session start before reading Letta blocks, to avoid colliding with a concurrent Stop hook from a previous session that may still be writing to Letta.
- _Rationale:_ Letta processes messages sequentially. If the previous session's Stop hook is still sending the transcript to Letta, reading blocks during that window returns stale state.
- _Acceptance:_ The background agent sleeps 15s before beginning its first Letta API read. Measured via timestamp logs.
- _Source:_ Investigation — Letta sequential message processing constraint.

##### AKP-FR-005: Logging

- _Statement:_ The promotion agent shall log all actions to `$STATE_DIR/health.log`:
  - Start time and trigger (session #N)
  - Facts considered for promotion (count)
  - Facts promoted (count, with entry slugs)
  - Facts skipped (count, with reason: already exists / not stable / not significant)
  - Errors encountered
  - End time and duration
- _Rationale:_ Auditability. The user should be able to trace what was promoted and why.
- _Acceptance:_ After a promotion pass, `health.log` contains a structured promotion report.
- _Source:_ Pitch requirement — auditability.

### 3.2 Promotion Queue (Stop Hook Enhancement)

##### AKP-FR-010: Transcript scanning for promotable facts

- _Statement:_ The `gestalt-stop.sh` hook shall add a lightweight promotion detector (Task D) that scans the session transcript for promotable facts. Promotable facts include: corrections to previous understanding, new architectural discoveries, established conventions or patterns, and debugging insights with reusable solutions.
- _Rationale:_ The Stop hook already parses the transcript for Letta (Task A) and session summaries (Task B). Adding a lightweight scan avoids redundant transcript reading at the next session start.
- _Acceptance:_ Task D completes in under 3 seconds. It identifies 0-10 candidate facts per session.
- _Source:_ Pitch Phase 2.

##### AKP-FR-011: Promotion queue file

- _Statement:_ Detected promotable facts shall be written to `$STATE_DIR/promotion-queue.json` as a JSON array. Each entry shall contain:
  ```json
  {
    "fact": "string — the promotable fact",
    "source": "transcript | letta_block | graphiti",
    "confidence": 0.0-1.0,
    "session_id": "string",
    "timestamp": "ISO8601"
  }
  ```
  The file is append-only between promotion passes. After a successful promotion pass, processed entries are removed.
- _Rationale:_ A persistent queue allows facts flagged at session end to inform the next promotion pass without re-scanning transcripts.
- _Acceptance:_ After a session with corrections, `promotion-queue.json` contains the flagged facts. After the next promotion pass, processed facts are removed.
- _Source:_ Pitch Phase 2.

##### AKP-FR-012: Queue consumption by promotion agent

- _Statement:_ The background promotion agent (AKP-FR-001) shall read `promotion-queue.json` in addition to Letta blocks when deciding what to promote. Queue entries provide pre-identified candidates that supplement the Letta block scan.
- _Rationale:_ The queue captures specific facts from individual sessions that might be compressed or lost in Letta's block-level summary.
- _Acceptance:_ A fact flagged in the queue that also appears in a Letta block gets promoted. A fact flagged in the queue that does NOT appear in Letta blocks is still evaluated for promotion.
- _Source:_ Design decision — queue supplements, not replaces, Letta scan.

### 3.3 Promotion Quality

##### AKP-FR-020: Stability filter

- _Statement:_ A fact shall only be promoted to the KB if it has been observed in at least 2 sessions. Observation is verified by checking: (a) the fact appears in a Letta block (which persists across sessions), OR (b) the fact appears in the promotion queue from 2+ different sessions (different `session_id` values).
- _Rationale:_ Single-session observations may be transient (debugging dead-ends, temporary workarounds). Multi-session persistence indicates stable, reusable knowledge.
- _Acceptance:_ A fact discovered in one session and never referenced again is not promoted, unless it was written into a Letta core-memory block by that session (clause a treats Letta persistence itself as satisfying the 2-session check, without counting real sessions). A fact discovered in the promotion queue in session 1 and confirmed in session 3 is promoted.
- _Known limitation (audit 2026-09-08):_ Clause (a) cannot actually verify multi-session observation — `read_letta_facts()` stamps every Letta-sourced fact with a constant `session_id`, so there is no way to distinguish a fact confirmed across many sessions from one written by a single session's Stop hook. Blocks are also last-writer-wins (`gestalt-stop.sh`'s own comment on stale-context clobbering), so a Letta-sourced fact can in practice reflect only its most recent contributing session. This was found, not fixed: `is_stable()`'s behavior is unchanged, `tests/test_auto_promote.py::test_is_stable_true_for_letta_block_facts` still pins clause (a) as written, and the mechanism has promoted zero facts in 26 logged runs over 11 days (2026-08-29 to 2026-09-08) — `is_significant()` is the actual bottleneck, so this limitation has had no observed real-world effect. Revisit if promotion volume ever becomes nonzero.
- _Source:_ Pitch requirement — conservative promotion.

##### AKP-FR-021: Significance filter

- _Statement:_ Only architecturally significant facts shall be promoted. The following categories qualify:
  - Service boundaries and data flows
  - Deployment procedures and infrastructure configuration
  - API contracts and interface specifications
  - Known gotchas, failure modes, and workarounds
  - Cross-repo relationships and dependencies
  - Established coding conventions or patterns

  The following do NOT qualify:
  - Transient task state ("currently debugging X")
  - User preferences (already in Letta `user_preferences` block)
  - Session-specific decisions ("we decided to try approach A")
  - Pending items (already in Letta `pending_items` block)
- _Rationale:_ The KB is for durable architectural knowledge, not session state. Letta blocks and Graphiti handle the latter.
- _Acceptance:_ "Platform deploys via ArgoCD with kustomize overlays" is promoted. "Currently investigating a 500 error on /api/pavement" is not.
- _Source:_ Pitch requirement — conservative promotion.

##### AKP-FR-022: Existing entry update over creation

- _Statement:_ When a promotable fact relates to a repo or service that already has a KB entry, the system shall update the existing entry rather than create a new one. New entries are created only when no relevant entry exists in MANIFEST.md.
- _Rationale:_ Prevents entry proliferation. The KB should have one authoritative entry per repo/service, not scattered facts.
- _Acceptance:_ A fact about platform deploys updates `knowledge/platform.md`, not creates `knowledge/platform-deploy-finding.md`.
- _Source:_ Gestalt convention — one entry per repo/service.

##### AKP-FR-023: Authoritative voice

- _Statement:_ Auto-promoted content shall be written in gestalt's authoritative register: stating the correct approach directly, as if the answer was always known. No "we discovered", "it turns out", or "after investigating" framing.
- _Rationale:_ Per the gestalt write philosophy: entries read as authoritative reference, not as investigation logs.
- _Acceptance:_ Promoted text says "Platform uses ArgoCD with kustomize overlays for deployment" not "We discovered that platform uses ArgoCD".
- _Source:_ Gestalt `auto-capture` convention in `rules/gestalt.md`.

### 3.4 Graphiti Staleness Propagation

##### AKP-FR-030: Expired fact detection

- _Statement:_ During each promotion pass, the agent shall query Graphiti for recently expired or superseded facts: `search_memory_facts(query="recently expired superseded invalid", group_id="gestalt", max_facts=20)`.
- _Rationale:_ Graphiti's temporal model tracks when facts become invalid. These signals should propagate to the KB.
- _Acceptance:_ The promotion agent queries Graphiti for expired facts and logs results. Zero expired facts is a valid (and logged) outcome.
- _Source:_ Pitch Phase 3 — `/consolidate` Step 3 logic.

##### AKP-FR-031: KB staleness flagging

- _Statement:_ For each expired Graphiti fact, the agent shall check whether the corresponding KB entry still contains the outdated information. If it does, the agent shall either: (a) update the entry with the current fact, or (b) add the entry slug to `STALENESS_REPORT.md` for human review, depending on confidence level.
- _Rationale:_ Some staleness can be auto-corrected (e.g., version number changed). Some requires human judgment (e.g., architectural shift).
- _Acceptance:_ A Graphiti fact "data-api uses REST endpoints" expired and replaced by "data-api uses GraphQL" → either `example-service-api.md` is updated or it's flagged in `STALENESS_REPORT.md`.
- _Source:_ Pitch Phase 3.

##### AKP-FR-032: Staleness confidence threshold

- _Statement:_ Auto-correction (direct entry update) shall only occur when the expired fact has a clear replacement with high confidence. When confidence is low or the change is architecturally significant, the entry shall be flagged in `STALENESS_REPORT.md` rather than auto-corrected.
- _Rationale:_ Automatic corrections of architectural facts could propagate errors. Flagging preserves human review for ambiguous cases.
- _Acceptance:_ Trivial updates (version bumps, URL changes) are auto-corrected. Architectural changes (new service, removed dependency) are flagged.
- _Source:_ Design decision — conservative over aggressive.

### 3.5 Index Regeneration

##### AKP-FR-040: Post-promotion index rebuild

- _Statement:_ After any KB entries are created or updated, the promotion agent shall regenerate all gestalt indices: MANIFEST.md, GRAPH.md, and the SQLite search index (`gestalt-index-builder.py --force`).
- _Rationale:_ Indices must reflect the updated entries. The unified-relevance-injection system queries the search index per-prompt.
- _Acceptance:_ After promotion, MANIFEST.md includes any new entries. `gestalt_search` returns content from updated entries.
- _Source:_ Gestalt convention — indices regenerated after every write.

##### AKP-FR-041: Feed promoted content to Graphiti

- _Statement:_ After writing or updating KB entries, the promotion agent shall feed the entry content to Graphiti via `add_memory(name="promote-{slug}-{date}", episode_body=<entry_content>, group_id="gestalt")`.
- _Rationale:_ Graphiti should know about the curated version of facts, not just the raw session transcript version. This closes the loop: Letta → KB → Graphiti.
- _Acceptance:_ After promoting a fact to `knowledge/platform.md`, the updated entry content is sent to Graphiti as an episode.
- _Source:_ `/save` Step 6.6 pattern — already feeds Graphiti after writes.

### 3.6 Letta Block Condensation

##### AKP-FR-050: Condense blocks after promotion

- _Statement:_ After promoting facts from Letta blocks to KB entries, the promotion agent shall send a message to the Letta agent instructing it to condense the promoted facts from its blocks. The message shall list which facts were promoted and to which entries, and instruct the agent to remove or summarize those facts in its blocks.
- _Rationale:_ Facts should live in one canonical location. After promotion to KB, Letta blocks should be condensed to free space for new observations.
- _Acceptance:_ After promoting "Platform uses ArgoCD" from `project_context` to `knowledge/platform.md`, the Letta agent's `project_context` block is shorter — the detailed ArgoCD notes are replaced with a reference or removed entirely.
- _Source:_ Design decision — single source of truth.

### 3.7 Graceful Degradation

##### AKP-FR-060: Letta unavailable

- _Statement:_ When Letta is unreachable during a promotion pass, the agent shall promote from the promotion queue only (if it has entries). Letta block scan is skipped.
- _Rationale:_ The queue is a local file and always available. Letta downtime should not prevent queue-based promotion.
- _Acceptance:_ With Letta stopped, queued facts are still evaluated and promoted if they meet quality criteria.
- _Source:_ GME graceful degradation pattern.

##### AKP-FR-061: Graphiti unavailable

- _Statement:_ When Graphiti is unreachable, staleness propagation (AKP-FR-030) is skipped. Letta → KB promotion proceeds normally. Post-promotion Graphiti feed (AKP-FR-041) is skipped with a warning.
- _Rationale:_ Graphiti staleness is supplementary. Core promotion (Letta → KB) does not depend on it.
- _Acceptance:_ With Graphiti stopped, promotion pass runs and writes KB entries. health.log contains a warning about skipped Graphiti steps.
- _Source:_ GME graceful degradation pattern.

##### AKP-FR-062: Promotion agent failure

- _Statement:_ If the promotion agent crashes or times out, no partial changes shall be committed. The next scheduled promotion pass shall retry from scratch.
- _Rationale:_ Partial KB updates could leave indices inconsistent.
- _Acceptance:_ A crashed promotion agent leaves KB entries unchanged. The next Nth session triggers a fresh pass.
- _Source:_ Design decision — atomicity.

### 3.8 Configuration

##### AKP-FR-070: Environment-based configuration

- _Statement:_ All configurable parameters shall be read from `gestalt/.env`:

| Variable | Default | Description |
|---|---|---|
| `GESTALT_AUTO_CONSOLIDATE` | `true` | Enable/disable auto-promotion |
| `GESTALT_CONSOLIDATE_INTERVAL` | `5` | Sessions between promotion passes |
| `GESTALT_PROMOTE_MIN_SESSIONS` | `2` | Minimum sessions a fact must span before promotion |
| `GESTALT_PROMOTE_QUEUE_PATH` | `$STATE_DIR/promotion-queue.json` | Location of the promotion queue file |

- _Rationale:_ Consistent with existing `.env` configuration pattern.
- _Acceptance:_ Changing variables in `.env` takes effect on next session start.
- _Source:_ Design decision — consistent configuration.

### 3.9 User Journey Flows

##### AKP-FR-080: Normal promotion flow

- _Statement:_ Over the course of 5 sessions, the system shall:
  1. Sessions 1-4: Stop hook scans transcripts, flags promotable facts to `promotion-queue.json`. Letta processes transcripts and accumulates facts in blocks.
  2. Session 5 start: Background promotion agent spawns. Waits 15s (race guard). Reads Letta blocks + promotion queue. Identifies stable, significant facts not in KB. Writes/updates KB entries. Rebuilds indices. Feeds Graphiti. Condenses Letta blocks. Clears processed queue entries. Logs results.
  3. Session 5 continues: User works normally. Promotion completes in background.
- _Rationale:_ End-to-end flow ensures all components connect.
- _Acceptance:_ After session 5, `git diff` shows new/updated KB entries. health.log shows the promotion report.
- _Source:_ SRS flow requirements rule.

##### AKP-FR-081: Empty promotion flow

- _Statement:_ When a promotion pass finds no promotable facts (all Letta block content already exists in KB, queue is empty), the system shall log "No promotable facts found" and complete without modifying any files.
- _Rationale:_ Empty passes are expected and normal. They should be fast and silent.
- _Acceptance:_ Promotion pass completes in <10s when nothing to promote. No KB files modified. No git changes.
- _Source:_ SRS flow requirements rule.

##### AKP-FR-082: Disabled promotion flow

- _Statement:_ When `GESTALT_AUTO_CONSOLIDATE=false`, the system shall emit the existing consolidation nudge (`<gestalt-consolidation-nudge>`) on every Nth session instead of running the promotion agent.
- _Rationale:_ Disabling auto-promotion should restore the previous nudge behavior, not remove all promotion awareness.
- _Acceptance:_ With auto-consolidate disabled, the nudge appears every N sessions. No background agent spawns.
- _Source:_ Design decision — graceful disable.

---

## 4. Verification Matrix

| Req ID | Verification Method | Pass Criteria |
|---|---|---|
| AKP-FR-001 | Run 5+ sessions, check KB for new entries | Facts promoted without manual `/consolidate` |
| AKP-FR-002 | Set interval to 3, run 3 sessions | Promotion triggers on session 3 |
| AKP-FR-003 | Measure session-start hook time | Completes within 10s; promotion runs in background |
| AKP-FR-004 | Check promotion agent timestamp logs | First Letta read is 15s+ after agent start |
| AKP-FR-005 | Read health.log after promotion pass | Structured report with counts and slugs |
| AKP-FR-010 | End a session with corrections, check queue | `promotion-queue.json` contains flagged facts |
| AKP-FR-011 | Inspect queue file format | Valid JSON matching specified schema |
| AKP-FR-012 | Flag a fact in queue, run promotion | Queued fact evaluated alongside Letta blocks |
| AKP-FR-020 | Discover fact in 1 session only, via the queue (clause b) | Fact NOT promoted |
| AKP-FR-020 | Discover fact in queue sessions 1 and 3 | Fact promoted on session 5 |
| AKP-FR-020 | Fact appears in a Letta block (clause a) | Fact treated as stable regardless of session count — see Known limitation above |
| AKP-FR-021 | Session with architectural discovery + debug noise | Architecture promoted, debug noise skipped |
| AKP-FR-022 | Fact about existing repo | Existing entry updated, no new entry created |
| AKP-FR-023 | Read promoted text | No "we discovered" or investigation framing |
| AKP-FR-030 | Check Graphiti for expired facts after promotion | Query returns results or empty (both valid) |
| AKP-FR-031 | Expire a Graphiti fact, run promotion | Corresponding KB entry updated or flagged |
| AKP-FR-040 | Check MANIFEST.md after promotion | New entries indexed |
| AKP-FR-041 | Check Graphiti for promote-{slug} episode | Episode exists with entry content |
| AKP-FR-050 | Read Letta blocks after promotion | Promoted facts condensed or removed |
| AKP-FR-060 | Stop Letta, run promotion with queue entries | Queue facts still evaluated |
| AKP-FR-061 | Stop Graphiti, run promotion | Letta→KB works, Graphiti steps skipped with warning |
| AKP-FR-062 | Kill promotion agent mid-run | No partial KB changes on next `git status` |
| AKP-FR-080 | Run 5 sessions with discoveries | `git diff` shows promotions after session 5 |
| AKP-FR-081 | Run 5 sessions with no new discoveries | No KB changes, log says "No promotable facts" |
| AKP-FR-082 | Set GESTALT_AUTO_CONSOLIDATE=false | Nudge appears, no agent spawns |

---

## 5. Promotion Plan

After implementation and validation:

1. Promote AKP-CONST-001 (no latency impact) and AKP-CONST-003 (KB format) to system-level gestalt SRS
2. Promote AKP-FR-020 (stability filter) and AKP-FR-021 (significance filter) as system-level quality gates for all KB writes
3. Update the gestalt knowledge entry (`knowledge/gestalt.md`) hooks section to reflect auto-promotion
4. Update `/consolidate` skill to note that Step 1 now runs automatically
5. Remove or update the consolidation nudge documentation
6. Consider merging with unified-relevance-injection at system level (both modify the same hooks)
