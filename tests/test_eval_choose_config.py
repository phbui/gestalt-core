"""choose_config: dev-only decision, the pre-registered rule, and the once-only test report (EVAL-007). Hermetic: saved runs only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
yaml = pytest.importorskip("yaml")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load  # noqa: E402


@pytest.fixture(scope="module")
def cc():
    return _load("choose_config_t", "evals/retrieval/choose_config.py")


# --- the hand-computed example ----------------------------------------------------------------------
#
# Six dev cases: four answerable (targets a, b, c, d, one cluster each) and two abstention cases.
#   ranks       a=1, b=2, c=miss, d=3
#   recall@1  = 1/4                      = 0.25
#   recall@3  = 3/4                      = 0.75
#   MRR       = (1 + 1/2 + 0 + 1/3) / 4  = 11/24 = 0.458333...
#   block     = a and b carry a block, a hits, b misses = 1/2
#   top scores: answerable 0.9, 0.5, 0.2, 0.4; abstention 0.3 and no result (minus infinity)
#   AUROC: pairs (answerable > abstention) out of 4 x 2 = 8:
#     0.9 beats both, 0.5 beats both, 0.2 beats only -inf, 0.4 beats both -> 7 of 8 = 0.875
#   With 0.0 in place of minus infinity (the old rule) 0.2 > 0.0 still, and a top score of -1 would lose, which is the bug.

HAND = [
    {"query": "qa", "expect_slug": "a", "rank": 1, "expect_block": "x", "block_hit": True, "top_score": 0.9},
    {"query": "qb", "expect_slug": "b", "rank": 2, "expect_block": "y", "block_hit": False, "top_score": 0.5},
    {"query": "qc", "expect_slug": "c", "rank": None, "top_score": 0.2},
    {"query": "qd", "expect_slug": "d", "rank": 3, "top_score": 0.4},
    {"query": "n1", "expect_slug": None, "abstain": True, "rank": None, "top_score": 0.3},
    {"query": "n2", "expect_slug": None, "abstain": True, "rank": None, "top_score": None},
]


def test_point_metrics_match_the_hand_computation(cc):
    rows = [{**r, "abstain": bool(r.get("abstain")), "cluster": r["query"]} for r in HAND]
    m = cc.point_metrics(rows)
    assert m["recall@1"] == 0.25 and m["recall@3"] == 0.75
    assert m["mrr"] == pytest.approx(11 / 24)
    assert m["block_precision"] == 0.5
    assert m["auroc"] == 0.875


def test_runner_abstain_metrics_use_minus_infinity_and_an_interval(cc):
    """EVAL-008 in the runner: the same 0.875, and a 90 percent cluster-bootstrap interval around it."""
    rows = [{**r, "bucket": "abstain" if r.get("abstain") else "ops"} for r in HAND]
    rows[3]["top_score"] = -1.0  # a negative logit: under the old 0.0 fill the empty abstention row would outrank it
    m = cc.runner.abstain_metrics(rows, iterations=2000)
    # 0.9, 0.5 beat both. 0.2 beats -inf only. -1.0 beats -inf only. 6 of 8.
    assert m["auroc"] == 0.75
    lo, hi = m["auroc_ci90"]
    assert 0.0 <= lo <= 0.75 <= hi <= 1.0
    rows_fts = [{**r, "score_kind": "bm25"} for r in rows]
    assert cc.runner.abstain_metrics(rows_fts)["auroc"] is None
    mixed = [{**r, "score_kind": "rerank" if i % 2 else "fused"} for i, r in enumerate(rows)]
    assert "mixed score kinds" in cc.runner.abstain_metrics(mixed)["auroc_note"]


# --- the decision ---------------------------------------------------------------------------------------

N_CLUSTERS = 40


def _golden(tmp_path: Path) -> Path:
    cases = [{"query": f"q{i}-{j}", "expect_slug": f"s{i}", "bucket": "ops", "split": "dev" if i < 30 else "test",
              **({"expect_block": "b"} if j == 0 else {})}
             for i in range(N_CLUSTERS) for j in range(2)]
    cases += [{"query": f"none {i}", "expect_none": True, "bucket": "abstain", "split": "dev" if i < 6 else "test"} for i in range(8)]
    p = tmp_path / "golden.yaml"
    p.write_text(yaml.safe_dump({"cases": cases}, sort_keys=False))
    return p


def _run(tmp_path: Path, name: str, hit, block=lambda i: True, score=lambda i: 1.0, fallbacks=0, cfg=None, with_qid=True) -> Path:
    """A saved runner output. hit(i) says whether the cases of cluster i hit at rank 1."""
    rows = []
    for i in range(N_CLUSTERS):
        for j in range(2):
            q = f"q{i}-{j}"
            rows.append({"query": q, "expect_slug": f"s{i}", "rank": 1 if hit(i) else None, "bucket": "ops",
                         "expect_block": "b" if j == 0 else None, "block_hit": block(i) if j == 0 else None,
                         "top_score": score(i), "score_kind": "fused"})
    rows += [{"query": f"none {i}", "expect_slug": None, "abstain": True, "rank": None, "bucket": "abstain",
              "top_score": 0.0, "score_kind": "fused"} for i in range(8)]
    if with_qid:
        import hashlib
        for r in rows:
            key = [r["query"], r["expect_slug"], [], bool(r.get("abstain"))]
            r["qid"] = hashlib.sha1(json.dumps(key).encode()).hexdigest()[:12]
    summary = {"mode": "hybrid", "config": {"rerank": "off", "dedup": 1.0, "stopwords": False, "fusion": "rrf", **(cfg or {})},
               "rerank_fallbacks": fallbacks, "embed": {"model": "m"}}
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"summary": summary, "results": rows}))
    return p


@pytest.fixture
def files(tmp_path):
    g = _golden(tmp_path)
    base = _run(tmp_path, "base", hit=lambda i: i % 2 == 0)
    return g, {
        "base": base,
        "better": _run(tmp_path, "better", hit=lambda i: i % 2 == 0 or i % 3 == 1, cfg={"rerank": "on"}),
        "better_simpler": _run(tmp_path, "better_simpler", hit=lambda i: i % 2 == 0 or i % 3 == 1, with_qid=False),
        "blocks_drop": _run(tmp_path, "blocks_drop", hit=lambda i: True, block=lambda i: i % 2 == 0),
        "auroc_falls": _run(tmp_path, "auroc_falls", hit=lambda i: True, score=lambda i: -1.0),
        "fell_back": _run(tmp_path, "fell_back", hit=lambda i: True, fallbacks=3, cfg={"rerank": "on"}),
        "test_only_gain": _run(tmp_path, "test_only_gain", hit=lambda i: i % 2 == 0 or i >= 30),
    }


def _choose(cc, g, paths, out, *names):
    args = ["--golden", str(g), "choose", "--baseline-name", "base", "--out", str(out), "--iterations", "2000",
            "--candidates", *[f"{n}={paths[n]}" for n in names]]
    return cc.main(args)


def test_the_rule_picks_the_simplest_of_the_best_and_says_why_others_lost(cc, files, tmp_path, capsys):
    g, paths = files
    out = tmp_path / "decision.json"
    assert _choose(cc, g, paths, out, *paths) == 0
    d = json.loads(out.read_text())
    assert d["chosen"] == "better_simpler", "same recall@3 and MRR as 'better', one knob fewer"
    t = d["table"]
    assert t["better"]["eligible"] and t["better"]["knobs"] == ["rerank"] and t["better_simpler"]["knobs"] == []
    assert not t["blocks_drop"]["eligible"] and any("block precision drops" in r for r in t["blocks_drop"]["reasons"])
    assert not t["auroc_falls"]["eligible"] and any("AUROC falls" in r for r in t["auroc_falls"]["reasons"])
    assert not t["fell_back"]["eligible"] and any("fell back" in r for r in t["fell_back"]["reasons"])
    assert not t["test_only_gain"]["eligible"], "a gain that lives only in test clusters is invisible on dev"
    assert t["test_only_gain"]["delta"]["recall@3"] == 0.0
    assert d["n_cases"] == 66 and d["golden_sha256"] and set(d["code_sha256"]) == {"choose_config.py", "run_retrieval_evals.py", "assign_splits.py"}
    text = capsys.readouterr().out
    assert cc.DECISION_RULE in text and cc.DEV_NOTE in text and "CHOSEN: better_simpler" in text


def test_dev_deltas_are_exact(cc, files):
    g, paths = files
    golden = cc.Golden(g)
    t = cc.evaluate_candidates({"base": paths["base"], "better": paths["better"]}, "base", golden, "dev", iterations=500)
    # dev clusters 0..29. Base hits the 15 even clusters. 'better' adds the odd clusters with i % 3 == 1: 1, 7, 13, 19, 25. 20 of 30.
    assert t["base"]["metrics"]["recall@3"] == 0.5 and t["better"]["metrics"]["recall@3"] == round(20 / 30, 4)
    assert t["better"]["delta"]["recall@3"] == round(5 / 30, 4)
    lo, hi = t["better"]["ci90"]["recall@3"]
    assert 0 < lo <= 5 / 30 <= hi


def test_no_eligible_candidate_keeps_the_baseline(cc, files, tmp_path):
    g, paths = files
    out = tmp_path / "d.json"
    assert _choose(cc, g, paths, out, "base", "blocks_drop", "fell_back") == 0
    assert json.loads(out.read_text())["chosen"] == "base"


def test_a_file_from_another_golden_set_is_refused(cc, files, tmp_path):
    g, paths = files
    run = json.loads(paths["better"].read_text())
    run["results"] = run["results"][1:]
    paths["short"] = tmp_path / "short.json"
    paths["short"].write_text(json.dumps(run))
    with pytest.raises(SystemExit, match="have no row"):
        _choose(cc, g, paths, tmp_path / "d.json", "base", "short")


# --- the test report -------------------------------------------------------------------------------------

def _report(cc, g, decision, chosen, *extra):
    return cc.main(["--golden", str(g), "report-test", "--decision", str(decision), "--chosen", chosen, "--baseline-name", "base", *extra])


def test_report_test_needs_a_decision_and_reports_once(cc, files, tmp_path, capsys):
    g, paths = files
    decision = tmp_path / "decision.json"
    assert _report(cc, g, decision, "better_simpler") == 1
    assert "Run `choose` on dev first" in capsys.readouterr().out
    assert _choose(cc, g, paths, decision, "base", "better", "better_simpler") == 0
    assert _report(cc, g, decision, "better") == 1, "only the decided configuration may be reported"
    assert _report(cc, g, decision, "better_simpler") == 0
    report = tmp_path / "test-report.json"
    r = json.loads(report.read_text())
    assert r["chosen"] == "better_simpler" and r["split"] == "test" and r["n_cases"] == 22 and r["force_log"] == []
    # test clusters 30..39: base hits 30, 32, 34, 36, 38. better_simpler adds 31 and 37. 7 of 10.
    assert r["table"]["better_simpler"]["metrics"]["recall@3"] == 0.7 and r["table"]["base"]["metrics"]["recall@3"] == 0.5
    before = report.read_text()
    assert _report(cc, g, decision, "better_simpler") == 0 and report.read_text() == before, "a repeat reprints, it does not recompute"
    assert _report(cc, g, decision, "better", "--force") == 0
    r = json.loads(report.read_text())
    assert r["chosen"] == "better" and len(r["force_log"]) == 1 and r["force_log"][0]["previously_reported"] == "better_simpler"


def test_report_test_refuses_a_changed_candidate_file(cc, files, tmp_path, capsys):
    g, paths = files
    decision = tmp_path / "decision.json"
    assert _choose(cc, g, paths, decision, "base", "better_simpler") == 0
    paths["better_simpler"].write_text(paths["better_simpler"].read_text().replace('"rank": 1', '"rank": 2', 1))
    assert _report(cc, g, decision, "better_simpler") == 1
    assert "changed since the decision" in capsys.readouterr().out


def test_report_test_ledger_blocks_a_second_report_under_another_out(cc, files, tmp_path, capsys):
    """MET-009: a different --out must not reopen the test side. The ledger sits beside the decision, keyed by golden_sha256."""
    g, paths = files
    decision = tmp_path / "decision.json"
    assert _choose(cc, g, paths, decision, "base", "better", "better_simpler") == 0
    assert _report(cc, g, decision, "better_simpler") == 0
    gsha = json.loads(decision.read_text())["golden_sha256"]
    ledger = json.loads((tmp_path / "test-ledger.json").read_text())
    assert list(ledger) == [gsha]
    other = tmp_path / "other-report.json"
    capsys.readouterr()
    assert _report(cc, g, decision, "better_simpler", "--out", str(other)) == 0
    assert "already reported" in capsys.readouterr().out and not other.exists(), "a repeat under a new --out reprints, it does not recompute"
    assert _report(cc, g, decision, "better", "--out", str(other)) == 1, "another configuration under a new --out is refused"
    assert not other.exists()
    assert _report(cc, g, decision, "better", "--force", "--out", str(other)) == 0
    assert json.loads((tmp_path / "test-ledger.json").read_text())[gsha]["chosen"] == "better"


def test_report_test_checks_the_hashes_before_the_already_reported_short_circuit(cc, files, tmp_path, capsys):
    """ENG-005: a changed candidate file or golden refuses even when test was already reported."""
    g, paths = files
    decision = tmp_path / "decision.json"
    assert _choose(cc, g, paths, decision, "base", "better_simpler") == 0
    assert _report(cc, g, decision, "better_simpler") == 0
    capsys.readouterr()
    original = paths["better_simpler"].read_text()
    paths["better_simpler"].write_text(original.replace('"rank": 1', '"rank": 2', 1))
    assert _report(cc, g, decision, "better_simpler") == 1
    assert "changed since the decision" in capsys.readouterr().out
    paths["better_simpler"].write_text(original)
    g.write_text(g.read_text().replace("q1-0", "q1-0 edited", 1))
    assert _report(cc, g, decision, "better_simpler") == 1
    assert "golden.yaml changed" in capsys.readouterr().out
