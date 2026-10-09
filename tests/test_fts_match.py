"""FTS query cleanup (spec 4, 2026-10-08). Hermetic."""
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import gestalt_rank  # noqa: E402
from conftest import _load  # noqa: E402
from test_harness_fidelity import _DUP_SECTIONS, _build_fixture_db  # noqa: E402

fts_match = gestalt_rank.fts_match


def test_stopwords_drop_closed_class_words():
    assert fts_match("how do I deploy a new client site", stopwords=True) == '"deploy" OR "new" OR "client" OR "site"'


def test_no_word_characters_gives_none_with_the_filter_on_or_off():
    assert fts_match("???!!!", stopwords=True) is None
    assert fts_match("???!!!", stopwords=False) is None
    assert fts_match("", stopwords=False) is None


def test_filter_off_is_the_plain_token_list_with_case_and_repeats_kept():
    assert fts_match("Deploy deploy the Site", stopwords=False) == '"Deploy" OR "deploy" OR "the" OR "Site"'


def test_a_token_with_a_digit_is_never_dropped():
    assert fts_match("the 2026 plan of a v2", stopwords=True) == '"2026" OR "plan" OR "v2"'


def test_filter_that_would_leave_nothing_falls_back_to_the_tokens():
    assert fts_match("what is the", stopwords=True) == '"what" OR "is" OR "the"'


def test_filter_on_dedupes_case_blind_and_caps_at_32_tokens():
    assert fts_match("Cache cache CACHE", stopwords=True) == '"Cache"'
    q = " ".join(f"tok{i}x" for i in range(50))
    assert fts_match(q, stopwords=True).count('"') == 64


def test_the_knob_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv("GESTALT_FTS_STOPWORDS", raising=False)
    assert fts_match("how to deploy") == '"how" OR "to" OR "deploy"'
    monkeypatch.setenv("GESTALT_FTS_STOPWORDS", "on")
    assert fts_match("how to deploy") == '"deploy"'


def test_stopwords_hold_no_content_word_the_golden_queries_depend_on():
    for w in ("deploy", "site", "client", "fleet", "search", "index", "model", "memory", "new"):
        assert w not in gestalt_rank.STOPWORDS


def test_search_uses_the_filter(tmp_path, monkeypatch):
    mod = _load("gms_fts_match", "tools/gestalt-mcp-server.py")
    db = tmp_path / "gestalt.db"
    _build_fixture_db(db, _DUP_SECTIONS)
    monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setenv("GESTALT_HITS_LOG", "off")
    monkeypatch.delenv("GESTALT_FTS_STOPWORDS", raising=False)
    q = "the of and in to"  # nothing but stopwords: both paths fall back to the plain tokens
    assert mod.gestalt_search_fts(q, limit=3) is not None
    seen = []
    real = gestalt_rank.fts_match
    monkeypatch.setattr(gestalt_rank, "fts_match", lambda query, stopwords=None: seen.append(stopwords) or real(query, stopwords))
    mod.gestalt_search_fts("the tokenisation", limit=3)
    assert seen == [None], "the FTS path leaves the choice to the GESTALT_FTS_STOPWORDS knob"
    seen.clear()
    mod.gestalt_route("review the diff")
    assert seen == [False], "skill routing keeps its measured query and ignores the knob"
