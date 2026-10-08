"""N11: auto-promote proposes and never edits knowledge/; auto-consolidate defaults off (2026-10-06)."""

import argparse
import re
from pathlib import Path

import pytest

from conftest import REPO, _load

FACT = "The deploy pipeline requires the gamma-service endpoint to stay on port 9000."


@pytest.fixture
def world(tmp_path, monkeypatch):
    ap = _load("auto_promote_under_test", "tools/auto-promote.py")
    gdir, state = tmp_path / "gestalt", tmp_path / "state"
    (gdir / "knowledge").mkdir(parents=True)
    state.mkdir()
    entry = gdir / "knowledge" / "gamma-service.md"
    entry.write_text("---\ntags: [gamma]\nupdated: 2026-01-01\n---\n\n# Gamma\n\nBody.\n", encoding="utf-8")
    (gdir / "MANIFEST.md").write_text("### gamma-service\nreference | Gamma service\n", encoding="utf-8")
    block_fact = {"fact": FACT, "source": "letta_block:project_context", "confidence": 0.6, "session_id": "letta"}
    monkeypatch.setattr(ap, "read_letta_facts", lambda *a, **k: [dict(block_fact)])
    monkeypatch.setattr(ap, "check_graphiti_staleness", lambda *a, **k: 0)
    for name in ("rebuild_indices", "feed_graphiti", "condense_letta", "update_entry"):
        monkeypatch.setattr(ap, name, lambda *a, **k: pytest.fail("must not be called in proposal mode"))
    args = argparse.Namespace(gestalt_dir=str(gdir), state_dir=str(state), letta_url="x", graphiti_url="y",
                              min_sessions=2, dry_run=False)
    return ap, args, gdir, state, entry


def test_proposes_and_leaves_knowledge_untouched(world):
    ap, args, gdir, state, entry = world
    before = (entry.read_bytes(), entry.stat().st_mtime_ns)
    stats = ap.run_promotion(args, ap.Log(state / "health.log"))
    assert (entry.read_bytes(), entry.stat().st_mtime_ns) == before
    assert sorted(p.name for p in (gdir / "knowledge").iterdir()) == ["gamma-service.md"]
    lines = [l for l in (state / ap.PROPOSALS_FILE).read_text().splitlines() if l.startswith("- ")]
    assert len(lines) == 1 and stats["promoted"] == 1
    assert re.match(r"- \d{4}-\d\d-\d\d \[proposal\] target \[\[gamma-service\]\] \| confidence 0.6 \| source letta_block:project_context \| ", lines[0])
    assert FACT in lines[0]


def test_second_run_does_not_duplicate(world):
    ap, args, _, state, _ = world
    ap.run_promotion(args, ap.Log(state / "health.log"))
    stats = ap.run_promotion(args, ap.Log(state / "health.log"))
    assert stats["promoted"] == 0
    assert (state / ap.PROPOSALS_FILE).read_text().count(FACT) == 1


def test_dry_run_writes_nothing(world):
    ap, args, gdir, state, entry = world
    args.dry_run = True
    ap.run_promotion(args, ap.Log(state / "health.log"))
    assert not (state / ap.PROPOSALS_FILE).exists()
    assert "updated: 2026-01-01" in entry.read_text()


def test_dry_run_flag_exists_on_cli():
    assert "--dry-run" in (REPO / "tools" / "auto-promote.py").read_text()


def test_hook_defaults_off():
    hook = (REPO / "claude-tree" / "hooks" / "gestalt-session-start.sh").read_text()
    assert 'AUTO_CONSOLIDATE="${GESTALT_AUTO_CONSOLIDATE:-false}"' in hook
