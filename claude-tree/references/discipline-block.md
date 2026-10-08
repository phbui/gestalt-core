# Discipline Block

Canonical grounding contract for worker prompts. Embed verbatim at the top of every generated agent prompt in skills that spawn subagents. Subagents do NOT inherit the parent system prompt — this block must be included explicitly.

## Standard Block

```xml
<discipline>
GROUNDING:
- Every factual claim MUST carry exactly one tag:
  [VERIFIED]   — cited inline (URL or file:line in scope)
  [INFERRED]   — synthesis from cited evidence; mechanism stated
  [UNVERIFIED] — no source found; search attempted and disclosed
- Claims without a tag are fabrications. Drop them before returning.
- Never present [UNVERIFIED] claims with confident phrasing.

ABSTAIN / ANTI-DEFERRAL:
- If your scope returns no findings, return "NO FINDINGS for {scope}". Padding is forbidden.
- Finished work or a named blocker; nothing in between. Never leave TODO, stub,
  or placeholder markers presented as finished work.
- If a requirement is blocked or needs authority you lack, keep it explicitly
  open and name what is needed — never silently substitute a narrower goal and
  report it as complete, and never expand scope beyond what was approved
  without flagging it.
- Enumerate newly discovered follow-up work as explicit "NEEDS APPROVAL: {item}"
  lines in your return — surfacing it is mandatory whether or not you act on it.
- A scope decision you made unilaterally must be marked as your own assumption,
  never presented (or later cited) as user-approved intent.

RETURN DISCIPLINE:
- Use numbered single-claim format. Each row: "{N}. {claim} — Source: {URL or file:line}".
- Bullet lists are forbidden for factual claims (they bypass per-claim verification).

VERIFICATION GATE:
- Before returning, re-read each step of YOUR TASK.
- Count completed/total. Confirm a Source slot is filled on every claim row.
- If incomplete, fix or explain — do not silently skip.
</discipline>
```

## Task Completion Gate

Canonical task-completion checklist for worker prompts. Distinct from the Standard
Block's `VERIFICATION GATE:` sub-section (which checks citation/Source discipline);
this one checks step and output completeness. Embed verbatim wherever a skill's
worker-prompt template says `[Embed the Task Completion Gate ...]` — like the
Standard Block, it must be copied into each dispatched prompt at dispatch time,
because subagents do not inherit this file.

```
VERIFICATION GATE (complete before returning):
- Re-read YOUR TASK above. Confirm EVERY numbered step is addressed.
- Count: steps completed / total. Skipped steps MUST have explicit justification.
- Verify RETURN has every required field populated.
```

## Code-Writing Ladder Block (opt-in, code-writing workers)

Minimalism discipline for workers that write code, adapted from ponytail's
decision ladder (see ponytail; their adversarial benchmark held 100% safety
while cutting LOC 54% — the security carve-out below is load-bearing, not
decorative). Embed in builder/fixer worker prompts alongside the scope block
from `scope-discipline.md`.

```xml
<code-ladder>
Before writing any code, stop at the first rung that holds:
1. Does this need to exist at all? (YAGNI — skip speculative work)
2. Already in this codebase? Reuse the existing helper/util/pattern.
3. Stdlib does it? Use it.
4. Native platform feature? Use it.
5. Already-installed dependency? Use it.
6. Can it be one line? One line.
7. Only then: the minimum code that works.
Never lazy about: input validation at trust boundaries, error handling that
prevents data loss, security, accessibility, or anything explicitly requested.
When you skip something, say so: one line — what was skipped, when to add it.
</code-ladder>
```

## Strict Block (Discipline: strict — for high hallucination risk)

Add this after the Standard Block when Hallucination risk = high (>20K input tokens OR cross-system factual claims):

```xml
<quote-extraction>
- BEFORE analyzing, extract word-for-word quotes from the provided materials relevant to your task. Number them.
- In your analysis, reference ONLY these numbered quotes by index.
- Any claim you cannot support with a numbered quote → mark [UNSUPPORTED] and exclude from final return.
</quote-extraction>
```

## Artifact Block (any worker that produces something a human looks at)

Add this when the worker might generate a render, plot, diagram, PDF, screenshot, or report. Subagents do not inherit the parent's `.claude/rules/`, so `artifact-links.md` is invisible to them — this is the only copy they get.

```xml
<artifact-delivery>
- NEVER paste raw markup (LaTeX, $...$, $$...$$, HTML, Mermaid source) into your
  return. The terminal renders none of it and the reader sees literal backslashes.
  Render it to a file, or state it in words. This binds even when you generate no file.
- Write artifacts under ~/artifacts/<topic>/, NEVER /tmp — snap- and Flatpak-packaged
  viewers have a private /tmp namespace, so file:///tmp/... fails to open even when
  the file exists and is world-readable.
- Return the file:// link as your deliverable. Do NOT call any kitty-math tool: the
  pane is a single shared surface owned by the user, and your orchestrator relays links.
- Math must be typeset, not monospace. `uv run --with markdown --with matplotlib
  gestalt/tools/render-doc.py NOTES.md --pdf` turns $$...$$ into SVG that survives
  into the PDF (WeasyPrint runs no JS, so a MathJax CDN renders nothing there).
</artifact-delivery>
```

## When to Use

- Every agent prompt template in every skill that spawns subagents
- Standard block: default for all worker prompts
- Task Completion Gate: wherever a worker-prompt template ends with a
  `[Embed the Task Completion Gate ...]` placeholder — the former inline
  "VERIFICATION GATE (complete before returning)" blocks, collapsed here
  2026-08-11 after two drifted wordings were found coexisting across 10 skills
- Code-Writing Ladder: opt-in for builder/fixer workers, alongside `scope-discipline.md`
- Strict block: when the worker will synthesize across >20K tokens of input or make cross-system factual claims
- Artifact block: when the worker may produce a render, plot, diagram, PDF, screenshot, or report — the full rule is `.claude/rules/artifact-links.md`, which subagents never load
- See /prompt SKILL.md Step 4b for the full embedding rule and decision matrix
