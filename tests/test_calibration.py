"""calibration.py: pure metrics on small seeded data, and one fixture built from a tiny fake query log and run files."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "tools"))
np = pytest.importorskip("numpy")
import calibration as cal  # noqa: E402


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def test_perfect_and_random_auroc():
    y = np.array([0.0] * 50 + [1.0] * 50)
    assert cal.auroc(y + 0.1, y) == 1.0
    assert cal.auroc(-y, y) == 0.0
    rng = np.random.default_rng(1)
    assert abs(cal.auroc(rng.normal(size=4000), rng.integers(0, 2, 4000)) - 0.5) < 0.05


def test_auroc_ties_count_half():
    assert cal.auroc([1, 1, 1, 1], [0, 1, 0, 1]) == 0.5


def test_ece_zero_when_calibrated_and_large_when_overconfident():
    levels = np.linspace(0.05, 0.95, 10)
    pred = np.repeat(levels, 100)
    obs = np.concatenate([np.array([1.0] * round(p * 100) + [0.0] * (100 - round(p * 100))) for p in levels])
    assert cal.ece(pred, obs) < 1e-9
    over = np.full(1000, 0.95)
    half = np.array([0.0, 1.0] * 500)
    assert abs(cal.ece(over, half) - 0.45) < 1e-9


def test_reliability_table_shape():
    rows = cal.reliability(np.linspace(0, 1, 100), np.zeros(100))
    assert [r["count"] for r in rows] == [10] * 10
    assert rows[-1]["mean_observed"] == 0.0


def test_temperature_fit_recovers_scale_and_offset():
    rng = np.random.default_rng(7)
    s = rng.normal(size=40000)
    y = (rng.random(40000) < sigmoid(2.5 * s - 0.5)).astype(float)
    a, b = cal.fit_scale_offset(s, y)
    assert abs(a - 2.5) < 0.1 and abs(b + 0.5) < 0.1


def test_fit_stays_finite_on_separable_data_and_is_deterministic():
    s = np.array([0.0] * 20 + [1.0] * 20)
    a, b = cal.fit_scale_offset(s, s.copy())
    assert math.isfinite(a) and math.isfinite(b) and a > 0
    assert (a, b) == cal.fit_scale_offset(s, s.copy())


def test_risk_coverage_monotone_for_a_perfect_ordering():
    y = np.array([1.0] * 70 + [0.0] * 30)
    rc = cal.risk_coverage(y, y)
    risks = [p["risk"] for p in rc["curve"]]
    assert risks == sorted(risks)
    assert rc["aurc"] == pytest.approx(rc["aurc_oracle"])
    assert rc["gap_closed"] == pytest.approx(1.0)


def test_selective_accuracy_at_full_coverage_is_overall_accuracy():
    rng = np.random.default_rng(3)
    y = (rng.random(200) < 0.7).astype(float)
    rc = cal.risk_coverage(rng.normal(size=200), y)
    assert rc["curve"][-1]["coverage"] == 1.0
    assert 1 - rc["curve"][-1]["risk"] == pytest.approx(y.mean())
    assert rc["overall_accuracy"] == pytest.approx(y.mean())


def test_ties_are_stable_under_input_order():
    rng = np.random.default_rng(5)
    qids = [f"q{i:03d}" for i in range(80)]
    sig = np.round(rng.normal(size=80), 0)  # many ties
    y = (rng.random(80) < 0.6).astype(float)
    base = cal.analyse_signal(sig, y, qids)
    perm = rng.permutation(80)
    again = cal.analyse_signal(sig[perm], y[perm], [qids[i] for i in perm])
    assert base["auroc"] == again["auroc"]
    assert base["risk_coverage"]["aurc"] == again["risk_coverage"]["aurc"]
    assert base["calibration"]["ece_test"] == again["calibration"]["ece_test"]


@pytest.mark.parametrize("label,word", [(1.0, "successes"), (0.0, "failures")])
def test_one_class_gives_a_message_not_a_crash(label, word):
    out = cal.analyse_signal(np.arange(30.0), np.full(30, label), [f"q{i}" for i in range(30)])
    assert out["status"] == "undefined" and word in out["message"]


def test_split_is_reproducible_and_balanced():
    ids = [f"q{i}" for i in range(2000)]
    dev = [cal.split_is_dev(q) for q in ids]
    assert dev == [cal.split_is_dev(q) for q in ids]
    assert 0.45 < sum(dev) / 2000 < 0.55


def test_bootstrap_interval_brackets_the_estimate_and_is_seeded():
    rng = np.random.default_rng(11)
    y = (rng.random(300) < 0.6).astype(float)
    s = y + rng.normal(size=300)
    qids = [f"q{i:03d}" for i in range(300)]
    a, b = cal.analyse_signal(s, y, qids), cal.analyse_signal(s, y, qids)
    lo, hi = a["auroc"]["ci95_bootstrap"]
    assert lo < a["auroc"]["value"] < hi and a["auroc"] == b["auroc"]


def test_compare_signals():
    rng = np.random.default_rng(13)
    y = (rng.random(400) < 0.5).astype(float)
    good, poor = y + rng.normal(size=400) * 0.5, y + rng.normal(size=400) * 2
    c = cal.compare_signals(good, poor, y)
    assert c["auroc_difference"] > 0.1 and c["ci95_bootstrap"][0] > 0 and c["p_value"] < 0.01
    same = cal.compare_signals(good, good, y)
    assert same["auroc_difference"] == 0 and same["p_value"] == 1.0
    assert c["p_floor"] == pytest.approx(1 / 20001, abs=1e-6)


# ---------------------------------------------------------------- fixture: fake query log, result JSON, run files

def make_fixture(tmp_path, n=60):
    rng = np.random.default_rng(21)
    qids = [f"q{i:02d}" for i in range(n)]
    success = rng.random(n) < 0.7
    lines = [json.dumps({"header": {}})]
    for q, ok in zip(qids, success):
        base = 4.0 if ok else 0.0
        for system, shift in (("hybrid_rerank", base), ("dense", base / 8), ("bm25", base * 3), ("hybrid", base / 100)):
            sc = sorted(rng.normal(shift, 1.0, 10), reverse=True)
            lines.append(json.dumps({"system": system, "qid": q, "ids": [f"d{i}" for i in range(10)], "scores": sc}))
    log = tmp_path / "queries-x.jsonl"
    log.write_text("\n".join(lines) + "\n")
    per = [float(rng.uniform(0.2, 1)) if ok else 0.0 for ok in success]
    res = {"dataset": "beir/fake", "metric": "nDCG@10", "query_ids": qids,
           "systems": {s: {"per_query": per} for s in ("bm25", "dense", "hybrid", "hybrid_rerank")}}
    rp = tmp_path / "beir-fake.json"
    rp.write_text(json.dumps(res))
    return rp, log, success


def test_cli_end_to_end_on_a_query_log(tmp_path, capsys):
    rp, log, _ = make_fixture(tmp_path)
    out = tmp_path / "out.json"
    code = cal.main([str(rp), "--query-log", str(log), "--compare", "top1", "dense_top", "--json", str(out)])
    assert code == 0
    data = json.loads(out.read_text())
    assert set(data["signals"]) == {"top1", "margin", "entropy", "n_above", "dense_top", "bm25_top", "rrf_margin"}
    assert data["signals"]["top1"]["auroc"]["value"] > 0.8
    assert data["settings"]["split_salt"] == cal.SALT and len(data["settings"]["code_sha256"]) == 64
    assert data["compare"]["a"] == "top1" and "p_value" in data["compare"]
    assert data["signals"]["top1"]["direction"] == "higher is more confident"
    assert "nDCG-based only" in data["label"]
    assert "AUROC" in capsys.readouterr().out


def test_rank_derived_run_file_is_refused_with_a_clear_message(tmp_path, capsys):
    rp, _, _ = make_fixture(tmp_path)
    for s in ("hybrid_rerank", "dense", "bm25", "hybrid"):
        (tmp_path / f"beir-fake.{s}.run").write_text("".join(f"q00 Q0 d{r} {r} {11 - r} gestalt-{s}\n" for r in range(1, 11)))
    assert cal.main([str(rp)]) == 2
    assert "rank-derived" in capsys.readouterr().err


def test_raw_score_run_file_is_accepted(tmp_path):
    rp, log, _ = make_fixture(tmp_path)
    scores = cal.read_log_scores(log)
    for s, d in scores.items():
        (tmp_path / f"beir-fake.{s}.run").write_text("".join(f"{q} Q0 d{r} {r} {v:.6f} t\n" for q, vs in d.items() for r, v in enumerate(vs, 1)))
    got, why = cal.load_scores(str(rp), ["hybrid_rerank", "dense"], None)
    assert not why and got["dense"]["q00"] == pytest.approx(scores["dense"]["q00"], abs=1e-5)


def test_signal_definitions_on_known_scores():
    sc = {"hybrid_rerank": {"a": [3.0, 1.0, 0.0], "b": [1.0, 0.9, 0.8]}}
    s = cal.compute_signals(sc, "hybrid_rerank", ["a", "b"])
    assert list(s["top1"]) == [3.0, 1.0]
    assert s["margin"][0] == 2.0 and s["margin"][1] == pytest.approx(0.1)
    assert s["entropy"][0] > s["entropy"][1]  # sharper scores, lower entropy, higher oriented value
    assert list(s["n_above"]) == [1.0, 0.0]  # median top1 is 2.0


def test_log_scores_for_bm25_and_dense_are_flipped_so_the_best_comes_first(tmp_path):
    """The query log keeps raw FTS5 ranks and L2 distances, where smaller is better. The reader negates them."""
    import json

    from calibration import read_log_scores

    log = tmp_path / "queries.jsonl"
    rows = [{"header": {"x": 1}},
            {"system": "bm25", "qid": "q1", "ids": ["a", "b"], "scores": [-1.0, -5.0]},
            {"system": "dense", "qid": "q1", "ids": ["a", "b"], "scores": [0.2, 0.9]},
            {"system": "hybrid_rerank", "qid": "q1", "ids": ["a", "b"], "scores": [0.1, 0.8]}]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = read_log_scores(log)
    assert out["bm25"]["q1"] == [5.0, 1.0]
    assert out["dense"]["q1"] == [-0.2, -0.9]
    assert out["hybrid_rerank"]["q1"] == [0.8, 0.1]
