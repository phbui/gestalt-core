#!/usr/bin/env bash
# Hook for UserPromptSubmit and Stop. Reads the watch-run registry (~/.fleet/watchers) and never lets an unhandled
# watcher outcome pass unnoticed.
#   prompt: inject the actionable lines as additionalContext so the turn starts knowing them.
#   stop:   if any watcher has an UNHANDLED OUTCOME (armed but its pid is dead, armed past its deadline, or ended
#           non-OK and not acknowledged) block the stop ONCE with the reason. If stop_hook_active is already true,
#           or there is nothing actionable, emit a systemMessage instead or nothing. A healthy armed watcher never
#           blocks: ending the turn is how Monitor events are delivered. Approved by Phi 2026-11-02.
# Session attribution is not possible (CLAUDE_SESSION_ID is not in the Bash environment), so the block is per
# machine: whichever session sees it first handles it or runs `watch-run ack <id>`.
MODE="${1:-prompt}"
WR="$HOME/.local/bin/watch-run"
[ -x "$WR" ] || WR="${CLAUDE_PROJECT_DIR:-$HOME/Documents/GitHub/gestalt}/tools/fleet/bin/watch-run"
[ -x "$WR" ] || exit 0
IN=$(cat)
lines=$(timeout 10 "$WR" status --brief 2>/dev/null | head -12)
[ -n "$lines" ] || exit 0
python3 -I - "$MODE" "$lines" "$IN" <<'PY'
import json, sys
mode, lines, raw = sys.argv[1], sys.argv[2], sys.argv[3]
try: inp = json.loads(raw) if raw.strip() else {}
except Exception: inp = {}
n = len(lines.splitlines())
text = (f"watch-run registry: {n} watcher(s) with an unhandled outcome. Read each line, report it, act on it, "
        f"then run `watch-run ack <id>`.\n{lines}")
if mode == "stop":
    if inp.get("stop_hook_active"):
        print(json.dumps({"systemMessage": "still unhandled after one continuation, not blocking again: " + text}))
    else:
        print(json.dumps({"decision": "block", "reason": text}))
else:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}))
PY
exit 0
