"""L2: `tools/gestalt rebuild` writes paper-* entries to MANIFEST-papers.md and keeps a pointer in MANIFEST.md.

Runs a copy of the script against a fixture tree in a temp dir, never the live repo.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import _load

REPO = Path(__file__).resolve().parent.parent
NORMAL = ["alpha", "beta"]
PAPERS = ["paper-one", "paper-three", "paper-two"]  # glob order


def _entry(slug: str) -> str:
    return f'---\ntitle: "T {slug}"\ntype: note\ntags: [x]\n---\n# {slug}\n\nSee [[alpha]]. ^{slug}-anchor\n'


def _kb(tmp_path: Path, papers=PAPERS) -> Path:
    root = tmp_path / "kb"
    (root / "tools").mkdir(parents=True)
    (root / "knowledge").mkdir()
    (root / "claude-tree" / "hooks").mkdir(parents=True)
    shutil.copy2(REPO / "tools" / "gestalt", root / "tools" / "gestalt")
    shutil.copy2(REPO / "claude-tree" / "hooks" / "safety-validate.sh", root / "claude-tree" / "hooks" / "safety-validate.sh")
    for slug in NORMAL + list(papers):
        (root / "knowledge" / f"{slug}.md").write_text(_entry(slug), encoding="utf-8")
    return root


def _rebuild(root: Path) -> None:
    env = {"GESTALT_DIR": str(root), "HOME": str(root.parent), "PATH": os.environ.get("PATH", "")}
    r = subprocess.run(["bash", str(root / "tools" / "gestalt"), "rebuild"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


def _slugs(path: Path) -> list[str]:
    return [ln[4:].strip() for ln in path.read_text(encoding="utf-8").split("\n") if ln.startswith("### ")]


def test_papers_land_in_second_file(tmp_path):
    root = _kb(tmp_path)
    _rebuild(root)
    assert _slugs(root / "MANIFEST.md") == NORMAL
    assert _slugs(root / "MANIFEST-papers.md") == PAPERS
    main = (root / "MANIFEST.md").read_text(encoding="utf-8")
    assert "## Papers" in main and "3 literature notes" in main and "MANIFEST-papers.md" in main
    papers = (root / "MANIFEST-papers.md").read_text(encoding="utf-8")
    assert "### paper-one\nnote | T paper-one\nLinks: [[alpha]]\nBlocks: ^paper-one-anchor" in papers
    assert "Holds the 3 paper-* entries" in papers


def test_rebuild_is_idempotent(tmp_path):
    root = _kb(tmp_path)
    _rebuild(root)
    before = [(root / n).read_bytes() for n in ("MANIFEST.md", "MANIFEST-papers.md", "GRAPH.md")]
    _rebuild(root)
    assert before == [(root / n).read_bytes() for n in ("MANIFEST.md", "MANIFEST-papers.md", "GRAPH.md")]


def test_no_papers_means_no_second_file_and_no_section(tmp_path):
    root = _kb(tmp_path, papers=[])
    _rebuild(root)
    assert not (root / "MANIFEST-papers.md").exists()
    assert "## Papers" not in (root / "MANIFEST.md").read_text(encoding="utf-8")
    # a stale second file from an earlier build is removed
    (root / "MANIFEST-papers.md").write_text("stale\n")
    _rebuild(root)
    assert not (root / "MANIFEST-papers.md").exists()


def test_read_manifest_sees_all_slugs(tmp_path):
    root = _kb(tmp_path)
    _rebuild(root)
    ap = _load("auto_promote_split", "tools/auto-promote.py")
    entries = ap.read_manifest(root)
    assert set(entries) == set(NORMAL + PAPERS)
    assert entries["paper-two"]["title"] == "T paper-two"
    assert entries["paper-two"]["tags"] == ["x"]


def _hook(root: Path, target: Path) -> str:
    payload = '{"tool_input":{"file_path":"%s"}}' % target
    r = subprocess.run(["bash", str(root / "claude-tree" / "hooks" / "safety-validate.sh")], input=payload,
                       env={"PATH": os.environ.get("PATH", ""), "HOME": str(root.parent)}, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.skipif(shutil.which("jq") is None, reason="hook needs jq")
def test_hook_count_includes_papers_and_does_not_warn(tmp_path):
    root = _kb(tmp_path)
    _rebuild(root)
    assert "WARNING" not in _hook(root, root / "MANIFEST.md")


@pytest.mark.skipif(shutil.which("jq") is None, reason="hook needs jq")
def test_hook_still_warns_on_truncated_manifest(tmp_path):
    root = _kb(tmp_path)
    _rebuild(root)
    (root / "MANIFEST-papers.md").unlink()
    (root / "MANIFEST.md").write_text("# Gestalt Manifest\n", encoding="utf-8")
    assert "WARNING" in _hook(root, root / "MANIFEST.md")
