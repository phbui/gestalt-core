"""beir_bench.py: every public BEIR set by id, the corpus cap and its subset label, and the legacy two-set path left alone.

No model loads and no dataset downloads. A hashing stub stands in for the embedder and a fake `ir_datasets` serves tiny corpora.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import namedtuple
from pathlib import Path

import numpy as np
import pytest
from conftest import HashEncoder, make_fake_ir_datasets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "tools"))
pytest.importorskip("sqlite_vec")
pytest.importorskip("ir_datasets")  # the harness imports it at module level; without the bench requirements these tests skip, as the README says
import beir_bench as bb  # noqa: E402

Doc = namedtuple("Doc", "doc_id title text")
Query = namedtuple("Query", "query_id text")
Qrel = namedtuple("Qrel", "query_id doc_id relevance")

# queries_excluded_no_positive joined the result file with EVAL-012: queries without a positive judgement are counted, not scored.
# query_log_header and model joined with MET-002: a finished dataset JSON records what --resume must match before it is reused.
LEGACY_RESULT_KEYS = {"dataset", "split", "documents", "queries", "queries_excluded_no_positive", "smoke_subset", "metric", "systems", "tests",
                      "seconds", "query_ids", "query_log_header", "model"}


class StubModel(HashEncoder):
    """Unnormalised bag-of-words counts, as the pipeline tests were written against."""
    normalize = False


def fake_ir_datasets(loaded: list[str]):
    docs = [Doc(f"d{i}", f"title{i}", f"alpha{i} beta{i} gamma") for i in range(30)]
    queries = [Query("q1", "alpha25 beta25"), Query("q2", "alpha27 beta27"), Query("q3", "alpha28")]
    return make_fake_ir_datasets(docs, queries, [Qrel("q1", "d25", 1), Qrel("q2", "d27", 2), Qrel("q3", "d28", -1)], loaded)


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    loaded: list[str] = []
    monkeypatch.setitem(sys.modules, "ir_datasets", fake_ir_datasets(loaded))
    monkeypatch.setattr(bb.harness, "_model", StubModel())
    monkeypatch.setattr(bb, "default_work_dir", lambda out: tmp_path / "work")  # keep every test's work dir inside its own tmp_path
    return loaded


def test_all_public_is_the_fifteen_sets():
    ids = bb.expand_datasets([], True)
    assert len(ids) == 15 and len(set(ids)) == 15
    for want in ("beir/msmarco", "beir/trec-covid", "beir/nfcorpus", "beir/nq", "beir/hotpotqa", "beir/fiqa", "beir/arguana",
                 "beir/webis-touche2020", "beir/cqadupstack", "beir/quora", "beir/dbpedia-entity", "beir/scidocs", "beir/fever",
                 "beir/climate-fever", "beir/scifact"):
        assert want in ids


def test_named_datasets_come_first_and_never_repeat():
    ids = bb.expand_datasets(["beir/fiqa", "beir/scifact"], True)
    assert ids[:2] == ["beir/fiqa", "beir/scifact"] and len(ids) == 15
    assert bb.expand_datasets(["beir/fiqa"], False) == ["beir/fiqa"]


def test_cqadupstack_has_twelve_subforums_each_without_a_test_suffix():
    assert len(bb.CQADUPSTACK) == 12
    assert all(bb.QRELS_SUFFIX[f"beir/cqadupstack/{c}"] == "" for c in bb.CQADUPSTACK)


def test_select_capped_keeps_relevant_and_stops_at_the_cap():
    docs = [Doc(f"d{i}", "", "x") for i in range(1000)]
    kept = bb.select_capped(iter(docs), {"d900", "d950"}, 10)
    ids = [d.doc_id for d in kept]
    assert "d900" in ids and "d950" in ids and len(ids) == 10
    assert ids[:8] == [f"d{i}" for i in range(8)]  # the fill is corpus order


def test_select_capped_exceeds_the_cap_rather_than_drop_a_relevant_doc():
    docs = [Doc(f"d{i}", "", "x") for i in range(50)]
    kept = bb.select_capped(iter(docs), {f"d{i}" for i in range(40, 50)}, 5)
    assert {d.doc_id for d in kept} == {f"d{i}" for i in range(40, 50)}


def test_select_capped_does_not_read_past_the_last_needed_doc():
    read = []

    def stream():
        for i in range(10_000):
            read.append(i)
            yield Doc(f"d{i}", "", "x")

    bb.select_capped(stream(), {"d3"}, 6)
    assert len(read) < 20


def test_capped_run_is_labelled_subset_and_warns(stubbed, tmp_path, capsys):
    r = bb.run_dataset("beir/fiqa", tmp_path, None, 8, StubModel(), None, max_docs=5)
    assert r["subset"] is True and r["max_docs"] == 5
    assert r["documents"] == 5
    err = capsys.readouterr().err
    assert "SUBSET" in err and "Do not quote" in err
    assert json.loads((tmp_path / "beir-fiqa.json").read_text())["subset"] is True


def test_uncapped_run_has_exactly_the_legacy_keys(stubbed, tmp_path):
    r = bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None)
    assert set(r) == LEGACY_RESULT_KEYS
    assert set(r["systems"]) == {"bm25", "dense", "hybrid"}


def test_qrels_come_from_the_right_split(stubbed, tmp_path):
    for name in ("beir/scifact", "beir/fiqa", "beir/arguana", "beir/msmarco", "beir/webis-touche2020"):
        stubbed.clear()
        bb.run_dataset(name, tmp_path, None, 8, StubModel(), None)
        assert stubbed[0] == name + {"beir/arguana": "", "beir/msmarco": "/dev", "beir/webis-touche2020": "/v2"}.get(name, "/test")


def test_a_query_without_a_positive_judgement_is_counted_not_scored(stubbed, tmp_path):
    # q3's only judgement is -1. nDCG has no ideal ranking for it, so pytrec_eval and mteb drop such a query (EVAL-012).
    r = bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None)
    assert r["queries"] == 2 and r["queries_excluded_no_positive"] == 1 and "q3" not in r["query_ids"]
    assert all(0.0 <= x <= 1.0 for x in r["systems"]["hybrid"]["per_query"])


def _summary(names, score):
    return {"results": {n: {"systems": {"hybrid": {"ndcg10": score}}} for n in names}}


def test_summary_gets_a_mean_and_the_subset_label_when_capped():
    s = bb.finish_summary(_summary(["beir/fiqa", "beir/scidocs"], 0.5), ["beir/fiqa", "beir/scidocs"], 1000)
    assert s["mean_ndcg10"] == {"hybrid": 0.5} and s["subset"] is True and s["max_docs"] == 1000 and "must not be quoted" in s["note"]


def test_summary_mean_over_uncapped_new_sets_is_not_labelled_subset():
    s = bb.finish_summary({"results": {"beir/fiqa": {"systems": {"hybrid": {"ndcg10": 0.2}}}, "beir/scidocs": {"systems": {"hybrid": {"ndcg10": 0.4}}}}},
                          ["beir/fiqa", "beir/scidocs"], None)
    assert s["mean_ndcg10"]["hybrid"] == 0.3 and s["subset"] is False and "note" not in s


def test_legacy_summary_is_untouched():
    base = _summary(list(bb.LEGACY_DATASETS), 0.7)
    assert bb.finish_summary(json.loads(json.dumps(base)), list(bb.LEGACY_DATASETS), None) == base


def test_argparse_defaults_snapshot(monkeypatch):
    class Caught(Exception):
        pass

    def grab(self, *a, **k):
        raise Caught(self)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", grab)
    with pytest.raises(Caught) as e:
        bb.main()
    parser = e.value.args[0]
    defaults = {a.dest: a.default for a in parser._actions if a.dest != "help"}
    assert defaults["datasets"] == ["beir/scifact", "beir/nfcorpus"]
    assert defaults["limit_docs"] is None and defaults["batch_size"] == 64 and defaults["rerank"] == "off"
    assert defaults["rerank_depth"] == 40 and defaults["fusion"] is None and defaults["alpha"] is None and defaults["embed_profile"] is None
    assert defaults["all_public"] is False and defaults["max_docs"] is None
    legacy = {"datasets", "out", "limit_docs", "batch_size", "embed_profile", "rerank", "rerank_model", "rerank_depth", "fusion", "alpha"}
    assert legacy <= set(defaults) and set(defaults) - legacy == {"all_public", "max_docs", "resume", "work_dir", "engine", "block_size", "rebuild",
                                                                  "fusion_grid"}
    assert defaults["fusion_grid"] is None  # no grid unless asked, so the default systems are the legacy ones
    assert defaults["resume"] is False and defaults["work_dir"] is None and defaults["engine"] == "auto"
    assert defaults["block_size"] == 20_000 and defaults["rebuild"] is False


def _main_with_fake_reranker(monkeypatch, stubbed, tmp_path, fallback: str) -> tuple[int, dict]:
    """main() over the stub corpus with gestalt_rank replaced by a reranker whose every call reports `fallback`."""
    monkeypatch.setattr(bb.engine.gestalt_rank, "rerank_rows",
                        lambda query, rows, alias: (list(rows), None, {"model": alias, "n": len(rows), "fallback": fallback}))
    monkeypatch.setattr(bb.harness, "get_model", lambda: StubModel())
    monkeypatch.setattr(bb, "environment", lambda model, rerank, block_size=None, **kw: {})
    monkeypatch.setattr(sys, "argv", ["beir_bench.py", "--datasets", "beir/scifact", "--out", str(tmp_path / "out"), "--rerank", "on",
                                      "--work-dir", str(tmp_path / "work")])
    rc = bb.main()
    return rc, json.loads((tmp_path / "out" / "summary.json").read_text())


def test_a_reranked_run_with_fallbacks_is_void_and_exits_3(monkeypatch, stubbed, tmp_path, capsys):
    rc, summary = _main_with_fake_reranker(monkeypatch, stubbed, tmp_path, "AcceleratorError")
    assert rc == 3
    assert summary["rerank_invalid"] == {"beir/scifact": 2}  # q3 has no positive judgement and is not scored (EVAL-012)
    assert summary["results"]["beir/scifact"]["rerank_fallback_queries"] == 2
    err = capsys.readouterr().err
    assert "void" in err and "Do not quote" in err


def test_a_reranked_run_without_fallbacks_exits_0(monkeypatch, stubbed, tmp_path):
    rc, summary = _main_with_fake_reranker(monkeypatch, stubbed, tmp_path, "none")
    assert rc == 0
    assert "rerank_invalid" not in summary
    assert summary["results"]["beir/scifact"]["rerank_fallback_queries"] == 0


def test_resume_reuses_a_matching_result_and_refuses_a_foreign_one(stubbed, tmp_path, capsys):
    ident = bb.run_identity(None, "rrf", 0.5)
    first = bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None)
    loaded_before = len(stubbed)
    again = bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None, resume=True)
    assert again == first and len(stubbed) == loaded_before  # nothing was loaded or recomputed
    assert "resumed from" in capsys.readouterr().err
    # a run that asks for the reranker must not reuse a result without that system
    assert bb.load_finished(tmp_path, "beir/scifact", True, None, None, ident) is None
    # a capped run must not reuse an uncapped file, nor the reverse
    assert bb.load_finished(tmp_path, "beir/scifact", False, None, 5, ident) is None
    assert bb.load_finished(tmp_path, "beir/scifact", False, 300, None, ident) is None
    # a result with reranker fallbacks is never reused
    path = tmp_path / "beir-scifact.json"
    d = json.loads(path.read_text()); d["systems"]["hybrid_rerank"] = d["systems"]["hybrid"]; d["rerank_fallback_queries"] = 2
    path.write_text(json.dumps(d))
    assert bb.load_finished(tmp_path, "beir/scifact", True, None, None, ident) is None
    d["rerank_fallback_queries"] = 0; path.write_text(json.dumps(d))
    assert bb.load_finished(tmp_path, "beir/scifact", True, None, None, ident) is not None
    (tmp_path / "beir-scifact.json").write_text("{not json")
    assert bb.load_finished(tmp_path, "beir/scifact", False, None, None, ident) is None


# --- MET-002 / ENG-001: a finished result is reused only under the configuration that wrote it ------------------------------------------------------------------------

RERANK = {"model": "bge", "depth": 40}


def _finished_reranked(tmp_path, monkeypatch):
    """A dataset JSON as a reranked run would have written it, plus the matching identity."""
    monkeypatch.delenv("GESTALT_RERANK_INSTRUCTION", raising=False)
    monkeypatch.delenv("GESTALT_RERANK_MAXCHARS", raising=False)
    bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None)
    ident = bb.run_identity(RERANK, "rrf", 0.5)
    path = tmp_path / "beir-scifact.json"
    d = json.loads(path.read_text())
    d["systems"]["hybrid_rerank"] = d["systems"]["hybrid"]
    d["rerank_fallback_queries"] = 0
    d["query_log_header"], d["model"] = ident["query_log_header"], ident["model"]
    path.write_text(json.dumps(d))
    return path, ident


def test_a_dataset_json_records_its_header_and_model_block(stubbed, tmp_path):
    r = bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None)
    on_disk = json.loads((tmp_path / "beir-scifact.json").read_text())
    assert on_disk["query_log_header"] == r["query_log_header"] and r["query_log_header"]["corpus_fingerprint"]
    assert r["model"] == {"embedding_model": bb.ec.MODEL_NAME, "embedding_revision": bb.ec.MODEL_REVISION, "embed_profile": bb.ec.PROFILE,
                          "embedding_load_path": bb.ec.LOAD_PATH, "reranker_alias": None, "reranker_model_id": None}


def test_the_identity_of_a_reranked_run_names_alias_resolved_id_depth_instruction_and_maxchars(monkeypatch):
    monkeypatch.delenv("GESTALT_RERANK_INSTRUCTION", raising=False)
    ident = bb.run_identity(RERANK, "rrf", 0.5)
    assert ident["model"]["reranker_alias"] == "bge" and ident["model"]["reranker_model_id"] == "BAAI/bge-reranker-v2-m3"
    rr = ident["query_log_header"]["rerank"]
    assert rr["model"] == "bge" and rr["depth"] == 40 and rr["maxchars"] == bb.harness.gestalt_rank.rerank_maxchars()
    assert rr["instruction"] == bb.harness.gestalt_rank.qwen_instruction()


def test_a_matching_result_is_reused(stubbed, tmp_path, monkeypatch):
    _path, ident = _finished_reranked(tmp_path, monkeypatch)
    assert bb.load_finished(tmp_path, "beir/scifact", True, None, None, ident) is not None


CHANGES = {
    "reranker alias": lambda mp: (mp, {"rerank": {"model": "qwen3-0.6b", "depth": 40}}),
    "rerank depth": lambda mp: (mp, {"rerank": {"model": "bge", "depth": 20}}),
    "fusion mode": lambda mp: (mp, {"fusion": "convex"}),
    "rerank instruction": lambda mp: (mp.setenv("GESTALT_RERANK_INSTRUCTION", "Given a claim, retrieve the abstract that supports it"), {}),
    "rerank max chars": lambda mp: (mp.setenv("GESTALT_RERANK_MAXCHARS", "1000"), {}),
    "embed profile": lambda mp: (mp.setattr(bb.ec, "PROFILE", "qwen3-4b"), {}),
    "embedding model revision": lambda mp: (mp.setattr(bb.ec, "MODEL_REVISION", "0" * 40), {}),
    "embedding load path": lambda mp: (mp.setattr(bb.ec, "LOAD_PATH", "remote-code" if bb.ec.LOAD_PATH == "native" else "native"), {}),
    "resolved reranker id": lambda mp: (mp.setitem(bb.harness.gestalt_rank.RERANK_MODELS, "bge", "BAAI/another-reranker"), {}),
    "code hashes": lambda mp: (mp.setattr(bb, "code_hashes", lambda: {"evals/retrieval/beir_bench.py": "f" * 64}), {}),
}


@pytest.mark.parametrize("what", sorted(CHANGES))
def test_a_changed_field_means_the_finished_result_is_not_reused(what, stubbed, tmp_path, monkeypatch):
    path, ident = _finished_reranked(tmp_path, monkeypatch)
    assert bb.load_finished(tmp_path, "beir/scifact", True, None, None, ident) is not None
    _, kw = CHANGES[what](monkeypatch)
    now = bb.run_identity(kw.get("rerank", RERANK), kw.get("fusion", "rrf"), 0.5)
    assert bb.load_finished(tmp_path, "beir/scifact", True, None, None, now) is None, what


def test_a_changed_alpha_means_the_finished_convex_result_is_not_reused(stubbed, tmp_path, monkeypatch):
    path, _ = _finished_reranked(tmp_path, monkeypatch)
    convex = bb.run_identity(RERANK, "convex", 0.4)
    d = json.loads(path.read_text())
    d["query_log_header"], d["model"] = convex["query_log_header"], convex["model"]
    path.write_text(json.dumps(d))
    assert bb.load_finished(tmp_path, "beir/scifact", True, None, None, convex) is not None
    assert bb.load_finished(tmp_path, "beir/scifact", True, None, None, bb.run_identity(RERANK, "convex", 0.6)) is None


@pytest.mark.parametrize("drop", ["model", "query_log_header", "header.alpha", "header.fusion", "header.rerank", "header.stopwords", "header.rrf_k"])
def test_a_json_without_the_recorded_fields_counts_as_a_mismatch(drop, stubbed, tmp_path, monkeypatch):
    path, ident = _finished_reranked(tmp_path, monkeypatch)
    d = json.loads(path.read_text())
    if drop.startswith("header."):
        del d["query_log_header"][drop.split(".")[1]]
    else:
        del d[drop]
    path.write_text(json.dumps(d))
    assert bb.load_finished(tmp_path, "beir/scifact", True, None, None, ident) is None


def test_a_resumed_run_after_a_fusion_change_recomputes(stubbed, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GESTALT_FUSION", raising=False)
    bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None)
    capsys.readouterr()
    monkeypatch.setenv("GESTALT_FUSION", "convex")
    r = bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None, resume=True, rebuild=True)
    assert "resumed from" not in capsys.readouterr().err
    assert r["query_log_header"]["fusion"] == "convex"


# --- the resumable engine against the old in-memory path ------------------------------------------------------------------------


def _old_bootstrap_ci(values):
    """The pure-Python bootstrap beir_bench shipped before EVAL-013, kept as the reference."""
    import random

    rng = random.Random(bb.SEED)
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(bb.BOOTSTRAP))
    return [round(means[int(0.025 * bb.BOOTSTRAP)], 4), round(means[int(0.975 * bb.BOOTSTRAP) - 1], 4)]


def _old_permutation_p(a, b):
    """The pure-Python sign-flip test beir_bench shipped before EVAL-013, kept as the reference."""
    import random

    diffs = [x - y for x, y in zip(a, b)]
    obs = sum(diffs) / len(diffs)
    rng = random.Random(bb.SEED)
    extreme = 0
    for _ in range(bb.PERMUTATIONS):
        flipped = sum(d if rng.random() < 0.5 else -d for d in diffs) / len(diffs)
        if abs(flipped) >= abs(obs) - 1e-15:
            extreme += 1
    return {"mean_difference": round(obs, 4), "p_value": round((extreme + 1) / (bb.PERMUTATIONS + 1), 5), "permutations": bb.PERMUTATIONS}


@pytest.mark.parametrize("n,seed", [(37, 1), (120, 2)])
def test_vectorised_statistics_equal_the_pure_python_reference_exactly(n, seed):
    rng = np.random.default_rng(seed)
    a = [round(float(x), 5) for x in rng.random(n)]
    b = [round(float(x), 5) if i % 3 else a[i] for i, x in enumerate(rng.random(n))]  # ties give zero differences
    assert bb.permutation_p(a, b) == _old_permutation_p(a, b)
    assert bb.bootstrap_ci(a) == _old_bootstrap_ci(a)


def _old_dense_only(db, model, query, k):
    """The dense leg beir_bench ran before the engine, kept as the reference."""
    qv = bb.ec.postprocess(model.encode(bb.ec.QUERY_PREFIX + query, convert_to_numpy=True))
    rows = db.execute("SELECT id FROM sections_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance", (qv.astype("float32").tobytes(), k)).fetchall()
    return [r["id"] for r in rows]


def corpus_dataset(n_docs: int, n_queries: int, loaded: list | None = None):
    """A fake ir_datasets with many lexical and dense ties: few distinct words, duplicated documents."""
    words = [f"w{i}" for i in range(25)]
    docs = []
    for i in range(n_docs):
        k = 3 + (i * 7) % 9
        body = " ".join(words[(i * j + j) % 25] for j in range(1, k))
        docs.append(Doc(f"doc{i}", f"t{i % 5}", body if i % 13 else docs[i - 1].text if i else body))
    queries = [Query(f"q{j}", " ".join(words[(j * m) % 25] for m in range(1, 2 + j % 4))) for j in range(n_queries)]
    qrels = [Qrel(f"q{j}", f"doc{(j * 31) % n_docs}", 1 + j % 2) for j in range(n_queries)] + [Qrel("q0", "doc3", 0)]

    mod = make_fake_ir_datasets(docs, queries, qrels, loaded)
    return mod, docs, queries


def read_runs(out: Path, name: str, systems=("bm25", "dense", "hybrid")) -> dict:
    runs = {}
    for s in systems:
        for line in (out / f"{name.replace('/', '-')}.{s}.run").read_text().splitlines():
            qid, _, doc, *_ = line.split()
            runs.setdefault(s, {}).setdefault(qid, []).append(doc)
    return runs


@pytest.mark.parametrize("fusion,dense", [("rrf", "vec"), ("convex", "vec"), ("rrf", "exact")])
def test_engine_rankings_equal_the_old_in_memory_path(monkeypatch, tmp_path, fusion, dense):
    """No published number may move: bm25, dense and hybrid rankings must be the old path's, id for id."""
    mod, docs, queries = corpus_dataset(400, 40)
    monkeypatch.setitem(sys.modules, "ir_datasets", mod)
    model = StubModel()
    monkeypatch.setattr(bb.harness, "_model", model)
    monkeypatch.setenv("GESTALT_RERANK", "off")
    monkeypatch.setenv("GESTALT_FUSION", fusion)
    monkeypatch.setenv("GESTALT_FUSION_ALPHA", "0.4")
    monkeypatch.setenv("GESTALT_BENCH_DENSE", dense)
    ids = [d.doc_id for d in docs]
    db = bb.build_db(ids, [bb.engine.doc_text(d.title, d.text) for d in docs], model, 8)
    old = {"bm25": {}, "dense": {}, "hybrid": {}}
    for q in queries:
        old["bm25"][q.query_id] = [ids[int(r["slug"][1:])] for r in bb.harness.search(db, q.text, limit=bb.TOPK, mode="fts")]
        old["hybrid"][q.query_id] = [ids[int(r["slug"][1:])] for r in bb.harness.search(db, q.text, limit=bb.TOPK, mode="hybrid")]
        old["dense"][q.query_id] = [ids[i] for i in _old_dense_only(db, model, q.text, bb.TOPK)]
    bb.run_dataset("beir/fiqa", tmp_path / "out", None, 8, model, None, work_dir=tmp_path / "work", block_size=64,
                   dense_mode=bb.engine.dense_mode(400))
    new = read_runs(tmp_path / "out", "beir/fiqa")
    scored = json.loads((tmp_path / "out" / "beir-fiqa.json").read_text())["query_ids"]
    for s in old:
        assert {q: new[s][q] for q in scored} == {q: old[s][q] for q in scored if old[s][q]}, s


