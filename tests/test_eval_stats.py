"""Paired statistics of the retrieval runner: one cluster set for both tests, dropped clusters reported, rows keyed by case id (EVAL-009). Hermetic."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load  # noqa: E402


@pytest.fixture(scope="module")
def runner():
    return _load("run_retrieval_evals_stats", "evals/retrieval/run_retrieval_evals.py")


def _rows(hits, qid=True):
    return [{**({"qid": f"id{i}"} if qid else {}), "query": f"q{i}", "expect_slug": f"s{i // 3}", "rank": 1 if h else None}
            for i, h in enumerate(hits)]


def test_both_tests_drop_the_same_unpaired_cluster_and_say_so(runner):
    before = {"results": _rows([True] * 18)}
    after = {"results": [r for r in _rows([False] * 18) if r["qid"] != "id0"]}  # cluster s0 lost a case: a golden edit
    boot = runner.cluster_bootstrap(before, after, iterations=500)
    pd = runner.paired_drop(before, after, iterations=500)
    assert boot["dropped_clusters"] == pd["dropped_clusters"] == 1
    assert boot["clusters"] == 5 and pd["n_shared"] == 15, "the 5 kept clusters hold 15 paired cases in both tests"
    assert "1 dropped" in boot["verdict"]


def test_rows_pair_by_case_id_not_by_query_text(runner):
    """Two cases may share a query and differ in target. Keyed by text they collapse into one row."""
    rows = _rows([True] * 12)
    rows[1] = {**rows[1], "query": "q0"}
    before, after = {"results": rows}, {"results": [{**r, "rank": None} for r in rows]}
    assert runner.paired_drop(before, after, iterations=200)["n_shared"] == 12
    assert runner.significance(before, after)["lost"] == 12
    legacy = [{k: v for k, v in r.items() if k != "qid"} for r in rows]
    assert runner.significance({"results": legacy}, {"results": [{**r, "rank": None} for r in legacy]})["lost"] == 11, \
        "files without ids pair by text, as they always did"


def test_compare_runs_prints_the_dropped_count(runner, tmp_path, capsys):
    saved = tmp_path / "s.json"
    saved.write_text(json.dumps({"summary": {"mode": "fts"}, "results": _rows([False] * 18)}))
    after = [r for r in _rows([True] * 18) if r["qid"] != "id0"]
    runner.compare_runs(str(saved), {"summary": {"mode": "fts"}, "results": after, "fts_results": after})
    assert "1 clusters dropped as not paired" in capsys.readouterr().out


def test_bootstrap_numbers_did_not_move_for_identical_case_sets(runner):
    """With no dropped cluster the shared pairing reproduces the old per-test numbers (pinned from the pre-refactor runner)."""
    before = {"results": _rows([i % 2 == 0 for i in range(30)])}
    after = {"results": _rows([i % 3 != 0 for i in range(30)])}
    pd = runner.paired_drop(before, after, iterations=2000)
    boot = runner.cluster_bootstrap(before, after, iterations=2000)
    assert (pd["net_lost"], pd["threshold"], pd["ci90"]) == (-5, 2, [0.0667, 0.2667])
    assert (boot["effect_size"], boot["p_value"]) == (0.1667, 0.002)
