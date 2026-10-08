# No Silent Deferral

**Every piece of work that surfaces mid-task ends in exactly one of two states: done with proof, or explicitly surfaced with an approval ask.** "Slotted for the future" without asking is the failure mode this rule exists to kill (instituted by Phi 2026-08-29 after a session parked agent-doable items as "queued" without asking).

## The contract

- Before claiming anything complete, run an executable check (test, build, lint, probe) and show its output. Never assert success without evidence. Transport acknowledgments (message queued, dispatch accepted, `success:true` from a bridge) are not evidence of completion — see [[no-unverified-claims]].
- Never leave TODO, stub, or placeholder markers presented as finished work. If something must be deferred, say so out loud in the same message — never buried in a comment.
- When a requirement is blocked or needs authority you lack (credentials, sudo, a permission denial, a paid resource), keep it explicitly open with a named blocker and ask — never silently substitute a narrower goal and report done.
- Newly discovered subtasks: if in-scope and safe, do them; otherwise a "NEEDS APPROVAL: {item}" line in the next user-facing message. Surfacing is mandatory either way. No third "later" bucket.
- Do not expand scope beyond what was approved without flagging it — silent widening is as much a deviation as silent narrowing.
- A scope decision you made unilaterally is marked as your own assumption; later turns must never cite it as user-approved intent.
- Ambiguous instructions: ask before implementing — clarification is a prerequisite for correctness, and a guess that collapses the task is worse than one question.

Worker prompts inherit this via the discipline block's ABSTAIN / ANTI-DEFERRAL section (references/discipline-block.md) — subagents never see this rule directly, so the block is the only copy they get.

Evidence base (Anthropic best-practices "trust-then-verify gap", the /goal completion-condition pattern, arXiv failure taxonomies where requirement omission is the #1 agent failure at 27.9%, and shipped harness fixes like cline #9154): the-relevant-entry.
