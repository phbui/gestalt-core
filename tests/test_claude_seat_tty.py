"""claude-seat with no terminal on stdin or stdout, and with a tmux server older than the binary (2026-10-15).

Until 2026-10-15 `tmux new-session -d` ran first and only the attach failed, so every retry from a caller
without a terminal left a detached Claude running in its own seat. Now a caller without a terminal gets the
seat created detached plus the attach command on stdout, and a client/server version mismatch (a package
upgrade that replaced tmux under the running server) is refused before any seat is created."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "fleet" / "bin" / "claude-seat"

STUB_TMUX = """#!/bin/sh
echo "$@" >> '{log}'
case "$1" in
  -V) echo "tmux ${{TMUX_STUB_BIN_VERSION:-3.6}}"; exit 0 ;;
  display-message|display) [ -n "${{TMUX_STUB_SERVER_VERSION:-}}" ] && {{ echo "$TMUX_STUB_SERVER_VERSION"; exit 0; }}; exit 1 ;;
  new-session|set-environment) exit 0 ;;
esac
exit 1
"""


def _run(tmp_path, args, env_extra):
    """Run claude-seat from a project dir with no terminal on stdin or stdout, a stub `claude`, and a stub
    `tmux` on PATH that records every call to tmux.log. The stub answers `-V` and `display-message -p` from
    TMUX_STUB_BIN_VERSION / TMUX_STUB_SERVER_VERSION (no server when the latter is unset), accepts
    new-session and set-environment, and refuses everything else (has-session, ls, attach)."""
    bindir = tmp_path / "bin"; bindir.mkdir(exist_ok=True)
    proj = tmp_path / "proj"; proj.mkdir(exist_ok=True)
    stub = bindir / "claude"; stub.write_text("#!/bin/sh\necho STUB-RAN \"$@\"\n"); stub.chmod(0o755)
    tmux = bindir / "tmux"; tmux.write_text(STUB_TMUX.format(log=tmp_path / "tmux.log")); tmux.chmod(0o755)
    env = dict(os.environ, CLAUDE_BIN=str(stub), PATH=f"{bindir}:{os.environ['PATH']}", FLEET_DIR=str(tmp_path / "fleet"), FLEET_HOST="testhost", **env_extra)
    env.pop("TMUX", None)
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, cwd=proj, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)


def _tmux_log(tmp_path):
    p = tmp_path / "tmux.log"
    return p.read_text().splitlines() if p.exists() else []


def test_no_terminal_creates_the_seat_detached_and_prints_the_attach_command(tmp_path):
    r = _run(tmp_path, ["--dangerously-skip-permissions"], {})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "claude-seat attach testhost:claude-proj-1"
    assert "detached" in r.stderr and "CLAUDE_NO_TMUX" in r.stderr
    assert "STUB-RAN" not in r.stdout
    log = _tmux_log(tmp_path)
    assert any(line.startswith("new-session -d -s claude-proj-1 ") and "--dangerously-skip-permissions" in line for line in log), log
    assert not any(line.startswith("attach") for line in log), log


def test_no_terminal_attach_to_a_live_seat_by_name_prints_instead_of_attaching(tmp_path):
    # `attach NAME` requires has-session to succeed; the stub refuses it, so the path exits 1 before attach.
    r = _run(tmp_path, ["attach", "claude-proj-1"], {})
    assert r.returncode == 1 and "no session claude-proj-1" in r.stderr
    assert not any(line.startswith("attach") for line in _tmux_log(tmp_path))


def test_no_terminal_still_runs_the_bare_cli_when_asked(tmp_path):
    r = _run(tmp_path, ["--dangerously-skip-permissions"], {"CLAUDE_NO_TMUX": "1"})
    assert r.returncode == 0 and "STUB-RAN --dangerously-skip-permissions" in r.stdout


def test_no_terminal_headless_print_mode_is_not_a_seat(tmp_path):
    r = _run(tmp_path, ["-p", "hello"], {})
    assert r.returncode == 0 and "STUB-RAN -p hello" in r.stdout


def test_server_older_than_binary_is_refused_before_any_seat_is_created(tmp_path):
    r = _run(tmp_path, [], {"TMUX_STUB_SERVER_VERSION": "3.4", "TMUX_STUB_BIN_VERSION": "3.6"})
    assert r.returncode == 3
    assert "3.4" in r.stderr and "3.6" in r.stderr and "Restart the server" in r.stderr
    assert not any(line.startswith("new-session") for line in _tmux_log(tmp_path)), _tmux_log(tmp_path)


def test_server_matching_binary_proceeds(tmp_path):
    r = _run(tmp_path, [], {"TMUX_STUB_SERVER_VERSION": "3.6", "TMUX_STUB_BIN_VERSION": "3.6"})
    assert r.returncode == 0 and r.stdout.startswith("claude-seat attach ")
    assert any(line.startswith("new-session -d -s claude-proj-1 ") for line in _tmux_log(tmp_path))


def test_no_server_running_means_nothing_to_compare(tmp_path):
    r = _run(tmp_path, [], {"TMUX_STUB_BIN_VERSION": "3.6"})
    assert r.returncode == 0 and r.stdout.startswith("claude-seat attach ")
