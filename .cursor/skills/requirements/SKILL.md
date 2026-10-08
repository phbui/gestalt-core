---
name: requirements
description: "Requirements engineering: explain concepts, inspect docs, author pitch/system/SRS/ADR/SDD docs (write down why you chose one approach over another), promote pitch to system, or run implement from spec to code. Subcommands: explain, pitch, system, srs, adr, sdd, promote, implement."
disable-model-invocation: true
---

# Requirements

One skill, eight subcommands, git-subcommand style. Covers the whole requirements-engineering lifecycle: explain concepts, scaffold a pitch or system, write an SRS/ADR/SDD, promote pitch docs to system docs, or run the full SRS→code orchestration. Merges the former `requirements`, `pitch`, `system`, `srs`, `adr`, `sdd`, `promote`, and `implement` skills — no capability was dropped, see each section's Reference pointer for full templates.

## Dispatch

The first argument selects the mode. If no argument, or intent is ambiguous, ask: "Which mode — explain, pitch, system, srs, adr, sdd, promote, or implement?"

| Mode | Selects when the user... | Section |
|---|---|---|
| `explain` | asks a concept question ("SRS vs SDD?", "when do I write an ADR?") OR names a specific pitch/system to inspect/summarize | [§Explain](#explain) |
| `pitch` | wants to create/update a pitch folder | [§Pitch](#pitch) |
| `system` | wants to create/update a system folder | [§System](#system) |
| `srs` | wants to create/update a Software Requirements Specification | [§SRS](#srs) |
| `adr` | wants to record an architectural decision | [§ADR](#adr) |
| `sdd` | wants to create/update a Software Design Description | [§SDD](#sdd) |
| `promote` | wants to move a completed pitch's docs into system docs (Cooldown) | [§Promote](#promote) |
| `implement` | wants the full SRS→ADR→SDD→Build→QA pipeline run | [§Implement](#implement) |

All modes except `explain` and `implement` are read/write document authors — **do NOT edit files unless the user asked for that mode**; `explain` is read-only unless explicitly asked otherwise.

---

## Explain

Two behaviors in one mode, auto-detected from the request:

**Concept Q&A** — user asks about the discipline itself ("what's the difference between SRS and SDD?", "when should I write an ADR?"). Ground answers in `docs/requirements/` and give concrete codebase examples. Do NOT edit files.

Key concepts to lead with:
> Requirements describe WHAT must remain true. Designs describe HOW that truth is achieved.
> Litmus test: "Does this answer 'how would an engineer implement this?' or 'what must always be true, regardless of implementation?'"

The three artifacts: **SRS** = what the system must do; **SDD** = how it's built; **ADR** = why a significant decision was made.

Reference table: `docs/requirements/index.md` (nav), `concepts.md` (philosophy/spectrum), `artifacts.md` (SRS/SDD/ADR definitions), `system-hierarchy.md` (levels of detail), `shape-up-integration.md` (phase guide), `traceability.md` (provenance/ADR feedback), `docs-organization.md` (file structure), `backfilling.md` (documenting existing systems).

**Inspect a pitch/system** — user names a specific pitch or system ("inspect c7-comms", "summarize this pitch's SRS"). Process:
1. Identify target: pitches live in `docs/pitches/{slug}/`, systems in `docs/systems/{system-id}/`. Ask if unclear.
2. Read all docs in the folder: `index.md`, `srs.md`, `sdd.md` OR `sdd/index.md` (+ component SDDs in `sdd/` for modular), `adr-*.md`.
3. Summarize — pitches: problem, stakeholder needs, in/out scope, 3-5 key requirements, status, affected systems. Systems: responsibilities, boundaries (does-not-own), key interfaces, 3-5 key requirements, key ADR decisions.
4. End with "What would you like to know more about?" and suggest follow-ups: `srs`/`sdd`/`adr` mode to create/update, `promote` mode, requirement details, traceability, gaps.

---

## Pitch

Ground future requirements/design in a concise stakeholder-perspective problem statement. No prerequisite — typically the first step.

1. **Gather:** pitch name, stakeholders, stakeholder needs, stakeholder requirements (high-level capabilities), in-scope, out-of-scope (non-goals), success criteria, affected systems.
2. **Notion:** if user references a Notion doc, try the Notion MCP server; if inaccessible, tell the user to install it — never fall back to browser automation.
3. **Location:** `docs/pitches/{pitch-slug}/` — kebab-case, optionally cycle-prefixed (`c6-user-ingestion-v2`).
4. **Create `index.md`** with frontmatter (`pitch_id`, `name`, `cycle`, `status: shaping|betting|building|cooldown|completed|abandoned`, `affected_systems`) and sections: References, Problem Statement, Stakeholders, Stakeholder Needs, Stakeholder Requirements, Scope (In/Out), Success Criteria.
5. **Missing info:** guess concisely, mark with `<!-- GUESS: verify with stakeholders -->`.
6. **Existing pitch:** read current `index.md`, propose targeted updates, preserve content unless told otherwise.
7. **Summarize:** what changed, what's still needed, suggest `srs` mode next.

Reference: `docs/requirements/shape-up-integration.md` for pitch↔Shape-Up phase mapping.

---

## System

Ground future requirements/design in a system-owner-perspective statement. No prerequisite, though systems are often created after a pitch names affected systems.

1. **Gather:** system name, system ID (kebab-case), owner team, repos, tier (`critical|standard|experimental`), owns, does-not-own, interfaces (provided/consumed APIs, events published/consumed, data contracts).
2. **Location:** `docs/systems/{system-id}/`.
3. **Create `index.md`** with frontmatter (`system_id`, `name`, `owner_team`, `repos`, `tier`) and sections: Purpose, Owns, Does Not Own, Interfaces (Provided/Consumed APIs, Events Published/Consumed, Data Contracts), Documentation (links to srs.md, sdd.md or sdd/index.md, adr-*.md), Related Systems.
4. **Update the registry:** add name/link/purpose/owner/tier to `docs/systems/index.md`.
5. **Missing info:** guess concisely, mark `<!-- GUESS: verify with system owner -->`.
6. **Existing system:** read current `index.md`, propose targeted updates, preserve content unless told otherwise.
7. **Summarize:** what changed, what's still needed, suggest `srs` mode next (often for backfilling).

Reference: `docs/requirements/docs-organization.md` (structure), `backfilling.md` (existing systems), `system-hierarchy.md` (level fit).

---

## SRS

Capture verifiable requirements for a pitch or system.

**Prerequisite:** `pitch` or `system` mode must have run first — verify `docs/pitches/{slug}/index.md` or `docs/systems/{id}/index.md` exists. If not, STOP and direct the user there.

1. Identify target: pitch SRS (new bet) vs system SRS (durable, possibly backfill).
2. If `srs.md` exists, follow any `TODO`/`<AGENT>` markers, else check accuracy/completeness. Otherwise copy `docs/requirements/reference/templates/srs-template.md`.
3. Work WITH the user per `gestalt/.cursor/references/collaborative-work.md`.
4. Capture each requirement as:
   ```
   ##### [ID]: Short title
   - _Statement_: The system shall [action] [object] [constraint/condition].
   - _Rationale:_ why this exists
   - _Acceptance:_ how to verify
   - _Source:_ pitch, ADR, or backfill provenance
   ```
   ID format: `[PREFIX]-[TYPE]-[NNN]` — PREFIX = system ID or pitch slug (e.g. `AUTH`, `C6-360`); TYPE = `FR`/`NFR`/`CONST`/`INT`; NNN sequential.
5. Litmus test every statement: "what must always be true?" vs "how would an engineer implement this?" — the latter belongs in the SDD, not here.
6. Pitch SRS only — end with a Promotion Plan table: `| Requirement | Target System | Action |`.
7. Summarize changes by requirement ID: Added / Modified, one line each.

**Document structure:** Introduction (purpose/scope/definitions/references) → Product Overview (perspective/functions/constraints/users) → Requirements (3.1 External Interfaces, 3.2 Functional, 3.3 QoS [performance/security/reliability/availability/observability], 3.4 Compliance, 3.5 Design & Implementation, 3.6 AI/ML if applicable) → Verification → Appendixes.

**Good-requirements checklist:** Verifiable, Unambiguous, Traceable, Independent, Necessary, Consistent.

Reference: `docs/requirements/concepts.md`, `artifacts.md`, `traceability.md`, `docs/requirements/reference/templates/srs-template.md`.

---

## ADR

Record WHY a significant decision was made — context, options, consequences, requirements impact.

**Prerequisite:** `srs.md` must exist (ADRs relate to requirements).

**When to write one:** interface boundary changes, storage/data-model decisions, auth model changes, reliability model changes, major dependency adoption/replacement, any expensive-or-dangerous-to-reverse decision, or spike conclusions.

1. Identify the decision, affected systems, whether it's a spike.
2. Check for existing/related/superseded ADRs in the target folder.
3. Location: same folder as pitch/system. Naming: `adr-{NNN}-{short-slug}.md`. Copy `docs/requirements/reference/templates/adr-template.md`.
4. Work WITH the user per `gestalt/.cursor/references/collaborative-work.md`.
5. Fill in: frontmatter (`status: proposed|rejected|accepted|deprecated|superseded by ADR-XXX`, `date`, `decision-makers`) then sections — Context and Problem Statement, Decision Drivers, Considered Options, Decision Outcome (chosen + justification, Consequences good/bad, Confirmation — how we'll know it was right / what triggers reconsideration), Pros and Cons per option, **Impact on Requirements**, More Information.
6. **Impact on Requirements is mandatory, never omit:** `Adds:`/`Modifies:`/`Constrains:`/`Removes:` with REQ-ID and text. If genuinely none: state `None — this decision is implementation-internal and does not affect requirements.`
7. If requirements changed: confirm with the user, then update the SRS with provenance: `_Source: [ADR-001](adr-001-slug.md) — consequence of ..._`.
8. Summarize: decision, impact rows, next steps.
9. **Spikes:** Context = "we need to know if X is feasible", Options = "we explored A/B/C", Outcome = the chosen path, Consequences = downstream requirement impact. The spike's conclusion IS the ADR's conclusion.

Reference: `docs/requirements/traceability.md`, `artifacts.md`, `docs/requirements/reference/templates/adr-template.md`.

---

## SDD

Capture how a system or pitch is designed/implemented.

**Prerequisite:** `srs.md` must exist in the target folder — SDD describes HOW requirements are met. If missing, STOP and direct the user to `srs` mode.

1. Identify target: pitch SDD (living doc during build) vs system SDD (as-built, durable).
2. Check for existing SDD in either structure: monolithic `sdd.md`, or modular `sdd/index.md` + `sdd/*.md` component files. If found, follow `TODO`/`<AGENT>` markers or check accuracy.
3. **Choosing structure for new SDDs:** simple/single-component → `sdd.md`; multiple subsystems/delegated concerns/large teams → `sdd/` folder. Modular `index.md` MUST link every component SDD and define component boundaries/progress.
4. Create: monolithic copies `docs/requirements/reference/templates/sdd-template.md`; modular creates the folder, `index.md` (see structure below), and component files linked from it.
5. Work WITH the user per `gestalt/.cursor/references/collaborative-work.md`.
6. Every major design element MUST trace to requirement(s): `**Satisfies:** AUTH-FR-001, AUTH-FR-002`.
7. Pitch SDD only — living Progress Tracking table: `| Requirement | Status (✅/🔄/⏳) | Notes |`.
8. Record every significant decision under a Decisions section (DEC-NNN: Context/Options/Outcome, cross-link its ADR if one exists).
9. If a change implies a requirement change: STOP, explain the implied change, get confirmation, update both docs. If it implies an architectural decision: prompt the user to run `adr` mode.
10. Summarize changes concisely; separately note anything that needs an ADR.

**Document structure:** Introduction → Design Overview (Stakeholder Concerns, Selected Viewpoints) → Design Views → Decisions → Appendixes.

**Modular `index.md` must contain:** §1 Introduction (purpose, component table linking to each component SDD, subject scope + explicit out-of-scope-see-component-SDDs), §2 Architecture Overview (context diagram, responsibilities table), §3 Design Decisions summary (link ADRs), §4 Work Breakdown (progress table, dependency graph), §5 Implementation Log (dated session entries). Each **component SDD** is self-contained: its own introduction, detailed design views (interfaces/data flows/state machines), component-specific decisions, implementation details, own progress tracking if needed. Reference specific sections: `[camera-service.md §5.5](./camera-service.md#55-...)`.

**Design viewpoints** (pick what's needed): Context (boundaries/actors), Composition (components/organization), Information (data structures/persistence), Interface (contracts), Interaction (runtime message flows), Deployment (infra mapping).

**SDD-SRS relationship:** SRS = WHAT must be true; SDD = HOW we make it true. If the SDD can't satisfy an SRS requirement, that's a requirements problem, not just a design one.

Reference: `docs/requirements/concepts.md`, `artifacts.md`, `docs/requirements/reference/templates/sdd-template.md`.

---

## Promote

Move pitch-scoped requirements/design/decisions into durable system documentation during Cooldown.

**Prerequisite:** the pitch needs an SRS with a Promotion Plan (mode `srs`); SDD and ADRs are optional-but-recommended (modes `sdd`, `adr`). Verify and report anything missing before proceeding.

1. Identify the pitch. Read its `srs.md` (esp. Promotion Plan), `sdd.md`/`sdd/index.md` (+ component SDDs), and all `adr-*.md`.
2. Review the Promotion Plan table (`Requirement | Target System | Action`).
3. Work WITH the user per `gestalt/.cursor/references/collaborative-work.md` — promotion touches multiple system docs, so confirm carefully.
4. Per affected system:
   - **Adding:** find next req-ID number in the system `srs.md`, add with provenance: `_Source: [pitch name](../../pitches/{slug}/srs.md), {PITCH}-FR-001_`.
   - **Modifying:** update text (keep old in revision history if significant), annotate `_Modified: ..._` + `_Source: ..._`.
5. If architecture changed: update the system SDD to as-built state (monolithic `sdd.md`, or modular `sdd/index.md` + relevant component files).
6. ADRs with lasting system impact: copy into the system folder, renumber to system ADR sequence, update requirement-ID references. Purely pitch-scoped ADRs stay put.
7. Finalize the pitch: set `status: completed`, `promoted_date: YYYY-MM-DD` in frontmatter; add an Archive Note linking to the systems' SRS docs, noting the doc is retained as historical record.
8. Verify traceability — confirm ALL: every pitch requirement maps to a system requirement; every system requirement carries provenance to the pitch; system SDD reflects architectural changes; lasting-impact ADRs are promoted.
9. Summarize: additions/modifications per system SRS, SDD updates, promoted ADRs, final pitch status.

**Traceability maintenance:** pitch SRS → system SRS gets a provenance link; ADR "Impact on Requirements" gets updated req-IDs; downstream docs referencing changed system requirements get updated too.

Reference: `docs/requirements/traceability.md`, `shape-up-integration.md` (Cooldown phase).

---

## Implement

Orchestrator: reads an SRS and runs the full requirements-to-code pipeline — SRS → ADRs → SDD → Build → QA — with every decision recorded in living documents. You are the **planner** in the planner-worker hierarchy (`gestalt/.cursor/references/orchestration.md`, `gestalt/.cursor/references/claude-orchestration.md`); phases 1-3 use scan-and-return subagents, phases 4-5 combine into one Agent Team so builders and verifiers can message each other directly instead of round-tripping through you.

**Prerequisite:** `srs.md` and its pitch/system `index.md` must exist — if not, STOP and direct the user to `srs` mode. ADRs/SDD are optional and get created during execution.

**Inputs:** SRS path (required), scope constraint (optional subset of requirements), supporting references (optional). E.g. `implement @docs/pitches/c7-comms/srs.md "Only FR-001 through FR-005"`.

### Mandatory worker-prompt convention

**Every subagent this mode spawns — Requirements Analyst, Codebase Scout, each Researcher, the Designer, each Builder, each Verifier — does NOT inherit this skill's context.** Every generated worker prompt MUST embed, verbatim, near the end:
```
[Embed the Task Completion Gate from gestalt/.cursor/references/discipline-block.md verbatim here — read that file once per skill run; subagents do not inherit it]
```
Read `discipline-block.md` once per `implement` run, then paste BOTH its Standard Block (top of each worker prompt) and its Task Completion Gate (at the placeholder above, near the end) into every worker prompt you generate across all phases below. This is not optional boilerplate — it is the worker's only grounding contract.

### Phase 0 — Load and Confirm

Read the SRS fully, the pitch/system `index.md`, and any existing ADRs/SDD/code. Present a load summary (SRS req counts, existing ADRs/SDD/code or "none"/"greenfield") and the 5-phase plan; get user go-ahead before spawning anything.

### Phase 1 — Analysis (2 parallel `explore` subagents)

- **Requirements Analyst:** parse the SRS into (1) Workstreams (grouped reqs, complexity S/M/L, dependencies), (2) Decision Points needing ADRs, (3) Constraints Map (global vs scoped NFRs), (4) Interface Boundaries, (5) User Flow Verification — trace every end-to-end journey, flag any step-to-step gap as a blocking **FLOW GAP**, (6) Data Contract Verification — flag missing field-level shapes as a blocking **CONTRACT GAP**.
- **Codebase Scout:** map Existing Components, Patterns/conventions, Gaps (build-from-scratch vs extend), Tech Stack.
- Merge findings into a master plan; present workstreams + decision points for user approval before continuing. FLOW/CONTRACT gaps must be resolved first.

### Phase 2 — Research (up to 4 parallel `researcher` subagents, batch beyond that)

One per decision point: read the SRS, identify 3-5 embedded technical decisions (storage, API/protocol, library/framework, architecture pattern, external integrations), and for each — context, 2-3 options with pros/cons/NFR trade-offs, what the codebase already uses, a recommendation with rationale. Output: one complete ADR draft per decision (Status/Context/Options/Decision/Rationale/Consequences). Rule: every codebase claim needs file:line; no speculation where evidence is absent.

Present the ADR summary to the user for approval; update the SRS if ADRs changed requirements (with confirmation).

### Phase 3 — Design (one `designer` subagent)

Given the SRS, approved ADRs, scout findings, and workstreams: produce a full SDD — System Overview, Component Architecture (with a text/ASCII diagram), concrete Interface Definitions (routes/types/events/schemas — no hand-waving), ordered Implementation Workstreams with explicit dependencies, Design Decisions cross-referenced to ADRs, and an Open Questions section (no "TBD" allowed — flag genuine unknowns there instead). Every choice must cite an SRS req-ID or ADR.

Review the SDD with the user; resolve any requirement conflicts before Phase 4.

### Phase 4+5 — Build & QA (one Agent Team)

The `builder-quality-gate.sh` Stop hook auto-runs lint/type checks on modified files; builders must fix failures before stopping.

Task list with dependency ordering (builders start immediately on independent workstreams; verifiers unblock on completion):
1-2. Build workstream N (file-clustered, no deps) · 3-4. Verify workstream N against its SRS req-IDs (blocked by 1/2) · 5. Integration wiring (blocked by 1,2) · 6. Code-quality review — DRY, dead code, TODO/FIXME/HACK (implement then remove comment, never delete unimplemented; escalate out-of-scope), maintainability (>50-line functions, naming, error handling), pattern consistency (blocked by 1,2) · 7. Documentation update — SDD progress + implementation log, SRS staleness check, ADR status check, pitch index links (blocked by 1,2,3,4) · 8. Final integration audit (blocked by 3,4,5,6,7).

Teammates: Builder A/B = `builder` agent, one workstream each, `isolation: worktree`; Verifier = `verifier` agent, audits each workstream on completion; Quality+Docs = `general-purpose`, covers task 6 and 7.

Builder prompts additionally require: "implement EVERY requirement from your assigned SRS workstream; count N/N; never silently skip one." Verifier prompts additionally require: "verification matrix has one row per SRS requirement, each PASS/PARTIAL(with gap)/MISSING; count N/N."

**Iteration loop:** if the final audit (task 8) finds PARTIAL/MISSING, create builder follow-ups. Max 2 full iterations, then escalate remainder to the user.

**Step 4.5 (mandatory):** run the shared verification procedure from `gestalt/.cursor/references/verification.md` on everything Phase 4 touched.

**Step 5 — Final consolidation:** verify full SRS→SDD→code traceability (matrix, one row per requirement, N/N traced); check/update ADR statuses; generate a session summary (Completed / Deferred-with-justification / Documents Updated / QA Results / Next Steps) and present it — Phase 5 isn't done until the user acknowledges it.

### Orchestration rules

Follow `gestalt/.cursor/references/orchestration.md`. Additionally: never skip inter-phase user confirmation; every phase updates ADRs/SDD/SRS as living docs; every session gets an Implementation Log entry; Phase 5 leaves no silent gaps — every requirement is PASS, explicitly deferred, or escalated.

### Incremental execution

Re-runnable: read existing ADRs/SDD/Implementation Log, diff against what's done, resume from the last completed phase, spawn agents only for remaining work. The SDD Progress Tracking table is the source of truth.

### Error handling

| Situation | Action |
|---|---|
| SRS missing | STOP, direct to `srs` mode |
| Requirement conflict found | STOP, present conflict, ask user |
| Design gap in SDD | Log, propose addition, continue only if non-blocking |
| Worker agent fails | Retry once, then report to user |
| All ADR options are bad | Log findings, propose new options, ask user |
| Scope too large for one session | Prioritize workstreams, defer rest to next `implement` run |

If the repo has no `docs/requirements/`, fall back to this skill's `srs`/`sdd`/`adr` sections for document structure.

**Follow-up:** after implementation, run the `audit` skill as a final quality gate (architecture, DRY, performance, requirements completeness).

Reference: `docs/requirements/concepts.md`, `artifacts.md`, `shape-up-integration.md`, `traceability.md`.

---

## Verification Gate

Execute every step in the dispatched mode's section — none are optional. Before finishing: confirm all steps completed and outputs match the required formats above.
