"""evals/zoo: the Searcher adapters, zoo_run, the cost helpers and the result schema.

No model loads and no dataset downloads. The hash encoder from conftest stands in for sentence-transformers and a fake ir_datasets serves a tiny corpus.
"""
from __future__ import annotations

import json
import sys
import time
from collections import namedtuple
from pathlib import Path

import pytest
from conftest import HashEncoder, make_fake_ir_datasets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
pytest.importorskip("sqlite_vec")
pytest.importorskip("yaml")
pytest.importorskip("jsonschema")
import evals.zoo  # noqa: E402,F401
import gestalt_rank  # noqa: E402
import pair_runs  # noqa: E402
from bench_engine import fuse_pools  # noqa: E402

from evals.zoo import cost, dense_hf, schema, zoo_run  # noqa: E402
from evals.zoo.fused import Fused  # noqa: E402

Doc = namedtuple("Doc", "doc_id title text")
Query = namedtuple("Query", "query_id text")
Qrel = namedtuple("Qrel", "query_id doc_id relevance")

COST_FIELDS = {"index_seconds", "docs_per_second", "query_latency_ms_p50", "query_latency_ms_p95", "latency_queries", "queries_per_second",
               "peak_vram_mib", "index_bytes", "parameters", "dtype", "gpu"}
YAML = """
systems:
  bm25: {adapter: bm25}
  hash: {adapter: dense_hf, args: {model: hash-test, block_size: 10, batch_size: 8, dtype: float32}}
  hash-bm25-rrf: {adapter: fused, args: {a: bm25, b: hash, fusion: rrf}}
  rr: {adapter: rerank, args: {inner: bm25, model: bge, depth: 5}}
"""


def fake_datasets():
    docs = [Doc(f"d{i}", f"title{i}", f"alpha{i} beta{i} gamma") for i in range(30)]
    queries = [Query("q1", "alpha25 beta25"), Query("q2", "alpha27 beta27"), Query("q3", "alpha28")]
    return make_fake_ir_datasets(docs, queries, [Qrel("q1", "d25", 1), Qrel("q2", "d27", 2), Qrel("q3", "d28", -1)])


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "ir_datasets", fake_datasets())
    enc = HashEncoder(dim=64)
    monkeypatch.setattr(dense_hf, "load_model", lambda *a, **k: enc)
    cfg = tmp_path / "zoo.yaml"
    cfg.write_text(YAML)
    return enc, cfg, tmp_path


def run(cfg, tmp_path, out, *systems):
    return zoo_run.main(["--dataset", "beir/scifact", "--systems", *systems, "--out", str(tmp_path / out), "--config", str(cfg),
                         "--work-dir", str(tmp_path / "work"), "--latency-queries", "2"])


def test_bm25_and_dense_run_validates_with_full_cost_block(env):
    _, cfg, tmp = env
    assert run(cfg, tmp, "out", "bm25", "hash") == 0
    path = tmp / "out" / "beir-scifact.json"
    assert schema.validate(path) == []
    r = json.loads(path.read_text())
    assert r["queries"] == 2 and r["queries_excluded_no_positive"] == 1 and r["documents"] == 30
    for name in ("bm25", "hash"):
        c = r["systems"][name]["cost"]
        assert set(c) == COST_FIELDS and c["index_seconds"] > 0 and c["index_bytes"] > 0 and c["latency_queries"] == 2
        assert c["query_latency_ms_p50"] <= c["query_latency_ms_p95"] and c["queries_per_second"] > 0
        assert (tmp / "out" / f"beir-scifact.{name}.run").read_text().startswith("q1 Q0 ")
    assert r["systems"]["hash"]["ndcg10"] > 0.5 and r["systems"]["bm25"]["ndcg10"] > 0.5
    assert (tmp / "out" / "summary.json").exists()


def test_fused_and_ad_hoc_fusion_systems_run(env):
    _, cfg, tmp = env
    assert run(cfg, tmp, "out", "bm25", "hash", "hash-bm25-rrf", "hash+bm25:convex@0.4") == 0
    r = json.loads((tmp / "out" / "beir-scifact.json").read_text())
    assert schema.validate(tmp / "out" / "beir-scifact.json") == []
    fused = r["systems"]["hash-bm25-rrf"]["cost"]
    leaf = r["systems"]["hash"]["cost"]
    assert fused["index_seconds"] >= leaf["index_seconds"] and fused["index_bytes"] >= leaf["index_bytes"]
    assert "hash-bm25-rrf_vs_hash" in r["tests"] and "hash+bm25:convex@0.4_vs_bm25" in r["tests"]


