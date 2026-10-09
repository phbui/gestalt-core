"""multitest.py: Holm, Benjamini-Hochberg, Friedman and Nemenyi against hand-computed and published values."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
import multitest as mt  # noqa: E402

# Benjamini and Hochberg 1995, the 15 p-values of their worked example. At q = 0.05 BH rejects the first 4, Holm the first 3.
BH1995 = [0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344, 0.0459, 0.3240, 0.4262, 0.5719, 0.6528, 0.7590, 1.000]


def test_holm_by_hand():
    named = {"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.005}
    # sorted 0.005 0.01 0.03 0.04 times 4 3 2 1 = 0.02 0.03 0.06 0.04, running max = 0.02 0.03 0.06 0.06
    assert mt.holm(named) == pytest.approx({"d": 0.02, "a": 0.03, "c": 0.06, "b": 0.06})


def test_bh_by_hand():
    named = {"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.005}
    # times 4/1 4/2 4/3 4/4 = 0.02 0.02 0.04 0.04, running min from the top
    assert mt.benjamini_hochberg(named) == pytest.approx({"d": 0.02, "a": 0.02, "c": 0.04, "b": 0.04})


def test_bh1995_example_rejection_counts():
    named = {f"h{i}": p for i, p in enumerate(BH1995)}
    assert sum(v <= 0.05 for v in mt.benjamini_hochberg(named).values()) == 4
    assert sum(v <= 0.05 for v in mt.holm(named).values()) == 3
    assert mt.benjamini_hochberg(named)["h3"] == pytest.approx(0.0095 * 15 / 4)


def test_adjusted_p_is_capped_and_never_below_raw():
    named = {"a": 0.6, "b": 0.9, "c": 0.0001}
    for f in (mt.holm, mt.benjamini_hochberg):
        adj = f(named)
        assert all(named[k] <= adj[k] <= 1 for k in named)


def test_single_test_is_unchanged():
    assert mt.holm({"x": 0.03}) == {"x": 0.03} and mt.benjamini_hochberg({"x": 0.03}) == {"x": 0.03}


def test_chi2_tail_values():
    assert mt._chi2_sf(8, 2) == pytest.approx(math.exp(-4), rel=1e-9)  # df 2 has the closed form exp(-x/2)
    assert mt._chi2_sf(9.28, 3) == pytest.approx(0.0258, abs=5e-4)
    sp = pytest.importorskip("scipy.stats")
    for x, df in ((0.5, 1), (3.3, 4), (12.0, 7), (30.0, 5)):
        assert mt._chi2_sf(x, df) == pytest.approx(sp.chi2.sf(x, df), rel=1e-8)


def test_friedman_nemenyi_demsar_2006():
    # Demsar 2006 section 3.2.2: 4 algorithms on 14 datasets. Mean ranks 3.143, 2.000, 2.893, 1.964. chi2 = 9.28, CD = 1.25.
    ranks = {"C4.5": 3.143, "C4.5+m": 2.000, "C4.5+m+cf": 2.893, "C4.5+cf": 1.964}
    out = mt.friedman_from_mean_ranks(ranks, 14)
    assert out["chi2"] == pytest.approx(9.28, abs=0.01)
    assert out["nemenyi_cd"] == pytest.approx(1.25, abs=0.01)
    assert out["p_value"] == pytest.approx(0.0258, abs=5e-4)
    assert not any(p["significant"] for p in out["pairs"])  # the largest gap, 1.179, is below the CD


def test_friedman_matrix_hand_example():
    # Three systems in the same order on four datasets: mean ranks 1, 2, 3. chi2 = 12*4/(3*4) * (1 + 0 + 1) = 8.
    scores = {"A": [0.9, 0.8, 0.7, 0.9], "B": [0.5, 0.5, 0.4, 0.5], "C": [0.1, 0.2, 0.1, 0.0]}
    out = mt.friedman_nemenyi(scores)
    assert out["mean_ranks"] == {"A": 1.0, "B": 2.0, "C": 3.0}
    assert out["chi2"] == pytest.approx(8.0) and out["p_value"] == pytest.approx(math.exp(-4))


def test_friedman_matches_scipy_with_ties():
    sp = pytest.importorskip("scipy.stats")
    scores = {"A": [0.9, 0.8, 0.7, 0.9, 0.6], "B": [0.5, 0.8, 0.4, 0.5, 0.6], "C": [0.1, 0.2, 0.7, 0.0, 0.3], "D": [0.3, 0.2, 0.9, 0.1, 0.3]}
    ours = mt.friedman_nemenyi(scores)
    ref = sp.friedmanchisquare(*scores.values())
    assert ours["chi2"] == pytest.approx(ref.statistic, rel=1e-9) and ours["p_value"] == pytest.approx(ref.pvalue, rel=1e-6)


def test_friedman_refuses_bad_input():
    with pytest.raises(ValueError):
        mt.friedman_nemenyi({"A": [1.0], "B": [2.0]})
    with pytest.raises(ValueError):
        mt.friedman_nemenyi({"A": [1.0, 1.0], "B": [1.0, 1.0]})


def test_nemenyi_table_matches_scipy():
    sp = pytest.importorskip("scipy.stats")
    for k in (3, 5, 10):
        assert mt.nemenyi_q(k, 0.05) == pytest.approx(sp.studentized_range.ppf(0.95, k, math.inf) / math.sqrt(2), abs=2e-3)


def test_correct_family_over_result_jsons(tmp_path):
    def pair(name, p):
        f = tmp_path / f"{name}.json"
        f.write_text(json.dumps({"dataset": name, "system_a": "x", "system_b": "y", "p_value": p, "permutations": 20000}))
        return str(f)
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"dataset": "d3", "system": "hybrid_rerank", "compare": {"a": "top1", "b": "dense_top", "p_value": 0.04, "permutations": 20000}}))
    fam = mt.correct_family([pair("d1", 0.00005), pair("d2", 0.02), str(cal)])
    rows = {r["test"]: r for r in fam["rows"]}
    assert fam["family_size"] == 3 and fam["p_floor"] == pytest.approx(1 / 20001)
    assert rows["d1:x-vs-y"]["at_p_floor"] and rows["d1:x-vs-y"]["holm"] == pytest.approx(0.00015)
    assert rows["d3:hybrid_rerank:top1-vs-dense_top"]["holm"] == pytest.approx(0.04)
    assert mt.main([pair("d4", 0.5)]) == 0


def test_duplicate_names_are_refused(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"dataset": "d", "system_a": "x", "system_b": "y", "p_value": 0.1}))
    with pytest.raises(ValueError):
        mt.correct_family([str(f), str(f)])