class CountingStub(StubModel):
    """StubModel that records every document and query it encodes, and can die when asked to encode a given document."""

    def __init__(self, die_on=None):
        super().__init__()
        self.docs: list[str] = []
        self.queries: list[str] = []
        self.die_on = die_on

    def encode(self, texts, **kw):
        batch = [texts] if isinstance(texts, str) else list(texts)
        if self.die_on and any(self.die_on in t for t in batch):
            raise RuntimeError("killed")
        out = super().encode(texts, **kw)
        for t in batch:
            (self.queries if t.startswith(bb.ec.QUERY_PREFIX) else self.docs).append(t)
        return out


def _result_without_timings(out: Path) -> dict:
    r = json.loads((out / "beir-fiqa.json").read_text())
    r.pop("seconds")
    return r


def test_a_killed_run_resumes_to_the_identical_result_and_redoes_no_finished_work(monkeypatch, tmp_path):
    mod, docs, queries = corpus_dataset(120, 30)
    monkeypatch.setitem(sys.modules, "ir_datasets", mod)
    monkeypatch.setenv("GESTALT_RERANK", "off")
    monkeypatch.setattr(bb.harness, "_model", StubModel())
    run = lambda model, tag: bb.run_dataset("beir/fiqa", tmp_path / f"out{tag}", None, 8, model, None,  # noqa: E731
                                            work_dir=tmp_path / f"work{tag}", block_size=16)
    run(StubModel(), "-clean")

    # kill 1: inside the encode callback, when block 2 (documents 32 to 47) starts
    first = CountingStub(die_on=f" {docs[32].text}")
    with pytest.raises(RuntimeError, match="killed"):
        run(first, "")
    assert len(first.docs) == 32
    # kill 2: inside the query loop, on the seventh fusion
    real_fuse, calls = bb.engine.fuse_pools, []

    def dying_fuse(*a, **k):
        calls.append(1)
        if len(calls) == 7:
            raise RuntimeError("killed")
        return real_fuse(*a, **k)

    monkeypatch.setattr(bb.engine, "fuse_pools", dying_fuse)
    second = CountingStub()
    with pytest.raises(RuntimeError, match="killed"):
        run(second, "")
    assert sorted(second.docs) == sorted(bb.ec.DOC_PREFIX + bb.engine.doc_text(d.title, d.text) for d in docs[32:])  # no finished block again
    calls.clear()
    monkeypatch.setattr(bb.engine, "fuse_pools", lambda *a, **k: (calls.append(1), real_fuse(*a, **k))[1])
    third = CountingStub()
    run(third, "")
    assert third.docs == []
    n_scored = len(json.loads((tmp_path / "out" / "beir-fiqa.json").read_text())["query_ids"])
    assert len(calls) == n_scored - 6  # the six finished queries were not run again
    log = next((tmp_path / "work" / "beir-fiqa").glob("queries-*.jsonl")).read_text().splitlines()
    assert len(log) == 1 + 3 * n_scored  # one line per (system, query), never a duplicate
    assert _result_without_timings(tmp_path / "out") == _result_without_timings(tmp_path / "out-clean")