def test_pair_runs_accepts_two_zoo_results(env, capsys):
    _, cfg, tmp = env
    assert run(cfg, tmp, "a", "bm25", "hash") == 0
    assert run(cfg, tmp, "b", "hash-bm25-rrf") == 0
    code = pair_runs.main([str(tmp / "a" / "beir-scifact.json"), str(tmp / "b" / "beir-scifact.json"), "--system-a", "hash", "--system-b", "hash-bm25-rrf"])
    assert code == 0 and "paired queries 2" in capsys.readouterr().out


class Flaky(HashEncoder):
    """Raises on the encode call after `ok` calls, as a kill would."""

    def __init__(self, ok: int):
        super().__init__(dim=64)
        self.ok, self.calls = ok, 0

    def encode(self, texts, **kw):
        self.calls += 1
        if self.calls > self.ok:
            raise RuntimeError("killed")
        return super().encode(texts, **kw)


def test_killed_dense_run_resumes_without_reencoding_finished_blocks(monkeypatch, tmp_path):
    docs = [(f"d{i}", f"alpha{i} beta{i}") for i in range(30)]
    kw = dict(name="x", model="hash-test", work_dir=tmp_path / "w", block_size=10, batch_size=64)
    first = Flaky(ok=2)
    monkeypatch.setattr(dense_hf, "load_model", lambda *a, **k: first)
    a = dense_hf.DenseHF(**kw)
    with pytest.raises(RuntimeError, match="killed"):
        a.index(docs)
    a.close()
    second = HashEncoder(dim=64)
    monkeypatch.setattr(dense_hf, "load_model", lambda *a, **k: second)
    b = dense_hf.DenseHF(**kw)
    b.index(docs)
    assert sorted(second.seen) == sorted(t for _, t in docs[20:])  # only the third block was encoded again
    assert b.search([("q", "alpha25 beta25")], 3)["q"][0][0] == "d25"
    b.close()


def fake_reranker(monkeypatch, fallback: bool):
    class Net:
        def parameters(self):
            return [type("P", (), {"numel": lambda self: 7})()]

    def rerank_rows(query, rows, alias=None, maxchars=None):
        if fallback:
            return list(rows), None, {"fallback": "cuda-error"}
        rows = list(reversed(rows))
        return rows, [float(len(rows) - i) for i in range(len(rows))], {"fallback": "none"}

    monkeypatch.setattr(gestalt_rank, "rerank_rows", rerank_rows)
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: Net())


def test_reranker_fallback_voids_the_run_with_exit_3(env, monkeypatch, capsys):
    _, cfg, tmp = env
    fake_reranker(monkeypatch, fallback=True)
    assert run(cfg, tmp, "out", "bm25", "rr") == 3
    r = json.loads((tmp / "out" / "beir-scifact.json").read_text())
    assert r["rerank_fallback_queries"] == 2 and "rerank_invalid" in json.loads((tmp / "out" / "summary.json").read_text())
    assert "void" in capsys.readouterr().err
    assert pair_runs.main([str(tmp / "out" / "beir-scifact.json")] * 2 + ["--system-a", "bm25", "--system-b", "rr"]) == 3


def test_reranker_without_fallback_runs_and_records_parameters(env, monkeypatch):
    _, cfg, tmp = env
    fake_reranker(monkeypatch, fallback=False)
    assert run(cfg, tmp, "out", "rr") == 0
    r = json.loads((tmp / "out" / "beir-scifact.json").read_text())
    assert r["rerank_fallback_queries"] == 0 and schema.validate(tmp / "out" / "beir-scifact.json") == []
    assert r["systems"]["rr"]["cost"]["parameters"] == 7


class Stub:
    """A leaf Searcher that returns fixed lists. Score is larger-is-better."""
    parts, indexed = (), True

    def __init__(self, name, hits):
        self.name, self.hits = name, hits

    def search(self, queries, k):
        return {q: self.hits[:k] for q, _ in queries}

    def describe(self):
        return {"model_id": self.name, "revision": None, "dtype": None, "parameters": None, "licence": None, "index_bytes": 1}


@pytest.mark.parametrize("fusion,alpha", [("rrf", 0.5), ("convex", 0.4)])
def test_fused_adapter_reproduces_fuse_pools(fusion, alpha):
    fts = [("d3", -9.0), ("d1", -7.5), ("d2", -4.0), ("d9", -1.0)]  # FTS5 ranks, smaller is better
    vec = [("d1", 0.2), ("d4", 0.5), ("d3", 0.9), ("d7", 1.1)]  # L2 distances, smaller is better
    a = Stub("lex", [(d, -r) for d, r in fts])
    b = Stub("dense", [(d, -dist) for d, dist in vec])
    got = Fused("f", a, b, fusion, alpha, depth=4).search([("q", "text")], 10)["q"]
    assert got == fuse_pools(fts, vec, fusion, alpha)[:10]
    assert Fused("f", a, b, fusion, alpha, depth=4).describe()["index_bytes"] == 2


