"""Slug-level dedup after fusion (spec 2, 2026-10-08). Hermetic: no model, no network."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import gestalt_rank  # noqa: E402
from conftest import _load  # noqa: E402
from test_harness_fidelity import _DUP_SECTIONS, _build_fixture_db  # noqa: E402


def _rows(*slugs):
    return [{"slug": s, "n": i} for i, s in enumerate(slugs)]


def _slugs(rows):
    return [r["slug"] for r in rows]


def test_decay_one_returns_the_input_order():
    rows = _rows("a", "a", "b", "a", "c")
    assert gestalt_rank.slug_decay_select(rows, 4, 1.0) == rows[:4]


def test_decay_zero_gives_distinct_slugs_first_then_backfills():
    rows = _rows("a", "a", "a", "b", "c")
    assert _slugs(gestalt_rank.slug_decay_select(rows, 3, 0.0)) == ["a", "b", "c"]
    # Fewer distinct slugs than the limit: the skipped chunks fill the rest, in rank order.
    got = gestalt_rank.slug_decay_select(_rows("a", "a", "b", "a"), 4, 0.0)
    assert _slugs(got) == ["a", "b", "a", "a"] and [r["n"] for r in got] == [0, 2, 1, 3]


def test_half_decay_demotes_a_repeat_but_keeps_it_when_nothing_else_is_left():
    rows = _rows("a", "a", "b")
    assert _slugs(gestalt_rank.slug_decay_select(rows, 3, 0.5)) == ["a", "b", "a"]
    assert _slugs(gestalt_rank.slug_decay_select(_rows("a", "a"), 2, 0.5)) == ["a", "a"]
    assert _slugs(gestalt_rank.slug_decay_select(rows, 2, 0.5)) == ["a", "b"]


def test_ties_go_to_the_earlier_rank():
    got = gestalt_rank.slug_decay_select(_rows("a", "a", "a", "b", "b"), 5, 0.0)
    assert [r["n"] for r in got] == [0, 3, 1, 2, 4]


def test_limit_larger_than_pool_and_empty_pool():
    assert gestalt_rank.slug_decay_select(_rows("a", "b"), 9, 0.5) == _rows("a", "b")
    assert gestalt_rank.slug_decay_select([], 3, 0.5) == []


def test_decay_stats_counts_distinct_slugs_and_displaced_rows():
    pool = _rows("a", "a", "a", "b")
    picked = gestalt_rank.slug_decay_select(pool, 3, 0.0)
    assert gestalt_rank.decay_stats(pool, picked, 3) == {"pool": 4, "slugs_distinct": 2, "demoted": 1}


@pytest.fixture
def server(tmp_path, monkeypatch):
    mod = _load("gms_slug_decay", "tools/gestalt-mcp-server.py")
    db = tmp_path / "gestalt.db"
    _build_fixture_db(db, _DUP_SECTIONS)
    monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setenv("GESTALT_HITS_LOG", "off")
    monkeypatch.delenv("GESTALT_SLUG_DECAY", raising=False)
    return mod


def test_fts_path_applies_the_decay(server, monkeypatch, capsys):
    q = "fusion vector search lists"
    plain = [r["slug"] for r in server.gestalt_search_fts(q, limit=3)]
    assert plain.count("retrieval-fusion") >= 2, "the fixture must put a repeated slug in the plain top 3"
    monkeypatch.setenv("GESTALT_SLUG_DECAY", "0")
    deduped = [r["slug"] for r in server.gestalt_search_fts(q, limit=3)]
    assert len(set(deduped)) == len(deduped) == 3
    assert "gestalt-mcp: dedup decay=0.0 pool=" in capsys.readouterr().err


def test_hybrid_path_applies_the_decay(server, monkeypatch):
    pytest.importorskip("sqlite_vec")
    from test_harness_fidelity import _StubModel
    monkeypatch.setenv("GESTALT_SEARCH_MODE", "hybrid")
    monkeypatch.setenv("GESTALT_RERANK", "off")
    monkeypatch.setattr(server, "get_model", lambda: _StubModel())
    monkeypatch.setenv("GESTALT_SLUG_DECAY", "0")
    rows = server.gestalt_search("fusion vector search lists", limit=3)
    assert len({r["slug"] for r in rows}) == 3
