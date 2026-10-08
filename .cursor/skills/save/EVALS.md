# Save — Evals


Test before shipping. Run on Haiku, Sonnet, and Opus separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Save what we learned this session about the spatial pipeline migration" | YES | Scan conversation for corrections, conventions, how-tos, decisions, debugging insights, data flow discoveries; check existing entries; update or create with `wikilinks` and `^block-ids`; regenerate indices; brief confirmation |
| 2 | "/save — capture the auth middleware bug we just fixed" | YES | Same: extract the specific bug + fix as a knowledge entry under the relevant repo entry; record only what's permanently useful; not a session log; regenerate indices |
| 3 | "Learn the platform repo from scratch" | NO | Route to `/learn` (deep multi-agent repo scan, not conversation-driven extraction) |
