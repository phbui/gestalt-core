"""Unit tests for tools/sync-cursor-tree.py on small fixtures (2026-10-06, roadmap L4).

Hermetic: the script's REPO, CLAUDE and CURSOR globals are pointed at a temp dir.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "sync-cursor-tree.py"


@pytest.fixture()
def gen(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("sync_cursor_tree_fixture", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    claude, cursor = tmp_path / "claude-tree", tmp_path / ".cursor"
    for d in ("rules", "skills/demo", "agents", "references", "hooks"):
        (claude / d).mkdir(parents=True)
    cursor.mkdir()
    monkeypatch.setattr(mod, "CLAUDE", claude)
    monkeypatch.setattr(mod, "CURSOR", cursor)
    monkeypatch.setattr(mod, "SKIP_FILES", {})
    monkeypatch.setattr(mod, "RULE_DESCRIPTIONS", {})
    monkeypatch.setattr(mod, "AGENT_DESCRIPTIONS", {})
    monkeypatch.setattr(mod, "SKILL_BACKGROUND", {})
    return mod, claude, cursor


def _gen_text(mod, rel):
    return mod.expected()[rel]


def test_rule_with_paths_becomes_globs_and_not_always_apply(gen):
    mod, claude, _ = gen
    (claude / "rules" / "scoped.md").write_text('---\npaths:\n  - "src/**"\n  - "docs/*.md"\n---\n\n# Scoped\n\nBody line.\n')
    out = _gen_text(mod, "rules/scoped.mdc")
    assert out.startswith("---\ndescription: Body line.\nglobs: \"src/**,docs/*.md\"\nalwaysApply: false\n---\n\n# Scoped")
    assert "paths" not in out


def test_rule_without_frontmatter_is_always_apply(gen):
    mod, claude, _ = gen
    (claude / "rules" / "always.md").write_text("# Always\n\nEvery turn.\n")
    out = _gen_text(mod, "rules/always.mdc")
    assert out == "---\ndescription: Every turn.\nalwaysApply: true\n---\n\n# Always\n\nEvery turn.\n"


def test_skill_drops_claude_fields_and_keeps_name_and_description(gen):
    mod, claude, _ = gen
    (claude / "skills/demo/SKILL.md").write_text(
        '---\nname: demo\ndescription: "Does a thing."\nmodel: sonnet\nuser-invocable: true\nargument-hint: "x"\n---\n\n# Demo\n'
    )
    out = _gen_text(mod, "skills/demo/SKILL.md")
    assert out == '---\nname: demo\ndescription: "Does a thing."\ndisable-model-invocation: true\n---\n\n# Demo\n'


def test_agent_keeps_name_and_description_only(gen):
    mod, claude, _ = gen
    (claude / "agents/a.md").write_text("---\nname: a\ndescription: Scans.\nmodel: haiku\nmaxTurns: 5\n---\n\nPrompt.\n")
    assert _gen_text(mod, "agents/a.md") == "---\nname: a\ndescription: Scans.\n---\n\nPrompt.\n"


def test_path_rewrites(gen):
    mod, claude, _ = gen
    (claude / "references" / "r.md").write_text(
        "See `gestalt/.claude/references/x.md`, `.claude/rules/y.md`, and `~/.claude/settings.json` and `$HOME/.claude/z`. `claude-tree/` stays.\n"
    )
    out = _gen_text(mod, "references/r.md")
    assert "gestalt/.cursor/references/x.md" in out
    assert ".cursor/rules/y.mdc" in out
    assert "~/.claude/settings.json" in out and "$HOME/.claude/z" in out
    assert "claude-tree/" in out


def test_check_write_and_idempotency(gen):
    mod, claude, cursor = gen
    (claude / "rules" / "always.md").write_text("# Always\n\nEvery turn.\n")
    (cursor / "stray.md").write_text("extra\n")
    assert mod.main(["--check"]) == 1
    assert mod.main(["--write"]) == 0
    assert not (cursor / "stray.md").exists()
    assert (cursor / "rules" / "always.mdc").exists()
    assert mod.main(["--check"]) == 0
    before = {p: p.read_bytes() for p in cursor.rglob("*") if p.is_file()}
    assert mod.main(["--write"]) == 0
    assert before == {p: p.read_bytes() for p in cursor.rglob("*") if p.is_file()}


def test_cursor_only_files_are_never_touched(gen):
    mod, claude, cursor = gen
    for name in ("hetslam.md", "hooks.json", "mcp.json"):
        (cursor / name).write_text(f"keep {name}\n")
    (claude / "rules" / "a.md").write_text("# A\n\nText.\n")
    assert mod.main(["--write"]) == 0
    for name in ("hetslam.md", "hooks.json", "mcp.json"):
        assert (cursor / name).read_text() == f"keep {name}\n"
    assert mod.main(["--check"]) == 0


def test_skip_file_is_left_alone_and_unlisted_source_fails(gen, monkeypatch, capsys):
    mod, claude, cursor = gen
    (claude / "rules" / "native.md").write_text("# N\n\nClaude text.\n")
    (cursor / "rules").mkdir()
    (cursor / "rules" / "native.mdc").write_text("cursor-native text\n")
    monkeypatch.setattr(mod, "SKIP_FILES", {"rules/native.md": "Cursor-native"})
    assert mod.main(["--write"]) == 0
    assert (cursor / "rules" / "native.mdc").read_text() == "cursor-native text\n"
    (claude / "mystery.txt").write_text("x")
    assert mod.main(["--check"]) == 2
    assert "mystery.txt" in capsys.readouterr().err
