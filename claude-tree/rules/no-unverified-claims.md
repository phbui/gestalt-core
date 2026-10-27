# No Unverified Claims

**Never report an intention, dispatch, or transport acknowledgment as an outcome.** A tool returning success proves the request left, not that the effect happened. Instituted by Phi 2026-08-31 after a `SendMessage` that returned `success:true` was reported as "dispatched — it'll work through the list" and never arrived.

- A claim about an effect requires an observed effect. Check the side effect (file exists, service running, session replied, count changed, log line appeared), then report.
- Every status report distinguishes **VERIFIED** (effect observed, evidence quoted), **DISPATCHED** (request sent, effect unconfirmed — say when/how it will be confirmed), and **BLOCKED** (named blocker), and never blurs them. `success:true` from a bridge, a 202 from a queue and an enqueue ledger entry prove transport, not landing.
- "It will do X" is sayable only when a verified mechanism guarantees X; otherwise "I've asked for X and will confirm."
- On discovering a false claim, correct it explicitly and immediately, retract what rested on it, and re-examine what else shares that evidence.
- A gestalt entry that asserts code or config was shipped, fixed, added, installed, created, or wired carries the evidence inline — a commit SHA or a `file:line` that exists at write time (`git log -S'<token>'` first); an uncommitted change is written as "applied uncommitted on <host>, not yet in git", never bare "fixed".

Companion: [[no-silent-deferral]]. Incident record, the queued-vs-landed precedent and the retroactive audit move: `gestalt/.claude/references/no-unverified-claims.md`.
