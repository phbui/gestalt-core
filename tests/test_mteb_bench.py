"""mteb_bench.py: the gestalt hybrid as an mteb search model, its ModelMeta, and the smoke switch. Stubs only, no model, no download."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from conftest import HashEncoder

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "tools"))
pytest.importorskip("sqlite_vec")
mteb = pytest.importorskip("mteb")
import mteb_bench as mb  # noqa: E402
from mteb.models.models_protocols import EncoderProtocol, SearchProtocol  # noqa: E402


class StubModel(HashEncoder):
    """Unnormalised bag-of-words counts, as the pipeline tests were written against."""
    normalize = False


CORPUS = [{"id": f"c{i}", "title": f"t{i}", "text": f"word{i} common filler{i}"} for i in range(20)]
QUERIES = [{"id": "q7", "text": "word7 filler7"}, {"id": "q3", "text": "word3"}]


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(mb.harness, "_model", StubModel())
    return mb.GestaltHybridSearch(StubModel())


def test_object_is_an_mteb_search_model_and_not_an_encoder(model):
    assert isinstance(model, SearchProtocol)
    assert not isinstance(model, EncoderProtocol)


def test_index_then_search_returns_ranked_scores_by_query_id(model):
    model.index(CORPUS)
    res = model.search(QUERIES, top_k=5)
    assert set(res) == {"q7", "q3"}
    assert list(res["q7"])[0] == "c7" and list(res["q3"])[0] == "c3"
    scores = list(res["q7"].values())
    assert len(scores) <= 5 and scores == sorted(scores, reverse=True)


def test_search_before_index_is_an_error(model):
    with pytest.raises(ValueError):
        model.search(QUERIES, top_k=5)


def test_max_docs_caps_the_indexed_corpus(monkeypatch):
    monkeypatch.setattr(mb.harness, "_model", StubModel())
    m = mb.GestaltHybridSearch(StubModel(), max_docs=6)
    m.index(CORPUS)
    assert m.ids == [f"c{i}" for i in range(6)]


def test_model_meta_fields():
    meta = mb.model_meta()
    assert meta.name == "phbui/gestalt-hybrid"
    assert meta.revision == mb.ec.MODEL_REVISION
    assert meta.training_datasets == set()
    assert meta.public_training_code is None and meta.public_training_data is None
    assert meta.embed_dim == mb.ec.EMBED_DIM and meta.adapted_from == mb.ec.MODEL_NAME
    assert meta.open_weights is None


def test_default_tasks_are_the_ten_mteb_eng_v2_retrieval_tasks():
    tasks = mb.default_tasks()
    assert len(tasks) == 10 and "ArguAna" in tasks and "SCIDOCS" in tasks


class _Result:
    task_results: list = []


def _run(monkeypatch, tmp_path, argv, during=None, rc=0):
    seen = {}

    def fake_eval(model, tasks, cache=None, **kw):
        seen.update(model=model, tasks=[t.metadata.name for t in tasks], cache=cache)
        if during:
            during(model)
        return _Result()

    monkeypatch.setattr(mteb, "evaluate", fake_eval)
    monkeypatch.setattr(mb.harness, "get_model", lambda: StubModel())
    monkeypatch.setattr(mb, "subset_relevant", lambda tasks: {})  # reading the judgements would download the task
    monkeypatch.setattr(sys, "argv", ["mteb_bench.py", *argv, "--output-folder", str(tmp_path)])
    assert mb.main() == rc
    return seen


def test_smoke_runs_scifact_capped_without_a_cache_and_says_so(monkeypatch, tmp_path, capsys):
    seen = _run(monkeypatch, tmp_path, ["--smoke"])
    assert seen["tasks"] == ["SciFact"] and seen["cache"] is None
    assert seen["model"].max_docs == mb.SMOKE_DOCS
    assert "SMOKE RUN" in capsys.readouterr().err
    assert (tmp_path / "SUBSET.txt").exists()


def test_full_run_uses_the_cache_and_no_subset_marker(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, ["--tasks", "SciFact"])
    assert seen["cache"] is not None and seen["model"].max_docs is None
    assert not (tmp_path / "SUBSET.txt").exists()


def test_full_run_removes_a_stale_subset_marker(monkeypatch, tmp_path):
    (tmp_path / "SUBSET.txt").write_text("old")
    _run(monkeypatch, tmp_path, ["--tasks", "SciFact"])
    assert not (tmp_path / "SUBSET.txt").exists()


def test_reference_is_the_public_repo():
    assert mb.REFERENCE == "https://github.com/phbui/gestalt-core"


def test_rerank_model_default_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("GESTALT_RERANK_MODEL", "qwen3-0.6b")
    seen = _run(monkeypatch, tmp_path, ["--tasks", "SciFact", "--rerank", "on"])
    assert seen["model"].rerank == {"model": "qwen3-0.6b", "depth": 40}


class _Meta:
    def __init__(self, name):
        self.name = name


def _fake_rerank(fallback="none"):
    return lambda q, rows, alias: (list(reversed(rows)), None if fallback != "none" else list(range(len(rows))), {"fallback": fallback})


def test_reranked_head_is_not_clipped_to_ten(monkeypatch, tmp_path):
    monkeypatch.setattr(mb.engine.gestalt_rank, "rerank_rows", _fake_rerank())
    corpus = [{"id": f"c{i}", "title": "", "text": f"word{i % 5} common filler{i}"} for i in range(60)]
    m = mb.GestaltHybridSearch(StubModel(), rerank={"model": "bge", "depth": 15}, work_dir=tmp_path)
    m.index(corpus, task_metadata=_Meta("T"), hf_split="test", hf_subset="default")
    plain = mb.GestaltHybridSearch(StubModel(), work_dir=tmp_path / "plain")
    plain.index(corpus, task_metadata=_Meta("T"), hf_split="test", hf_subset="default")
    q = [{"id": "q1", "text": "word3 common"}]
    got = list(m.search(q, task_metadata=_Meta("T"), hf_split="test", hf_subset="default", top_k=30)["q1"])
    hyb = list(plain.search(q, task_metadata=_Meta("T"), hf_split="test", hf_subset="default", top_k=30)["q1"])
    assert len(got) == 30
    assert got[:15] == list(reversed(hyb[:15]))  # all 15 reranked documents lead, not 10
    assert got[15:] == [d for d in hyb if d not in got[:15]][:15]


def test_a_task_with_rerank_fallbacks_is_void_and_exits_3(monkeypatch, tmp_path):
    monkeypatch.setattr(mb.engine.gestalt_rank, "rerank_rows", _fake_rerank("AcceleratorError"))

    def during(model):
        model.index(CORPUS, task_metadata=_Meta("SciFact"), hf_split="test", hf_subset="default")
        model.search(QUERIES, task_metadata=_Meta("SciFact"), hf_split="test", hf_subset="default", top_k=5)

    cached = tmp_path / "cached.json"
    cached.write_text("{}")
    monkeypatch.setattr(mteb.ResultCache, "get_task_result_path", lambda self, task, meta: cached)
    _run(monkeypatch, tmp_path, ["--tasks", "SciFact", "--rerank", "on"], during=during, rc=3)
    assert json.loads((tmp_path / "RERANK_INVALID.json").read_text())["rerank_fallback_queries"] == {"SciFact": 2}
    assert not cached.exists() and (tmp_path / "cached.json.void-rerank").exists()  # the next run recomputes the task


def test_a_second_search_of_the_same_task_runs_no_finished_query(tmp_path):
    class Counting(StubModel):
        queries = 0

        def encode(self, texts, **kw):
            if isinstance(texts, str):
                Counting.queries += 1
            return super().encode(texts, **kw)

    for _ in range(2):
        m = mb.GestaltHybridSearch(Counting(), work_dir=tmp_path)
        m.index(CORPUS, task_metadata=_Meta("T"), hf_split="test", hf_subset="default")
        res = m.search(QUERIES, task_metadata=_Meta("T"), hf_split="test", hf_subset="default", top_k=5)
    assert Counting.queries == len(QUERIES) and list(res["q7"])[0] == "c7"


def test_a_subset_keeps_the_relevant_documents(tmp_path):
    m = mb.GestaltHybridSearch(StubModel(), max_docs=6, work_dir=tmp_path, relevant={("T", "default", "test"): {"c15"}})
    m.index(CORPUS, task_metadata=_Meta("T"), hf_split="test", hf_subset="default")
    assert m.ids == ["c0", "c1", "c2", "c3", "c4", "c15"]