def test_cqadupstack_aggregate_pools_queries_and_sums_fallbacks():
    def sub(vals_h, vals_b, fb):
        return {"documents": 10, "queries": len(vals_h), "rerank_fallback_queries": fb, "queries_excluded_no_positive": 1,
                "systems": {s: {"ndcg10": round(sum(v) / len(v), 4), "per_query": v}
                            for s, v in (("bm25", vals_b), ("dense", vals_b), ("hybrid", vals_h), ("hybrid_rerank", vals_h))}}
    subs = {bb.CQADUPSTACK[0]: sub([1.0, 0.0], [0.5, 0.0], 0), bb.CQADUPSTACK[1]: sub([1.0, 1.0, 1.0, 0.0], [0.0, 0.0, 0.0, 0.0], 3)}
    r = bb.cqadupstack_aggregate(subs, None, None, True)
    assert r["systems"]["hybrid"]["ndcg10"] == round((0.5 + 0.75) / 2, 4)  # the BEIR headline: unweighted mean of forum means
    assert r["systems"]["hybrid"]["ndcg10_pooled"] == round(4 / 6, 4)
    assert len(r["systems"]["hybrid"]["ci95_bootstrap"]) == 2
    assert set(r["tests"]) == {"hybrid_vs_bm25", "hybrid_vs_dense", "rerank_vs_hybrid"}
    assert r["rerank_fallback_queries"] == 3 and r["queries_excluded_no_positive"] == 2


