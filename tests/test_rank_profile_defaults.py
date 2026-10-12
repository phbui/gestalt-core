"""Profile knob defaults, the reranker blend and dense-score abstention in tools/gestalt_rank.py (2026-10-10)."""
from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import gestalt_rank as gr


STELLA_KNOBS = ("GESTALT_RERANK", "GESTALT_FUSION", "GESTALT_FUSION_ALPHA", "GESTALT_FUSION_NORM", "GESTALT_RERANK_MODEL",
                "GESTALT_RERANK_BLEND")


def test_stella_profile_drops_bm25_and_blends_the_reranker_and_an_env_var_still_wins(monkeypatch):
    for k in STELLA_KNOBS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GESTALT_EMBED_PROFILE", "stella-400m")
    assert gr.rerank_mode() == "auto" and gr.rerank_alias() == "qwen3-0.6b" and gr.rerank_blend() == 0.5
    assert gr.fusion_mode() == "convex" and gr.fusion_alpha() == 0.0 and gr.fusion_norm() == "tmin"
    monkeypatch.setenv("GESTALT_RERANK_BLEND", "0")
    monkeypatch.setenv("GESTALT_FUSION", "rrf")
    assert gr.rerank_blend() == 0.0 and gr.fusion_mode() == "rrf"


def test_stella_fusion_keeps_the_dense_order_and_puts_lexical_only_hits_last(monkeypatch):
    monkeypatch.setenv("GESTALT_EMBED_PROFILE", "stella-400m")
    for k in STELLA_KNOBS:
        monkeypatch.delenv(k, raising=False)
    fts = [(9, -12.0), (3, -8.0)]  # 9 is lexical only
    vec = [(1, 0.40), (2, 0.55), (3, 0.90)]  # L2 distances of unit vectors, best first
    scores = gr.fuse(fts, vec)
    assert list(scores)[:3] == [1, 2, 3] and list(scores)[-1] == 9
    assert scores[3] > 0 == scores[9]


def test_nomic_profile_keeps_the_module_defaults(monkeypatch):
    monkeypatch.delenv("GESTALT_RERANK", raising=False)
    monkeypatch.setenv("GESTALT_EMBED_PROFILE", "nomic")
    assert gr.profile_knob_defaults() == {} and gr.rerank_mode() == "auto"
    assert gr.profile_knob_defaults("stella-400m")["GESTALT_RERANK_BLEND"] == "0.5"


def test_blend_zero_keeps_the_reranker_order_and_half_lets_the_fused_order_vote():
    ids = ["a", "b", "c"]
    rr = {"a": 0.9, "b": 0.8, "c": 0.1}
    fused = {"a": 0.01, "b": 0.05, "c": 0.04}
    assert [i for i, _ in gr.blend_scores(ids, rr, fused, 0.0)] == ["a", "b", "c"]
    assert [i for i, _ in gr.blend_scores(ids, rr, fused, 0.5)] == ["b", "a", "c"]
    assert gr.blend_scores([], rr, fused, 0.5) == []
    assert [i for i, _ in gr.blend_scores(["x", "y"], {"x": 1, "y": 1}, {"x": 1, "y": 1}, 0.3)] == ["x", "y"]


def test_blend_rejects_a_bad_value(monkeypatch):
    monkeypatch.setenv("GESTALT_RERANK_BLEND", "1.5")
    with pytest.raises(ValueError, match="GESTALT_RERANK_BLEND"):
        gr.rerank_blend()


