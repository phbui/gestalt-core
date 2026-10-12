"""Fixes from the 2026-10-11 line review of tools/gestalt_rank.py. Each test failed before its fix."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import gestalt_rank as gr  # noqa: E402
from test_wikilink_expand import QUERY_VEC, SECTIONS, db  # noqa: E402,F401


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("GESTALT_RERANK_MODEL", "GESTALT_RERANK_ALLOW_ANY", "GESTALT_LINK_EXPAND", "GESTALT_LINK_EXPAND_K", "GESTALT_EMBED_PROFILE",
              "GESTALT_EMBED_NORMALIZE", "GESTALT_ABSTAIN", "GESTALT_FUSION", "GESTALT_SLUG_DECAY", "GESTALT_RERANK_BLEND"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GESTALT_RERANK", "off")
    monkeypatch.setenv("GESTALT_RERANK_DEPTH", "4")
    monkeypatch.setattr(gr, "_warned", set())


def test_a_hub_id_names_the_same_reranker_as_its_alias(monkeypatch):
    """`GESTALT_RERANK_MODEL=Qwen/Qwen3-Reranker-0.6B` loaded fine but skipped every `alias.startswith("qwen3")` test: no instruction
    prompt, no VRAM check for the 4B, and a second copy of a model already resident under its short alias."""
    assert gr.canonical_alias("Qwen/Qwen3-Reranker-0.6B") == "qwen3-0.6b"
    assert gr.canonical_alias("qwen3-4b") == "qwen3-4b" and gr.canonical_alias("some/unknown") == "some/unknown"
    monkeypatch.setenv("GESTALT_RERANK_MODEL", "Qwen/Qwen3-Reranker-4B")
    assert gr.rerank_alias() == "qwen3-4b"


def test_allow_any_accepts_true_in_any_case(monkeypatch):
    monkeypatch.setenv("GESTALT_RERANK_ALLOW_ANY", "True")
    assert gr.pinned_model("org/model") == ("org/model", None)


def test_a_bool_profile_default_reads_as_on_or_off(monkeypatch):
    monkeypatch.setattr(gr, "_PROFILE_DEFAULTS", {"demo": {"GESTALT_LINK_EXPAND": True}})
    monkeypatch.setenv("GESTALT_EMBED_PROFILE", "demo")
    assert gr.link_expand_on() is True


def test_config_records_every_knob_that_changes_a_result():
    c = gr.config()
    for k in ("rerank_blend", "link_expand_k", "link_expand_max", "rescue", "rerank_dtype", "tf32", "attn_impl"):
        assert k in c, k


def test_link_expansion_is_skipped_without_a_reranker(db, monkeypatch):
    """With the reranker off the appended rows sit below the cut by construction (golden S4 equalled S0 on 2026-10-11), so the
    expansion queries ran for nothing and `expanded` reported rows the caller never saw."""
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    calls = []
    real = gr.expand_links
    monkeypatch.setattr(gr, "expand_links", lambda *a, **k: calls.append(1) or real(*a, **k))
    r = gr.hybrid_search(db, "quokka", 1, embed_query=lambda q: QUERY_VEC, rerank=False, full=True)
    assert r.expanded == 0 and calls == [] and len(r.rows) == 1


def test_an_incoming_link_with_spaces_inside_the_brackets_is_found(db):
    db.execute("INSERT INTO sections_meta (id, slug, heading, block_id, content, file_path, content_hash, anchors) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (50, "h-note", "Eta", None, "Eta points at [[ a-note ]] with spaces.", "knowledge/h-note.md", "h50", ""))
    metas = [dict(r) for r in db.execute("SELECT * FROM sections_meta WHERE slug = 'a-note'")]
    got = {r["slug"] for r in gr.expand_links(db, metas, {m["id"]: 1.0 for m in metas}, 1)}
    assert "h-note" in got and "c-note" in got


def test_the_top_score_is_the_rerankers_best_even_after_a_blend(db, monkeypatch):
    """With GESTALT_RERANK_BLEND on, row 0 is the blended winner, which need not be the reranker's favourite. Abstention asks whether
    the reranker found an answer, so its signal is the reranker's top score."""
    monkeypatch.setenv("GESTALT_RERANK", "on")
    monkeypatch.setenv("GESTALT_RERANK_BLEND", "0.9")
    monkeypatch.setenv("GESTALT_ABSTAIN", "on")

    class Flip:  # the reranker's favourite is the fused leg's last row
        def predict(self, pairs, **_kw):
            return [1.0 + i for i in range(len(pairs))]

    monkeypatch.setattr(gr, "get_reranker", lambda alias=None: Flip())
    r = gr.hybrid_search(db, "quokka", 4, embed_query=lambda q: QUERY_VEC, rerank=True, full=True)
    assert r.blend == 0.9 and r.rerank_scores
    assert r.top_score == max(r.rerank_scores.values())
    assert r.confidence_source == "rerank"


