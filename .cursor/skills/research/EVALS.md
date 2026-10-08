# Research — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Research the tradeoffs between Qdrant and Pinecone for vector search" | YES | Decompose into 3-4 sub-questions (Fundamentals/Landscape/Trade-offs/Practical); spawn parallel researcher agents; cross-reference citations; synthesize dense briefing with every claim sourced |
| 2 | "Best practices for real-time sync in offline-first PWAs in 2026" | YES | Same: parallel sub-question researchers; web sources cited inline; comparison tables where appropriate; offer `/save` if research worth persisting |
| 3 | "What did we learn last week about the spatial pipeline?" | NO | Route to `/recall` (temporal knowledge graph query, not external web research) |
