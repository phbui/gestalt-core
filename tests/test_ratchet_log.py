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
