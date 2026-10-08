# Consolidate — Evals


Test before shipping. Run on Haiku, Sonnet, and Opus separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Consolidate gestalt — promote anything in Letta that should be permanent and add missing cross-links" | YES | Read all 4 layers (Letta, gestalt files, Graphiti, MEMORY.md); identify promotable facts; check MANIFEST for existing entries; update or create entries; add missing `wikilinks`; regenerate indices |
| 2 | "Run a memory cleanup pass — prune duplicates and merge fragmented knowledge across the four layers" | YES | Scan all layers for duplicates; merge fragmented entries; promote stable facts from Letta to gestalt; add cross-links; report what was pruned/merged/promoted |
| 3 | "Pull the latest Notion/Linear updates into gestalt" | NO | Route to `/sync` (external source ingestion, not internal cross-layer synthesis) |
| 4 | "Show me gestalt status" | YES (status mode) | Infrastructure table + four layer sections + hooks table per Mode: status Step 6 |
| 5 | "What did I learn about the spatial pipeline last week?" | YES (recall mode) | Current Facts / Superseded Facts / Related Entities grouping per Mode: recall step 4 |
| 6 | "Generate a gestalt briefing for the team Claude Project" | YES (publish mode) | BRIEFING.md with System Overview / Repository Map / Architecture & Data Flow / Active Context |
| 7 | "Push my changes to the upstream remote" | NO | Route to `/commit` |
