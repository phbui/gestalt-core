# Fix — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Fix all 12 of these basedpyright errors" (paste linter output) | YES | Triage agent partitions errors by file ownership; up to 4 parallel fix agents work disjoint clusters; verifier confirms; report fixed/skipped/blocked counts |
| 2 | "Apply these 8 changes from the code review comments" | YES | Parse review comments; partition by file; spawn fix agents per cluster (worktree-isolated if writes overlap); verify lint/type-check; final summary |
| 3 | "Audit my pitch implementation before merge" | NO | Route to `/audit` (final quality gate with specialist reviewers, not a known list of fixes to execute) |