def test_no_abstention_signal_from_a_downgraded_reranker(db, monkeypatch):
    monkeypatch.setenv("GESTALT_RERANK", "on")
    monkeypatch.setenv("GESTALT_ABSTAIN", "on")
    monkeypatch.setenv("GESTALT_RERANK_MODEL", "qwen3-4b")
    monkeypatch.setattr(gr, "resolve_alias", lambda alias=None: "bge")

    class Flat:
        def predict(self, pairs, **_kw):
            return [1.0] * len(pairs)

    monkeypatch.setattr(gr, "get_reranker", lambda alias=None: Flat())
    r = gr.hybrid_search(db, "quokka", 2, embed_query=lambda q: QUERY_VEC, rerank=True, full=True)
    assert r.rerank["fallback"] == "downgraded-to-bge" and r.confidence is None and r.abstain is False


def test_the_dense_cosine_is_reported_only_for_a_unit_vector_profile(db, monkeypatch):
    """1 - d*d/2 is the cosine only for unit vectors. nomic ships unnormalised, so under it the number was not a cosine and the
    abstention floor meant nothing."""
    monkeypatch.setenv("GESTALT_EMBED_NORMALIZE", "0")
    r = gr.hybrid_search(db, "quokka", 1, embed_query=lambda q: QUERY_VEC, rerank=False, full=True)
    assert r.dense_top is None
    monkeypatch.setenv("GESTALT_EMBED_NORMALIZE", "1")
    r = gr.hybrid_search(db, "quokka", 1, embed_query=lambda q: QUERY_VEC, rerank=False, full=True)
    assert r.dense_top is not None
    monkeypatch.delenv("GESTALT_EMBED_NORMALIZE")
    assert gr.dense_is_unit("stella-400m") is True and gr.dense_is_unit("nomic") is False


def test_a_reranker_that_fails_to_load_keeps_the_order_marks_the_fallback_and_swaps_nothing_in(monkeypatch):
    """The review found no direct test: a mutant that answered a load failure with another model's order would have passed."""
    rows = [{"id": 1, "slug": "a", "heading": "", "content": "x"}, {"id": 2, "slug": "b", "heading": "", "content": "y"},
            {"id": 3, "slug": "c", "heading": "", "content": "z"}]
    monkeypatch.setattr(gr, "resolve_alias", lambda alias=None: alias or "bge")
    loads = []

    def boom(alias=None):
        loads.append(alias)
        raise RuntimeError("weights offline")
    monkeypatch.setattr(gr, "get_reranker", boom)
    monkeypatch.setattr(gr, "_rerankers", {})
    out, scores, info = gr.rerank_rows("q", rows, "qwen3-0.6b")
    assert [r["id"] for r in out] == [1, 2, 3] and scores is None
    assert info["fallback"] == "RuntimeError" and info["model"] == "qwen3-0.6b" and info["requested"] == "qwen3-0.6b"
    assert loads == ["qwen3-0.6b"] and gr._rerankers == {}
