#!/usr/bin/env python3
"""Behavioral test for tools/closure-verify.py.

closure-verify runs a capture-inbox commitment's verification command and prints timestamped
output for a human or agent to paste back by hand. It must never decide an item is closed and
must never touch capture-inbox.md (or anything else). This exercises the real `run()` helper
directly (imported via importlib, since the module is a hyphenated filename) and the CLI
end-to-end via subprocess, matching the standalone runner in tests/test_tools_discrimination_scripts.py
which executes every tools/tests/test-* script and asserts exit 0.
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "closure-verify.py"

spec = importlib.util.spec_from_file_location("closure_verify", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fail = 0


def check(name, cond, detail=""):
    global fail
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fail = 1


# --- unit-level: the run() helper ---

out, rc, timed_out = mod.run("echo hi; exit 7", timeout=5)
check("run() captures stdout", "hi" in out, out)
check("run() propagates exit code", rc == 7, rc)
check("run() reports not timed out", timed_out is False)

out, rc, timed_out = mod.run("echo err-line >&2", timeout=5)
check("run() merges stderr into the combined stream", "err-line" in out, out)

out, rc, timed_out = mod.run("sleep 5", timeout=0.3)
check("run() reports a timeout without raising", timed_out is True)
check("run() gives no exit code on timeout", rc is None, rc)

# --- CLI-level: format and the never-touch-capture-inbox contract ---


def cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


proc = cli("--command", "echo paste-me", "--label", "my-tag")
lines = proc.stdout.splitlines()
check("cli exits 0 on a successful probe", proc.returncode == 0, proc.returncode)
check("cli header carries an ISO timestamp and the label", bool(lines) and lines[0].startswith("[") and "my-tag" in lines[0], lines[:1])
check("cli echoes the command for the paste", "$ echo paste-me" in proc.stdout)
check("cli prints the probe's own output", "paste-me" in proc.stdout)
check("cli prints an exit: 0 trailer", proc.stdout.strip().splitlines()[-1] == "exit: 0", proc.stdout.strip().splitlines()[-1:])

proc = cli("--command", "exit 4")
check("cli itself exits 0 even when the probed command fails", proc.returncode == 0, proc.returncode)
check("cli reports the probed command's nonzero exit", "exit: 4" in proc.stdout, proc.stdout)

proc = cli("--command", "sleep 5", "--timeout", "0.3")
check("cli exits nonzero on a timed-out probe", proc.returncode == 1, proc.returncode)
check("cli reports TIMEOUT", "TIMEOUT" in proc.stdout, proc.stdout)

with tempfile.TemporaryDirectory() as td:
    inbox = Path(td) / "capture-inbox.md"
    inbox.write_text("- [ ] a commitment\n", encoding="utf-8")
    before = inbox.read_text(encoding="utf-8")
    subprocess.run(
        [sys.executable, str(SCRIPT), "--command", "echo noop"],
        cwd=td,
        capture_output=True,
        text=True,
        timeout=30,
    )
    after = inbox.read_text(encoding="utf-8")
    check("cli never writes to capture-inbox.md", before == after)

sys.exit(1 if fail else 0)
