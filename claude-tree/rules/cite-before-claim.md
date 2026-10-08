# Cite Before Claim

Before asserting behavioral claims about an external library, framework, or tool — "notorious for", "well-known", bug patterns, version compatibility, historical failure modes, why a tool fails — search for evidence first (WebSearch + the project's issue tracker). Full workflow and anti-patterns: `gestalt/.claude/references/cite-before-claim.md`.

## Confidence Tiers

Always mark one explicitly when making external-system claims:

- **[VERIFIED]** — cited source (link, issue #, commit, log line, file:line in scope) included inline
- **[INFERRED]** — reasoning from actual code/logs in scope, no external citation, mechanism explicitly hypothesized
- **[UNVERIFIED]** — from training knowledge; search attempted and returned nothing, OR not yet searched

## Rule

Never present [UNVERIFIED] claims as [VERIFIED]. If searches return nothing, say "no public documentation found for this failure pattern" — never substitute confident prose for missing evidence. If challenged and you cannot back a claim, retract it explicitly — do not double down.

Applies to: external-library behavior, dependency compatibility, third-party failure modes. Does NOT apply to: math, language semantics, internal codebase facts (read the code), well-cited Anthropic/Claude docs.
