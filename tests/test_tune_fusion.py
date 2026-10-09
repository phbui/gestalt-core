"""tune_fusion.py --results: choose a fusion setting on the dev half of beir_bench grid results. Synthetic per-query data, no model."""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "tools"))
pytest.importorskip("numpy")
import pair_runs  # noqa: E402
import tune_fusion as tf  # noqa: E402

GRID = ["hybrid:wrrf@0", "hybrid:wrrf@0.1", "hybrid:wrrf@0.3", "hybrid:wrrf@0.5", "hybrid:convex@0.2"]


def result(dataset: str, n: int, make, seed: int = 0) -> dict:
    """A beir_bench-shaped result JSON. make(rng, dense, bm25, name) gives one query's nDCG for a fused system."""
    rng = random.Random(seed)
    ids = [f"q{i}" for i in range(n)]
    dense = [rng.random() for _ in ids]
    bm25 = [rng.random() for _ in ids]
    systems = {"dense": dense, "bm25": bm25, "hybrid": [make(rng, d, b, "hybrid:wrrf@0.5") for d, b in zip(dense, bm25)]}
    for g in GRID:
        systems[g] = [make(rng, d, b, g) for d, b in zip(dense, bm25)]
    return {"dataset": dataset, "metric": "nDCG@10", "query_ids": ids,
            "query_log_header": {"fusion": "rrf", "fusion_params": {"norm": "minmax", "missing": "zero"}},
            "systems": {s: {"ndcg10": sum(v) / n, "per_query": v} for s, v in systems.items()}}


def noise(rng, d, b, name):
    """BM25 is pure noise: the more lexical weight, the worse. wrrf@0 is the dense leg."""
    w = tf.lexical_weight(name)
    return d if w == 0 else max(0.0, d - w * 0.4 * rng.random())


def matched(rng, d, b, name):
    """Two legs of equal strength that miss different queries. Fusion recovers the better of the two."""
    w = tf.lexical_weight(name)
    return max(d, b) if 0.05 < w < 0.6 else (d if w == 0 else (d + b) / 2)


def test_bm25_as_noise_falls_back_to_the_dense_only_setting():
    res = tf.tune_grid([result("beir/a", 300, noise), result("beir/b", 200, noise, 1)])
    assert res["best_single_leg"] == "dense" and res["chosen"] == "hybrid:wrrf@0"
    assert res["env"] == ["GESTALT_FUSION=wrrf", "GESTALT_FUSION_W_BM25=0"]
    assert res["safeguard"]["passed"] is False


def test_matched_legs_choose_a_fused_setting():
    res = tf.tune_grid([result("beir/a", 300, matched)])
    assert res["chosen"] in ("hybrid:wrrf@0.1", "hybrid:wrrf@0.3", "hybrid:wrrf@0.5", "hybrid:convex@0.2", "hybrid")
    assert res["chosen"] == "hybrid:wrrf@0.1"  # ties go to the lower lexical weight
    assert res["safeguard"]["passed"] and res["safeguard"]["ci95_bootstrap"][0] > 0
    assert res["env"] == ["GESTALT_FUSION=wrrf", "GESTALT_FUSION_W_BM25=0.1"]


def test_the_tuner_never_reads_a_test_query():
    base = result("beir/a", 300, matched)
    test_idx = [j for j, q in enumerate(base["query_ids"]) if tf.query_split("beir/a", q) == "test"]
    assert 0 < len(test_idx) < 300
    poisoned = json.loads(json.dumps(base))
    for v in poisoned["systems"].values():
        for j in test_idx:
            v["per_query"][j] = 1e9 if j % 2 else -1e9
    a, b = tf.tune_grid([base]), tf.tune_grid([poisoned])
    assert a == b
    test_ids = {base["query_ids"][j] for j in test_idx}
    assert not test_ids & set(json.dumps(a).replace('"', " ").split())
    assert sum(a["n_dev"].values()) == 300 - len(test_idx)


def test_the_split_is_stable_and_about_seventy_percent_dev():
    ids = [f"q{i}" for i in range(1000)]
    first = [tf.query_split("beir/scifact", q) for q in ids]
    assert first == [tf.query_split("beir/scifact", q) for q in ids]
    assert 0.6 < first.count("dev") / 1000 < 0.8


def test_results_from_different_fusion_knobs_are_refused():
    a, b = result("beir/a", 50, matched), result("beir/b", 50, matched)
    b["query_log_header"]["fusion_params"] = {"norm": "zscore"}
    with pytest.raises(SystemExit, match="fusion knobs"):
        tf.tune_grid([a, b])


def test_cli_writes_the_choice_and_prints_the_env_lines(tmp_path, monkeypatch, capsys):
    p = tmp_path / "beir-a.json"
    p.write_text(json.dumps(result("beir/a", 300, matched)))
    monkeypatch.setattr(sys, "argv", ["tune_fusion.py", "--results", str(p), "--out", str(tmp_path / "choice.json")])
    tf.main()
    out = capsys.readouterr().out
    assert "GESTALT_FUSION=wrrf" in out and "GESTALT_FUSION_W_BM25=0.1" in out
    saved = json.loads((tmp_path / "choice.json").read_text())
    assert saved["chosen"] == "hybrid:wrrf@0.1" and saved["rule"] == tf.GRID_RULE


def test_pair_runs_split_keeps_one_half(tmp_path):
    p = tmp_path / "beir-a.json"
    r = result("beir/a", 300, matched)
    p.write_text(json.dumps(r))
    full = pair_runs.pair(str(p), str(p), "hybrid:wrrf@0.1", "dense")
    test = pair_runs.pair(str(p), str(p), "hybrid:wrrf@0.1", "dense", split="test")
    dev = pair_runs.pair(str(p), str(p), "hybrid:wrrf@0.1", "dense", split="dev")
    assert test["paired_queries"] + dev["paired_queries"] == full["paired_queries"] == 300
    assert test["paired_queries"] == sum(tf.query_split("beir/a", q) == "test" for q in r["query_ids"])
