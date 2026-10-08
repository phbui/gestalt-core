# Help — Evals


Test before shipping. Run on Haiku, Sonnet, and Opus separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "List all gestalt commands" | YES | Reads `references/skill-index.md` and renders the full index |
| 2 | "What does /audit do?" | YES | Reads `references/skill-index.md` and filters to /audit's section |
| 3 | "Save what we discussed" | NO | Route to /save (knowledge capture, not command lookup) |

