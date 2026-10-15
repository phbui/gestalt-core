"""retrieval-ratchet-check.sh appends one line per run to ~/.fleet/ratchet.log (roadmap N10) and keeps its exit codes."""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "retrieval-ratchet-check.sh"


@pytest.fixture
def rig(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    repo = tmp_path / "repo"; (repo / ".search").mkdir(parents=True)
    bindir = tmp_path / "bin"; bindir.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return {"home": home, "repo": repo, "bin": bindir, "log": home / ".fleet" / "ratchet.log"}


def uv(rig, body: str) -> None:
    p = rig["bin"] / "uv"; p.write_text(f"#!/bin/sh\n{body}\n"); p.chmod(0o755)


def db(rig, vec: bool) -> None:
    c = sqlite3.connect(rig["repo"] / ".search" / "gestalt.db")
    c.execute("create table if not exists sections(a)")
    if vec:
        c.execute("create table sections_vec(a)")
    c.commit(); c.close()


def run(rig):
    env = dict(os.environ, HOME=str(rig["home"]), PATH=f"{rig['bin']}:{os.environ['PATH']}")
    env.pop("RATCHET_LOG", None)
    return subprocess.run(["sh", str(SCRIPT)], cwd=rig["repo"], env=env, capture_output=True, text=True, timeout=30)


def last(rig) -> list[str]:
    return rig["log"].read_text().splitlines()[-1].split()


def test_skip_without_an_index(rig):
    r = run(rig)
    f = last(rig)
    assert r.returncode == 0 and f[3] == "SKIP" and f[4] == "reason=no-index"


def test_skip_without_the_vector_leg(rig):
    db(rig, vec=False)
    assert run(rig).returncode == 0 and last(rig)[3:] == ["SKIP", "reason=no-vector-leg"]


def test_pass_logs_and_exits_zero(rig):
    db(rig, vec=True); uv(rig, "exit 0")
    assert run(rig).returncode == 0 and last(rig)[3] == "PASS" and len(last(rig)) == 4


def test_fail_logs_and_still_blocks_the_push(rig):
    db(rig, vec=True); uv(rig, "echo regression; exit 1")
    r = run(rig)
    assert r.returncode == 1 and last(rig)[3] == "FAIL"


def test_harness_error_is_a_logged_skip_with_exit_zero(rig):
    db(rig, vec=True); uv(rig, "echo Traceback; echo boom; exit 1")
    assert run(rig).returncode == 0 and last(rig)[3:] == ["SKIP", "reason=harness-error"]


def test_one_line_per_run(rig):
    db(rig, vec=True); uv(rig, "exit 0")
    run(rig); run(rig)
    assert len(rig["log"].read_text().splitlines()) == 2


# ---- clamp avoidance and GPU loss on the hub (2026-10-15) -------------------------------------

NO_CUDA = "echo 'hub: cannot run here: no CUDA device: the hub configuration reranks in fp16 on a GPU'"


def test_gpu_duty_and_allocator_defaults_reach_the_runner(rig):
    db(rig, vec=True); uv(rig, 'echo "$GESTALT_GPU_DUTY $PYTORCH_CUDA_ALLOC_CONF" > "$HOME/seen"; exit 0')
    assert run(rig).returncode == 0
    assert (rig["home"] / "seen").read_text().split() == ["0.6", "expandable_segments:True"]


def test_a_caller_can_override_the_duty(rig):
    db(rig, vec=True); uv(rig, 'echo "$GESTALT_GPU_DUTY" > "$HOME/seen"; exit 0')
    env = dict(os.environ, HOME=str(rig["home"]), PATH=f"{rig['bin']}:{os.environ['PATH']}", GESTALT_GPU_DUTY="0.35")
    env.pop("RATCHET_LOG", None)
    subprocess.run(["sh", str(SCRIPT)], cwd=rig["repo"], env=env, capture_output=True, text=True, timeout=30)
    assert (rig["home"] / "seen").read_text().strip() == "0.35"


def test_lost_gpu_checks_the_default_config_and_says_the_hub_config_was_not_checked(rig):
    db(rig, vec=True)
    uv(rig, f'case "$*" in *--allow-missing-hub-config*) exit 0;; *) {NO_CUDA}; exit 1;; esac')
    r = run(rig)
    assert r.returncode == 0 and last(rig)[3:] == ["PASS", "reason=hub-config-unchecked"]
    assert "NOT checked" in r.stderr and "restart WSL" in r.stderr


def test_lost_gpu_still_blocks_a_regression_in_the_default_config(rig):
    db(rig, vec=True)
    uv(rig, f'case "$*" in *--allow-missing-hub-config*) echo "default: r@1 dropped"; exit 1;; *) {NO_CUDA}; exit 1;; esac')
    r = run(rig)
    assert r.returncode == 1 and last(rig)[3] == "FAIL" and "r@1 dropped" in r.stdout


def test_device_restart_during_the_run_is_named_in_the_skip(rig):
    db(rig, vec=True)
    ts = rig["home"] / ".fleet" / "gpu-unclamp" / "last-restart-ts"; ts.parent.mkdir(parents=True)
    uv(rig, f'echo Traceback; echo "RuntimeError: CUDA error"; date +%s > "{ts}"; exit 1')
    r = run(rig)
    assert r.returncode == 0 and last(rig)[3:] == ["SKIP", "reason=gpu-restarted-mid-run"]
    assert "watchdog restarted the device" in r.stdout


def test_an_old_device_restart_does_not_relabel_a_real_harness_error(rig):
    db(rig, vec=True)
    ts = rig["home"] / ".fleet" / "gpu-unclamp" / "last-restart-ts"; ts.parent.mkdir(parents=True); ts.write_text("1000000000\n")
    uv(rig, "echo Traceback; echo boom; exit 1")
    assert run(rig).returncode == 0 and last(rig)[3:] == ["SKIP", "reason=harness-error"]
