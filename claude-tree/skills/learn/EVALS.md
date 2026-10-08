# Learn — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Learn the repo-a repo and create gestalt coverage for it" | YES | Git pre-flight; check existing coverage in MANIFEST; spawn 4 parallel scanner agents (Infra, Code, Test/Docs, External); synthesize findings into `gestalt/knowledge/repo-a.md`; regenerate indices |
| 2 | "Learn about the deployment pipeline that connects repo-a to repo-b" | YES | Cross-repo topic scope; scanners focus on data-flow boundaries; produce or update entry with `## Data Flow` section, `wikilinks`, and `^block-id` anchors; regenerate MANIFEST/GRAPH/SOURCES |
| 3 | "Verify all gestalt entries are accurate and find stale claims" | NO | Route to `/review` (audits existing entries against code; doesn't create new coverage) |
