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
pytest.importorskip("ir_datasets")  # the harness imports it at module level; without the bench requirements these tests skip, as the README says
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


def test_the_candidates_config_loads_and_every_candidate_is_pinned(monkeypatch):
    """evals/zoo/zoo-candidates.yaml names exact revisions, a prefix recipe per model, and a pinned code repo wherever remote code runs."""
    from evals.zoo import zoo_run

    specs = zoo_run.load_specs(Path(__file__).resolve().parent.parent / "evals" / "zoo" / "zoo-candidates.yaml")
    dense = {n: s["args"] for n, s in specs.items() if s["adapter"] == "dense_hf"}
    assert {"nomic", "gte-base-v15", "stella-400m", "denseon"} <= set(dense)
    for name, a in dense.items():
        assert len(a["revision"]) == 40, name  # a full commit hash, never a branch
        assert a.get("licence"), name
    assert dense["gte-base-v15"]["trust_remote_code"] and len(dense["gte-base-v15"]["code_revision"]) == 40  # its code lives in another repo
    assert dense["stella-400m"]["trust_remote_code"] and "code_revision" not in dense["stella-400m"]  # its code is in the pinned model repo
    assert "trust_remote_code" not in dense["denseon"]
    assert dense["denseon"]["query_prefix"] == "query: " and dense["denseon"]["doc_prefix"] == "document: "
    assert dense["stella-400m"]["query_prefix"].startswith("Instruct: ") and "doc_prefix" not in dense["stella-400m"]
    # every fused and reranked variant points at a system that exists in the same file
    for name, s in specs.items():
        for key in ("a", "b", "c", "inner"):
            if s["adapter"] in ("fused", "rerank") and key in s["args"]:
                assert s["args"][key] in specs, (name, key)


def test_a_pinned_code_revision_reaches_the_loader_and_the_fingerprint(monkeypatch, tmp_path):
    seen = {}

    def fake_load(model_id, revision, dtype, max_seq_length, trust_remote_code, code_revision=None, config_kwargs=None):
        seen["code_revision"] = code_revision
        return HashEncoder(dim=64)

    monkeypatch.setattr(dense_hf, "load_model", fake_load)
    docs = [(f"d{i}", f"alpha{i} beta{i}") for i in range(12)]
    a = dense_hf.DenseHF(name="x", model="m", work_dir=tmp_path / "a", block_size=10, code_revision="c" * 40, trust_remote_code=True)
    a.index(docs); a.close()
    assert seen["code_revision"] == "c" * 40
    b = dense_hf.DenseHF(name="x", model="m", work_dir=tmp_path / "a", block_size=10, trust_remote_code=True)
    with pytest.raises(Exception, match="another configuration"):
        b.index(docs)  # the same work directory under a different code revision is refused, never mixed
    b.close()


def test_the_zoo_runner_starts_as_a_script():
    """2026-10-09: an import re-sort put the harness imports before `import evals.zoo`, which is what puts evals/retrieval on
    sys.path, so `python evals/zoo/zoo_run.py` died with ModuleNotFoundError on the hub while every in-process test passed."""
    import subprocess
    import sys

    r = subprocess.run([sys.executable, str(REPO / "evals/zoo/zoo_run.py"), "--help"], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-800:]
    assert "--systems" in r.stdout


