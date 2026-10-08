# Investigate — Evals

Dev-time test matrix. Not consulted at runtime — read this only when validating or modifying the skill.

Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Investigate why snapshot messages drop during reconnect in the PWA" | YES | Frame investigation; spawn parallel agents (code explorer, gestalt grounding, web research); trace execution paths; cite sources; synthesize root cause + recommendation; no guessing |
| 2 | "Help me understand how the tool registry in agentic-backend works" | YES | Conceptual deep-dive (formerly `/understand`); read code; cite line numbers; produce architecture explanation grounded in actual implementation; offer follow-ups |
| 3 | "Should we adopt CRDTs for offline collaboration? Talk me through tradeoffs" | NO | Route to `/discuss` (decision rubber-ducking with steelman/critique, not a single-issue investigation) |
