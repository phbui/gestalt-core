# Commit — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Commit these changes" | YES | Run git status/diff/log in parallel; analyze every changed file; draft `<type>(<scope>): <description>` message matching repo style; present for review; wait for approval before committing |
| 2 | "Commit my staged work with a message about fixing the auth middleware bug" | YES | Use the user's hint; respect their wording; still analyze diff for accuracy; draft conventional-commits subject + body explaining why; present then commit |
| 3 | "Open a PR for this branch with a multi-persona review" | NO | Route to `/pr` (PR review/creation, not commit creation) |
