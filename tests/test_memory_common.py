"""evals/memory/common.py: units, dataset records, run resolution, outputs, level summaries and the dense leg."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pytest
from conftest import HashEncoder

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "memory"))
pytest.importorskip("sqlite_vec")
import common as C  # noqa: E402


def sessions():
    return [
        C.Session("s1", "2024-01-05", [C.Turn("s1:0", "user", "I bought a red apple."), C.Turn("s1:1", "assistant", "  "), C.Turn("s1:2", "assistant", "Nice choice.")]),
        C.Session("empty", "2024-01-06", []),
        C.Session("s2", "2024-02-01", [C.Turn("s2:0", "user", "Plum cake tonight.")]),
    ]


def test_session_units_make_one_unit_per_nonempty_session_with_the_date_as_header():
    units = C.session_units(sessions())
    assert [u.uid for u in units] == ["s1", "s2"]
    assert units[0].header == "2024-01-05"
    assert units[0].body == "user: I bought a red apple.\n\nassistant:   \n\nassistant: Nice choice."
    assert all(u.uid == u.session for u in units)


def test_session_units_never_put_the_session_id_in_the_text():
    units = C.session_units([C.Session("answer_secret", "2024-01-05", [C.Turn("t0", "user", "hello")])])
    assert "answer_secret" not in units[0].header + units[0].body


def test_turn_units_make_one_unit_per_nonblank_turn_with_date_and_speaker_in_the_header():
    units = C.turn_units(sessions())
    assert [(u.uid, u.session, u.header, u.body) for u in units] == [
        ("s1:0", "s1", "2024-01-05 — user", "I bought a red apple."),
        ("s1:2", "s1", "2024-01-05 — assistant", "Nice choice."),
        ("s2:0", "s2", "2024-02-01 — user", "Plum cake tonight."),
    ]


def test_build_units_picks_the_granularity_and_rejects_others():
    assert [u.uid for u in C.build_units(sessions(), "session")] == ["s1", "s2"]
    assert len(C.build_units(sessions(), "turn")) == 3
    with pytest.raises(ValueError):
        C.build_units(sessions(), "paragraph")


def test_dataset_record_reports_the_hash_size_and_whether_it_matches_the_pin(tmp_path, monkeypatch):
    f = tmp_path / "locomo10.json"
    f.write_bytes(b'{"a": 1}')
    digest = hashlib.sha256(b'{"a": 1}').hexdigest()
    rec = C.dataset_record(f, "locomo10")
    assert rec["sha256"] == digest and rec["bytes"] == 8 and rec["path"] == str(f)
    assert rec["verified"] is False and rec["pinned_sha256"] == C.DATASETS["locomo10"]["sha256"]
    monkeypatch.setitem(C.DATASETS, "locomo10", {**C.DATASETS["locomo10"], "sha256": digest})
    assert C.dataset_record(f, "locomo10")["verified"] is True


def test_code_hashes_cover_the_memory_files_the_chunker_and_the_beir_files(tmp_path):
    bench = tmp_path / "my_bench.py"
    bench.write_text("x = 1\n")
    got = C.code_hashes(bench)
    assert got["evals/memory/my_bench.py"] == hashlib.sha256(b"x = 1\n").hexdigest()
    assert got["evals/memory/common.py"] == hashlib.sha256((REPO / "evals" / "memory" / "common.py").read_bytes()).hexdigest()
    assert got["tools/gestalt-index-builder.py"] == hashlib.sha256((REPO / "tools" / "gestalt-index-builder.py").read_bytes()).hexdigest()
    assert "evals/retrieval/beir_bench.py" in got and "evals/retrieval/bench_stats.py" in got


def test_write_outputs_writes_both_files_and_leaves_no_temp_file(tmp_path):
    out = tmp_path / "run" / "nested"
    C.write_outputs(out, {"n": 2, "systems": ["bm25"]}, [{"qid": "q1"}, {"qid": "q2"}])
    assert json.loads((out / "summary.json").read_text()) == {"n": 2, "systems": ["bm25"]}
    assert [json.loads(line) for line in (out / "per_question.jsonl").read_text().splitlines()] == [{"qid": "q1"}, {"qid": "q2"}]
    assert sorted(p.name for p in out.iterdir()) == ["per_question.jsonl", "summary.json"]


def test_write_outputs_replaces_an_earlier_result_whole(tmp_path):
    C.write_outputs(tmp_path, {"v": 1}, [{"qid": "a"}, {"qid": "b"}])
    C.write_outputs(tmp_path, {"v": 2}, [{"qid": "c"}])
    assert json.loads((tmp_path / "summary.json").read_text()) == {"v": 2}
    assert (tmp_path / "per_question.jsonl").read_text() == '{"qid": "c"}\n'


# --- resolve_run ---------------------------------------------------------------------------------


def run_args(**kw):
    base = dict(embed_profile=None, systems="bm25,dense,hybrid", rerank="off", rerank_model="bge", rerank_depth=7)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def model_calls(monkeypatch):
    calls = {"get": 0, "installed": []}

    def get_model():
        calls["get"] += 1
        return HashEncoder()

    monkeypatch.setattr(C.harness, "get_model", get_model)
    monkeypatch.setattr(C, "_install_model", lambda m: calls["installed"].append(m))
    return calls


def test_resolve_run_with_a_lexical_system_only_loads_no_model(model_calls):
    systems, rerank, model = C.resolve_run(run_args(systems="bm25"))
    assert systems == ["bm25"] and rerank is None and isinstance(model, C.NullEncoder)
    assert model_calls["get"] == 0 and model_calls["installed"] == []


def test_resolve_run_with_a_dense_system_wraps_the_model_in_the_cache_and_installs_it(model_calls):
    systems, rerank, model = C.resolve_run(run_args(systems=" bm25, dense ,"))
    assert systems == ["bm25", "dense"] and rerank is None
    assert isinstance(model, C.CachedEncoder) and model_calls["installed"] == [model] and model_calls["get"] == 1


def test_resolve_run_with_rerank_on_adds_the_rerank_system_and_forces_hybrid_in(model_calls):
    systems, rerank, _ = C.resolve_run(run_args(systems="bm25", rerank="on", rerank_model="qwen3-0.6b"))
    assert systems == ["hybrid", "bm25", "hybrid_rerank"]
    assert rerank == {"model": "qwen3-0.6b", "depth": 7}


def test_resolve_run_does_not_duplicate_hybrid(model_calls):
    systems, _, _ = C.resolve_run(run_args(systems="hybrid", rerank="on"))
    assert systems == ["hybrid", "hybrid_rerank"]


def test_resolve_run_rejects_an_unknown_system(model_calls):
    with pytest.raises(SystemExit, match="unknown systems"):
        C.resolve_run(run_args(systems="bm25,colbert"))


def test_resolve_run_rejects_a_profile_that_did_not_take_effect(model_calls):
    other = next(p for p in C.ec.PROFILES if p != C.ec.PROFILE)
    with pytest.raises(SystemExit, match="did not take effect"):
        C.resolve_run(run_args(embed_profile=other))
    C.resolve_run(run_args(embed_profile=C.ec.PROFILE, systems="bm25"))


# --- dense_scored --------------------------------------------------------------------------------


@pytest.fixture
def index():
    units = [C.Unit("u0", "s0", "2024-01-05", "red apple pie recipe"), C.Unit("u1", "s1", "2024-01-06", "blue plum cake recipe"),
             C.Unit("u2", "s2", "2024-01-07", "green pear tart recipe")]
    return C.MemoryIndex(units, HashEncoder(), batch_size=2)


def test_dense_scored_returns_chunk_ids_nearest_first_and_the_negated_top_distance(index):
    ids, top = index.dense_scored("plum cake", 3)
    assert len(ids) == 3 and sorted(ids) == [0, 1, 2]
    assert index.units[index.owner[ids[0]]].uid == "u1"
    nearest = index.db.execute("SELECT distance FROM sections_vec WHERE embedding MATCH ? AND k = 1",
                               (C.ec.postprocess(index.model.encode(C.ec.QUERY_PREFIX + "plum cake", convert_to_numpy=True)).astype("float32").tobytes(),)).fetchone()[0]
    assert top == pytest.approx(-nearest) and top <= 0.0


def test_dense_scored_honours_k_and_maps_to_units_through_rank(index):
    ids, _ = index.dense_scored("green pear", 1)
    assert len(ids) == 1
    units, top = index.rank("green pear", "dense", 3)
    assert units[0] == "u2" and sorted(units) == ["u0", "u1", "u2"] and top is not None


# --- summarize_levels ----------------------------------------------------------------------------


def row(qid, ndcg, recall, group="single", cluster=None):
    r = {"qid": qid, "group": group, "scores": {"ndcg@10": ndcg, "recall@10": recall}}
    if cluster is not None:
        r["cluster"] = cluster
    return r


def level_result(**kw):
    qs = [f"q{i}" for i in range(6)]
    hybrid = [row(q, 0.9, 1.0) for q in qs]
    bm25 = [row(q, 0.4, 0.5) for q in qs]
    result = {"rows": {"session": {"bm25": bm25, "hybrid": hybrid, "dense": []}},
              "abstain": {"session": {"bm25": ([], []), "hybrid": ([0.9, 0.8], [0.1]), "dense": ([], [])}}}
    result.update(kw)
    return result


def test_summarize_levels_aggregates_each_system_with_rows_and_skips_empty_ones():
    out = C.summarize_levels(level_result(), ["bm25", "dense", "hybrid"])["session"]
    assert set(out["systems"]) == {"bm25", "hybrid"}
    assert out["systems"]["hybrid"]["all"]["n"] == 6 and out["systems"]["hybrid"]["all"]["ndcg@10"] == 0.9
    assert out["systems"]["bm25"]["all"]["recall@10"] == 0.5


def test_summarize_levels_runs_the_paired_tests_on_the_pooled_rows():
    tests = C.summarize_levels(level_result(), ["bm25", "hybrid"])["session"]["tests"]
    t = tests["hybrid_vs_bm25.ndcg@10"]
    assert t["mean_difference"] == pytest.approx(0.5) and 0.0 < t["p_value"] <= 1.0
    assert "hybrid_vs_dense.ndcg@10" not in tests, "dense has no rows, so that pair is not tested"


def test_summarize_levels_scores_abstention_only_for_systems_with_abstain_scores():
    out = C.summarize_levels(level_result(), ["bm25", "dense", "hybrid"])["session"]
    assert list(out["abstention_auroc"]) == ["hybrid"]
    assert out["abstention_auroc"]["hybrid"]["auroc"] == 1.0
    none = C.summarize_levels(level_result(abstain={"session": {s: ([], []) for s in ("bm25", "dense", "hybrid")}}), ["bm25", "dense", "hybrid"])
    assert none["session"]["abstention_auroc"] is None


def test_summarize_levels_keeps_excluded_groups_out_of_the_pooled_cell():
    rows = level_result()
    rows["rows"]["session"]["hybrid"].append(row("adv", 0.0, 0.0, group="adversarial"))
    rows["rows"]["session"]["bm25"].append(row("adv", 0.0, 0.0, group="adversarial"))
    out = C.summarize_levels(rows, ["bm25", "hybrid"], exclude_from_all={"adversarial"})["session"]
    cells = out["systems"]["hybrid"]
    assert cells["all"]["n"] == 6 and cells["adversarial"]["n"] == 1


def test_summarize_levels_voids_the_rerank_system_when_the_reranker_fell_back():
    result = level_result(rerank_fallbacks=2)
    result["rows"]["session"]["hybrid_rerank"] = [row(f"q{i}", 0.9, 1.0) for i in range(6)]
    result["abstain"]["session"]["hybrid_rerank"] = ([], [])
    out = C.summarize_levels(result, ["bm25", "hybrid", "hybrid_rerank"])["session"]
    assert out["void_systems"] == ["hybrid_rerank"]
    assert "void_systems" not in C.summarize_levels(level_result(rerank_fallbacks=2), ["bm25", "hybrid"])["session"]
