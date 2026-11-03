"""watch-run ends in exactly one terminal line on every branch, records it in the registry, and `status` surfaces what needs attention."""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WR = REPO / "tools" / "fleet" / "bin" / "watch-run"


def run(reg, *args, timeout=30):
    env = dict(os.environ, WATCH_RUN_REGISTRY=str(reg))
    p = subprocess.run([str(WR), *args, "--no-push", "--beat", "1"], capture_output=True, text=True, env=env, timeout=timeout)
    terminal = [l for l in p.stdout.splitlines() if l.startswith("WATCH ") and l.split()[2] in ("OK", "FAIL", "TIMEOUT", "STALL", "BLIND", "ENDED")]
    return p.returncode, terminal, p.stdout


def state(reg, i):
    return json.load(open(reg / f"{i}.json"))["state"]


@pytest.mark.parametrize("name,want,rc,args", [
    ("ok", "OK", 0, ["--ok", "true", "--timeout", "10", "--interval", "1"]),
    ("fail", "FAIL", 1, ["--ok", "exit 1", "--fail", "true", "--timeout", "10", "--interval", "1"]),
    ("timeout", "TIMEOUT", 2, ["--ok", "exit 1", "--timeout", "2", "--interval", "1"]),
    ("stall", "STALL", 3, ["--ok", "exit 1", "--progress", "echo same", "--stall", "2", "--timeout", "20", "--interval", "1"]),
    ("blind", "BLIND", 4, ["--ok", "sleep 5", "--probe-timeout", "1", "--blind-max", "2", "--timeout", "30", "--interval", "1"]),
])
def test_every_branch_ends_in_exactly_one_terminal_line(tmp_path, name, want, rc, args):
    code, terminal, out = run(tmp_path, "--id", name, *args)
    assert code == rc, out
    assert len(terminal) == 1 and terminal[0].split()[2] == want, out
    assert state(tmp_path, name) == want


def test_a_killed_watcher_is_recorded_as_ended(tmp_path):
    env = dict(os.environ, WATCH_RUN_REGISTRY=str(tmp_path))
    p = subprocess.Popen([str(WR), "--id", "k", "--no-push", "--ok", "exit 1", "--timeout", "60", "--interval", "1"], stdout=subprocess.PIPE, text=True, env=env)
    time.sleep(2)
    p.send_signal(signal.SIGTERM)
    out, _ = p.communicate(timeout=10)
    assert "WATCH k ENDED" in out
    assert state(tmp_path, "k") == "ENDED"


def test_status_surfaces_unacked_failures_and_hides_acked_ones(tmp_path):
    run(tmp_path, "--id", "f", "--ok", "exit 1", "--fail", "true", "--timeout", "5", "--interval", "1")
    env = dict(os.environ, WATCH_RUN_REGISTRY=str(tmp_path))
    st = subprocess.run([str(WR), "status", "--brief"], capture_output=True, text=True, env=env).stdout
    assert "f: FAIL" in st and "unacknowledged" in st
    subprocess.run([str(WR), "ack", "f"], check=True, capture_output=True, env=env)
    st2 = subprocess.run([str(WR), "status", "--brief"], capture_output=True, text=True, env=env).stdout
    assert "f:" not in st2


def test_a_dead_armed_watcher_shows_as_ended_unreported(tmp_path):
    reg = tmp_path / "z.json"
    json.dump(dict(id="z", state="armed", detail="", pid=999999, started=0, deadline=10**10, last_beat=int(time.time()) - 30, note="n", session="", acked=False), open(reg, "w"))
    env = dict(os.environ, WATCH_RUN_REGISTRY=str(tmp_path))
    st = subprocess.run([str(WR), "status", "--brief"], capture_output=True, text=True, env=env).stdout
    assert "z: ENDED-UNREPORTED" in st


def test_selftest_passes():
    p = subprocess.run([str(WR), "selftest"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "SELFTEST PASS" in p.stdout, p.stdout + p.stderr