def test_a_cqadupstack_forum_with_fallbacks_voids_the_aggregate(monkeypatch, stubbed, tmp_path):
    monkeypatch.setattr(bb.engine.gestalt_rank, "rerank_rows",
                        lambda q, rows, alias: (list(rows), None, {"fallback": "AcceleratorError" if alias else "none"}))
    monkeypatch.setattr(bb.harness, "get_model", lambda: StubModel())
    monkeypatch.setattr(bb, "environment", lambda model, rerank, block_size=None, **kw: {})
    monkeypatch.setattr(sys, "argv", ["beir_bench.py", "--datasets", "beir/cqadupstack", "--out", str(tmp_path / "out"), "--rerank", "on",
                                      "--work-dir", str(tmp_path / "work")])
    assert bb.main() == 3
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["rerank_invalid"] == {"beir/cqadupstack": 24}
    assert "ci95_bootstrap" in summary["results"]["beir/cqadupstack"]["systems"]["hybrid"]


@pytest.mark.parametrize("flags,smoke,subset", [(["--max-docs", "5"], False, True), (["--limit-docs", "28"], True, False)])
def test_summary_carries_the_subset_labels(monkeypatch, stubbed, tmp_path, flags, smoke, subset):
    monkeypatch.setattr(bb.harness, "get_model", lambda: StubModel())
    monkeypatch.setattr(bb, "environment", lambda model, rerank, block_size=None, **kw: {})
    monkeypatch.setattr(sys, "argv", ["beir_bench.py", "--datasets", "beir/fiqa", "--out", str(tmp_path / "out"), "--work-dir", str(tmp_path / "w"), *flags])
    assert bb.main() == 0
    s = json.loads((tmp_path / "out" / "summary.json").read_text())
    r = s["results"]["beir/fiqa"]
    assert s["smoke_subset"] is smoke and r["smoke_subset"] is smoke
    assert s["subset"] is subset and r.get("subset", False) is subset
    if subset:
        assert s["max_docs"] == 5 and r["max_docs"] == 5


