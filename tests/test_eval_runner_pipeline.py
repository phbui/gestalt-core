"""The retrieval runner ranks through gestalt_rank.hybrid_search and still reports the numbers it reported before (EVAL-001, EVAL-002).

The equivalence test loads the runner as it stood before it was rewired (commit PRE_REFACTOR in this repository's
history) and runs both versions end to end on one synthetic index with a stub encoder. Every per-case rank, block hit
and top score must agree, and so must recall@1, recall@3 and MRR, which are also pinned as numbers. Hermetic: no model,
no network, CPU only.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load  # noqa: E402
from test_harness_fidelity import _build_fixture_db, _StubModel, _StubReranker  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import gestalt_rank  # noqa: E402

PRE_REFACTOR = "dd675f2373733ddc0ad829e6572f8c48d744b6b1"
RUNNER_PATH = "evals/retrieval/run_retrieval_evals.py"

SECTIONS = [
    {"id": i, "slug": slug, "heading": head, "block_id": block, "content": text, "anchors": anchors,
     "file_path": f"knowledge/{slug}.md", "content_hash": f"h{i}"}
    for i, (slug, head, block, anchors, text) in enumerate([
        ("retrieval-fusion", "Fusion overview", "overview", "", "Reciprocal rank fusion combines BM25 and vector search scores over ranked lists."),
        ("retrieval-fusion", "Fusion constant", "constant", "k-value", "The constant K in rank fusion damps the top ranks of the vector search lists."),
        ("retrieval-fusion-log", "Fusion history", "history", "", "Fusion of ranked lists was introduced for metasearch before vector search existed."),
        ("fts-tokenisation", "FTS5 tokenisation", "quoting", "", "FTS5 must OR-join quoted terms, so a phrase query never silences the lexical leg."),
        ("embedding-prefixes", "Asymmetric prefixes", "prefixes", "", "The query prefix and the document prefix pair up in the embedding space."),
        ("embedding-prefixes", "Model revision", "revision", "pinned", "Pin the embedding model revision so the index and the query agree on vectors."),
        ("gpu-fleet", "Hub GPU", "gpu", "", "The hub carries the only GPU, so the reranker loads there in half precision."),
        ("gpu-fleet", "Leaf nodes", "leaves", "", "A leaf node has no GPU and runs the full-text leg of search only."),
        ("backup-policy", "Nightly backup", "nightly", "", "Backups run nightly to an external disk and rotate after thirty days."),
        ("backup-policy", "Restore drill", "restore", "drill", "A restore drill once a month proves the backup can be read back."),
    ])
]

GOLDEN = """
families:
  - [retrieval-fusion, retrieval-fusion-log]
cases:
  - query: "how does rank fusion combine vector search lists"
    expect_slug: retrieval-fusion
    bucket: ops
    expect_block: overview
  - query: "what damps the top ranks in fusion"
    expect_slug: retrieval-fusion
    bucket: ops
    expect_block: k-value
  - query: "when was metasearch fusion introduced"
    expect_slug: retrieval-fusion
    bucket: ops
  - query: "why must quoted terms be OR joined"
    expect_slug: fts-tokenisation
    bucket: ops
    hard: true
  - query: "which prefix goes on the query side"
    expect_slug: embedding-prefixes
    bucket: research
    expect_block: prefixes
  - query: "pin the model revision for vectors"
    expect_slug: embedding-prefixes
    bucket: research
  - query: "where does the reranker load"
    expect_slug: gpu-fleet
    bucket: projects
    expect_any: [backup-policy]
  - query: "how often are backups rotated"
    expect_slug: backup-policy
    bucket: projects
    expect_block: drill
    hard: true
  - query: "boiling point of ethanol"
    expect_none: true
    bucket: abstain
  - query: "capital city of mongolia"
    expect_none: true
    bucket: abstain
