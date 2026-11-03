# Watchers Never End in Silence

Every background wait is armed through `watch-run`. It ends in exactly one terminal line, `WATCH <id> OK|FAIL|TIMEOUT|STALL|BLIND|ENDED`, records it in `~/.fleet/watchers/`, pushes to the phone on anything but OK, and heartbeats. The hooks re-state that registry at every prompt and stop. Instituted 2026-10-08, extended 2026-11-02 (incident record in the reference).

- **Name the failure before arming.** `--ok` is the effect, `--fail` the producer's failure signal (a log line, an rc file, a dead pid), `--progress` plus `--stall` the proof of motion. A watcher with only `--ok` is the bug this rule kills. If no failure probe exists, say so when arming.
- **Silence is never success.** After any wait, verify the effect directly before reporting it ([[no-unverified-claims]]). A Monitor filter covers every terminal state.
- **Inline `until ...; do sleep; done` loops are banned.** Use `watch-run`, with `--detach` when the wait must outlive the session.
- **Test before arming.** Run each probe once by hand and quote its exit code. Bespoke watchers follow the five checks in the reference.
- **Acknowledge, never ignore.** When the hook reports a watcher, act and run `watch-run ack <id>`. A successor ends and acks the watcher it supersedes.
