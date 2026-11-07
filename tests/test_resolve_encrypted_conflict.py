"""tools/resolve-encrypted-conflict.sh merges both sides of a rebase conflict in a git-crypt file.

Runs the tool against a throwaway repo with a stub `git-crypt` whose smudge is `cat`, so the three
stages are plaintext. On 2026-11-06 the real tool died at its ciphertext check (the error branch had
slipped into a comment, and set -e turned a plaintext side into a silent exit), left the upstream
side in the tree and dropped four lines the rebased commit had added.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = Path(os.environ.get("RESOLVE_TOOL_UNDER_TEST") or REPO / "tools" / "resolve-encrypted-conflict.sh")
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=ENV)


def _conflicted_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    hub = repo / "hub.md"
    hub.write_text("# hub\n\n- [[paper-a]]: A\n- [[paper-b]]: B\n")
    _git(repo, "add", "hub.md"); _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "ingest")
    hub.write_text(hub.read_text() + "- [[paper-theirs1]]: T1\n- [[paper-theirs2]]: T2\n")
    _git(repo, "commit", "-q", "-am", "ingest adds two")
    _git(repo, "checkout", "-q", "main")
    hub.write_text(hub.read_text() + "- [[paper-ours1]]: O1\n")
    _git(repo, "commit", "-q", "-am", "main adds one")
    _git(repo, "checkout", "-q", "ingest")
    r = _git(repo, "rebase", "main")
    assert r.returncode != 0 and "CONFLICT" in (r.stdout + r.stderr)
    stub = tmp_path / "bin"; stub.mkdir()
    (stub / "git-crypt").write_text('#!/usr/bin/env bash\n[ "$1" = smudge ] && exec cat\nexit 0\n')
    (stub / "git-crypt").chmod(0o755)
    return repo, stub


def test_merge_keeps_both_sides(tmp_path: Path) -> None:
    repo, stub = _conflicted_repo(tmp_path)
    env = dict(ENV, PATH=f"{stub}:{os.environ['PATH']}")
    r = subprocess.run(["bash", str(TOOL), "hub.md"], cwd=repo, capture_output=True, text=True, env=env)
    merged = (repo / "hub.md").read_text()
    assert r.returncode == 0, f"tool exited {r.returncode}: {r.stdout!r} {r.stderr!r}"
    assert "paper-ours1" in merged, merged
    assert "paper-theirs1" in merged and "paper-theirs2" in merged, f"theirs side dropped: {merged!r}"
    assert "WARNING" not in r.stderr or "<<<<<<<" in merged, r.stderr