def test_the_work_dir_is_git_ignored(stubbed, tmp_path):
    bb.run_dataset("beir/fiqa", tmp_path / "out", None, 8, StubModel(), None, work_dir=tmp_path / "w")
    assert (tmp_path / "w" / ".gitignore").read_text() == "*\n"
    assert json.loads((tmp_path / "w" / "beir-fiqa" / "status.json").read_text())["phase"] == "done"


# --- ndcg_at_10, build_db and code_hashes ---------------------------------------------------------


def test_ndcg_at_10_with_graded_relevance_matches_the_hand_computed_value():
    # Judgements: a=3, b=2, d=1, c=0. Ranking: b, x, a, d.
    # DCG  = 2/log2(2) + 0/log2(3) + 3/log2(4) + 1/log2(5) = 2 + 0 + 1.5 + 0.430677 = 3.930677
    # IDCG = 3/log2(2) + 2/log2(3) + 1/log2(4)             = 3 + 1.261860 + 0.5      = 4.761860
    # nDCG = 3.930677 / 4.761860 = 0.825450
    ranked = ["b", "x", "a", "d"]
    rel = {"a": 3, "b": 2, "c": 0, "d": 1}
    assert bb.ndcg_at_10(ranked, rel) == pytest.approx(0.825450, abs=1e-6)


