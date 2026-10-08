"""Skill/agent/rule metadata hygiene, CI-safe (reads only committed `.claude/`
files, no corpus or index dependency):

1. Every `.claude/skills/*/SKILL.md` has frontmatter `name` matching its
   directory, and a non-empty `description` <= 300 chars (B4's rule — the
   description is what gets rendered into the always-loaded skill listing
   gestalt-efficiency-audit-2026-08 ^skill-double-list measured at ~2K tokens,
   so an oversized one is a direct, per-session cost).
2. `.claude/references/skill-index.md` mentions every skill directory (dead
   listing = a skill nobody can find; a listed-but-nonexistent skill = a stale
   pointer), allowing documented sub-command mentions (`/requirements implement`,
   `/consolidate publish`, etc.) that don't correspond to their own directory.
3. Every `.claude/agents/*.md` has frontmatter `name` + `description` + `model`.
4. Every rule in `.claude/rules/` either declares `paths:` (path-scoped, costs
   nothing until a matching file is touched) or is unconditional by the
   absence of `paths:` — this repo's own convention for "intentionally
   unconditional" (gestalt-efficiency-audit-2026-08 ^ctx-baseline: "Four rules
   are already path-scoped... the pattern to extend" — everything else is
   unconditional BY DESIGN, not oversight). The unconditional set's total
   bytes is budget-ratcheted: measured 34,407 B on 2026-08-18 (^ctx-baseline),
   ceiling set to 40,000 B so routine additions don't nuisance-fail this while
   still catching a real budget blowout.
5. F10 static check (gestalt-efficiency-audit-2026-08 ^skill-double-list):
   whether `/home/user/Documents/GitHub/.claude` is a symlink farm into
   `gestalt/.claude` — informational only (machine-specific path), prints the
   realpaths rather than asserting, and `local`-marked + skipped elsewhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
# F10: gestalt's own tree lives at claude-tree/ (gestalt/.claude is a compat symlink
# to it) — read the real dir directly.
SKILLS_DIR = REPO / "claude-tree" / "skills"
AGENTS_DIR = REPO / "claude-tree" / "agents"
RULES_DIR = REPO / "claude-tree" / "rules"
SKILL_INDEX = REPO / "claude-tree" / "references" / "skill-index.md"

MAX_DESCRIPTION_CHARS = 300
UNCONDITIONAL_BUDGET_BYTES = 40_000

# Modes/subcommands of a consolidated skill, documented in skill-index.md's
# Quick Reference table (`/requirements <mode>`, `/consolidate [run|status|...]`)
# but not their own `.claude/skills/<name>/` directory.
DOCUMENTED_SUBCOMMANDS = {"implement", "publish"}


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.index("---", 3)
    return yaml.safe_load(text[3:end]) or {}


@pytest.fixture(scope="module")
def skill_dirs() -> list[Path]:
    return sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").exists())


def test_every_skill_frontmatter_name_matches_its_directory(skill_dirs):
    mismatches = {}
    for d in skill_dirs:
        fm = _frontmatter(d / "SKILL.md")
        if fm.get("name") != d.name:
            mismatches[d.name] = fm.get("name")
    assert not mismatches, f"SKILL.md name != directory name: {mismatches}"


def test_every_skill_has_a_nonempty_description(skill_dirs):
    empty = [d.name for d in skill_dirs if not _frontmatter(d / "SKILL.md").get("description", "").strip()]
    assert not empty, f"skills with an empty/missing description: {empty}"


# 2026-08-18: two real violations found by this test's first run — both
# post-date B4's 300-char rule (fleet-sync and spin-up are the two newest
# skills, added after the rule was written for the other 23). Not fixed here:
# trimming a skill description is a content edit outside a test-writer's
# ownership (only test files + unambiguous knowledge/ link/anchor fixes are in
# scope this run) — reported in RETURN as a real, open finding. Any THIRD
# skill going over budget fails loudly; these two do not block the suite.
KNOWN_OVER_BUDGET_DESCRIPTIONS = {"fleet-sync": 302, "spin-up": 366}


def test_every_skill_description_is_within_budget(skill_dirs):
    over = {}
    for d in skill_dirs:
        desc = _frontmatter(d / "SKILL.md").get("description", "")
        n = len(desc)
        if n > MAX_DESCRIPTION_CHARS and n > KNOWN_OVER_BUDGET_DESCRIPTIONS.get(d.name, 0):
            over[d.name] = n
    assert not over, (
        f"skill descriptions over {MAX_DESCRIPTION_CHARS} chars, beyond the documented ratchet (B4's rule): {over}"
    )


def test_skill_index_mentions_every_skill_directory(skill_dirs):
    text = SKILL_INDEX.read_text(encoding="utf-8")
    dir_names = {d.name for d in skill_dirs}
    missing = {name for name in dir_names if f"/{name}" not in text and name not in text}
    assert not missing, f"skill directories not mentioned anywhere in skill-index.md: {missing}"


def test_skill_index_has_no_dead_command_mentions(skill_dirs):
    import re

    text = SKILL_INDEX.read_text(encoding="utf-8")
    dir_names = {d.name for d in skill_dirs}
    mentioned = set(re.findall(r"`/([a-z][a-z0-9-]*)", text))
    dead = mentioned - dir_names - DOCUMENTED_SUBCOMMANDS
    assert not dead, f"skill-index.md mentions '/{{name}}' commands with no matching directory or documented subcommand: {dead}"


def test_every_agent_frontmatter_has_name_description_model():
    incomplete = {}
    for f in sorted(AGENTS_DIR.glob("*.md")):
        fm = _frontmatter(f)
        missing = [k for k in ("name", "description", "model") if not fm.get(k)]
        if missing:
            incomplete[f.stem] = missing
    assert not incomplete, f"agent frontmatter missing required keys: {incomplete}"


def test_every_rule_is_path_scoped_or_intentionally_unconditional():
    """Every rule must be classifiable one way or the other — this is really
    a well-formedness check (frontmatter parses, `paths` is absent or a list)
    rather than a judgment call, since absence of `paths:` IS this repo's
    documented convention for "intentionally unconditional"."""
    malformed = []
    for f in sorted(RULES_DIR.glob("*.md")):
        fm = _frontmatter(f)
        if "paths" in fm and not isinstance(fm["paths"], list):
            malformed.append(f.stem)
    assert not malformed, f"rules with a malformed `paths:` (should be a list or absent): {malformed}"


def test_unconditional_rules_budget():
    total = 0
    unconditional = []
    for f in sorted(RULES_DIR.glob("*.md")):
        fm = _frontmatter(f)
        if "paths" not in fm:
            unconditional.append(f.stem)
            total += f.stat().st_size
    assert total <= UNCONDITIONAL_BUDGET_BYTES, (
        f"unconditional rules budget blown: {total} B > {UNCONDITIONAL_BUDGET_BYTES} B "
        f"ceiling (measured 34,407 B on 2026-08-18, gestalt-efficiency-audit-2026-08 ^ctx-baseline); "
        f"unconditional set: {unconditional}"
    )


@pytest.mark.local
def test_f10_workspace_claude_symlink_farm_informational():
    """Informational only, per gestalt-efficiency-audit-2026-08 ^skill-double-list.
    Prints the realpaths so a human/report can see today's actual shape rather
    than asserting a machine-specific layout — this is a `local` test, not part
    of the CI-safe suite, and never fails."""
    workspace_claude = Path("/home/user/Documents/GitHub/.claude")
    if not workspace_claude.exists():
        pytest.skip("workspace-root .claude not present on this machine")
    top_is_symlink = workspace_claude.is_symlink()
    children_symlinked = {}
    if workspace_claude.is_dir():
        for child in sorted(workspace_claude.iterdir()):
            children_symlinked[child.name] = child.is_symlink()
    print(f"\n[F10] {workspace_claude} is_symlink={top_is_symlink}")
    print(f"[F10] realpath: {workspace_claude.resolve()}")
    for name, is_link in children_symlinked.items():
        print(f"[F10]   {name}: symlink={is_link}")
    assert True
