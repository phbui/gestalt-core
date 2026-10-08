# Caveman — Evals


Test before shipping. Run on Haiku, Sonnet, and Opus separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Switch to caveman mode for the rest of this session" | YES | All subsequent prose drops articles, filler, pleasantries; technical terms intact; code blocks unchanged; ~75% token reduction vs baseline |
| 2 | "Why is my React component re-rendering?" (with caveman active) | YES | Fragment-style answer like "New object ref each render. Inline object prop = new ref. Wrap in `useMemo`."; no articles, no hedging |
| 3 | "Compress the gestalt knowledge entries to use fewer tokens" | NO | Route to `/mutate` or refuse — caveman is conversational compression only; gestalt files must NEVER be compressed (per caveman-safeguard rule) |
