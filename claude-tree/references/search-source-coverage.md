# Search Source Coverage

Companion to `rules/search-source-coverage.md`, which carries the binding summary that loads in every session. This file is the complete original rule — rationale, incident history, evidence, and mechanics — loaded on demand.

`WebSearch` cannot see `reddit.com` or `stackoverflow.com`, and gives no warning: a query whose best answer lives there returns a confident synthesis from SEO reposts and Medium mirrors, with no error and no flag. Verified 2026-08-15; full evidence, mechanism, and working routes: `gestalt/knowledge/the-relevant-entry.md`.

The rule is not "search harder." It is: **never let a coverage gap pass as coverage.**

## Before Answering From Search

Ask whether the best answer would plausibly live on a forum — Reddit, Stack Overflow, Discourse, a community thread. Signals: lived experience with a tool, an error message, a workaround, "is X worth it", "why does Y happen to everyone", version-specific breakage — anywhere the authority is a person who hit it, not a document.

If yes, you have two obligations, and the second is not optional:

1. **Route around the gap.** Use the real APIs — they serve the same content the crawler is blocked from:
   - Stack Overflow → `curl -s "https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&q=<query>&site=stackoverflow&pagesize=5"`
   - Reddit → `curl -s "https://arctic-shift.photon-reddit.com/api/posts/search?subreddit=<sub>&title=<kw>&limit=25"` (needs a subreddit; rate-limited — pace retries)
   - Hacker News → `curl -s "https://hn.algolia.com/api/v1/search?query=<q>&tags=story&hitsPerPage=5"`
   - GitHub → `gh search issues "<query>" --limit 20`
2. **Say what you did and didn't reach.** One line, in the answer: which sources you actually consulted. If you could not reach a source that would have mattered, name it.

## The Prohibition

Never present a search-derived answer as complete when a blocked domain would have been a primary source and you did not route around it. Specifically:

- Do not write "Reddit users report…" or "the Stack Overflow consensus is…" on the strength of an article that *quotes* them. That is laundering — the citation chain looks intact and isn't. Cite the article as the article, or go get the primary source.
- Do not treat a thin or irrelevant result set as evidence of absence: "nothing found" and "nothing indexed" are indistinguishable from inside one search call, however hard you look at the results.
- Do not read a non-error as coverage. `site:reddit.com` in the query string does not error — it silently returns non-Reddit results. Only the `allowed_domains` parameter produces the 400.

## Query Craft

Start short and broad — three to six words, no operators. Long, fully specified opening queries are the documented agent failure mode: too few results, and nothing learned about the corpus's own vocabulary. Narrow on the second or third pass, once you've seen the terms real sources use.

When a query underperforms, change the *words*, not the word order: genuine synonyms, another stakeholder's terminology, a hypernym. Re-asking in a slightly different shape re-runs the same search.

Prefer primary sources over top-ranked ones — search ranking favors SEO content farms over official docs, maintainer comments, and papers, a known bias worth correcting deliberately. For a vendor claim that matters (pricing, free tiers, limits), open the vendor's own current page rather than an aggregator's summary.

Stop at saturation, not a fixed count — say so if you stop early for budget or time.

## Scope

This applies whenever search output feeds an answer, an entry, or a decision — nearly always, and hardest on quick factual lookups, where the first confident paragraph is most tempting.

Related: `cite-before-claim.md` assumes a corpus this rule shows is incomplete — a failed search there means an index missing two sources, not no public reports.
