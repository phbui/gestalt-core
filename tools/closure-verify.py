#!/usr/bin/env python3
"""Run a capture-inbox commitment's verification command and print timestamped output to paste back.

Phi copy-pastes a bash command already written inside a knowledge/capture-inbox.md commitment
and pastes the output back into the file by hand. This tool runs that command for him and formats
the result for a clean paste. It NEVER decides an item is closed and NEVER touches
capture-inbox.md (or any other file) — closure is a human/agent judgment call made after reading
the output this prints, not something this script performs.

Usage:
  tools/closure-verify.py --command 'ssh hub systemctl --user is-active fleet-heartbeat.timer'
  tools/closure-verify.py --command 'curl -s http://hub:8787/health' --label 'dashboard-health'
  tools/closure-verify.py --command 'false' --timeout 5
"""
import argparse
import datetime
import subprocess
import sys


def run(command: str, timeout: float):
    """Run command in a shell, capturing stdout+stderr combined and the exit code.

    Returns (combined_output, returncode, timed_out).
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.stdout.decode("utf-8", errors="replace"), proc.returncode, False
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, (bytes, bytearray)) else (e.stdout or "")
        return out, None, True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--command", required=True, help="shell command to run")
    ap.add_argument("--label", default="", help="tag printed alongside the timestamp")
    ap.add_argument("--timeout", type=float, default=120, help="seconds before the command is killed (default 120)")
    a = ap.parse_args()

    ts = datetime.datetime.now(datetime.UTC).astimezone().isoformat(timespec="seconds")
    header = f"[{ts}]" + (f" {a.label}" if a.label else "")
    print(header)
    print(f"$ {a.command}")

    output, rc, timed_out = run(a.command, a.timeout)
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    if timed_out:
        print(f"(timed out after {a.timeout}s)")
        print("exit: TIMEOUT")
        sys.exit(1)
    print(f"exit: {rc}")
    sys.exit(0)


if __name__ == "__main__":
    main()
