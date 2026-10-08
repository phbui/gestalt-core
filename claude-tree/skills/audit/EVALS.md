# Audit — Evals

Dev-time test matrix. Not consulted at runtime — read this only when validating or modifying the skill.

Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Run a final quality audit on the c7-comms pitch implementation before we merge" | YES | Phase 1-7 orchestration; 4 parallel specialist reviewers (Architecture, Code Quality, Performance, Requirements); ordered fix batches (structural -> quality -> optimization); regression verification; updated SDD/Implementation Log |
| 2 | "Audit the changes on this branch — check for DRY violations, performance issues, and architecture problems" | YES | Scout agent reads git diff; specialist reviewers spawned in parallel; triage by orchestrator; fix-and-verify cycle; final report |
| 3 | "Check whether gestalt entries match the actual code in platform/" | NO | Route to `/review` (audits gestalt knowledge base against code, not pitch implementations) |
