# Watcher Dry Run, the full protocol

The rule `claude-tree/rules/watcher-dry-run.md` is the short form. This reference holds the complete checks, the stub recipe and the incidents behind each clause.

Every background watcher, waiter or launcher is tested on every branch before it is left running, and reported as armed only after the test. Instituted by Phi 2026-10-08 ("remember to test watchers or waiters to confirm they work before letting them go") and widened 2026-10-09 ("make sure the rule ensures a full test like that every time") after a watcher's done, vanished-session and stall branches had been read but not run.

The watcher is a script file in the session scratchpad, never an inline one-liner. It takes an iteration cap, and it takes a sample file argument that makes it parse that file once and exit. Its remote command is one ssh call whose output it parses. Before arming, run these five checks and quote each result in chat:

1. **Must fire.** A synthetic sample with the arm-time marker followed by one line per event kind the parser reports (start, end, failure, stop, lease wait, GPU loss). The parser must print exactly those lines.
2. **Must not fire.** The real log as it stands before the marker, or with no marker. The parser must print nothing. A pattern an old line can satisfy is a bug, so anchor to lines after the marker, a count that must grow, or a timestamp after the arm time.
3. **Terminal branches with a stub.** Put a stub `ssh` first on PATH that prints a canned output file, and run the watcher once per terminal state: the done flag, the vanished session or process, a plain cap exit. Each must print its own line and stop, and a cap exit must not print done.
4. **Stall branch.** Copy the script with the stall threshold and the sleep cut to seconds, run it against an unchanging stub, and see the STALL line carry the last log line. Never edit the live script while it runs, because bash reads it incrementally.
5. **Live first iteration.** After arming, read the first event and the first heartbeat from the task output and quote them. The heartbeat must carry the state that tells silence from progress (the lease holder, the last log line, a counter).

A launcher that acts on an event is tested the same way with the action replaced by an echo. A waiter that triggers a remote action states the probe that proves the effect, and the effect is checked afterwards ([[no-unverified-claims]]).

## Survive the session, and test the producer's real lines (2026-10-09)

- **Durable location, detached process.** The watcher script and its samples live under `~/artifacts/<topic>/watch/`, never in the session scratchpad, which vanishes when the session ends. Arm it with `setsid nohup bash watcher.sh <cap> >> watcher.log 2>&1 < /dev/null & disown`, write its pid to a file beside it, and read `watcher.log` on every check-in. A watcher started as a session background task dies with the session (00:17 tonight, ten unwatched minutes). Pair it with a `ScheduleWakeup` fallback so a check-in happens even when the harness sends no notification.
- **The must-fire sample is copied from the producer.** Take every `log "..."` statement from the runner or driver (`grep -n 'log "' runner.sh`) and put one real line per statement in the sample. The pattern that missed `STOP:` tonight had been written from memory with a space after STOP.
- **Thresholds are environment variables.** `STALL_SECS` and `SLEEP_SECS` default to the live values and drop to seconds in the stall test, so the stall branch runs on the same file that is armed, with no edited copy.
- **Senders get a dry-run flag, never a PATH stub.** A stub binary on PATH does not reach a call that runs under `bash -lc`, which reloads the login PATH. The 00:29 "dry run" sent a real placeholder email. Every mail or push script has a `..._DRY_RUN=1` branch that prints what it would send and exits 0, and the guard tests use that flag.
- **The stall clock follows the step, not only the driver's log.** A long step writes nothing to the driver log for hours, so the watcher also reads the active step's own progress line (the newest `rc*/<step>.err` tail) and counts a stall only when both are frozen. Test both ways: a frozen stub must produce STALL, and a stub whose progress line changes on every call must not (01:02 and 01:22 false STALLs on a three-hour step, 2026-10-09).
- **Stop the old watcher before editing, and prove it is gone.** `kill` the pid, then `pgrep -af` the script name and expect nothing, and only then edit and re-arm. A watcher still in its sleep survives a moment past the kill and would otherwise read the edited file.
- **A report or mail failure is itself an event.** The runner mails the raw step tail under a named subject when the report script fails, and the mail script substitutes a named subject for a blank one, so no step result reaches the phone as an empty "mail failed" ping.

The stub is three lines and lives beside the watcher:

```
#!/usr/bin/env bash
# stub ssh: print the canned hub output named by WATCH_STUB
cat "$WATCH_STUB"
```

Run as `WATCH_STUB=sample.txt PATH=stubdir:$PATH bash watcher.sh 3`. Reference implementation and its test transcript: the phase-2 watcher of 2026-10-09 in the-relevant-entry. Related: `feedback_test_watchers_before_arming` and `feedback_watchers_stall_detection` in the session memory, hub-invariants for what a hub watcher may act on.
