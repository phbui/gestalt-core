# Sync — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Sync gestalt with the latest from Notion, Linear, and Slack" | YES | Git pre-flight; read SOURCES.md; spawn parallel syncer agents per source (Notion, Linear, Slack MCP); reconcile updates with existing entries; flag conflicts; regenerate SOURCES.md and indices; report N/N sources queried |
| 2 | "Pull the latest project updates from Linear into gestalt before our planning meeting" | YES | Same flow but Linear-focused; query Linear MCP for project/issue updates; merge into relevant entries; flag conflicts; regenerate indices |
| 3 | "Promote knowledge from Letta into permanent gestalt entries" | NO | Route to `/consolidate` (cross-layer internal synthesis, not external MCP source ingestion) |