"""

# recall@1, recall@3, MRR of the hybrid default run on this fixture, from the pre-refactor runner.
PINNED_HYBRID_DEFAULT = (0.875, 1.0, 0.9167)


def _old_runner(tmp_path: Path):
    """The runner at PRE_REFACTOR, loaded as a module. Skips where that commit is absent (a shallow clone or the public export)."""
    try:
        src = subprocess.run(["git", "-C", str(REPO), "show", f"{PRE_REFACTOR}:{RUNNER_PATH}"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip(f"commit {PRE_REFACTOR[:8]} is not in this clone")
    path = tmp_path / "run_retrieval_evals_pre_refactor.py"
    path.write_text(src)
    spec = importlib.util.spec_from_file_location("run_retrieval_evals_pre_refactor", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_retrieval_evals_pre_refactor"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def new_runner():
    return _load("run_retrieval_evals_pipeline", RUNNER_PATH)


@pytest.fixture
def index(tmp_path, monkeypatch):
    for k in ("GESTALT_RERANK_MODEL", "GESTALT_RERANK_DEPTH", "GESTALT_RERANK_MAXCHARS", "GESTALT_SLUG_DECAY",
              "GESTALT_FTS_STOPWORDS", "GESTALT_FUSION", "GESTALT_FUSION_ALPHA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GESTALT_RERANK", "off")
    db = tmp_path / "idx.db"
    _build_fixture_db(db, SECTIONS)
    golden = tmp_path / "golden.yaml"
    golden.write_text(GOLDEN)
    return db, golden


def _point(mod, monkeypatch, db, golden):
    monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(mod, "GOLDEN", golden)
    monkeypatch.setattr(mod, "get_model", lambda: _StubModel())


def _rows(out):
    return [(r["query"], r["rank"], r["block_hit"], r["got"], r["top_score"]) for r in out["results"]]


def _headline(s):
    return (s["recall"], s["mrr"], s["block_precision"], s["hard"], s["easy"], s["overall"], s["family_rescued"],
            {b: m for b, m in s["buckets"].items() if b != "abstain"})


KNOBS = [
    ("hybrid", {}),
    ("hybrid", {"decay": 0.5}),
    ("hybrid", {"stopwords": True}),
    ("hybrid", {"fusion": "convex", "alpha": 0.3}),
    ("hybrid", {"rerank": True}),
    ("fts", {}),
    ("fts", {"decay": 0.0}),
]


@pytest.mark.parametrize("mode,knobs", KNOBS, ids=[f"{m}-{'-'.join(f'{k}={v}' for k, v in kn.items()) or 'default'}" for m, kn in KNOBS])
def test_runner_reports_the_same_numbers_as_before_the_refactor(tmp_path, monkeypatch, index, new_runner, mode, knobs):
    old = _old_runner(tmp_path)
    db, golden = index
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _StubReranker())
    outs = []
    for mod in (old, new_runner):
        _point(mod, monkeypatch, db, golden)
        outs.append(mod.evaluate(mode, **knobs))
    before, after = outs
    assert _rows(after) == _rows(before)
    assert _headline(after["summary"]) == _headline(before["summary"])
    assert [(r["query"], r["rank"]) for r in after["fts_results"]] == [(r["query"], r["rank"]) for r in before["fts_results"]]
    if mode == "hybrid":
        assert after["summary"]["buckets"]["abstain"]["auroc"] == before["summary"]["buckets"]["abstain"]["auroc"]


def test_the_hybrid_default_numbers_are_pinned(monkeypatch, index, new_runner):
    """Holds without git history too: the numbers the pre-refactor runner reported on this fixture."""
    db, golden = index
    _point(new_runner, monkeypatch, db, golden)
    s = new_runner.evaluate("hybrid")["summary"]
    assert (s["recall"]["@1"], s["recall"]["@3"], s["mrr"]) == PINNED_HYBRID_DEFAULT


def test_unset_rerank_resolves_as_the_server_does(monkeypatch, index, new_runner):
    """EVAL-001: rerank None means the server's rule. auto reranks on the hub with CUDA, and nowhere else."""
    db, golden = index
    _point(new_runner, monkeypatch, db, golden)
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _StubReranker())
    monkeypatch.setenv("GESTALT_RERANK", "auto")
    conn = new_runner.get_db()
    for hub, cuda, want in ((True, True, True), (True, False, False), (False, True, False)):
        monkeypatch.setattr(gestalt_rank, "is_hub", lambda hub=hub: hub)
        monkeypatch.setattr(gestalt_rank, "cuda_available", lambda cuda=cuda: cuda)
        found = new_runner.search_result(conn, "where does the reranker load", 5)
        assert (found.rerank is not None) is want
        assert new_runner.resolve_opts()["rerank"] is want, "evaluate and search resolve auto by one rule"
        assert (new_runner.search_result(conn, "where does the reranker load", 5, rerank=False).rerank is None)


def test_search_and_search_scored_keep_their_signatures(monkeypatch, index, new_runner):
    db, golden = index
    _point(new_runner, monkeypatch, db, golden)
    conn = new_runner.get_db()
    rows, top = new_runner.search_scored(conn, "rank fusion", 3, "hybrid", rerank=False, decay=1.0)
    assert len(rows) == 3 and isinstance(top, float)
    assert [r["id"] for r in new_runner.search(conn, "rank fusion", 3, mode="fts")] == \
           [r["id"] for r in new_runner.search_scored(conn, "rank fusion", 3, "fts")[0]]
    assert new_runner.K_RRF == gestalt_rank.RRF_K


def _boom(alias=None):
    raise ImportError("no sentence_transformers")


def test_fallbacks_are_counted_per_run(monkeypatch, index, new_runner):
    """EVAL-002: every query whose requested rerank did not run is counted."""
    db, golden = index
    _point(new_runner, monkeypatch, db, golden)
    monkeypatch.setattr(gestalt_rank, "get_reranker", _boom)
    s = new_runner.evaluate("hybrid", rerank=True)["summary"]
    assert s["config"]["rerank"] == "on" and s["rerank_fallbacks"] == s["n_cases"] == 10
    monkeypatch.setattr(gestalt_rank, "get_reranker", lambda alias=None: _StubReranker())
    assert new_runner.evaluate("hybrid", rerank=True)["summary"]["rerank_fallbacks"] == 0
    assert new_runner.evaluate("hybrid", rerank=False)["summary"]["rerank_fallbacks"] == 0