def test_ndcg_at_10_is_one_for_the_ideal_order_and_zero_without_a_hit():
    rel = {"a": 3, "b": 2, "d": 1}
    assert bb.ndcg_at_10(["a", "b", "d", "x"], rel) == pytest.approx(1.0)
    assert bb.ndcg_at_10(["x", "y"], rel) == 0.0
    assert bb.ndcg_at_10(["a"], {}) == 0.0
    assert bb.ndcg_at_10([], rel) == 0.0


def test_ndcg_at_10_ignores_everything_below_rank_ten():
    # The only relevant document sits at rank 11. DCG is 0, and the ideal list is cut at ten as well.
    ranked = [f"x{i}" for i in range(10)] + ["a"]
    assert bb.ndcg_at_10(ranked, {"a": 1}) == 0.0
    # Eleven judged documents: the ideal DCG stops at ten of them, so a perfect top ten still scores 1.
    rel = {f"d{i}": 1 for i in range(11)}
    assert bb.ndcg_at_10([f"d{i}" for i in range(10)], rel) == pytest.approx(1.0)


def test_build_db_stores_one_row_per_text_in_all_three_tables():
    from conftest import HashEncoder

    ids = ["docA", "docB", "docC"]
    texts = ["red apple pie", "green pear tart", "blue plum cake"]
    db = bb.build_db(ids, texts, HashEncoder(), 2)
    meta = db.execute("SELECT id, slug, content, file_path FROM sections_meta ORDER BY id").fetchall()
    assert [(r["id"], r["slug"], r["content"], r["file_path"]) for r in meta] == [(0, "d0", texts[0], "docA"), (1, "d1", texts[1], "docB"), (2, "d2", texts[2], "docC")]
    assert db.execute("SELECT count(*) FROM sections_vec").fetchone()[0] == 3
    assert [r[0] for r in db.execute("SELECT rowid FROM sections_fts WHERE sections_fts MATCH 'pear'")] == [1]
    blob = db.execute("SELECT embedding FROM sections_vec WHERE id = 0").fetchone()[0]
    assert len(blob) == bb.ec.EMBED_DIM * 4


