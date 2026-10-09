"""The retrieval bank and gate: golden hash, fail-closed check, named configurations, splits (EVAL-004, EVAL-005, EVAL-007). Hermetic."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load  # noqa: E402
from test_eval_runner_pipeline import GOLDEN, SECTIONS  # noqa: E402
from test_harness_fidelity import _build_fixture_db, _StubModel, _StubReranker  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import gestalt_rank  # noqa: E402


@pytest.fixture
def runner():
    return _load("run_retrieval_evals_gate", "evals/retrieval/run_retrieval_evals.py")


@pytest.fixture
def splits():
    return _load("assign_splits_gate", "evals/retrieval/assign_splits.py")


@pytest.fixture
def world(tmp_path, monkeypatch, runner):
    """A fixture index, a golden file and an empty bank, with the runner pointed at all three."""
    for k in ("GESTALT_RERANK_MODEL", "GESTALT_RERANK_DEPTH", "GESTALT_RERANK_MAXCHARS", "GESTALT_SLUG_DECAY",
              "GESTALT_FTS_STOPWORDS", "GESTALT_FUSION", "GESTALT_FUSION_ALPHA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GESTALT_RERANK", "off")
    db = tmp_path / "idx.db"
    _build_fixture_db(db, SECTIONS)
    golden = tmp_path / "golden.yaml"
    golden.write_text(GOLDEN)
    monkeypatch.setattr(runner, "DB_PATH", db)
    monkeypatch.setattr(runner, "DEFAULT_DB_PATH", db)
    monkeypatch.setattr(runner, "GOLDEN", golden)
    monkeypatch.setattr(runner, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(runner, "get_model", lambda: _StubModel())
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _StubReranker())
    return tmp_path


def _main(runner, monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["run", *argv])
    try:
        runner.main()
    except SystemExit as e:
        return e.code or 0
    return 0


# --- the golden hash --------------------------------------------------------------------------------

CASES = [
    {"query": "a", "expect_slug": "x", "bucket": "ops", "source": "authored"},
    {"query": "b", "expect_slug": "y", "expect_any": ["z", "w"], "bucket": "paper"},
    {"query": "c", "expect_none": True, "bucket": "abstain"},
]


def test_golden_hash_ignores_order_and_bookkeeping_fields(runner):
    h = runner.golden_sha256(CASES, [["x", "x-log"]])
    assert h == runner.golden_sha256(list(reversed(CASES)), [["x-log", "x"]])
    assert h == runner.golden_sha256([{**CASES[0], "source": "other"}, *CASES[1:]], [["x", "x-log"]])
    assert h == runner.golden_sha256([CASES[0], {**CASES[1], "expect_any": ["w", "z"]}, CASES[2]], [["x", "x-log"]])


@pytest.mark.parametrize("edit", [
    lambda cs, fs: ([{**cs[0], "query": "a2"}, *cs[1:]], fs),
    lambda cs, fs: ([{**cs[0], "expect_slug": "x2"}, *cs[1:]], fs),
    lambda cs, fs: ([{**cs[0], "bucket": "voice"}, *cs[1:]], fs),
    lambda cs, fs: ([{**cs[0], "split": "test"}, *cs[1:]], fs),
    lambda cs, fs: ([{**cs[0], "expect_block": "b"}, *cs[1:]], fs),
    lambda cs, fs: (cs[:2], fs),
    lambda cs, fs: (cs, [["x", "x-log", "x-log-2"]]),
], ids=["query", "target", "bucket", "split", "block", "case-removed", "family"])
def test_golden_hash_moves_with_every_scored_field(runner, edit):
    fams = [["x", "x-log"]]
    assert runner.golden_sha256(*edit(CASES, fams)) != runner.golden_sha256(CASES, fams)


def test_case_ids_separate_two_cases_with_one_query(runner):
    a = runner.case_id({"query": "q", "expect_slug": "s1"})
    assert a != runner.case_id({"query": "q", "expect_slug": "s2"})
    assert a == runner.case_id({"query": "q", "expect_slug": "s1", "split": "dev", "bucket": "ops"})
    assert runner.case_id({"id": "fixed", "query": "q"}) == "fixed"


# --- bank and fail-closed check ------------------------------------------------------------------------

def test_bank_then_check_passes_and_the_bank_carries_hash_and_count(world, runner, monkeypatch, capsys):
    assert _main(runner, monkeypatch, "--baseline", "bank", "--config-name", "default") == 0
    bank = json.loads((world / "baseline.json").read_text())
    entry = bank["configs"]["default"]
    cases, fams = runner.load_cases()
    assert entry["golden_sha256"] == runner.golden_sha256(cases, fams) and entry["n_cases"] == len(cases) == 10
    assert entry["config"]["rerank"] == "off" and entry["rerank_fallbacks"] == 0
    assert (world / "baseline-results.json").exists()
    capsys.readouterr()
    assert _main(runner, monkeypatch, "--baseline", "check") == 0
    assert "No retrieval regression" in capsys.readouterr().out


def test_save_is_the_old_spelling_of_bank_default(world, runner, monkeypatch):
    assert _main(runner, monkeypatch, "--baseline", "save") == 0
    assert list(json.loads((world / "baseline.json").read_text())["configs"]) == ["default"]


def test_check_without_a_bank_fails(world, runner, monkeypatch, capsys):
    assert _main(runner, monkeypatch, "--baseline", "check") == 1
    assert "No baseline yet" in capsys.readouterr().out


def _bank(world, runner, monkeypatch):
    assert _main(runner, monkeypatch, "--baseline", "bank") == 0


@pytest.mark.parametrize("breakage,needle", [
    ("golden", "golden.yaml changed"),
    ("results", "results file is missing"),
    ("count", "the bank holds 9 cases"),
    ("old-bank", "no golden_sha256"),
    ("mode", "run mode fts differs"),
])
def test_check_fails_closed_when_not_comparable(world, runner, monkeypatch, capsys, breakage, needle):
    _bank(world, runner, monkeypatch)
    bank_path = world / "baseline.json"
    argv = ["--baseline", "check"]
    if breakage == "golden":
        (world / "golden.yaml").write_text(GOLDEN.replace("how often are backups rotated", "how often do backups rotate"))
    elif breakage == "results":
        (world / "baseline-results.json").unlink()
    elif breakage == "count":
        bank = json.loads(bank_path.read_text())
        bank["configs"]["default"]["n_cases"] = 9
        bank_path.write_text(json.dumps(bank))
    elif breakage == "old-bank":
        bank = json.loads(bank_path.read_text())["configs"]["default"]
        bank.pop("golden_sha256")
        bank_path.write_text(json.dumps(bank))
    elif breakage == "mode":
        argv += ["--mode", "fts"]
    capsys.readouterr()
    assert _main(runner, monkeypatch, *argv) == 1
    out = capsys.readouterr().out
    assert needle in out and "REGRESSION OR NOT COMPARABLE" in out
    assert _main(runner, monkeypatch, *argv, "--allow-rebank") == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_check_fails_on_a_knob_given_on_the_command_line(world, runner, monkeypatch, capsys):
    _bank(world, runner, monkeypatch)
    capsys.readouterr()
    assert _main(runner, monkeypatch, "--baseline", "check", "--dedup", "0.5") == 1
    assert "config drift: dedup=0.5" in capsys.readouterr().out


def test_check_pins_the_banked_knobs_and_ignores_the_environment(world, runner, monkeypatch, capsys):
    """A stray environment knob does not change what the gate measures: it reproduces the banked configuration."""
    _bank(world, runner, monkeypatch)
    monkeypatch.setenv("GESTALT_FTS_STOPWORDS", "on")
    monkeypatch.setenv("GESTALT_RERANK", "on")
    assert _main(runner, monkeypatch, "--baseline", "check") == 0


# --- named configurations ----------------------------------------------------------------------------

def _as_hub(monkeypatch, cuda: bool = True):
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: cuda)


def test_default_bank_refuses_the_rerank(world, runner, monkeypatch, capsys):
    assert _main(runner, monkeypatch, "--baseline", "bank", "--rerank", "on") == 1
    assert "conflicts with configuration default" in capsys.readouterr().out
    assert not (world / "baseline.json").exists()


def test_hub_bank_refuses_without_cuda(world, runner, monkeypatch, capsys):
    _as_hub(monkeypatch, cuda=False)
    assert _main(runner, monkeypatch, "--baseline", "bank", "--config-name", "hub") == 1
    assert "no CUDA device" in capsys.readouterr().out


def test_both_configurations_bank_and_gate(world, runner, monkeypatch, capsys):
    _bank(world, runner, monkeypatch)
    _as_hub(monkeypatch)
    monkeypatch.setenv("GESTALT_RERANK_MODEL", "qwen3-0.6b")
    monkeypatch.setattr(gestalt_rank, "resolve_alias", lambda alias=None: alias or "qwen3-0.6b")
    assert _main(runner, monkeypatch, "--baseline", "bank", "--config-name", "hub") == 0
    bank = json.loads((world / "baseline.json").read_text())["configs"]
    assert set(bank) == {"default", "hub"}
    assert bank["hub"]["config"]["rerank"] == "on" and bank["hub"]["config"]["rerank_model"] == "qwen3-0.6b"
    assert bank["hub"]["config"]["rerank_depth"] == gestalt_rank.rerank_depth()
    assert bank["default"]["config"]["rerank"] == "off", "banking hub leaves default alone"
    assert (world / "baseline-results-hub.json").exists()
    capsys.readouterr()
    assert _main(runner, monkeypatch, "--baseline", "check") == 0
    out = capsys.readouterr().out
    assert "== configuration default ==" in out and "== configuration hub ==" in out


def test_hub_check_without_cuda_fails_unless_allowed(world, runner, monkeypatch, capsys):
    _bank(world, runner, monkeypatch)
    _as_hub(monkeypatch)
    assert _main(runner, monkeypatch, "--baseline", "bank", "--config-name", "hub") == 0
    _as_hub(monkeypatch, cuda=False)
    capsys.readouterr()
    assert _main(runner, monkeypatch, "--baseline", "check") == 1
    assert "hub: cannot run here: no CUDA device" in capsys.readouterr().out
    assert _main(runner, monkeypatch, "--baseline", "check", "--allow-missing-hub-config") == 0
    out = capsys.readouterr().out
    assert "SKIPPED: no CUDA device" in out and "No retrieval regression" in out


def test_hub_check_refuses_a_run_whose_rerank_fell_back(world, runner, monkeypatch, capsys):
    _bank(world, runner, monkeypatch)
    _as_hub(monkeypatch)
    assert _main(runner, monkeypatch, "--baseline", "bank", "--config-name", "hub") == 0
    monkeypatch.setattr(runner, "_reranker_unavailable", lambda alias: None)

    def boom(alias=None):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(gestalt_rank, "get_reranker", boom)
    assert _main(runner, monkeypatch, "--baseline", "check", "--config-name", "hub") == 3


# --- splits in the runner -------------------------------------------------------------------------------

def test_split_selects_tagged_cases_and_is_recorded(world, runner, splits, monkeypatch, capsys):
    assert splits.main(["--golden", str(world / "golden.yaml")]) == 0
    tagged, _ = runner.load_cases()
    n_dev = sum(c["split"] == "dev" for c in tagged)
    assert 0 < n_dev < len(tagged)
    for split, n in (("dev", n_dev), ("test", len(tagged) - n_dev), ("all", len(tagged))):
        s = runner.evaluate("hybrid", split=split)["summary"]
        assert s["split"] == split and s["n_cases"] == n
    assert _main(runner, monkeypatch, "--baseline", "bank", "--split", "dev") == 1
    assert "Refusing to bank a split" in capsys.readouterr().out


def test_split_needs_tags(world, runner):
    with pytest.raises(SystemExit, match="carry no split tag"):
        runner.evaluate("hybrid", split="dev")
    assert runner.evaluate("hybrid")["summary"]["split"] == "all", "the default reads every case, tagged or not"


def test_summary_prints_the_population_numbers(world, runner):
    """EVAL-010: the numbers the old docstring hard-coded are computed per run."""
    s = runner.evaluate("hybrid")["summary"]
    # 8 answerable cases over 5 targets. retrieval-fusion and its log share a family, gpu-fleet accepts backup-policy,
    # so the components are {retrieval-fusion, -log}, {fts-tokenisation}, {embedding-prefixes}, {gpu-fleet, backup-policy}.
    assert s["population"] == {"answerable": 8, "targets": 5, "clusters": 4, "mean_queries_per_cluster": 2.0}


def test_check_exits_non_zero_when_every_configuration_was_skipped(world, runner, monkeypatch, capsys):
    """ENG-008: --allow-missing-hub-config with only hub configs banked gated nothing and used to print 'No retrieval regression.'"""
    _bank(world, runner, monkeypatch)
    _as_hub(monkeypatch)
    assert _main(runner, monkeypatch, "--baseline", "bank", "--config-name", "hub") == 0
    bank_path = world / "baseline.json"
    bank = json.loads(bank_path.read_text())
    del bank["configs"]["default"]
    bank_path.write_text(json.dumps(bank))
    _as_hub(monkeypatch, cuda=False)
    capsys.readouterr()
    assert _main(runner, monkeypatch, "--baseline", "check", "--allow-missing-hub-config") == 1
    out = capsys.readouterr().out
    assert "NOTHING GATED" in out and "No retrieval regression" not in out