def test_cost_helpers_with_a_fake_model(tmp_path):
    class P:
        def __init__(self, n):
            self.n = n

        def numel(self):
            return self.n

    class Model:
        def parameters(self):
            return [P(3), P(4)]

    class Wrapper:  # a CrossEncoder keeps its network in .model
        model = Model()

    assert cost.param_count(Model()) == 7 and cost.param_count(Wrapper()) == 7 and cost.param_count(object()) is None
    (tmp_path / "sub").mkdir()
    (tmp_path / "a").write_bytes(b"x" * 10)
    (tmp_path / "sub" / "b").write_bytes(b"y" * 5)
    assert cost.dir_bytes(tmp_path) == 15 and cost.dir_bytes(tmp_path / "a") == 10
    lat = cost.batch1_latencies_ms(lambda qs, k: (time.sleep(0.002), len(qs))[1], [("q", "t")] * 5, 10, 3)
    assert len(lat) == 3 and all(x >= 2 for x in lat)
    assert cost.percentile([1, 2, 3, 4, 100], 50) == 3 and cost.percentile([], 50) is None
    vram = cost.peak_vram_mib()
    assert vram is None or vram >= 0
    block = cost.cost_block(index_seconds=2.0, n_docs=100, search_seconds=4.0, n_queries=8, latencies_ms=lat, peak_vram=vram, index_bytes=15,
                            parameters=7, dtype="float32", gpu=cost.gpu_name())
    assert set(block) == COST_FIELDS and block["docs_per_second"] == 50.0 and block["queries_per_second"] == 2.0


def test_schema_rejects_a_missing_cost_field_and_a_short_per_query(env, tmp_path):
    _, cfg, tmp = env
    run(cfg, tmp, "out", "bm25")
    path = tmp / "out" / "beir-scifact.json"
    r = json.loads(path.read_text())
    del r["systems"]["bm25"]["cost"]["gpu"]
    bad = tmp / "bad.json"
    bad.write_text(json.dumps(r))
    assert any("gpu" in e for e in schema.validate(bad))
    r = json.loads(path.read_text())
    r["systems"]["bm25"]["per_query"] = r["systems"]["bm25"]["per_query"][:1]
    bad.write_text(json.dumps(r))
    assert any("per_query" in e for e in schema.validate(bad))
    assert schema.main([str(bad)]) == 1 and schema.main([str(path)]) == 0


def test_beir_bench_result_passes_without_a_cost_block(monkeypatch, tmp_path):
    import beir_bench as bb

    class Stub64(HashEncoder):
        normalize = False

    monkeypatch.setitem(sys.modules, "ir_datasets", fake_datasets())
    monkeypatch.setattr(bb.harness, "_model", Stub64())
    bb.run_dataset("beir/scifact", tmp_path / "bb", None, 8, bb.harness._model, work_dir=tmp_path / "work", dense_mode="exact")
    assert schema.validate(tmp_path / "bb" / "beir-scifact.json") == []


REAL = Path.home() / "artifacts/retrieval-upgrade/results-hub/beir-final-scifact/beir-scifact.json"


@pytest.mark.skipif(not REAL.exists(), reason="the hub result is not on this machine")
def test_real_beir_bench_json_passes():
    assert schema.validate(REAL) == []


def test_a_stalled_zoo_embed_exits_4_like_the_bench(monkeypatch, tmp_path, capsys):
    """2026-10-09: a zoo run sat 23 minutes at full CPU with the GPU idle and nothing ended it. The zoo now carries the bench's embed guard."""
    import functools
    import time

    import bench_engine as E

    class Slow(HashEncoder):
        def encode(self, texts, **kw):
            time.sleep(3.0)
            return super().encode(texts, **kw)

    monkeypatch.setenv("GESTALT_EVAL_EMBED_TIMEOUT", "1")
    monkeypatch.setattr(dense_hf, "EmbedGuard", functools.partial(E.EmbedGuard, poll=0.1, grace=60.0, exit_fn=lambda code: pytest.fail("forced exit")))
    monkeypatch.setattr(dense_hf, "load_model", lambda *a, **k: Slow(dim=64))
    docs = [(f"d{i}", f"alpha{i} beta{i}") for i in range(12)]
    z = dense_hf.DenseHF(name="x", model="hash-test", work_dir=tmp_path / "w", block_size=10, batch_size=64)
    with pytest.raises(SystemExit) as stop:
        z.index(docs)
    assert stop.value.code == 4
    assert "embed stall: no sub-batch finished in 1s" in capsys.readouterr().err
    z.close()
