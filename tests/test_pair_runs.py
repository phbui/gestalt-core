"""pair_runs.py: pairing two benchmark runs on shared queries. Fake result JSONs, no model, no dataset."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "tools"))
pytest.importorskip("numpy")
import pair_runs as pr  # noqa: E402

IDS = [f"q{i}" for i in range(40)]
BASE = [0.3 + 0.015 * (i % 20) for i in range(40)]


def result(per_query, ids=IDS, system="dense", **extra):
    r = {"dataset": "beir/scifact", "metric": "nDCG@10", "smoke_subset": False, "query_ids": list(ids),
         "systems": {system: {"per_query": list(per_query)}},
         "model": {"embed_profile": "nomic", "embedding_revision": "abc", "reranker_alias": None},
         "query_log_header": {"rerank": None}}
    r.update(extra)
    return r


def write(tmp_path, name, r):
    p = tmp_path / name
    p.write_text(json.dumps(r))
    return str(p)


def run(tmp_path, capsys, a, b, *flags):
    code = pr.main([write(tmp_path, "a.json", a), write(tmp_path, "b.json", b), "--system-a", "dense", "--system-b", "dense", *flags])
    return code, capsys.readouterr()


def test_clear_win_means_difference_interval_and_small_p(tmp_path, capsys):
    out = tmp_path / "o.json"
    code, cap = run(tmp_path, capsys, result([x + 0.1 for x in BASE]), result(BASE), "--json", str(out))
    res = json.loads(out.read_text())
    assert code == 0
    assert res["mean_difference"] == pytest.approx(0.1, abs=1e-4)
    assert res["mean_a"] == pytest.approx(sum(BASE) / 40 + 0.1, abs=1e-4)
    assert res["mean_b"] == pytest.approx(sum(BASE) / 40, abs=1e-4)
    lo, hi = res["ci95_bootstrap_difference"]
    assert lo <= res["mean_difference"] <= hi
    assert res["p_value"] < 0.001
    assert res["score_source"] == {"a": "json", "b": "json"}
    assert "scores from: A json" in cap.out and "embed_profile=nomic" in cap.out


def test_identical_runs_give_large_p(tmp_path, capsys):
    out = tmp_path / "o.json"
    code, _ = run(tmp_path, capsys, result(BASE), result(BASE), "--json", str(out))
    res = json.loads(out.read_text())
    assert code == 0 and res["mean_difference"] == 0 and res["p_value"] > 0.9


def test_dataset_metric_or_subset_mismatch_exits_2(tmp_path, capsys):
    for change in ({"dataset": "beir/nfcorpus"}, {"metric": "nDCG@20"}, {"max_docs": 200, "subset": True}):
        code, cap = run(tmp_path, capsys, result(BASE, **change), result(BASE))
        assert code == 2 and "differs" in cap.err


def test_rerank_fallback_exits_3(tmp_path, capsys):
    code, cap = run(tmp_path, capsys, result(BASE), result(BASE, rerank_fallback_queries=2))
    assert code == 3 and "fell back" in cap.err


def test_missing_query_is_dropped_and_counted(tmp_path, capsys):
    out = tmp_path / "o.json"
    code, cap = run(tmp_path, capsys, result(BASE[:38], ids=IDS[:38]), result(BASE), "--json", str(out))
    res = json.loads(out.read_text())
    assert code == 0 and res["paired_queries"] == 38
    assert res["dropped_from_a"] == 0 and res["dropped_from_b"] == 2
    assert "dropped: 0 from A, 2 from B" in cap.out


def test_scores_recomputed_from_run_files_when_json_has_none(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    import beir_bench as bb

    qrels = {"q1": {"d1": 1}, "q2": {"d2": 1}}
    queries = [type("Q", (), {"query_id": q}) for q in ("q1", "q2")]
    monkeypatch.setattr(bb, "scorable_queries", lambda name: (qrels, queries, 0))
    for name, top in (("a", {"q1": "d1", "q2": "d2"}), ("b", {"q1": "d1", "q2": "dx"})):
        (tmp_path / name).mkdir()
        (tmp_path / name / "beir-scifact.dense.run").write_text("".join(f"{q} Q0 {d} 1 10 t\n" for q, d in top.items()))
        r = result([], ids=["q1", "q2"])
        del r["systems"]["dense"]["per_query"]
        write(tmp_path, f"{name}/beir-scifact.json", r)
    out = tmp_path / "o.json"
    code = pr.main([str(tmp_path / "a" / "beir-scifact.json"), str(tmp_path / "b" / "beir-scifact.json"),
                    "--system-a", "dense", "--system-b", "dense", "--json", str(out)])
    res = json.loads(out.read_text())
    assert code == 0 and res["score_source"] == {"a": "run-files", "b": "run-files"}
    assert res["mean_a"] == 1.0 and res["mean_b"] == 0.5
