"""`tools/gestalt rebuild` indexes tracked knowledge files only.

Every node regenerates MANIFEST.md on pull and commits it, so a file that exists on one laptop only
(2026-09-06: eight untracked paper stubs on mobile) made every other node's rebuild differ and
blocked fleet-sync on the hub and the laptop2 with a dirty MANIFEST. Runs against a throwaway git repo.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "gestalt"


def _git(repo: Path, *args: str) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _entry(title: str) -> str:
    return f"---\ntitle: \"{title}\"\ntype: repo\n---\n# {title}\n\nBody of {title}. ^{title}-anchor\n\n## Data Flow\n\n- A -> B\n"


def _rebuild(root: Path, **extra_env: str) -> str:
    env = {"GESTALT_DIR": str(root), "HOME": str(Path.home()), "PATH": os.environ.get("PATH", ""), **extra_env}
    r = subprocess.run(["bash", str(TOOL), "rebuild"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return (root / "MANIFEST.md").read_text(encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    (root / "knowledge").mkdir(parents=True)
    (root / "rules").mkdir()
    (root / "knowledge" / "tracked-entry.md").write_text(_entry("tracked-entry"), encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    (root / "knowledge" / "only-here.md").write_text(_entry("only-here"), encoding="utf-8")
    return root


def test_untracked_entry_is_left_out_of_manifest_and_graph(tmp_path) -> None:
    root = _repo(tmp_path)
    manifest = _rebuild(root)
    assert "### tracked-entry" in manifest
    assert "only-here" not in manifest
    assert "only-here" not in (root / "GRAPH.md").read_text(encoding="utf-8")


def test_opt_in_includes_untracked(tmp_path) -> None:
    root = _repo(tmp_path)
    manifest = _rebuild(root, GESTALT_INDEX_UNTRACKED="1")
    assert "### only-here" in manifest


def test_from_index_ignores_unstaged_edits_and_sees_staged_ones(tmp_path) -> None:
    """The other-session case: an unstaged edit adds ^new-anchor to a tracked entry. With
    GESTALT_INDEX_FROM_INDEX=1 the index names only what git holds; staging the edit admits it."""
    root = _repo(tmp_path)
    entry = root / "knowledge" / "tracked-entry.md"
    entry.write_text(entry.read_text(encoding="utf-8") + "\nA new fact. ^new-anchor\n", encoding="utf-8")
    assert "^new-anchor" in _rebuild(root)  # default: working tree
    manifest = _rebuild(root, GESTALT_INDEX_FROM_INDEX="1")
    assert "^new-anchor" not in manifest and "^tracked-entry-anchor" in manifest
    assert "only-here" not in manifest
    _git(root, "add", "knowledge/tracked-entry.md")
    assert "^new-anchor" in _rebuild(root, GESTALT_INDEX_FROM_INDEX="1")


def test_from_index_cleans_its_snapshot(tmp_path) -> None:
    root = _repo(tmp_path)
    tmpdir = tmp_path / "t"
    tmpdir.mkdir()
    _rebuild(root, GESTALT_INDEX_FROM_INDEX="1", TMPDIR=str(tmpdir))
    assert not list(tmpdir.glob("gestalt-index.*"))


def test_a_directory_without_git_is_indexed_whole(tmp_path) -> None:
    root = tmp_path / "plain"
    (root / "knowledge").mkdir(parents=True)
    (root / "rules").mkdir()
    (root / "knowledge" / "a.md").write_text(_entry("a"), encoding="utf-8")
    assert "### a" in _rebuild(root)


def test_a_fresh_repo_with_nothing_tracked_is_indexed_whole(tmp_path) -> None:
    root = tmp_path / "fresh"
    (root / "knowledge").mkdir(parents=True)
    (root / "rules").mkdir()
    (root / "knowledge" / "a.md").write_text(_entry("a"), encoding="utf-8")
    _git(root, "init", "-q")
    assert "### a" in _rebuild(root)
