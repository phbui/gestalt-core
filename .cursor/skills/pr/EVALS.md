# Pr — Evals


Test before shipping. Run on Haiku, Sonnet, and Opus separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Review PR #1234" | YES | Phase 0 scout + 3 parallel personas (Infra/Senior/QAL); Phase 2 selectable fix menu grouped by severity |
| 2 | "Review the current branch — focus only on the auth module" | YES | Same orchestration; focus constraint passed to all 3 personas |
| 3 | "Audit the c8-grag pitch implementation" | NO | Route to /audit (requires SRS/SDD; reviews against requirements, not multi-persona PR review) |

