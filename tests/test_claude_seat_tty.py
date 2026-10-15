"""claude-seat refuses to start a seat when stdin or stdout is not a terminal (2026-10-15).

Before this guard, `tmux new-session -d` ran first and only the attach failed, so every retry from a
non-terminal caller left a detached Claude running in its own seat."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "fleet" / "bin" / "claude-seat"


def _run(tmp_path, args, env_extra):
    """Run claude-seat with no terminal on stdin or stdout, a stub `claude`, and a stub `tmux` on PATH that
    records every call to tmux.log and refuses, so the test can prove the guard fired before any tmux use."""
    bindir = tmp_path / "bin"; bindir.mkdir(exist_ok=True)
    stub = bindir / "claude"; stub.write_text("#!/bin/sh\necho STUB-RAN \"$@\"\n"); stub.chmod(0o755)
    tmux = bindir / "tmux"; tmux.write_text(f"#!/bin/sh\necho \"$@\" >> '{tmp_path}/tmux.log'\nexit 1\n"); tmux.chmod(0o755)
    env = dict(os.environ, CLAUDE_BIN=str(stub), PATH=f"{bindir}:{os.environ['PATH']}", FLEET_DIR=str(tmp_path / "fleet"), **env_extra)
    env.pop("TMUX", None)
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)


def test_no_terminal_means_no_seat_and_a_named_refusal(tmp_path):
    r = _run(tmp_path, ["--dangerously-skip-permissions"], {})
    assert r.returncode == 2
    assert "no terminal" in r.stderr and "CLAUDE_NO_TMUX" in r.stderr
    assert "STUB-RAN" not in r.stdout
    assert not (tmp_path / "tmux.log").exists(), (tmp_path / "tmux.log").read_text()


def test_no_terminal_still_runs_the_bare_cli_when_asked(tmp_path):
    r = _run(tmp_path, ["--dangerously-skip-permissions"], {"CLAUDE_NO_TMUX": "1"})
    assert r.returncode == 0 and "STUB-RAN --dangerously-skip-permissions" in r.stdout


def test_no_terminal_headless_print_mode_is_not_a_seat(tmp_path):
    r = _run(tmp_path, ["-p", "hello"], {})
    assert r.returncode == 0 and "STUB-RAN -p hello" in r.stdout
