# Discuss — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Should we use DuckDB or Polars for the transform layer? Talk me through the tradeoffs" | YES | Skeptical senior-engineer persona; ground in gestalt; spawn parallel evidence agents (code + web); steelman both options; cite sources inline; make honest recommendation; flag failure modes |
| 2 | "I'm thinking of moving the spatial pipeline to event-driven processing — challenge my assumptions" | YES | Critical critique with evidence; reference gestalt entries (`[[platform]]`, `spatial`); steelman alternative architectures; raise scaling/operational concerns; cite code paths |
| 3 | "Investigate why our snapshot reconnect drops messages" | NO | Route to `/investigate` (root-cause investigation of an actual bug, not architectural rubber-ducking) |
