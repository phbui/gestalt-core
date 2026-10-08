"""B-02: `tools/gestalt rebuild` lists the real rules (claude-tree/rules/*.md) in MANIFEST.md.

Runs a copy of the script against a small fixture tree in a temp dir, never the live repo.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _rebuild(tmp_path: Path, with_rules: bool = True) -> str:
    root = tmp_path / "kb"
    (root / "tools").mkdir(parents=True)
    (root / "knowledge").mkdir()
    shutil.copy2(REPO / "tools" / "gestalt", root / "tools" / "gestalt")
    (root / "knowledge" / "one.md").write_text('---\ntitle: "one"\ntype: repo\n---\n# one\n\nBody. ^one-anchor\n', encoding="utf-8")
    if with_rules:
        rules = root / "claude-tree" / "rules"
        rules.mkdir(parents=True)
        (rules / "alpha-rule.md").write_text("---\npaths:\n  - x\n---\n\n# Alpha Rule Title\n\nbody\n", encoding="utf-8")
        (rules / "beta-rule.md").write_text("# Beta Rule Title\n\nbody\n", encoding="utf-8")
    env = {"GESTALT_DIR": str(root), "HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    r = subprocess.run(["bash", str(root / "tools" / "gestalt"), "rebuild"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return (root / "MANIFEST.md").read_text(encoding="utf-8")


def test_manifest_lists_claude_tree_rules(tmp_path):
    m = _rebuild(tmp_path)
    assert "- `alpha-rule` — Alpha Rule Title" in m
    assert "- `beta-rule` — Beta Rule Title" in m
    assert "(none)" not in m.split("## Knowledge")[0]


def test_manifest_without_rules_still_says_none(tmp_path):
    m = _rebuild(tmp_path, with_rules=False)
    assert m.split("## Knowledge")[0].rstrip().endswith("(none)")