def _fallback_run(n: int) -> dict:
    rows = [{"qid": f"q{i}", "query": f"q{i}", "expect_slug": f"s{i % 4}", "rank": 1, "expect_block": None, "block_hit": None}
            for i in range(12)]
    return {"summary": {"mode": "hybrid", "config": {"rerank": "on"}, "rerank_fallbacks": n}, "results": rows, "fts_results": rows}


@pytest.mark.parametrize("which", ["before", "after"])
def test_compare_files_refuses_a_run_with_fallbacks(tmp_path, monkeypatch, capsys, new_runner, which):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(_fallback_run(0)))
    bad.write_text(json.dumps(_fallback_run(2)))
    pair = [str(bad), str(good)] if which == "before" else [str(good), str(bad)]
    monkeypatch.setattr(sys, "argv", ["run", "--compare-files", *pair])
    with pytest.raises(SystemExit) as e:
        new_runner.main()
    assert e.value.code == new_runner.EXIT_RERANK_FALLBACK == 3
    assert "fell back to the fusion order on 2 queries" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["run", "--compare-files", str(good), str(good)])
    new_runner.main()
    assert "recall@3 moved" in capsys.readouterr().out


def test_compare_and_bank_refuse_a_run_with_fallbacks(tmp_path, monkeypatch, capsys, new_runner):
    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps(_fallback_run(0)))
    monkeypatch.setattr(new_runner, "evaluate", lambda *a, **k: _fallback_run(1))
    monkeypatch.setattr(new_runner, "report", lambda o: None)
    monkeypatch.setattr(new_runner, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(new_runner, "_reranker_unavailable", lambda alias: None)
    for argv in (["--rerank", "on", "--compare", str(saved)], ["--baseline", "bank", "--config-name", "hub"]):
        monkeypatch.setattr(sys, "argv", ["run", *argv])
        with pytest.raises(SystemExit) as e:
            new_runner.main()
        assert e.value.code == 3, argv
    assert not (tmp_path / "baseline.json").exists(), "a refused bank writes nothing"


class _CountingModel:
    def __init__(self):
        self.calls = 0

    def encode(self, text, **_kw):
        self.calls += 1
        return _StubModel().encode(text)


def test_set_model_installs_the_encoder_and_throttles_it(monkeypatch, new_runner):
    sleeps = []
    monkeypatch.setenv("GESTALT_GPU_DUTY", "0.5")
    monkeypatch.setattr(gestalt_rank, "duty_sleep", lambda busy: sleeps.append(busy))
    m = _CountingModel()
    new_runner.set_model(m)
    assert new_runner.get_model() is m
    new_runner._embed_query("q")
    assert m.calls == 1 and len(sleeps) == 1, "encode runs once and is followed by the duty pause"
    new_runner.set_model(m)
    new_runner._embed_query("q")
    assert len(sleeps) == 2, "a second set_model does not wrap twice"


def test_get_model_throttles_the_model_it_loads(monkeypatch, new_runner):
    import types
    built = _CountingModel()
    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = lambda *a, **k: built
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setattr(new_runner, "_model", None)
    seen = []
    monkeypatch.setattr(gestalt_rank, "throttle_calls", lambda obj, method: seen.append((obj, method)))
    assert new_runner.get_model() is built and seen == [(built, "encode")]


def test_stall_watchdog_aborts_a_search_past_the_timeout_and_stays_quiet_otherwise(capsys):
    """F1 of 2026-10-08: a wedged search ran 18 minutes with no output. The watchdog ends the process with code 4."""
    import time

    import run_retrieval_evals as R

    exits = []
    dog = R.StallWatchdog(0.2, exit_fn=exits.append, poll=0.05)
    dog.arm("case quick")
    dog.disarm()
    time.sleep(0.4)
    assert exits == []
    dog.arm("case slow")
    time.sleep(0.6)
    dog.close()
    assert exits == [R.EXIT_QUERY_STALL]
    assert "ABORT search case slow" in capsys.readouterr().err


def test_progress_prints_every_n_cases_and_at_the_end(capsys):
    import run_retrieval_evals as R

    p = R.Progress(25, every=10, seconds=10**6)
    for n in range(26):
        p.tick(n)
    err = capsys.readouterr().err
    assert "10/25 cases" in err and "20/25 cases" in err and "25/25 cases" in err and ": 5/25 cases" not in err


def test_query_timeout_knob_parses_and_refuses_junk(monkeypatch):
    import pytest

    import run_retrieval_evals as R

    monkeypatch.delenv("GESTALT_EVAL_QUERY_TIMEOUT", raising=False)
    assert R.query_timeout() == 300.0
    monkeypatch.setenv("GESTALT_EVAL_QUERY_TIMEOUT", "0")
    assert R.query_timeout() == 0.0
    monkeypatch.setenv("GESTALT_EVAL_QUERY_TIMEOUT", "abc")
    with pytest.raises(SystemExit):
        R.query_timeout()
