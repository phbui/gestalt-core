# Prompt — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "/prompt help me debug the snapshot reconnect issue and fix it across the PWA and gateway" | YES | Analyze intent; check gestalt context silently; detect caveman state; produce optimized structured prompt with phases, parallelism plan, and skill routing; present for confirmation before execution |
| 2 | "/prompt build a retry wrapper, then run audit, then commit" | YES | Decompose into chained skill invocations (`/build` -> `/audit` -> `/commit`); produce Team Manifest if multi-agent work warranted; confirm with user; execute |
| 3 | "Just commit my staged changes with a quick message" | NO | Route directly to `/commit` — single, scoped, mechanical request doesn't need prompt optimization (per Clarification Gate skip rule) |

