"""Fixes from the 2026-10-11 line review of the benchmark harness and the zoo. Each test failed before its fix."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO))
import bench_engine as E  # noqa: E402
import resumable as R  # noqa: E402
from evals.zoo import collect_costs, colbert, zoo_run  # noqa: E402


class _NaNModel:
    def encode(self, texts, batch_size, convert_to_numpy, show_progress_bar):
        out = np.ones((len(texts), 4), dtype=np.float32)
        out[0, 0] = np.nan
        return out


def test_a_non_finite_document_vector_stops_the_encode_before_any_block_is_saved():
    """A GPU that drops mid-block can return garbage rather than raise. The block was then receipted as valid and reused on every
    resume. dense_hf refused such rows since the DenseOn fp16 incident; the bench did not."""
    with pytest.raises(RuntimeError, match="not finite"):
        E.encode_documents(_NaNModel(), ["a b", "c d", "e f"], max_batch=8, doc_prefix="", postprocess=lambda a: a)


def test_a_query_log_from_another_configuration_is_refused_before_anything_is_truncated(tmp_path):
    path = tmp_path / "queries.jsonl"
    raw = b'{"header": {"fingerprint": "other"}}\n{"system": "bm25", "qid": "q1", "ids": []}\n{"system": "bm25", "qi'
    path.write_bytes(raw)
    with pytest.raises(R.FingerprintMismatch):
        R.QueryLog(path, {"fingerprint": "mine"})
    assert path.read_bytes() == raw  # refused without --rebuild, so nothing of the other run's log is lost


def test_rebuild_also_removes_the_temporary_block_files_a_kill_left_behind(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"fingerprint": "old", "block_size": 20000}))
    (tmp_path / "emb-00000.npy").write_bytes(b"x")
    (tmp_path / ".emb-00000.npy.abc123.tmp").write_bytes(b"y" * 10)
    store = R.BlockStore(tmp_path, "new", 20000, rebuild=True)
    store.close()
    assert not (tmp_path / "emb-00000.npy").exists() and not list(tmp_path.glob(".emb-*.tmp"))


class _Part:
    name = "rr"
    parts = ()

    def __init__(self, fallbacks):
        self.fallbacks = fallbacks

    def describe(self):
        return {}


def test_a_checkpoint_of_a_void_system_is_not_reused_and_the_key_follows_the_code(tmp_path, monkeypatch):
    """A reranker that fell back on some queries was checkpointed and replayed on every retry, so the run exited 3 for ever. And a
    checkpoint written under one version of the code was replayed under another while the header named the new hashes."""
    ck = tmp_path / "rr.json"
    zoo_run.save_checkpoint(ck, "k", {"q": [("d", 1.0)]}, {"c": 1}, _Part(fallbacks=2), 1.0)
    assert zoo_run.load_checkpoint(ck, "k") is None
    zoo_run.save_checkpoint(ck, "k", {"q": [("d", 1.0)]}, {"c": 1}, _Part(fallbacks=0), 1.0)
    assert zoo_run.load_checkpoint(ck, "k") is not None
    specs = {"a": {"adapter": "bm25"}}
    monkeypatch.setattr(zoo_run, "code_hashes", lambda: {"x": "1"})
    k1 = zoo_run.checkpoint_key("a", specs, "beir/scifact", ["q"], 10)
    monkeypatch.setattr(zoo_run, "code_hashes", lambda: {"x": "2"})
    assert zoo_run.checkpoint_key("a", specs, "beir/scifact", ["q"], 10) != k1


def _summary(path: Path, system: str, ndcg: float, **extra):
    path.parent.mkdir(parents=True)
    cost = {"query_latency_ms_p50": 1.0, "query_latency_ms_p95": 2.0}
    path.write_text(json.dumps({"results": {"beir/scifact": {"queries": 10, "documents": 100, **extra,
                                                                "systems": {system: {"ndcg10": ndcg, "cost": cost}}}}}))


def test_the_cost_table_drops_void_and_subset_rows_instead_of_letting_them_replace_a_good_one(tmp_path):
    _summary(tmp_path / "zoo-a" / "summary.json", "s", 0.7)
    _summary(tmp_path / "zoo-b" / "summary.json", "s", 0.1, rerank_fallback_queries=3)
    _summary(tmp_path / "zoo-c" / "summary.json", "s", 0.2, subset=True, max_docs=500)
    rows = collect_costs.collect([tmp_path / d / "summary.json" for d in ("zoo-a", "zoo-b", "zoo-c")])
    assert [r["ndcg10"] for r in rows] == [0.7]


def test_the_cost_table_carries_the_gpu_duty_from_the_summary_or_the_map_and_null_otherwise(tmp_path):
    """Every zoo latency was measured under GESTALT_GPU_DUTY, which pauses after each model call, so a row without its duty cannot be
    compared with another. The zoo records it since 2026-10-12; earlier runs get it from a map of run name to duty."""
    _summary(tmp_path / "zoo-a" / "summary.json", "s", 0.7)
    _summary(tmp_path / "zoo-b" / "summary.json", "t", 0.6)
    _summary(tmp_path / "zoo-c" / "summary.json", "u", 0.5)
    recorded = tmp_path / "zoo-c" / "summary.json"
    d = json.loads(recorded.read_text()); d["environment"] = {"gpu_duty": 1.0}; recorded.write_text(json.dumps(d))
    files = [tmp_path / d / "summary.json" for d in ("zoo-a", "zoo-b", "zoo-c")]
    rows = {r["system"]: r for r in collect_costs.collect(files, {"zoo-a": 0.3})}
    assert rows["s"]["gpu_duty"] == 0.3 and rows["t"]["gpu_duty"] is None and rows["u"]["gpu_duty"] == 1.0
    assert all("gpu_duty" in r for r in collect_costs.collect(files))


def test_colbert_refuses_a_non_finite_token_vector_like_the_dense_adapter_does():
    with pytest.raises(RuntimeError):
        colbert._unit(np.array([[np.nan, 1.0], [1.0, 0.0]], dtype=np.float32))


def test_the_zoo_normalise_flag_yields_unit_vectors_and_keeps_direction():
    """The flag was tested only as "differs from raw" (2026-10-11 review); a transform that doubled every vector would have passed."""
    from evals.zoo import dense_hf
    x = np.array([[3.0, 4.0], [0.0, 2.0], [1.0, 1.0]], dtype=np.float32)
    u = dense_hf._unit(x)
    assert np.allclose(np.linalg.norm(u, axis=1), 1.0)
    assert np.allclose(u[0], [0.6, 0.8]) and np.allclose(u[1], [0.0, 1.0])