def _db(vectors: dict[int, list[float]], texts: dict[int, str]):
    """A stand-in for the index: FTS5 and a dense table queried the way hybrid_search queries sqlite-vec."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE VIRTUAL TABLE sections_fts USING fts5(content)")
    db.execute("CREATE TABLE sections_meta (id INTEGER PRIMARY KEY, slug TEXT, title TEXT, heading TEXT, content TEXT, block_id TEXT, anchors TEXT)")
    for i, t in texts.items():
        db.execute("INSERT INTO sections_fts(rowid, content) VALUES (?, ?)", (i, t))
        db.execute("INSERT INTO sections_meta VALUES (?, ?, ?, ?, ?, ?, ?)", (i, f"s{i}", f"t{i}", "", t, None, ""))
    return db


class _VecDB:
    """Wraps the sqlite stand-in and answers the sections_vec query with exact L2 distances to the query vector."""

    def __init__(self, db, vectors):
        self.db, self.vectors = db, vectors

    def execute(self, sql, params=()):
        if "sections_vec" in sql:
            q = struct.unpack(f"{len(params[0]) // 4}f", params[0])
            rows = sorted(((i, sum((a - b) ** 2 for a, b in zip(q, v)) ** 0.5) for i, v in self.vectors.items()), key=lambda r: r[1])[: params[1]]
            return _Rows([{"id": i, "distance": d} for i, d in rows])
        return self.db.execute(sql, params)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


def test_dense_abstention_reports_the_top_cosine_and_abstains_only_below_an_explicit_floor(monkeypatch):
    for k in ("GESTALT_RERANK", "GESTALT_ABSTAIN_DENSE_MIN", "GESTALT_EMBED_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GESTALT_RERANK", "off")
    monkeypatch.setenv("GESTALT_ABSTAIN", "on")
    monkeypatch.setenv("GESTALT_EMBED_NORMALIZE", "1")  # the fixture's vectors are unit length; the cosine is defined only then (2026-10-11)
    vecs = {1: [1.0, 0.0], 2: [0.0, 1.0]}
    db = _VecDB(_db(vecs, {1: "alpha note", 2: "beta note"}), vecs)
    q = [0.6, 0.8]  # unit vector; cosine 0.6 to row 1, 0.8 to row 2

    def embed(_s):
        return struct.pack("2f", *q)

    r = gr.hybrid_search(db, "beta", 2, embed_query=embed)
    assert r.confidence_source == "dense" and r.confidence == pytest.approx(0.8, abs=1e-6) and r.abstain is False
    monkeypatch.setenv("GESTALT_ABSTAIN_DENSE_MIN", "0.9")
    r = gr.hybrid_search(db, "beta", 2, embed_query=embed)
    assert r.abstain is True and r.dense_top == pytest.approx(0.8, abs=1e-6)


def test_a_loaded_reranker_is_never_swapped_by_a_later_vram_check(monkeypatch):
    """2026-10-10: the qwen3-4b runs of 2026-10-09 swapped to bge ALIAS_TTL_S into the run, because the 4B's own weights filled the card."""
    monkeypatch.setattr(gr, "_alias_memo", {})
    monkeypatch.setattr(gr, "_resolve_alias_uncached", lambda a: "bge")  # what a free-VRAM check says with the 4B resident
    monkeypatch.setattr(gr, "_rerankers", {"qwen3-4b": object()})
    assert gr.resolve_alias("qwen3-4b") == "qwen3-4b"
    monkeypatch.setattr(gr, "_rerankers", {})
    assert gr.resolve_alias("qwen3-4b") == "bge"


def test_a_downgraded_alias_reranks_but_is_flagged_so_a_benchmark_voids_it(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals" / "retrieval"))
    from bench_engine import rerank_failed

    class Fake:
        def predict(self, pairs, batch_size=8, **kw):
            return [float(len(b)) for _, b in pairs]

    monkeypatch.setattr(gr, "resolve_alias", lambda a=None: "bge")
    monkeypatch.setattr(gr, "get_reranker", lambda alias=None: Fake())
    rows = [{"id": 1, "slug": "a", "title": "a", "heading": "", "content": "x"}, {"id": 2, "slug": "b", "title": "b", "heading": "", "content": "xxxx"}]
    ranked, scores, info = gr.rerank_rows("q", rows, "qwen3-4b")
    assert scores is not None and [r["id"] for r in ranked] == [2, 1]
    assert info["requested"] == "qwen3-4b" and info["model"] == "bge" and info["fallback"] == "downgraded-to-bge"
    assert rerank_failed(info)


def test_every_stella_knob_yields_to_its_env_var(monkeypatch):
    """2026-10-11 audit: the override test covered two knobs. A profile default that ignored the other three would have passed."""
    monkeypatch.setenv("GESTALT_EMBED_PROFILE", "stella-400m")
    for k in STELLA_KNOBS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GESTALT_FUSION_ALPHA", "0.3")
    monkeypatch.setenv("GESTALT_FUSION_NORM", "minmax")
    monkeypatch.setenv("GESTALT_RERANK_MODEL", "bge")
    assert gr.fusion_alpha() == 0.3 and gr.fusion_norm() == "minmax" and gr.rerank_alias() == "bge"


def test_an_exported_but_empty_knob_falls_through_to_the_profile_default(monkeypatch):
    """`export GESTALT_FUSION_ALPHA=` in a shell wrapper used to beat the profile and then land on the module default (alpha 0.5, blend 0,
    bge), so the stella profile silently ran as the nomic shape. Empty means unset."""
    monkeypatch.setenv("GESTALT_EMBED_PROFILE", "stella-400m")
    for k in STELLA_KNOBS:
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("GESTALT_RERANK", "")
    assert gr.fusion_alpha() == 0.0 and gr.fusion_norm() == "tmin" and gr.fusion_mode() == "convex"
    assert gr.rerank_alias() == "qwen3-0.6b" and gr.rerank_blend() == 0.5 and gr.rerank_mode() == "auto"


def test_a_blend_over_an_all_but_equal_inner_leg_keeps_the_reranker_order():
    """Scores equal in exact arithmetic but apart by one ulp gave a standard deviation near 1e-17, and the z-scores became ±1 noise
    mixed in at full weight. Such a leg carries no information, so its z-scores must be zero."""
    ids = [1, 2, 3, 4]
    rr = {1: 0.1, 2: 0.9, 3: 0.5, 4: 0.7}
    inner = {1: 0.3, 2: 0.3 + 1e-16, 3: 0.3 - 1e-16, 4: 0.3}
    order = [i for i, _ in gr.blend_scores(ids, rr, inner, 0.5)]
    assert order == [2, 4, 3, 1]
