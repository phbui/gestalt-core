"""bench_engine.py: exact dense search against sqlite-vec, the file-backed FTS5 index against the in-memory one, the encode
budgets, donor reuse and query-level resume. CPU only, stub encoders, no model."""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "tools"))
pytest.importorskip("sqlite_vec")
pytest.importorskip("torch")
import beir_bench as bb  # noqa: E402
import bench_engine as E  # noqa: E402
from resumable import BlockStore, FingerprintMismatch, QueryLog  # noqa: E402


class Killed(Exception):
    pass


def unit_store(tmp_path, n=5000, dim=32, seed=0, block_size=1000):
    """n random unit vectors where about a third are exact copies of others, so many distances tie."""
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((n, dim)).astype(np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    src = rng.integers(0, n, n // 3)
    X[rng.integers(0, n, n // 3)] = X[src]
    store = BlockStore(tmp_path / "emb", "x" * 64, block_size=block_size)
    store.encode_missing([""] * n, lambda texts, it=iter(range(0, n, block_size)): X[next(it):][:len(texts)])
    Q = rng.standard_normal((200, dim)).astype(np.float32)
    Q /= np.linalg.norm(Q, axis=1, keepdims=True)
    Q[:20] = X[rng.integers(0, n, 20)]  # queries equal to a document: distance zero
    return store, X, Q


@pytest.mark.parametrize("k", [10, 37])
def test_exact_dense_matches_sqlite_vec_ids_and_distances_including_ties(tmp_path, k):
    store, X, Q = unit_store(tmp_path)
    vec = E.VecDense(store, len(X), X.shape[1])
    ex = E.ExactDense(store, len(X), device="cpu")
    vi, vd = vec.topk(Q, k)
    ei, ed = ex.topk(Q, k)
    ties = 0
    for j in range(len(Q)):
        assert ei[j].tolist() == vi[j].tolist(), f"query {j}"
        np.testing.assert_allclose(ed[j], vd[j], atol=1e-5)
        ties += int((np.diff(vd[j]) == 0).sum())
    assert ties > 50  # the fixture really exercises ties


def test_exact_dense_with_a_tiny_memory_budget_gives_the_same_answer(tmp_path):
    store, X, Q = unit_store(tmp_path, n=1200, block_size=500)
    big = E.ExactDense(store, len(X), device="cpu").topk(Q, 15)
    small = E.ExactDense(store, len(X), device="cpu", budget_bytes=40_000).topk(Q, 15)
    assert [a.tolist() for a in big[0]] == [a.tolist() for a in small[0]]


def test_dense_mode_threshold_and_override(monkeypatch):
    monkeypatch.delenv("GESTALT_BENCH_DENSE", raising=False)
    assert E.dense_mode(E.DENSE_THRESHOLD) == "vec" and E.dense_mode(E.DENSE_THRESHOLD + 1) == "exact"
    monkeypatch.setenv("GESTALT_BENCH_DENSE", "exact")
    assert E.dense_mode(10) == "exact"
    monkeypatch.setenv("GESTALT_BENCH_DENSE", "nope")
    with pytest.raises(SystemExit):
        E.dense_mode(10)


VOCAB = [f"w{i}" for i in range(60)] + ["the", "of", "and", "a"]


def synthetic_docs(n=2000, seed=3):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        words = rng.choice(VOCAB, size=int(rng.integers(3, 25)), p=None).tolist()
        out.append((f"doc{i}", " ".join(words)))
    for i in range(0, n, 50):  # exact duplicates tie in BM25
        out[i + 1] = (out[i + 1][0], out[i][1])
    return out


class ZeroModel:
    def encode(self, texts, batch_size=64, convert_to_numpy=True, show_progress_bar=False):
        return np.zeros((len(texts), bb.ec.FULL_DIM), dtype="float32")


def test_fts_file_build_resumed_after_a_kill_equals_the_in_memory_index(tmp_path, monkeypatch):
    docs = synthetic_docs()
    ref = bb.build_db([d for d, _ in docs], [t for _, t in docs], ZeroModel(), 64)
    monkeypatch.setattr(E, "FTS_TXN_ROWS", 300)
    idx = E.FtsIndex(tmp_path / "corpus.sqlite")

    def die_after_two(done, total):
        if done >= 600:
            raise Killed

    with pytest.raises(Killed):
        idx.load_docs(iter(docs), progress=lambda d: die_after_two(d, 0) if d >= 900 else None)
    assert 600 <= idx.n_docs() < len(docs) and not idx.docs_complete()
    idx.load_docs(iter(docs))
    with pytest.raises(Killed):
        idx.build_fts(progress=die_after_two)
    idx.close()
    idx = E.FtsIndex(tmp_path / "corpus.sqlite")  # a new process
    assert idx.get("fts_rows") == 600
    idx.build_fts()
    rng = np.random.default_rng(9)
    for _ in range(200):
        q = " ".join(rng.choice(VOCAB, size=int(rng.integers(1, 6))).tolist())
        m = E.gestalt_rank.fts_match(q, False)
        want = [(r[0], r[1]) for r in ref.execute("SELECT rowid, rank FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT 100", (m,))]
        assert idx.search(m, 100) == want
    assert idx.doc_ids([5, 0]) == ["doc5", "doc0"] and idx.texts()[2:4] == [docs[2][1], docs[3][1]]


def test_plan_subbatches_follows_the_token_and_attention_budgets():
    lengths = [10, 2000, 500, 500, 30, 1000]
    plan = E.plan_subbatches(lengths, max_batch=64, token_budget=16384, attn_budget=4_000_000)
    assert sorted(i for b in plan for i in b) == list(range(6))
    assert plan[0] == [1]  # L=2000: attention allows one text
    for b in plan:
        L = max(lengths[i] for i in b)
        assert len(b) <= max(1, min(64, 16384 // L, 4_000_000 // (L * L)))
    assert [lengths[i] for b in plan for i in b] == sorted(lengths, reverse=True)


class BagModel:
    """crc32 bag of words. Records the size of every encode call. No tokenizer, so lengths are estimated."""

    def __init__(self):
        self.batches = []

    def encode(self, texts, batch_size=64, convert_to_numpy=True, show_progress_bar=False):
        one = isinstance(texts, str)
        texts = [texts] if one else texts
        self.batches.append(len(texts))
        out = np.zeros((len(texts), 16), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in t.split():
                out[i, zlib.crc32(w.encode()) % 16] += 1.0
        return out[0] if one else out


def test_encode_documents_keeps_input_order_and_equals_one_unbatched_call(monkeypatch):
    monkeypatch.setenv("GESTALT_BENCH_TOKEN_BUDGET", "40")
    monkeypatch.setenv("GESTALT_BENCH_ATTN_BUDGET", "400")
    texts = [" ".join(["w"] * n) + f" id{n}" for n in (3, 30, 1, 12, 7, 25, 2)]
    m = BagModel()
    got = E.encode_documents(m, texts, max_batch=64, doc_prefix="p: ", postprocess=lambda a: a)
    want = BagModel().encode(["p: " + t for t in texts])
    np.testing.assert_array_equal(got, want)
    assert len(m.batches) > 1 and max(m.batches) < len(texts)  # the budgets split the call
    assert E.budgets() == {"token_budget": 40, "attn_budget": 400}


def test_token_lengths_use_the_model_tokenizer_when_there_is_one():
    class Tok:
        def __call__(self, texts, add_special_tokens=True, truncation=False, max_length=None):
            return {"input_ids": [[0] * min(len(t), max_length or 10**9) for t in texts]}

    class M:
        tokenizer = Tok()
        max_seq_length = 5

    assert E.token_lengths(M(), ["abc", "abcdefgh"]) == [3, 5]
    assert E.token_lengths(object(), ["one two three four five"]) == [7]


def test_select_capped_takes_a_doc_id_function():
    rows = [{"id": i} for i in range(100)]
    kept = E.select_capped(rows, {"90"}, 5, doc_id=lambda r: str(r["id"]))
    assert [r["id"] for r in kept] == [0, 1, 2, 3, 90]
    assert [r["id"] for r in E.select_capped(rows, set(), 3, doc_id=lambda r: str(r["id"]))] == [0, 1, 2]


class FakeEc:
    MODEL_NAME, MODEL_REVISION, PROFILE, EMBED_DIM, DOC_PREFIX, QUERY_PREFIX = "stub", "r1", "nomic", 16, "d: ", "q: "

    @staticmethod
    def postprocess(a):
        return a


def _prepare(tmp_path, name, docs, model, donors=()):
    return E.prepare_corpus(tmp_path / name, name, lambda: iter(docs), model=model, ec=FakeEc, batch_size=8, block_size=7,
                            rebuild=False, status=E.Status(None, "test", every_s=1e9), donors=[tmp_path / d for d in donors])


def test_a_corpus_with_the_same_documents_reuses_the_donor_vectors(tmp_path):
    docs_a = [(f"d{i}", f"text {i} shared") for i in range(30)]
    docs_b = docs_a[:10] + [("new1", "brand new text")] + docs_a[10:25] + [("d26", "changed text")]
    m = BagModel()
    _prepare(tmp_path, "fever", docs_a, m)
    m2 = BagModel()
    fts_b, store_b = _prepare(tmp_path, "climate", docs_b, m2, donors=["fever"])
    assert sum(m2.batches) == 2  # only the new document and the one whose text changed
    got = np.concatenate([np.asarray(b) for _, b in store_b.iter_blocks(len(docs_b))])
    np.testing.assert_array_equal(got, BagModel().encode(["d: " + t for _, t in docs_b]))


def test_a_rerun_embeds_nothing_and_a_new_model_is_refused(tmp_path):
    docs = [(f"d{i}", f"text {i}") for i in range(20)]
    _prepare(tmp_path, "c", docs, BagModel())
    m = BagModel()
    _prepare(tmp_path, "c", docs, m)
    assert m.batches == []

    class OtherEc(FakeEc):
        MODEL_NAME = "other"

    with pytest.raises(FingerprintMismatch):
        E.prepare_corpus(tmp_path / "c", "c", lambda: iter(docs), model=m, ec=OtherEc, batch_size=8, block_size=7,
                         rebuild=False, status=E.Status(None, "t", every_s=1e9))


def test_fuse_pools_rrf_and_convex():
    fts = [(1, -5.0), (2, -3.0)]
    vec = [(2, 0.1), (3, 0.5)]
    assert [i for i, _ in E.fuse_pools(fts, vec, "rrf", 0.5)] == [2, 1, 3]
    assert [i for i, _ in E.fuse_pools(fts, vec, "convex", 0.5)] == list(E.gestalt_rank.convex_fuse(fts, vec, 0.5))


def _engine(tmp_path, docs):
    model = BagModel()
    fts, store = _prepare(tmp_path, "c", docs, model)
    return fts, E.VecDense(store, fts.n_docs(), 16), lambda texts: E.encode_queries(model, texts, "q: ", lambda a: a)


DOCS = [(f"d{i}", f"alpha{i % 9} beta{i % 4} gamma") for i in range(60)]
QUERIES = [(f"q{i}", f"alpha{i} beta{i % 4}") for i in range(9)]


def test_a_query_whose_rerank_fell_back_is_run_again_and_the_rest_are_skipped(tmp_path, monkeypatch):
    fts, dense, embed = _engine(tmp_path, DOCS)
    calls = []
    gpu_ok = [False]

    def flaky(query, rows, alias):
        calls.append(query)
        fb = "AcceleratorError" if query == "alpha3 beta3" and not gpu_ok[0] else "none"
        return list(reversed(rows)), None if fb != "none" else list(range(len(rows))), {"fallback": fb}

    monkeypatch.setattr(E.gestalt_rank, "rerank_rows", flaky)
    rerank = {"model": "bge", "depth": 5}
    systems = ["bm25", "dense", "hybrid", "hybrid_rerank"]
    hdr = E.query_log_header(fingerprint="f", systems=systems, limit=3, rerank=rerank, fusion="rrf", alpha=0.5, fill_from_hybrid=False)
    with QueryLog(tmp_path / "q.jsonl", hdr) as log:
        E.retrieve(QUERIES, fts=fts, dense=dense, embed=embed, log=log, systems=systems, limit=3, rerank=rerank)
        assert E.runs_from_log(log, systems, [q for q, _ in QUERIES])[1] == 1
    calls.clear()
    gpu_ok[0] = True
    with QueryLog(tmp_path / "q.jsonl", hdr) as log:
        E.retrieve(QUERIES, fts=fts, dense=dense, embed=embed, log=log, systems=systems, limit=3, rerank=rerank)
        assert calls == ["alpha3 beta3"]  # only the fallen-back query ran again
        assert E.runs_from_log(log, systems, [q for q, _ in QUERIES])[1] == 0
    off = E.query_log_header(fingerprint="f", systems=systems[:3], limit=3, rerank=None, fusion="rrf", alpha=0.5, fill_from_hybrid=False)
    with pytest.raises(FingerprintMismatch, match="rerank"):
        QueryLog(tmp_path / "q.jsonl", off)


def test_fill_from_hybrid_keeps_the_whole_reranked_head(tmp_path, monkeypatch):
    fts, dense, embed = _engine(tmp_path, DOCS)
    monkeypatch.setattr(E.gestalt_rank, "rerank_rows", lambda q, rows, alias: (list(reversed(rows)), list(range(len(rows))), {"fallback": "none"}))
    systems = ["hybrid", "hybrid_rerank"]
    rerank = {"model": "bge", "depth": 15}
    hdr = E.query_log_header(fingerprint="f", systems=systems, limit=25, rerank=rerank, fusion="rrf", alpha=0.5, fill_from_hybrid=True)
    with QueryLog(tmp_path / "q.jsonl", hdr) as log:
        E.retrieve(QUERIES[:2], fts=fts, dense=dense, embed=embed, log=log, systems=systems, limit=25, rerank=rerank, fill_from_hybrid=True)
        runs, _ = E.runs_from_log(log, systems, ["q0"])
    head = runs["hybrid_rerank"]["q0"]
    assert len(head) == 25 and len(set(head)) == 25
    hyb = runs["hybrid"]["q0"]
    assert head[15:] == [d for d in hyb if d not in head[:15]][:10]


# --- QAL-007 and QAL-010: the documentation says what the code does ------------------------------------------------------------------------


def test_exactdense_topk_and_block_valid_have_docstrings():
    from resumable import BlockStore

    assert "sqlite-vec's tie order" in (E.ExactDense.topk.__doc__ or "")
    assert "receipt" in (BlockStore.block_valid.__doc__ or "")


def test_the_module_header_carries_no_dated_measurement_and_names_fuse_pools_as_its_own_fusion():
    import re

    head = E.__doc__
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", head)
    assert "not a call into gestalt_rank.hybrid_search" in head


def test_cap_gpu_memory_passes_fraction_and_rejects_bad_values(monkeypatch):
    import types
    calls = []
    fake = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True, set_per_process_memory_fraction=calls.append))
    monkeypatch.setitem(sys.modules, "torch", fake)
    monkeypatch.delenv("GESTALT_BENCH_VRAM_FRACTION", raising=False)
    assert E.cap_gpu_memory() is None and calls == []
    monkeypatch.setenv("GESTALT_BENCH_VRAM_FRACTION", "0.6")
    assert E.cap_gpu_memory() == 0.6 and calls == [0.6]
    for bad in ("0", "1.5", "-1", "abc", "nan"):
        monkeypatch.setenv("GESTALT_BENCH_VRAM_FRACTION", bad)
        with pytest.raises(SystemExit, match="GESTALT_BENCH_VRAM_FRACTION"):
            E.cap_gpu_memory()
    assert calls == [0.6]


def test_a_reranker_alias_downgraded_for_vram_counts_as_a_fallback(tmp_path, monkeypatch):
    """2026-10-09: resolve_alias may swap qwen3-4b for bge when VRAM is short. A run scored under another model than it reports is wrong, so the swap voids it."""
    fts, dense, embed = _engine(tmp_path, DOCS)

    def downgraded(query, rows, alias):
        return list(rows), list(range(len(rows))), {"fallback": "none", "model": "bge"}  # the reranker loaded bge, not the requested model

    monkeypatch.setattr(E.gestalt_rank, "rerank_rows", downgraded)
    monkeypatch.setattr(E.gestalt_rank, "resolve_alias", lambda a=None: "bge")
    rerank = {"model": "qwen3-4b", "depth": 5}
    systems = ["bm25", "dense", "hybrid", "hybrid_rerank"]
    hdr = E.query_log_header(fingerprint="f", systems=systems, limit=3, rerank=rerank, fusion="rrf", alpha=0.5, fill_from_hybrid=False)
    assert hdr["rerank"]["resolved_model"] == "bge" and hdr["rerank"]["model"] == "qwen3-4b"
    with QueryLog(tmp_path / "q.jsonl", hdr) as log:
        E.retrieve(QUERIES, fts=fts, dense=dense, embed=embed, log=log, systems=systems, limit=3, rerank=rerank)
        assert E.runs_from_log(log, systems, [q for q, _ in QUERIES])[1] == len(QUERIES)  # every query is a fallback, so the run is void


def test_fts_counts_a_match_the_tokenizer_refuses_and_raises_any_other_database_error(tmp_path, capsys):
    import sqlite3

    fts, _ = _prepare(tmp_path, "c", DOCS, BagModel())
    assert fts.match_failures == 0
    assert fts.search('"unterminated', 5) == []  # FTS5 syntax error: scored as no lexical hits
    assert fts.match_failures == 1 and "FTS MATCH failed" in capsys.readouterr().err

    class Locked:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    real = fts.db
    fts.db = Locked()
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            fts.search("alpha1", 5)
    finally:
        fts.db = real
    assert fts.match_failures == 1
