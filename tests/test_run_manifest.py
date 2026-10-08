"""Hermetic tests for tools/run-manifest (roadmap L1, 2026-10-06). Temp git repos, no network."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "run-manifest"


def git(repo, *a):
    subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q")
    (r / "train.py").write_text("print('hi')\n")
    (r / "config.yaml").write_text("lr: 1\n")
    (r / "data").mkdir()
    (r / "data" / "a.txt").write_text("aaa")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    return r


def rm(repo, *args, **kw):
    return subprocess.run([sys.executable, str(TOOL), *args], cwd=repo, capture_output=True, text=True, **kw)


def manifests(repo):
    return [json.loads(p.read_text()) for p in sorted((repo / "runs").glob("*/manifest.json"))]


def test_clean_run(repo):
    r = rm(repo, "run", "--seed", "7", "--tag", "exp=base", "--", "sh", "-c", "echo out; echo err >&2; echo $RUN_SEED")
    assert r.returncode == 0 and "out" in r.stdout
    m = manifests(repo)[0]
    assert m["status"] == "ok" and m["exit_code"] == 0 and m["seed"] == 7
    assert m["git"]["dirty"] is False and len(m["git"]["commit"]) == 40 and m["git"]["diff_file"] is None
    assert m["tags"] == {"exp": "base"} and m["ended_utc"] and m["duration_s"] >= 0
    d = repo / "runs" / m["run_id"]
    assert "out" in (d / "stdout.log").read_text() and "7" in (d / "stdout.log").read_text()
    assert "err" in (d / "stderr.log").read_text()


def test_dirty_run_saves_diff(repo):
    (repo / "train.py").write_text("print('changed')\n")
    rm(repo, "run", "--", "true")
    m = manifests(repo)[0]
    assert m["git"]["dirty"] is True and m["git"]["diff_file"] == "dirty.patch"
    assert "changed" in (repo / "runs" / m["run_id"] / "dirty.patch").read_text()


def test_runs_dir_does_not_make_tree_dirty(repo):
    rm(repo, "run", "--", "true")
    rm(repo, "run", "--", "true")
    assert all(m["git"]["dirty"] is False for m in manifests(repo))


def test_failing_command_keeps_exit_code(repo):
    r = rm(repo, "run", "--", "sh", "-c", "exit 3")
    assert r.returncode == 3
    m = manifests(repo)[0]
    assert m["status"] == "failed" and m["exit_code"] == 3


def test_missing_command(repo):
    r = rm(repo, "run", "--", "no-such-binary-xyz")
    assert r.returncode == 127
    assert manifests(repo)[0]["status"] == "failed"


def test_config_hash_changes(repo):
    rm(repo, "run", "--config", "config.yaml", "--", "true")
    (repo / "config.yaml").write_text("lr: 2\n")
    rm(repo, "run", "--config", "config.yaml", "--", "true")
    a, b = manifests(repo)
    assert a["configs"][0]["sha256"] != b["configs"][0]["sha256"]
    assert a["configs"][0]["method"] == "contents"


def test_directory_hash_changes_with_file(repo):
    rm(repo, "run", "--data", "data", "--", "true")
    (repo / "data" / "a.txt").write_text("aaaa-longer")
    rm(repo, "run", "--data", "data", "--", "true")
    a, b = manifests(repo)
    assert a["data"][0]["method"] == "listing" and a["data"][0]["files"] == 1
    assert a["data"][0]["sha256"] != b["data"][0]["sha256"]


def test_killed_command_status_interrupted(repo):
    r = rm(repo, "run", "--", "sh", "-c", "kill -TERM $$")
    assert r.returncode == 128 + signal.SIGTERM
    m = manifests(repo)[0]
    assert m["status"] == "interrupted" and m["signal"] == signal.SIGTERM


def test_wrapper_terminated_leaves_interrupted_manifest(repo):
    p = subprocess.Popen([sys.executable, str(TOOL), "run", "--", "sleep", "30"], cwd=repo,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(100):
        if list((repo / "runs").glob("*/manifest.json")):
            break
        time.sleep(0.05)
    assert manifests(repo)[0]["status"] == "running"
    time.sleep(0.3)
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=10)
    m = manifests(repo)[0]
    assert m["status"] == "interrupted" and p.returncode == 128 + signal.SIGTERM


def test_list_show_cite(repo):
    rm(repo, "run", "--config", "config.yaml", "--seed", "3", "--", "true")
    m = manifests(repo)[0]
    out = rm(repo, "list").stdout
    assert m["run_id"] in out and "ok" in out
    shown = json.loads(rm(repo, "show", m["run_id"][:12]).stdout)
    assert shown["run_id"] == m["run_id"]
    cite = rm(repo, "cite", m["run_id"]).stdout.strip()
    assert cite == f"run {m['run_id']} | commit {m['git']['commit'][:7]} | config {m['configs'][0]['sha256'][:8]} | seed 3"
    assert rm(repo, "show", "nope").returncode != 0