def test_build_db_vector_search_finds_the_document_the_query_resembles():
    from conftest import HashEncoder

    model = HashEncoder()
    db = bb.build_db(["a", "b"], ["red apple pie", "blue plum cake"], model, 8)
    qv = bb.ec.postprocess(model.encode(bb.ec.QUERY_PREFIX + "plum cake", convert_to_numpy=True)).astype("float32").tobytes()
    top = db.execute("SELECT id FROM sections_vec WHERE embedding MATCH ? AND k = 1", (qv,)).fetchone()[0]
    assert top == 1


def test_code_hashes_name_each_file_with_the_sha256_of_its_bytes():
    import hashlib

    got = bb.code_hashes()
    assert {"evals/retrieval/beir_bench.py", "evals/retrieval/bench_engine.py", "evals/retrieval/resumable.py", "tools/gestalt_embed_config.py"} <= set(got)
    for name, digest in got.items():
        assert digest == hashlib.sha256((REPO / name).read_bytes()).hexdigest()
        assert len(digest) == 64


def test_a_full_run_on_a_cpu_model_is_refused_but_a_subset_or_an_explicit_choice_passes(monkeypatch):
    """F7 of 2026-10-08: a run that lost its GPU would score a CPU fallback. Subsets are exempt, so the clean-clone smoke still runs."""
    import pytest

    class M:
        device = "cpu"

    monkeypatch.delenv("GESTALT_BENCH_ALLOW_CPU", raising=False)
    monkeypatch.delenv("GESTALT_EMBED_DEVICE", raising=False)
    with pytest.raises(SystemExit):
        bb.refuse_cpu(M())
    bb.refuse_cpu(M(), smoke=True)
    monkeypatch.setenv("GESTALT_BENCH_ALLOW_CPU", "1")
    bb.refuse_cpu(M())


def test_work_dir_key_separates_embedding_profiles():
    """F8 of 2026-10-08: two profiles under one --work-dir collided on the fingerprint check. The nomic default keeps the bare name."""
    assert bb.dataset_key("beir/scifact", None, None, profile="nomic") == "beir-scifact"
    assert bb.dataset_key("beir/scifact", None, None, profile="qwen3-4b") == "beir-scifact-qwen3-4b"
    assert bb.dataset_key("beir/scifact", 100, None, profile="qwen3-4b") == "beir-scifact-qwen3-4b-limit100"


def test_a_query_log_from_other_code_is_requeried_not_replayed(stubbed, tmp_path, monkeypatch, capsys):
    """2026-10-08: a rerun meant to put the final code's hashes on the headline replayed every logged query in 67 s. A log whose
    header matches apart from code_sha256 is now re-queried, and the result carries the hashes of the code that ran."""
    monkeypatch.delenv("GESTALT_FUSION", raising=False)
    monkeypatch.setattr(bb, "code_hashes", lambda: {"evals/retrieval/beir_bench.py": "a" * 64})
    bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None)
    capsys.readouterr()
    monkeypatch.setattr(bb, "code_hashes", lambda: {"evals/retrieval/beir_bench.py": "b" * 64})
    r = bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None, resume=True)
    err = capsys.readouterr().err
    assert "written by other code, re-querying" in err
    assert r["query_log_header"]["code_sha256"] == {"evals/retrieval/beir_bench.py": "b" * 64}
    r2 = bb.run_dataset("beir/scifact", tmp_path, None, 8, StubModel(), None, resume=True)
    assert "written by other code" not in capsys.readouterr().err, "same code replays"
    assert r2["query_log_header"]["code_sha256"] == r["query_log_header"]["code_sha256"]


# --- run-file scores and the fusion grid -----------------------------------------------------------------------------

GRID = ["hybrid:wrrf@0", "hybrid:wrrf@0.2", "hybrid:wrrf@0.5", "hybrid:convex@0", "hybrid:convex@0.3", "hybrid:rescue"]


def _run_lines(out: Path, name: str, system: str) -> dict[str, list[tuple[int, str, float]]]:
    got: dict = {}
    for line in (out / f"{name.replace('/', '-')}.{system}.run").read_text().splitlines():
        qid, q0, doc, rank, score, tag = line.split()
        assert q0 == "Q0" and tag == f"gestalt-{system}"
        got.setdefault(qid, []).append((int(rank), doc, float(score)))
    return got


def _grid_run(monkeypatch, tmp_path, grid, tag):
    mod, _docs, _queries = corpus_dataset(200, 25)
    monkeypatch.setitem(sys.modules, "ir_datasets", mod)
    monkeypatch.setenv("GESTALT_RERANK", "off")
    monkeypatch.setattr(bb.harness, "_model", StubModel())
    out = tmp_path / f"out{tag}"
    r = bb.run_dataset("beir/fiqa", out, None, 8, StubModel(), None, work_dir=tmp_path / "work", block_size=64, grid=grid)
    return out, r


