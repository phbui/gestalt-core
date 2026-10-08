# Review — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Review all gestalt entries against the actual code" | YES | Read MANIFEST/BRANCHES; staleness pre-flight; git pre-flight per repo; spawn parallel verifier agents; correct factual claims to match code (code wins); validate `wikilinks` and `^block-ids`; regenerate indices |
| 2 | "Verify the platform-deploy entry is still accurate — Argo CD config changed last week" | YES | Scoped review on one entry; staleness detector ranks it HIGH; verifier agent compares against current code; update entry; regenerate indices |
| 3 | "Audit my pitch implementation before merge" | NO | Route to `/audit` (final code-quality gate for pitch implementations, not gestalt knowledge base verification) |
