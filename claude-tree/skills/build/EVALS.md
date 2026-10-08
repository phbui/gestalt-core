# Build — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Build a retry wrapper with exponential backoff for our HTTP client in src/api/" | YES | Plan workstream(s); read existing API code; implement wrapper directly or via builder agents; verify via lint/type-check/tests; report changes |
| 2 | "Build per this SDD: @docs/pitches/c7-comms/sdd.md — implement the NATS connection handler" | YES | Read SDD; identify workstreams; spawn parallel builder agents (or build directly if single workstream); verify; report next steps |
| 3 | "Investigate why our snapshot messages drop during reconnect" | NO | Route to `/investigate` (deep analysis of existing behavior, not building new code) |