def test_run_files_carry_the_real_score_higher_is_better_in_rank_order(monkeypatch, tmp_path):
    out, r = _grid_run(monkeypatch, tmp_path, [], "")
    log = next((tmp_path / "work" / "beir-fiqa").glob("queries-*.jsonl")).read_text().splitlines()[1:]
    logged = {(e["system"], e["qid"]): e for e in map(json.loads, log)}
    for system in ("bm25", "dense", "hybrid"):
        for qid, rows in _run_lines(out, "beir/fiqa", system).items():
            assert [rk for rk, _, _ in rows] == list(range(1, len(rows) + 1))
            scores = [sc for _, _, sc in rows]
            assert scores == sorted(scores, reverse=True), (system, qid)  # higher is better, so the column never rises
            raw = logged[(system, qid)]["scores"]
            assert scores == [bb.run_score(system, x) for x in raw]  # the logged score, sign-flipped for the two distance-like legs
            assert [d for _, d, _ in rows] == logged[(system, qid)]["ids"]
    assert any(sc != bb.TOPK - rk + 1 for rows in _run_lines(out, "beir/fiqa", "hybrid").values() for rk, _, sc in rows)


def test_the_readers_in_this_directory_accept_the_new_run_files(monkeypatch, tmp_path):
    import calibration
    import pair_runs

    out, r = _grid_run(monkeypatch, tmp_path, [], "")
    for system in ("bm25", "dense", "hybrid"):
        path = out / f"beir-fiqa.{system}.run"
        lines = _run_lines(out, "beir/fiqa", system)
        assert pair_runs.read_run(path) == {q: [d for _, d, _ in rows] for q, rows in lines.items()}
        assert calibration.read_run_scores(path) == {q: [sc for _, _, sc in rows] for q, rows in lines.items()}


def test_parse_fusion_grid_names_one_system_per_setting():
    assert bb.parse_fusion_grid(["wrrf=0,0.1,0.10", "convex=0.3", "rescue"]) == ["hybrid:wrrf@0", "hybrid:wrrf@0.1", "hybrid:convex@0.3", "hybrid:rescue"]
    assert bb.parse_fusion_grid(None) == []
    for bad in (["wrrf=1.5"], ["wrrf=x"], ["convex"], ["rescue=1"], ["rrf=0.1"]):
        with pytest.raises(SystemExit):
            bb.parse_fusion_grid(bad)
    assert [bb.engine.grid_system(n) for n in bb.parse_fusion_grid(["wrrf=0.2", "rescue"])] == [("wrrf", 0.2), ("rescue", None)]
    assert bb.engine.grid_system("hybrid") is None and bb.engine.grid_system("hybrid_rerank") is None


def test_the_grid_adds_systems_and_leaves_the_default_systems_and_their_numbers_alone(monkeypatch, tmp_path):
    plain_out, plain = _grid_run(monkeypatch, tmp_path, [], "-plain")
    grid_out, grid = _grid_run(monkeypatch, tmp_path, GRID, "-grid")
    assert set(plain["systems"]) == {"bm25", "dense", "hybrid"}
    assert set(grid["systems"]) == {"bm25", "dense", "hybrid", *GRID}
    for s in ("bm25", "dense", "hybrid"):
        assert grid["systems"][s] == plain["systems"][s], s
        assert (grid_out / f"beir-fiqa.{s}.run").read_text() == (plain_out / f"beir-fiqa.{s}.run").read_text(), s
    for s in GRID:
        assert len(grid["systems"][s]["per_query"]) == len(grid["query_ids"]) and (grid_out / f"beir-fiqa.{s}.run").exists()
    runs = {s: {q: [d for _, d, _ in rows] for q, rows in _run_lines(grid_out, "beir/fiqa", s).items()} for s in ("dense", "hybrid", *GRID)}
    assert runs["hybrid:wrrf@0.5"] == runs["hybrid"]  # weights (1, 1) are the plain RRF scores
    assert runs["hybrid:wrrf@0"] == runs["dense"]  # all weight on the dense leg, the degenerate setting tune_fusion --results falls back to
    # convex@0 is not compared: this corpus has exact distance ties, and convex breaks a tie by the best rank in either leg.
    hyb = _run_lines(grid_out, "beir/fiqa", "hybrid")
    half = _run_lines(grid_out, "beir/fiqa", "hybrid:wrrf@0.5")
    assert {q: [sc for *_, sc in v] for q, v in half.items()} == {q: [sc for *_, sc in v] for q, v in hyb.items()}
    assert "fusion_params" in grid["query_log_header"] and "fusion_params" not in plain["query_log_header"]
    logs = sorted(p.name for p in (tmp_path / "work" / "beir-fiqa").glob("queries-*.jsonl"))
    assert len(logs) == 2 and sum("-grid-" in n for n in logs) == 1  # the grid run keeps its own log


def test_a_grid_result_is_reused_only_by_a_run_with_the_same_grid_and_knobs(monkeypatch, tmp_path):
    out, r = _grid_run(monkeypatch, tmp_path, GRID[:2], "")
    same = bb.run_identity(None, "rrf", 0.5, list(bb.engine.BASE_SYSTEMS) + GRID[:2])
    assert bb.load_finished(out, "beir/fiqa", False, None, None, same) is not None
    assert bb.load_finished(out, "beir/fiqa", False, None, None, bb.run_identity(None, "rrf", 0.5)) is None  # a plain run reruns, it never reads a grid file
    wider = bb.run_identity(None, "rrf", 0.5, list(bb.engine.BASE_SYSTEMS) + GRID)
    assert bb.load_finished(out, "beir/fiqa", False, None, None, wider) is None
    monkeypatch.setenv("GESTALT_FUSION_NORM", "zscore")
    assert bb.load_finished(out, "beir/fiqa", False, None, None, bb.run_identity(None, "rrf", 0.5, list(bb.engine.BASE_SYSTEMS) + GRID[:2])) is None
