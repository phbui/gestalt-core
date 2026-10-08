# Requirements — Evals


Test before shipping. Run on Haiku, Sonnet, and Opus separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Explain the difference between an SRS and an SDD" | YES | Concept mode — explains requirements engineering terms with examples |
| 2 | "Inspect the c8-grag pitch's requirements documents" | YES | Inspect mode — summarizes existing srs/sdd/adrs and their statuses |
| 3 | "Write the SRS for the new auth pitch" | YES (srs mode) | Superseded by A1 — authoring is now an in-skill mode, no longer routed away |


## Added 2026-08-11 — absorbed modes (former srs/adr/sdd/pitch/system/promote/implement skills)

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| A1 | "Write the SRS for the new auth pitch" | YES (srs mode) | Prerequisite check, requirement blocks with ID/Statement/Rationale/Acceptance/Source, Promotion Plan for pitch SRS |
| A2 | "Write down why we chose postgres over dynamo" | YES (adr mode) | ADR with Context/Options/Outcome/Consequences and mandatory Impact on Requirements |
| A3 | "Write a software design document from the spec" | YES (sdd mode) | SDD with Satisfies: traceability, structure decision (monolithic vs modular) |
| A4 | "Start a new bet folder for this feature idea" | YES (pitch mode) | docs/pitches/{slug}/index.md with frontmatter + stakeholder sections |
| A5 | "Set up a new system folder for grounding" | YES (system mode) | docs/systems/{id}/index.md + registry update |
| A6 | "Move these decisions into the durable system docs" | YES (promote mode) | Per-system SRS additions with provenance, traceability verification checklist |
| A7 | "Take this spec all the way to working code" | YES (implement mode) | 5-phase pipeline with inter-phase confirmations, worker prompts embed discipline blocks |
| A8 | "Fix these 12 linter errors" | NO | Route to `/fix` |
