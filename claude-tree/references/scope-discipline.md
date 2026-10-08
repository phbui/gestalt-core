# Scope Discipline Block

Canonical scope contract for code-writing worker prompts. Embed in every generated agent prompt that writes or designs code (build, fix, implement workstreams; builder/designer agents), alongside the grounding contract from `references/discipline-block.md`.

Completeness ("nothing missing") and minimalism ("nothing extra") are ONE rule, never two. Stating them as separate competing instructions measurably craters compliance — conflicting instruction pairs drop primary-instruction adherence from ~75–91% to ~10–46%, and priority markers only partially recover it ([Control Illusion, arXiv 2502.15851](https://arxiv.org/html/2502.15851v1)). The evidence that LLM builders need this counter-pressure: copy/pasted code in AI-assisted repos rose from 8.3% to 12.3% of changed lines while refactor-moved code fell below 10% ([GitClear 2025, 211M lines](https://www.gitclear.com/ai_assistant_code_quality_2025_research)), and GPT-4-class models "frequently produce more complex code that may need more reworking to ensure maintainability" ([arXiv 2501.16857](https://arxiv.org/abs/2501.16857)).

## Standard Block

```xml
<scope>
SCOPE = the stated requirements — all of them, and nothing else.
A missing requirement and an unrequested addition are the SAME error: a scope mismatch.

SIMPLEST MECHANISM:
- Implement each requirement with the simplest mechanism that satisfies its acceptance
  criteria. When two designs both satisfy it, ship the one with fewer moving parts.
- Add a new abstraction, file, dependency, config option, or fallback ONLY when a stated
  requirement cannot be met without it. Prefer editing existing code over creating files.
- Code is a liability, not an asset: every unrequested line adds carry cost (Fowler's
  YAGNI — cost of build, delay, carry, repair).

SCOPE GATE (before returning, answer BOTH directions):
1. Is anything requested still missing or partial? Fix it, or report it with a reason.
2. Is anything present that no requirement asked for — speculative generality, defensive
   code for impossible cases, unrequested files or options? Remove it, or justify it
   against a specific requirement ID.
RETURN both counts: "{N}/{N} requirements implemented. Unrequested additions: none"
(or list with justification).
</scope>
```

## Reviewer Corollary

Reviewers cause over-engineering too: "A reviewer prompted to find gaps will usually report some, even when the work is sound... Chasing every finding leads to over-engineering" ([Anthropic, Claude Code best practices](https://code.claude.com/docs/en/best-practices)). Embed in verifier/reviewer/audit agent prompts:

```
Flag ONLY gaps that affect correctness or the stated requirements. Style preferences,
speculative edge cases that cannot occur in practice, and "could be more robust"
observations are OPTIONAL notes, clearly separated — never blocking findings.
Recommending an unrequested abstraction is itself a scope mismatch.
```

## Placement Rules

- Construction-time, not post-hoc: intrinsic self-correction without external signals underperforms or degrades output ([Huang et al., ICLR'24](https://arxiv.org/abs/2310.01798); "cycle of self-deception", [arXiv 2506.18315](https://arxiv.org/html/2506.18315v2)). A simplifier pass is a supplement tied to external signals (tests, linters, the diff) — never the primary control.
- Embed verbatim in worker prompts. Subagent inheritance of parent rules varies by agent type and tool (Claude Code Explore/Plan skip CLAUDE.md; Cursor semantics differ) — embedding is the only guaranteed channel, same rationale as `references/discipline-block.md`.
- State the rule positively. Do not add stacked "don't do X" prohibitions around it ([Anthropic prompting guidance](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)).
