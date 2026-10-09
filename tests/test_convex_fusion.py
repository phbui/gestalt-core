"""Convex fusion and the fusion tuner (spec 9c, 2026-10-08). Hermetic: stub model, synthetic pools."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import gestalt_rank  # noqa: E402
from conftest import _load  # noqa: E402
from test_harness_fidelity import _DUP_QUERIES, _DUP_SECTIONS, _StubModel, _build_fixture_db  # noqa: E402

np = pytest.importorskip("numpy")


def test_vec0_distance_is_l2_and_smaller_is_closer():
    """convex_fuse negates the distance on this reading. The tables are declared without distance_metric, so vec0 uses L2."""
    import sqlite3
    sv = pytest.importorskip("sqlite_vec")
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sv.load(db)
    db.execute("CREATE VIRTUAL TABLE v USING vec0(id INTEGER PRIMARY KEY, embedding FLOAT[3])")
    for i, vec in enumerate([[1, 0, 0], [0, 1, 0], [0.9, 0.1, 0]]):
        db.execute("INSERT INTO v(id, embedding) VALUES (?, ?)", (i, np.array(vec, dtype=np.float32).tobytes()))
    rows = db.execute("SELECT id, distance FROM v WHERE embedding MATCH ? AND k = 3 ORDER BY distance",
                      (np.array([1, 0, 0], dtype=np.float32).tobytes(),)).fetchall()
    assert [r[0] for r in rows] == [0, 2, 1] and rows[0][1] == 0.0 and rows[1][1] < rows[2][1]
    assert "distance_metric" not in (Path(__file__).resolve().parent.parent / "tools" / "gestalt-index-builder.py").read_text()


FTS = [(10, -9.0), (11, -5.0), (12, -1.0)]       # bm25 rank: more negative is better, so the order is 10, 11, 12
VEC = [(12, 0.1), (13, 0.5), (10, 0.9)]          # L2 distance: smaller is closer, so the order is 12, 13, 10


def _order(scores):
    return sorted(scores, key=scores.get, reverse=True)


def test_alpha_one_reproduces_the_lexical_order_and_alpha_zero_the_vector_order():
    assert _order(gestalt_rank.convex_fuse(FTS, VEC, 1.0))[:3] == [10, 11, 12]
    assert _order(gestalt_rank.convex_fuse(FTS, VEC, 0.0))[:3] == [12, 13, 10]


def test_missing_leg_scores_zero_and_the_blend_is_linear():
    s = gestalt_rank.convex_fuse(FTS, VEC, 0.5)
    assert s[11] == pytest.approx(0.5 * 0.5)           # lexical 0.5 after minmax, absent from the vector leg
    assert s[13] == pytest.approx(0.5 * 0.5)           # vector 0.5 after minmax, absent from the lexical leg
    assert s[10] == pytest.approx(0.5 * 1.0 + 0.5 * 0.0)
    assert s[12] == pytest.approx(0.5 * 0.0 + 0.5 * 1.0)


def test_single_leg_and_empty_pools():
    assert _order(gestalt_rank.convex_fuse(FTS, [], 0.5)) == [10, 11, 12]
    assert gestalt_rank.convex_fuse([], [], 0.5) == {}
    assert gestalt_rank.convex_fuse([(1, -3.0)], [], 1.0) == {1: 0.5}  # one distinct score: the degenerate 0.5, not full credit


def test_sign_matters_a_wrong_negation_would_reverse_the_order():
    # If the lexical rank were not negated, the worst match (-1.0 is "largest") would lead.
    assert _order(gestalt_rank.convex_fuse(FTS, [], 1.0))[0] == 10


@pytest.fixture
def servers(tmp_path, monkeypatch):
    db = tmp_path / "convex.db"
    _build_fixture_db(db, _DUP_SECTIONS)
    mod = _load("gms_convex", "tools/gestalt-mcp-server.py")
    monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(mod, "get_model", lambda: _StubModel())
    for k in ("GESTALT_RERANK_MODEL", "GESTALT_SLUG_DECAY", "GESTALT_FTS_STOPWORDS", "GESTALT_FUSION", "GESTALT_FUSION_ALPHA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GESTALT_RERANK", "off")
    monkeypatch.setenv("GESTALT_SEARCH_MODE", "hybrid")
    monkeypatch.setenv("GESTALT_HITS_LOG", "off")
    return mod, db


def test_server_alpha_one_leads_with_the_fts_order_and_alpha_zero_follows_the_vector_leg(servers, monkeypatch):
    mod, db = servers
    q = _DUP_QUERIES[0]
    fts_order = [(r["slug"], r["block_id"]) for r in mod.gestalt_search_fts(q, limit=6)]
    monkeypatch.setenv("GESTALT_FUSION", "convex")
    monkeypatch.setenv("GESTALT_FUSION_ALPHA", "1")
    hybrid = [(r["slug"], r["block_id"]) for r in mod.gestalt_search(q, limit=6)]
    assert hybrid[: len(fts_order)] == fts_order
    monkeypatch.setenv("GESTALT_FUSION_ALPHA", "0")
    conn = mod.get_db()
    vec = [r[0] for r in conn.execute("SELECT id FROM sections_vec WHERE embedding MATCH ? AND k = 12 ORDER BY distance",
                                      (mod._embed_query(_StubModel(), q).tobytes(),)).fetchall()]
    conn.close()
    meta = {s["id"]: (s["slug"], s["block_id"]) for s in _DUP_SECTIONS}
    got = [(r["slug"], r["block_id"]) for r in mod.gestalt_search(q, limit=6)]
    assert got == [meta[i] for i in vec][:6]


def test_rrf_stays_the_default(servers, monkeypatch):
    mod, _ = servers
    q = _DUP_QUERIES[1]
    default = mod.gestalt_search(q, limit=4)
    monkeypatch.setenv("GESTALT_FUSION", "rrf")
    assert mod.gestalt_search(q, limit=4) == default


# --- the tuner ---------------------------------------------------------------------------------------

tf = _load("tune_fusion_t", "evals/retrieval/tune_fusion.py")


def test_split_is_stable_and_by_target_cluster():
    members = [frozenset([f"entry-{i}"]) for i in range(200)]
    assert [tf.is_train(m) for m in members] == [tf.is_train(m) for m in members]
    assert 0.55 < sum(tf.is_train(m) for m in members) / 200 < 0.85
    dev = [m for m in members if tf.assign_splits.is_dev(m)]
    assert 0 < sum(tf.is_train(m) for m in dev) < len(dev), "the tuning split is independent of the dev/test split"


def test_families_keep_a_cluster_on_one_side():
    """EVAL-006: two targets linked by a family are one cluster, so a family never straddles train and held out."""
    c = _synthetic_pools(40)
    c["accept"] = np.array([f"s{i}|s{i + 1}" if i % 2 == 0 else f"s{i - 1}|s{i}" for i in range(40)])
    labels, members = tf.clusters_of(c)
    assert len(set(labels)) == 20 and all(labels[i] == labels[i + 1] for i in range(0, 40, 2))
    res = tf.tune(c, iterations=200)
    assert res["train_clusters"] + res["held_out_clusters"] == 20


def _synthetic_pools(n_clusters=40):
    """Per case: the gold chunk is first on the lexical leg and last on the vector leg, so a heavy lexical weight wins and RRF does not."""
    q, exp, acc, f_ids, f_sc, f_off, v_ids, v_dist, v_off, cand, cslug = [], [], [], [], [], [0], [], [], [0], [], []
    for i in range(n_clusters):
        gold, d1, d2 = 3 * i, 3 * i + 1, 3 * i + 2
        q.append(f"query {i}"); exp.append(f"s{i}"); acc.append(f"s{i}")
        f_ids += [gold, d1, d2]; f_sc += [-9.0, -4.0, -1.0]; f_off.append(len(f_ids))
        v_ids += [d1, d2, gold]; v_dist += [0.1, 0.2, 0.9]; v_off.append(len(v_ids))
        cand += [gold, d1, d2]; cslug += [f"s{i}", f"x{i}", f"y{i}"]
    return {"queries": np.array(q), "expect": np.array(exp), "accept": np.array(acc),
            "f_ids": np.array(f_ids), "f_sc": np.array(f_sc), "f_off": np.array(f_off),
            "v_ids": np.array(v_ids), "v_dist": np.array(v_dist), "v_off": np.array(v_off),
            "cand_ids": np.array(cand), "cand_slugs": np.array(cslug)}


def test_tune_picks_a_lexical_weight_on_train_and_reports_held_out_clusters():
    res = tf.tune(_synthetic_pools(), iterations=500)
    assert res["alpha_star"] >= 0.6
    assert res["train_n"] + res["held_out_n"] == 40 and res["held_out_n"] >= 5
    assert res["train_clusters"] == res["train_n"] and res["held_out_clusters"] == res["held_out_n"]
    assert res["held_out"]["alpha_star"]["mrr"] > res["held_out"]["rrf"]["mrr"]
    assert res["bootstrap_held_out"] is not None


def test_build_cache_round_trips_through_npz_at_both_served_pool_sizes(tmp_path, monkeypatch):
    mod = _load("rre_tune_build", "evals/retrieval/run_retrieval_evals.py")
    db_path = tmp_path / "t.db"
    _build_fixture_db(db_path, _DUP_SECTIONS)
    import sqlite3
    sv = pytest.importorskip("sqlite_vec")
    db = sqlite3.connect(str(db_path)); db.row_factory = sqlite3.Row; db.enable_load_extension(True); sv.load(db)
    cases = [{"query": "rank fusion vector search", "expect_slug": "retrieval-fusion"},
             {"query": "nothing here", "expect_none": True},
             {"query": "quoted terms tokenisation", "expect_slug": "fts-tokenisation"}]
    monkeypatch.delenv("GESTALT_RERANK_DEPTH", raising=False)
    pools = tf.served_pools()
    assert pools == (20, 40), "the server reads 2 x 10 per leg with the rerank off and the rerank depth 40 with it on"
    embed = lambda q: _StubModel().encode("search_query: " + q)
    out = tmp_path / "pools.npz"
    tf.save_caches(str(out), [tf.build_cache(db, cases, [], embed, mod.accept_set, p) for p in pools])
    assert tf.cache_pools(str(out)) == [20, 40]
    for p in pools:
        again = tf.load_cache(str(out), p)
        assert again["pool"] == p and len(again["queries"]) == 2, "an abstention case has no gold and is left out"
        assert list(again["expect"]) == ["retrieval-fusion", "fts-tokenisation"]
        assert tf.ranks_for(again, tf.rrf_order)[0] is not None
    with pytest.raises(SystemExit, match="not 30"):
        tf.load_cache(str(out), 30)
    legacy = tmp_path / "legacy.npz"
    np.savez(legacy, queries=np.array(["q"]))
    with pytest.raises(SystemExit, match="records no pool size"):
        tf.load_cache(str(legacy))


def test_tuning_reads_dev_cases_only_once_the_split_exists():
    cases = [{"query": "a", "expect_slug": "x", "split": "dev"}, {"query": "b", "expect_slug": "y", "split": "test"},
             {"query": "c", "expect_none": True, "split": "dev"}]
    assert [c["query"] for c in tf.tuning_cases(cases)] == ["a"]
    assert [c["query"] for c in tf.tuning_cases([{k: v for k, v in c.items() if k != "split"} for c in cases])] == ["a", "b"]
    with pytest.raises(SystemExit, match="carry no split"):
        tf.tuning_cases([cases[0], {"query": "d", "expect_slug": "z"}])


def test_rrf_order_is_the_shared_fusion():
    fts, vec = [(1, -3.0), (2, -2.0)], [(2, 0.1), (3, 0.2)]
    scores = gestalt_rank.rrf_fuse([[1, 2], [2, 3]])
    assert tf.rrf_order(fts, vec) == sorted(scores, key=scores.get, reverse=True) == [2, 1, 3]
