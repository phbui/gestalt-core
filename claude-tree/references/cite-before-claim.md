# Cite Before Claim — Full Workflow

Companion to `rules/cite-before-claim.md` (which defines the confidence tiers and the core rule). This file holds the full search workflow and anti-patterns.

## Search-First Workflow

For triggered claims (external-library behavior, "notorious for", version compatibility, historical failure modes):
1. WebSearch for the specific error string, function name, or symptom (not the abstract category)
2. Check the library's own issue tracker (GitHub Issues, GitLab Issues, Bugzilla)
3. If both empty: state "no public reports of this pattern found" and downgrade to [INFERRED] or [UNVERIFIED]
4. For [INFERRED] claims, the reasoning chain must trace to actual code/logs cited inline — not to remembered training material

## Anti-patterns

Never do these:
- Asserting "X is notorious for Y" without a link
- Pattern-matching a symptom to a remembered failure mode without verification
- Using "consistent with" / "likely due to" / "this is the classic case of" as a substitute for evidence
- Doubling down when challenged — retract instead

## Why This Rule Exists

Production investigations get cited downstream (Linear issues, PRs, deliverables, gestalt entries). An [UNVERIFIED] claim presented as [VERIFIED] poisons the citation chain and erodes trust. The cost of saying "I don't know" is much lower than the cost of an authoritative-sounding wrong answer that someone acts on.
