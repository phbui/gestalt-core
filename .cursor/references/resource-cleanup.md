# Resource Cleanup

Playbook for tearing down runtime resources an agent creates: background shells,
subagents, monitors, worktrees, and cloud compute. Loaded on demand by
`.cursor/rules/resource-cleanup.mdc`.

The governing asymmetry: **leaving something running wastes money; killing
something still working destroys hours of work.** Every ambiguity below is
therefore resolved toward *not* killing. Beware the subtler danger: a check that
fails toward "nothing is running" is worse than no check at all, because it
licenses a teardown.

## What the harness already handles (do not duplicate)

| Resource | Automatic behavior | Source |
|---|---|---|
| Background Bash tasks | "automatically cleaned up when Claude Code exits" — but backgrounding the session instead of exiting hands them to the background session, "where they keep running" | [interactive-mode](https://code.claude.com/docs/en/interactive-mode) |
| Background task, runaway output | terminated above 5 GB of output | same |
| Background task, memory pressure | killed on OS memory signal, but only after 30 min idle AND no turn/subagent running (`CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP`) | same |
| Subagent-owned background shells | terminated after 60 min (`CLAUDE_SUBAGENT_BG_SHELL_MAX_MS`) | same |
| Finished background subagents | stay listed in `/tasks` until the session clears its task list | [sub-agents](https://code.claude.com/docs/en/sub-agents) |
| Agent worktrees (`isolation: "worktree"`) | auto-removed **only if unchanged** — an agent that wrote anything leaves one behind | [Agent tool](https://code.claude.com/docs/en/tools-reference) |

Two gaps that guarantee is silent about, so plan for them:

- The exit-time guarantee is the app's own cleanup path. A `SIGKILL`, OOM-kill,
  or crash of the CLI cannot run it. Treat "the harness will clean up" as true
  for graceful exit only.
- Whether `TaskStop` kills a shell's grandchildren (e.g. a wrapper script's
  `python` subprocess) is **not documented**. Verify with `ps`, do not assume.

## Tools — and the one that looks right but is not

`/tasks` is the background-task view: it lists running shells and subagents and
can stop them. `TaskStop` stops one by task id, agent-team teammate id, or named
background agent. `Monitor` watches stop by being cancelled, via `TaskStop`, or
at session end — but one started with `persistent` never times out on its own.
Read a task's output with `Read` on its output file path (`TaskOutput` is
deprecated; `KillShell` and `BashOutput` no longer exist).

**`TaskList` is not this.** `TaskList`/`TaskCreate`/`TaskGet`/`TaskUpdate` are
the session to-do checklist — they replaced `TodoWrite` — and are scoped to
agent-team teammates. They report nothing about running shells, so **a clean
`TaskList` is not evidence that nothing is running.** Inventory processes with
`/tasks` and `ps`.

Concurrency ceilings worth mirroring in your own launchers: 20 concurrent
subagents, 200 per session. There is **no** equivalent automatic ceiling on OS
processes you spawn yourself — spawning ~121 concurrent upload processes
OOM-killed a launcher on 2026-08-05 and completed zero work. Cap your own
fan-out.

## Process matching: the trap and the actual fix

`pgrep`/`pkill` **never match themselves** — the man page guarantees "the
running pgrep, pkill, or pidwait process will never report itself as a match."
So the `grep` bracket trick (`[s]ky`) is unnecessary for them.

The real false positive is the **ancestor**: with `-f`, "the full command line is
used," so a parent `bash -c "... pgrep -fc 'sky jobs launch' ..."` contains the
pattern in its own argv and gets counted. On 2026-08-05 this reported two live
processes that did not exist (two wrapper layers, two phantom matches).

- Add `-A` (`--ignore-ancestors`, "exclude our ancestors from results") to drop
  the wrapper shells: `pgrep -A -f <pattern>`. This is the fix — `-x` is not.
  `-x` matches the process *name* only, so `pgrep -x 'sky jobs launch'` returns
  nothing even while that job runs; `-xf` demands the whole command line match
  exactly, so it misses any invocation carrying extra flags. Both fail silently
  toward "nothing is running", the direction that kills live work.
  (`-A` is procps-ng; BSD/macOS `pgrep` lacks it — use the `ps` form there.)
- To eyeball it instead, use `ps -eo pid,args | grep -F <pattern>` and read the
  ancestors out by PID. Do **not** append `grep -v grep`: it also hides real
  processes whose command line contains "grep" (a `--logdir /var/grep_logs`
  training run disappears completely).
- Never `pkill -f` a broad pattern that could match another session's work, a
  dev server, or another user's process. Prefer a PID recorded at launch, and
  scope with `-u $(id -u)` when you must match by pattern.

## Cloud compute

Stopping is not terminating, and the difference is billing:

| Action | Instance billing | Disk billing | Data |
|---|---|---|---|
| `sky stop` / autostop | stops | **continues** | preserved |
| `sky down` / autodown | stops | stops | **lost** |

So a cluster left STOPPED is still costing money, and a cluster you `down` is
gone for good. Terminate when results are fetched and verified; stop when you
intend to resume. `sky status` alone does not show queued work — check
`sky jobs queue` too before concluding a cluster is idle.

The SkyPilot **managed-jobs controller** deserves its own note: roughly
$0.25/hr running versus under $0.004/hr stopped, and it **autostops after 10
minutes idle**. That autostop is documented behavior, not a fault — but a cold
controller stalls the next submission, so start it explicitly before a batch
rather than letting concurrent submissions race to wake it. Never tear it down
casually: doing so "loses all logs and status information for the finished
managed jobs," and SkyPilot only permits it when no managed job is in flight.

## Other resource classes

Each of these is covered by no harness guarantee; name them in your inventory
when you created one: git worktrees (`git worktree list` / `git worktree
remove` — auto-cleanup skips any worktree you modified), docker containers
(`docker ps` / `docker compose down`), tmux sessions (`tmux ls`), ssh tunnels
and `kubectl port-forward` (background shells that look idle), and MCP servers
you started.

## The procedure

1. **Inventory before acting.** `ps -eo pid,ppid,etime,args` for OS processes,
   `/tasks` for background shells and subagents, `sky status` **and**
   `sky jobs queue` for cloud, plus `docker ps`, `git worktree list`, `tmux ls`
   as they apply. Never kill from memory of what you started, and never treat an
   empty to-do list as an empty process table.
2. **Classify each item** as (a) mine and finished, (b) mine and still working
   or with work queued, (c) not mine / unknown.
3. **Act only on (a).** Stop the task, terminate the cluster, remove the temp
   artifact.
4. **(b) is never auto-cleaned.** A long training run, a live experiment, a
   cluster with a job pending, a deliberately persistent cluster: leave it, and
   say in your reply that it is still running and why.
5. **(c) requires confirmation.** Another session's shell, an unrecognized
   cluster, anything you cannot prove you created. Report it; let the user
   decide.
6. **Verify the kill landed** with the same tool that saw it alive (`ps` by PID,
   `/tasks`, `sky status`) and say so. A tool that never observed the resource
   cannot confirm its death. Report cost implications when cloud was involved.

## When to run it

- After fetching results from a remote job: terminate the compute in the same
  turn, not "later".
- When a long-running task ends and its background watcher is now pointless.
- Before ending a session that spawned background work, or when the user asks
  to clean up.
- **Not** mid-experiment. If timed check-ins are driving a long job, the
  launcher shell is disposable *only if the job is detached* — confirm with
  `ps -o pid,ppid,pgid,sid,args` that the job is not in the shell's process
  group, or that it was started under `setsid`/`nohup`/`disown`. An attached
  child dies with its parent's process group. Per the note above, whether
  `TaskStop` reaches grandchildren is undocumented; check, do not assume.
