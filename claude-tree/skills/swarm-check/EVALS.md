# Swarm Check — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "I changed the data-api `/spatial/radius` response schema — check the ripple across the workspace" | YES | Read GRAPH.md for data-api consumers; spawn parallel ripple-check agents (PWA, agentic-backend, c8-grag, etc.); each verifies whether the contract change breaks them; report N/N checked with breaking + non-breaking findings |
| 2 | "I just renamed an env var in repo-a — what else needs to change?" | YES | Same: GRAPH.md lookup; ripple agents per dependent repo; report which repos reference the old name and need updates |
| 3 | "Fix all 12 of these basedpyright errors" | NO | Route to `/fix` (multi-fix execution, not cross-repo ripple detection) |