def test_rebuild_nonpersistent_buffers_restores_the_rotary_cache_and_position_ids():
    """transformers 5 leaves remote code's non-persistent buffers uninitialised (gte-v1.5 crashed on 2026-10-09)."""
    torch = pytest.importorskip("torch")
    from evals.zoo.dense_hf import rebuild_nonpersistent_buffers

    class Rotary(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.dim, self.base, self.max_position_embeddings = 8, 10000.0, 16
            self.register_buffer("inv_freq", torch.full((4,), float("nan")), persistent=False)
            self.register_buffer("cos_cached", torch.full((3, 8), float("nan")), persistent=False)
            self.register_buffer("sin_cached", torch.full((3, 8), float("nan")), persistent=False)

        def _set_cos_sin_cache(self, seq_len, device, dtype):
            t = torch.arange(seq_len, device=device, dtype=torch.float32)
            emb = torch.cat([torch.outer(t, self.inv_freq)] * 2, dim=-1)
            self.register_buffer("cos_cached", emb.cos().to(dtype), persistent=False)
            self.register_buffer("sin_cached", emb.sin().to(dtype), persistent=False)

    class Embeddings(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.register_buffer("position_ids", torch.tensor([-3674210544, 7, 7, 7]), persistent=False)
            self.rotary = Rotary()

    m = Embeddings()
    assert rebuild_nonpersistent_buffers(m) == 2
    assert m.position_ids.tolist() == [0, 1, 2, 3]
    assert m.rotary.cos_cached.shape == (16, 8) and torch.isfinite(m.rotary.cos_cached).all()
    assert torch.allclose(m.rotary.inv_freq, 1.0 / (10000.0 ** (torch.arange(0, 8, 2).float() / 8)))
    assert rebuild_nonpersistent_buffers(torch.nn.Linear(2, 2)) == 0


def test_the_attention_mask_shim_accepts_the_device_argument_remote_code_passes():
    """stella's remote code calls get_extended_attention_mask(..., device=...), which transformers 5 no longer takes."""
    torch = pytest.importorskip("torch")
    from transformers.modeling_utils import ModuleUtilsMixin

    from evals.zoo.dense_hf import shim_extended_attention_mask_device

    shim_extended_attention_mask_device()
    assert shim_extended_attention_mask_device() is False  # idempotent

    class M(torch.nn.Module, ModuleUtilsMixin):
        def __init__(self):
            super().__init__()
            self.lin = torch.nn.Linear(2, 2)
            self.config = type("C", (), {"is_decoder": False})()

    m = M()
    mask = torch.tensor([[1, 1, 0]])
    out = m.get_extended_attention_mask(mask, (1, 3), device=torch.device("cpu"))
    assert out.shape[-1] == 3 and torch.isfinite(out[..., :2]).all() and out[..., 2].item() < -1e4
    assert torch.equal(out, m.get_extended_attention_mask(mask, (1, 3)))


def test_blend_mixes_reranker_and_inner_scores_and_zero_keeps_the_reranker_order():
    from evals.zoo.rerank import Rerank, blend_scores

    reranked = [("a", 0.9), ("b", 0.8), ("c", 0.1)]  # reranker order a, b, c
    inner = {"a": 0.01, "b": 0.05, "c": 0.04}  # the fused first stage liked b and c
    assert [d for d, _ in blend_scores(reranked, inner, 0.0)] == ["a", "b", "c"]
    mixed = blend_scores(reranked, inner, 0.5)
    assert [d for d, _ in mixed] == ["b", "a", "c"]  # b wins once half the weight is the first stage
    assert all(isinstance(s, float) for _, s in mixed)
    assert [d for d, _ in blend_scores([("x", 1.0), ("y", 1.0)], {"x": 1.0, "y": 1.0}, 0.3)] == ["x", "y"]  # ties keep the order
    with pytest.raises(ValueError):
        Rerank("r", inner=None, model="qwen3-0.6b", work_dir="/nonexistent", blend=1.0)


@pytest.mark.parametrize("config", ["zoo-wave2.yaml", "zoo-wave3.yaml", "zoo-wave4.yaml"])
def test_the_wave_configs_are_pinned_and_pair_every_variant_against_the_shipped_pipeline(config):
    """evals/zoo/zoo-wave{2,3}.yaml: exact revisions, the shipped pipeline as the baseline row, depths from the sweep, blends below 1."""
    from evals.zoo import zoo_run

    specs = zoo_run.load_specs(Path(__file__).resolve().parent.parent / "evals" / "zoo" / config)
    for name, s in specs.items():
        a = s.get("args", {})
        if s["adapter"] in ("dense_hf", "colbert"):
            assert len(a["revision"]) == 40 and a.get("licence"), name
        if s["adapter"] == "rerank":
            assert a["model"] in gestalt_rank.RERANK_REVISIONS and 0 <= a.get("blend", 0) < 1 and a["depth"] in (20, 40, 100), name
            assert a["inner"] in specs and specs[a["inner"]]["adapter"] in ("fused", "dense_hf"), name
    assert specs["nomic-bm25-rrf-rerank-qwen3-0.6b"]["args"] == {"inner": "nomic-bm25-rrf", "model": "qwen3-0.6b", "depth": 40}
    assert specs["nomic"]["args"]["dtype"] == "float16" and specs["nomic"]["args"]["doc_prefix"] == "search_document: "
    if config == "zoo-wave2.yaml":
        assert {"lateon", "gte-modernbert", "granite-r2", "qwen3-0.6b"} <= set(specs)
    elif config == "zoo-wave3.yaml":
        assert {"stella-400m", "stella-400m-rerank-qwen3-0.6b", "stella-400m-bm25-convex0.2-rerank-qwen3-0.6b"} <= set(specs)
    else:
        assert {"harrier-270m", "harrier-0.6b", "pplx-0.6b", "stella-rr-qwen3-4b-d20-b05", "stella-rr-ettin-150m-d40"} <= set(specs)


def test_a_non_finite_embedding_fails_the_run_instead_of_becoming_a_zero_row():
    """DenseOn at float16 overflowed on 628 SciFact documents (2026-10-09); the normaliser used to zero them silently."""
    import numpy as np

    from evals.zoo.dense_hf import _unit

    ok = _unit(np.array([[3.0, 4.0], [0.0, 0.0]]))
    assert np.allclose(ok, [[0.6, 0.8], [0.0, 0.0]])
    with pytest.raises(RuntimeError, match="2 of 3 embeddings are not finite"):
        _unit(np.array([[1.0, float("inf")], [float("nan"), 1.0], [1.0, 1.0]]))


def test_a_finished_system_is_resumed_from_its_checkpoint_and_a_changed_spec_is_not(env, monkeypatch):
    """2026-10-10: five hub wedges each threw away one to two hours of finished pipelines. Each measured system is now checkpointed
    under <out>/checkpoints and reused when the run (specs, dataset, queries, rerank settings) is the same."""
    _, cfg, tmp = env
    assert run(cfg, tmp, "out", "bm25", "hash", "hash-bm25-rrf") == 0
    first = json.loads((tmp / "out" / "beir-scifact.json").read_text())
    assert sorted(p.name for p in (tmp / "out" / "checkpoints").glob("*.json")) == ["bm25.json", "hash-bm25-rrf.json", "hash.json"]

    def boom(*a, **k):
        raise AssertionError("measure must not run for a checkpointed system")

    monkeypatch.setattr(zoo_run, "measure", boom)
    assert run(cfg, tmp, "out", "bm25", "hash", "hash-bm25-rrf") == 0
    second = json.loads((tmp / "out" / "beir-scifact.json").read_text())
    for n in ("bm25", "hash", "hash-bm25-rrf"):
        assert second["systems"][n]["ndcg10"] == first["systems"][n]["ndcg10"] and second["systems"][n]["per_query"] == first["systems"][n]["per_query"]
    assert "hash-bm25-rrf_vs_hash" in second["tests"]
    strip = lambda d: {k: v for k, v in d.items() if k != "index_bytes"}  # noqa: E731  the directory size is read live and grows
    assert strip(second["model"]["systems"]["hash"]) == strip(first["model"]["systems"]["hash"])
    assert (tmp / "out" / "beir-scifact.hash.run").read_text().startswith("q1 Q0 ")

    # a different spec under the same name must not reuse the checkpoint
    cfg.write_text(YAML.replace("depth: 5", "depth: 7"))
    with pytest.raises(AssertionError, match="must not run"):
        run(cfg, tmp, "out", "rr")
