"""Dual-agent mirror parity: `.cursor/` is generated from `claude-tree/` by
`tools/sync-cursor-tree.py`, so the one statement that matters is that
`tools/sync-cursor-tree.py --check` passes (nothing missing, stale or extra).

Replaced 2026-10-06. The old dated ratchet (KNOWN_DIVERGENT_*) compared two
hand-kept trees. The generator makes it unnecessary. What stays here checks
what the generator does not: the frontmatter of files on the generator's skip
table (hand-kept Cursor-native copies), and that the skip table is honest.
Hermetic: reads committed files only, no corpus, no network.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
# F10: gestalt's own Claude Code tree lives at claude-tree/ (gestalt/.claude is a
# compat symlink to it) — read the real dir directly.
CLAUDE = REPO / "claude-tree"
CURSOR = REPO / ".cursor"
SCRIPT = REPO / "tools" / "sync-cursor-tree.py"


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text.strip()
    end = text.index("---", 3)
    fm = yaml.safe_load(text[3:end]) or {}
    return fm, text[end + 3 :].strip()


def test_cursor_tree_is_generated_from_claude_tree():
    """The strong statement: the generator reports no missing, stale or extra file."""
    r = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, f"tools/sync-cursor-tree.py --check failed. Run it with --write.\n{r.stdout}{r.stderr}"


def test_skip_table_entries_are_real_and_explained():
    spec = importlib.util.spec_from_file_location("sync_cursor_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bad = []
    for rel, reason in mod.SKIP_FILES.items():
        if not reason.strip():
            bad.append(f"{rel}: no reason")
        if not (CLAUDE / rel).exists():
            bad.append(f"{rel}: not in claude-tree/")
    for rel in mod.skipped_targets():
        if not (CURSOR / rel).exists():
            bad.append(f"{rel}: skipped but absent from .cursor/")
    assert not bad, f"skip table out of date: {bad}"


# --- Skills: frontmatter name/description parity; body may legitimately diverge


def test_skill_frontmatter_name_matches_directory_both_sides():
    for cf in sorted(CLAUDE.glob("skills/*/SKILL.md")):
        name = cf.parent.name
        uf = CURSOR / "skills" / name / "SKILL.md"
        if not uf.exists():
            continue
        fm1, _ = _frontmatter(cf)
        fm2, _ = _frontmatter(uf)
        assert fm1.get("name") == name, f".claude/skills/{name}: frontmatter name {fm1.get('name')!r} != dir name"
        assert fm2.get("name") == name, f".cursor/skills/{name}: frontmatter name {fm2.get('name')!r} != dir name"


def test_skill_description_present_both_sides():
    missing = []
    for cf in sorted(CLAUDE.glob("skills/*/SKILL.md")):
        name = cf.parent.name
        uf = CURSOR / "skills" / name / "SKILL.md"
        if not uf.exists():
            continue
        fm1, _ = _frontmatter(cf)
        fm2, _ = _frontmatter(uf)
        if not fm1.get("description"):
            missing.append(f".claude/skills/{name}")
        if not fm2.get("description"):
            missing.append(f".cursor/skills/{name}")
    assert not missing, f"skills missing a frontmatter description: {missing}"


# --- Rules: frontmatter shape follows the documented translation table -----


# 2026-08-18: `.cursor/rules/supabase-schema-migrations.mdc` was fixed to
# carry `alwaysApply: true` (+ `description:`, no `globs:`), matching its
# Claude counterpart's no-frontmatter (always-loaded) shape per
# dual-agent-sync.md's translation table. No known gaps remain; kept empty so
# any new one fails loudly instead of silently joining a growing allowlist.
KNOWN_RULE_FRONTMATTER_GAPS: set[str] = set()


def test_rule_frontmatter_translation_shape():
    """Claude side: either no frontmatter (always-loaded) or a `paths:` list.
    Cursor side: either `alwaysApply: true` or a non-empty `globs:` — and per
    dual-agent-sync.md ("Always add description: in the Cursor version"),
    a path-scoped cursor rule must carry a `description:`. An empty `globs: `
    key (present but valueless, a template leftover on a couple of always-
    loaded rules) does not count as scoped — `alwaysApply` is authoritative."""
    violations = []
    for cf in sorted(CLAUDE.glob("rules/*.md")):
        uf = CURSOR / "rules" / f"{cf.stem}.mdc"
        if not uf.exists():
            continue
        fm1, _ = _frontmatter(cf)
        fm2, _ = _frontmatter(uf)
        claude_scoped = "paths" in fm1
        cursor_scoped = bool(fm2.get("globs"))
        bad = []
        if claude_scoped != cursor_scoped:
            bad.append(f"claude paths-scoped={claude_scoped} vs cursor globs-scoped={cursor_scoped}")
        elif cursor_scoped and not fm2.get("description"):
            bad.append("cursor .mdc is globs-scoped but has no description:")
        elif not cursor_scoped and not fm2.get("alwaysApply"):
            bad.append("claude rule is always-loaded but cursor .mdc lacks alwaysApply: true")
        if bad and cf.stem not in KNOWN_RULE_FRONTMATTER_GAPS:
            violations.append(f"{cf.stem}: {'; '.join(bad)}")
    assert not violations, f"rule frontmatter translation violations (not in the documented ratchet): {violations}"
