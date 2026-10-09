# No Unverified Claims

Companion to `rules/no-unverified-claims.md`, which carries the binding summary that loads in every session. This file is the complete original rule — rationale, incident history, evidence, and mechanics — loaded on demand.

**Never report an intention, dispatch, or transport acknowledgment as an outcome.** A tool returning success proves the request left; it does not prove the effect happened. Instituted by Phi 2026-08-31 after a live failure: a SendMessage to a Remote Control session returned `success:true`, the plan was reported as "dispatched — it'll work through the list," and the target session never received anything; the false claim stood for several turns until Phi checked the machine himself.

## The contract

- A claim about an effect requires an observed effect. Check the side effect (file exists, service running, session replied, count changed, log line appeared), then report. "Sent" is a claim about transport; "done", "running", "it will work through" are claims about the world.
- Every status report distinguishes three states and never blurs them: **VERIFIED** (effect observed, evidence quoted), **DISPATCHED** (request sent, effect unconfirmed — say so and say when/how it will be confirmed), **BLOCKED** (named blocker). DISPATCHED must never wear VERIFIED's words.
- Forward-looking statements are commitments, not facts. "It will do X" is sayable only when a verified mechanism guarantees X (a timer that has fired before, a queue observed draining). Otherwise: "I've asked for X and will confirm."
- Optimistic tool results are the trap this rule exists for: `success:true` from a bridge, a 202 from a queue, an enqueue ledger entry — all proved transport, none proved landing (same failure class as the graphiti queued-vs-landed lie, capture-inbox 2026-08-30).
- On discovering a false claim: correct it explicitly and immediately, retract conclusions that rested on it, and re-examine what else was built on the same evidence.

## Gestalt writes: a "shipped/fixed" claim carries its evidence

When writing a gestalt entry (knowledge, capture-inbox, rules) that asserts code or config was **shipped, fixed, added, installed, created, or wired**, the sentence MUST carry the evidence inline: a commit SHA, or a `file:line` that exists at write time. A claim like "Fix: `_SKIP=…` at :226" with no SHA is unverifiable and rots into a false claim the moment the working-tree change is lost or was never committed — which is exactly what happened (2026-08-31 audit: the skip-names fix was live but uncommitted, so the note asserting it read as fictional against committed history; four independent audit agents flagged it). Before writing such a claim, run `git log -S'<token>'` or confirm the `file:line`; if the change is uncommitted, either commit it first or write "applied uncommitted on <host>, not yet in git" — never bare "fixed". Retroactive audit move: `grep` claim-verbs across gestalt, then verify each named file/flag/function against `git log -S` + current tree.

Companion of [[no-silent-deferral]], which requires proof before claiming completion — this rule pins the delivery-vs-effect case that slipped past it.
