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


## watch-run, the only sanctioned arming path (2026-11-02)

`tools/fleet/bin/watch-run` replaces hand-written loops. It takes `--ok` (effect observed, exit 0; not yet, exit 1; anything else is a blind poll), `--fail` (producer failed, exit 0), `--progress` with `--stall` (a line that must keep changing), `--timeout`, `--interval`, `--beat`, `--probe-timeout`, `--blind-max`, `--note`, `--no-push` and `--detach`. Every probe runs under `timeout`. The run ends in exactly one terminal line, `WATCH <id> OK|FAIL|TIMEOUT|STALL|BLIND|ENDED <detail>`, with exit codes 0 to 5 in that order. An EXIT trap writes ENDED when the process is killed, so a watcher that dies with its session still leaves a record. The registry is `~/.fleet/watchers/<id>.json`. `watch-run status --brief` lists armed watchers past their deadline, armed watchers whose pid is dead (ENDED-UNREPORTED), and non-OK terminals not yet acknowledged. `watch-run ack <id>` clears one. `watch-run selftest` exercises all six branches plus a must-not-fire case in about 15 seconds. The hook `claude-tree/hooks/watcher-registry-hook.sh` injects the status as `additionalContext` on UserPromptSubmit and as a `systemMessage` at Stop.

Use it as a Monitor command for a wait inside the session, so each heartbeat and the terminal line arrive as events. Use `--detach` for a wait that must outlive the session. Keep a Monitor's `timeout_ms` under 30 minutes, which is the harness cap regardless of the value asked (anthropics/claude-code #94393), and set `--timeout` a minute below it so the tool's own TIMEOUT line lands before the harness expiry notice.

### Incident 2026-11-02, a success-only Monitor through a hub push failure

**What happened.** At 23:11:18Z a Monitor was armed to poll `git ls-remote` until origin reached `315c10a`, the hub's index commit. The hub's push failed at 23:13:42Z on the retrieval ratchet (the golden set hash had changed). The Monitor had one success branch and a ten-minute cap, so it emitted nothing. The assistant idled. Phi asked "how far in is it" at 23:19:31Z. The assistant learned of the failure at 23:19:36Z, 5 min 54 s after it happened, by running the check it should have armed. The Monitor's own cap line arrived at 23:21:43Z. Minutes later the same shape recurred in a sister session: its re-bank producer on the hub was killed by a `gpu-unclamp-watchdog` WSL restart at 23:31:38Z, and its Monitor polled only for a done flag.

**Root causes.** (1) The watcher observed the effect, not the producer. No `--fail` probe read the hub log. (2) No dry run. The five-check rule existed since 2026-10-08 and was applied to about 6% of the 184 watchers armed in the previous 35 days (ledger in the 2026-11-02 audit). (3) The Monitor tool's own contract, "silence is not success", was not followed. (4) Nothing re-stated the pending watcher to the assistant between turns.

**Fixes.** `watch-run` with a registry and push (shipped 2026-11-02, this commit). The registry hook (shipped to `~/artifacts/gestalt-audit-2026-11/hooks/`, installed by Phi through `install.sh`). `dispatch-watch` probe timeout and blind-poll lines, `watch-pr-checks.sh` poll cap, repeated failure line and `ghp` (shipped, this commit). Rule `claude-tree/rules/watcher-dry-run.md` rewritten around `watch-run` (shipped, this commit).

**Standing rule.** A watcher names its failure before it is armed, ends in exactly one terminal line, and is re-stated to the assistant on every turn until acknowledged. Silence is never success.

**Stop hook (approved by Phi 2026-11-02).** The registry hook's `stop` mode blocks the turn once, with the reason, when a watcher has an unhandled outcome: armed with a dead pid, armed past its deadline, or ended non-OK and unacknowledged. It never blocks on a healthy armed watcher, because ending the turn is how Monitor events arrive. It respects `stop_hook_active`, so a second stop gets a system message instead of a block. It does not read `background_tasks`, which is unreliable (#102617, #98816). Attribution is per machine, not per session, since `CLAUDE_SESSION_ID` does not reach the Bash environment.

**What did NOT ship.** A hook event on background-task end does not exist in Claude Code (anthropics/claude-code #100201), so a task that ends mid-turn still fires no hook; the registry hook covers it at the next prompt or stop. Per-session attribution of watchers waits on a session id in the tool environment.

### Supersession and leftover processes (2026-11-02, from session github-2d)

A new watcher first ends the one it supersedes. `TaskStop` the Monitor or `kill` the pid, confirm with `pgrep -af '[w]atch-run --id <id>'` (the bracket keeps the grep from matching itself), then `watch-run ack <id>` so the ENDED record stops repeating. Two live watchers on one job is the ceiling, and only when two sessions split the work.

A Monitor built as `tail -F log | grep -m1 pattern` leaves the `tail` alive after the match until the log gets another line, so every wake leaves one orphan. Five lingered on 2026-11-02. Kill it in the same command: `tail -F log | grep -m1 pattern; pkill -f "tail -F log"`. `watch-run` with `--progress` and `--fail` probes avoids the pattern altogether.
