"""Tests for the LongMemEval-S and LoCoMo-10 retrieval harnesses in evals/memory/.

Everything here is synthetic. The fixtures follow the real JSON shapes and hold invented text. A stub encoder stands in for the model, so no weights load.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MEM = Path(os.environ.get("GESTALT_MEMORY_BENCH_DIR", REPO / "evals" / "memory"))  # a scratch copy can stand in, for the discrimination runs
sys.path.insert(0, str(MEM))
pytest.importorskip("sqlite_vec")
pytest.importorskip("numpy")
from conftest import HashEncoder  # noqa: E402
import common as C  # noqa: E402
import locomo_bench as K  # noqa: E402
import longmemeval_bench as L  # noqa: E402


StubModel = HashEncoder  # unit-length crc32 bag of words


# ---------------------------------------------------------------- fixtures

def lme_fixture() -> list[dict]:
    """Three questions in the LongMemEval shape. The third is an abstention question."""
    def sess(word, answer_turn=None):
        t = [{"role": "user", "content": f"We talked about {word} today."}, {"role": "assistant", "content": f"Noted, {word} it is."}]
        if answer_turn is not None:
            t[answer_turn]["has_answer"] = True
        return t

    def q(qid, qtype, question, ans_sid, words, ans_turn):
        sids = [f"{qid}_s{i}" for i in range(3)]
        sids[1] = ans_sid
        return {"question_id": qid, "question_type": qtype, "question": question, "question_date": "2024/01/05 (Fri) 10:00", "answer": "x",
                "answer_session_ids": [ans_sid], "haystack_dates": ["2024/01/01 (Mon) 09:00", "2024/01/02 (Tue) 09:00", "2024/01/03 (Wed) 09:00"],
                "haystack_session_ids": sids, "haystack_sessions": [sess(words[0]), sess(words[1], ans_turn), sess(words[2])]}
    return [q("q1", "single-session-user", "what about zebrafish", "answer_aaa", ["kayak", "zebrafish", "tulip"], 0),
            q("q2", "multi-session", "what about marmalade", "answer_bbb", ["quartz", "marmalade", "walnut"], 1),
            q("q3_abs", "single-session-user", "what about unicycle", "answer_ccc_abs", ["cobalt", "pewter", "ember"], None)]


def locomo_fixture() -> list[dict]:
    """Two conversations in the LoCoMo shape. Both mention 'lantern', so only scoping keeps them apart."""
    def conv(sid, a, b, word):
        turns = [{"speaker": a, "dia_id": "D1:1", "text": f"I bought a {word} yesterday."}, {"speaker": b, "dia_id": "D1:2", "text": "Nice, tell me more."}]
        s2 = [{"speaker": a, "dia_id": "D2:1", "text": "The lantern broke on the trail."}]
        return {"sample_id": sid, "conversation": {"speaker_a": a, "speaker_b": b, "session_1_date_time": "1:00 pm on 8 May, 2023", "session_1": turns,
                "session_2_date_time": "2:00 pm on 9 May, 2023", "session_2": s2},
                "qa": [{"question": f"What did {a} buy?", "answer": word, "evidence": ["D1:1"], "category": 4},
                       {"question": f"When did the lantern break for {a}?", "answer": "9 May", "evidence": ["D2:1"], "category": 2},
                       {"question": "An open one?", "answer": "n", "evidence": [], "category": 3},
                       {"question": f"Joined evidence for {a} and {b}?", "answer": "n", "evidence": ["D1:1; D1:2", "D:2:1"], "category": 1}]}
    return [conv("conv-A", "Ann", "Bo", "teapot"), conv("conv-B", "Cy", "Di", "rucksack")]


@pytest.fixture
def lme_file(tmp_path):
    p = tmp_path / "lme.json"
    p.write_text(json.dumps(lme_fixture()))
    return p


@pytest.fixture
def locomo_file(tmp_path):
    p = tmp_path / "locomo.json"
    p.write_text(json.dumps(locomo_fixture()))
    return p


@pytest.fixture
def stub_model(monkeypatch):
    monkeypatch.setattr(C.harness, "get_model", lambda: StubModel())
    monkeypatch.setattr(C.harness, "_model", None)


def args_for(tmp_path, data, **kw):
    import argparse

    base = dict(out=str(tmp_path / "out"), granularity="both", systems="bm25,dense,hybrid", depth=20, limit_questions=None, batch_size=8, data=str(data),
                embed_profile=None, rerank="off", rerank_model="bge", rerank_depth=5, fusion=None, alpha=None, conversations=None,
                scope_dir=None, rebuild=False, include_adversarial=False)
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------- unit construction

def test_lme_units_and_gold(lme_file):
    qs, hay, counts = L.load_questions(lme_file)
    q1 = qs[0]
    assert q1["gold_session"] == {"answer_aaa"} and q1["gold_turn"] == {"answer_aaa#0"}
    assert qs[1]["gold_turn"] == {"answer_bbb#1"}
    s_units = C.build_units(hay["q1"], "session")
    t_units = C.build_units(hay["q1"], "turn")
    assert len(s_units) == 3 and len(t_units) == 6
    assert t_units[2].uid == "answer_aaa#0" and t_units[2].session == "answer_aaa"
    assert s_units[0].header == "2024/01/01 (Mon) 09:00" and "user: We talked about kayak" in s_units[0].body
    assert t_units[0].header == "2024/01/01 (Mon) 09:00 — user"


def test_session_ids_never_reach_the_text(lme_file):
    """LongMemEval names evidence sessions 'answer_...'. An id in the text would hand the model the answer."""
    _, hay, _ = L.load_questions(lme_file)
    for g in ("session", "turn"):
        _, texts = C.chunk_units(C.build_units(hay["q1"], g))
        assert texts and not any("answer_" in t or "q1_s" in t for t in texts)


def test_chunks_use_the_index_builder_limits_and_repeat_the_header():
    b = C.stats._index_builder()
    assert (b.MAX_CHUNK_CHARS, b.SUBCHUNK_OVERLAP) == (2000, 150)
    sess = [C.Session("s", "DATE-HEADER", [C.Turn("s#0", "u", ("word " * 1500).strip())])]
    owner, texts = C.chunk_units(C.build_units(sess, "session"))
    assert len(texts) >= 4 and set(owner) == {0}
    assert all(t.startswith("DATE-HEADER\n\n") for t in texts)
    assert all(len(t) <= 2000 + len("DATE-HEADER\n\n") for t in texts)


def test_abstention_counts(lme_file):
    qs, _, counts = L.load_questions(lme_file)
    assert counts["abstention"] == 1 and [q["abstain"] for q in qs] == [False, False, True]
    assert counts["scored_session"] == 2 and counts["scored_turn"] == 2


# ---------------------------------------------------------------- scoring

def test_score_ranked_values():
    sc = C.score_ranked(["x", "g1", "y", "z", "g2"], {"g1", "g2"})
    assert sc["recall@1"] == 0.0 and sc["recall@3"] == 0.5 and sc["recall@5"] == 1.0
    assert sc["any@1"] == 0.0 and sc["any@3"] == 1.0 and sc["all@3"] == 0.0 and sc["all@5"] == 1.0
    assert sc["mrr"] == 0.5
    assert 0.5 < sc["ndcg@10"] < 0.8
    assert C.score_ranked(["g1"], {"g1"})["ndcg@10"] == 1.0
    assert C.score_ranked(["a", "b"], {"g1"})["mrr"] == 0.0


def test_turn_and_session_levels_score_separately(lme_file, stub_model, tmp_path):
    s = L.run(args_for(tmp_path, lme_file, systems="bm25"))
    lv = s["levels"]
    assert set(lv) == {"session", "turn", "session_from_turns"}
    assert lv["turn"]["systems"]["bm25"]["all"]["n"] == 2 and lv["session"]["systems"]["bm25"]["all"]["n"] == 2
    # q2's gold turn is the assistant line. Both levels still find it, each against its own gold set.
    assert lv["session"]["systems"]["bm25"]["all"]["recall@1"] == 1.0
    assert lv["turn"]["systems"]["bm25"]["all"]["recall@10"] == 1.0


def test_abstention_is_skipped_in_recall_and_scored_by_auroc(lme_file, stub_model, tmp_path):
    s = L.run(args_for(tmp_path, lme_file, systems="dense", granularity="session"))
    lv = s["levels"]["session"]
    assert lv["systems"]["dense"]["all"]["n"] == 2
    assert "q3_abs" not in {json.loads(x)["qid"] for x in (tmp_path / "out" / "per_question.jsonl").read_text().splitlines()}
    ab = lv["abstention_auroc"]["dense"]
    assert ab["n"] == 1 and ab["answerable_n"] == 2 and ab["auroc"] is not None


def test_abstention_auroc_matches_rank_sum():
    assert C.abstention_auroc([5.0, 4.0], [1.0])["auroc"] == 1.0
    assert C.abstention_auroc([1.0], [5.0])["auroc"] == 0.0
    assert C.abstention_auroc([1.0], []) is None


def test_per_category_aggregation(locomo_file, stub_model, tmp_path):
    s = K.run(args_for(tmp_path, locomo_file, systems="bm25", granularity="turn"))
    cells = s["levels"]["turn"]["systems"]["bm25"]
    assert set(cells) == {"all", "1 multi-hop", "2 temporal", "4 single-hop"}
    assert cells["all"]["n"] == 6 and cells["1 multi-hop"]["n"] == 2 and cells["2 temporal"]["n"] == 2
    assert "recall@5_ci95" in cells["all"] and len(cells["all"]["mrr_ci95"]) == 2


def test_aggregate_groups_and_means():
    rows = [{"group": "a", "scores": {"recall@5": 1.0}}, {"group": "a", "scores": {"recall@5": 0.0}}, {"group": "b", "scores": {"recall@5": 1.0}}]
    agg = C.aggregate(rows)
    assert agg["all"]["n"] == 3 and agg["a"]["recall@5"] == 0.5 and agg["b"]["n"] == 1


def test_bootstrap_is_seeded_and_brackets_the_mean():
    v = [0.0, 1.0] * 20
    assert C.bootstrap_ci(v) == C.bootstrap_ci(v)
    lo, hi = C.bootstrap_ci(v)
    assert lo < 0.5 < hi


# ---------------------------------------------------------------- LoCoMo

def test_locomo_scoping_keeps_conversations_apart(locomo_file, stub_model, tmp_path):
    """Both conversations mention a lantern. A question of conv-A must never return a conv-B turn."""
    s = K.run(args_for(tmp_path, locomo_file, systems="bm25,dense,hybrid", granularity="turn"))
    out = [json.loads(x) for x in (tmp_path / "out" / "per_question.jsonl").read_text().splitlines()]
    q = [x for x in out if x["level"] == "turn" and x["qid"].startswith("conv-A")]
    assert q
    for x in out:
        scope = x["qid"].split("#")[0]
        # conv-A speakers are Ann and Bo. conv-B speakers are Cy and Di. The ranked turn ids carry no scope, so count candidates.
        assert len(x["top10"]) <= 3, f"{scope} saw turns outside its conversation"
    assert s["levels"]["turn"]["systems"]["bm25"]["all"]["n"] == 6


def test_parse_evidence_handles_the_dirty_strings():
    assert K.parse_evidence(["D1:3"]) == ["D1:3"]
    assert K.parse_evidence(["D8:6; D9:17"]) == ["D8:6", "D9:17"]
    assert K.parse_evidence(["D:11:26", "D9:1 D4:4 D4:6"]) == ["D11:26", "D9:1", "D4:4", "D4:6"]
    assert K.parse_evidence(["D"]) == []


def test_locomo_counts_and_session_gold(locomo_file):
    qs, _, counts = K.load_questions(locomo_file)
    assert counts["questions"] == 8 and counts["no_evidence"] == 2 and counts["scored"] == 6
    joined = [q for q in qs if q["group"].startswith("1 ")][0]
    assert joined["gold_turn"] == {"D1:1", "D1:2", "D2:1"} and joined["gold_session"] == {"session_1", "session_2"}
    assert [q["scope"] for q in K.load_questions(locomo_file, ["conv-B"])[0]] == ["conv-B"] * 3
    assert K.load_questions(locomo_file, limit=2)[0].__len__() == 2


def test_locomo_summary_carries_the_penfield_caveat(locomo_file, stub_model, tmp_path):
    s = K.run(args_for(tmp_path, locomo_file, systems="bm25", granularity="session"))
    assert "6.4 percent" in s["answer_key_caveat"] and "Penfield" in s["answer_key_caveat"]


# ---------------------------------------------------------------- systems and summary

def test_all_three_systems_and_paired_tests(lme_file, stub_model, tmp_path):
    s = L.run(args_for(tmp_path, lme_file, granularity="session"))
    lv = s["levels"]["session"]
    assert set(lv["systems"]) == {"bm25", "dense", "hybrid"}
    assert {"hybrid_vs_bm25.ndcg@10", "hybrid_vs_dense.recall@10"} <= set(lv["tests"])
    assert all(0 < t["p_value"] <= 1 for t in lv["tests"].values())


def test_rerank_system_reorders_and_scores(lme_file, stub_model, tmp_path, monkeypatch):
    import gestalt_rank

    calls = []

    def fake(query, rows, alias=None, maxchars=None):
        calls.append(alias)
        order = sorted(rows, key=lambda r: "tulip" not in r["content"])  # push the wrong session first, so a reorder is visible
        return order, [float(len(rows) - i) for i in range(len(order))], {"fallback": "none"}

    monkeypatch.setattr(gestalt_rank, "rerank_rows", fake)
    s = L.run(args_for(tmp_path, lme_file, granularity="session", rerank="on", rerank_model="stubmodel"))
    assert "hybrid_rerank" in s["levels"]["session"]["systems"] and calls and set(calls) == {"stubmodel"}
    assert "hybrid_rerank_vs_hybrid.ndcg@10" in s["levels"]["session"]["tests"]
    assert s["config"]["rerank"] == {"model": "stubmodel", "depth": 5}
    lines = [json.loads(x) for x in (tmp_path / "out" / "per_question.jsonl").read_text().splitlines()]
    hyb = [x for x in lines if x["system"] == "hybrid" and x["qid"] == "q1"][0]
    rr = [x for x in lines if x["system"] == "hybrid_rerank" and x["qid"] == "q1"][0]
    assert hyb["top10"][0] == "answer_aaa" and rr["top10"][0] == "q1_s2"


def test_summary_carries_sha_licence_hashes_and_honesty(lme_file, stub_model, tmp_path):
    L.run(args_for(tmp_path, lme_file, systems="bm25", granularity="session"))
    s = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert s["dataset"]["sha256"] == C.sha256_file(lme_file) and len(s["dataset"]["sha256"]) == 64
    assert s["dataset"]["verified"] is False  # a fixture is not the pinned file, and the record says so
    assert "MIT" in s["dataset"]["licence"] and "revision" in s["dataset"]
    hashes = s["environment"]["code_sha256"]
    assert {"evals/memory/common.py", "evals/memory/longmemeval_bench.py", "tools/gestalt-index-builder.py", "evals/retrieval/beir_bench.py", "evals/retrieval/bench_stats.py"} <= set(hashes)
    assert all(len(h) == 64 for h in hashes.values())
    assert "not QA accuracy" in s["honesty_note"] and "vendor QA" in s["honesty_note"] and "sha256" in s["honesty_note"]


def test_locomo_licence_is_recorded(locomo_file, stub_model, tmp_path):
    s = K.run(args_for(tmp_path, locomo_file, systems="bm25", granularity="session"))
    assert "CC BY-NC 4.0" in s["dataset"]["licence"]


def test_fetch_refuses_a_file_that_is_not_the_pinned_one(tmp_path):
    (tmp_path / C.DATASETS["locomo10"]["file"]).write_text("{}")
    with pytest.raises(SystemExit, match="does not match the pin"):
        C.fetch_dataset("locomo10", cache_dir=tmp_path, download=False)


def test_pinned_hashes_are_the_ones_downloaded():
    assert C.DATASETS["longmemeval_s"]["sha256"] == "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
    assert C.DATASETS["locomo10"]["sha256"] == "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"


def test_limit_questions_marks_smoke(lme_file, stub_model, tmp_path):
    s = L.run(args_for(tmp_path, lme_file, systems="bm25", granularity="session", limit_questions=1))
    assert s["smoke_subset"] is True and s["counts"]["questions"] == 1


# ---------------------------------------------------------------- shards

def test_shard_spec_parsing():
    assert C.parse_shard(None) is None and C.parse_shard("2/4") == (2, 4)
    for bad in ("0/3", "4/3", "x", "1/", "2"):
        with pytest.raises(SystemExit):
            C.parse_shard(bad)


def test_shards_partition_whole_scopes_in_order():
    qs = [{"scope": s, "qid": f"{s}{i}"} for s in "ABCDE" for i in range(2)]
    parts = [C.apply_shard(qs, f"{k}/3") for k in (1, 2, 3)]
    assert [sorted({q["scope"] for q in p}) for p in parts] == [["A", "B"], ["C", "D"], ["E"]]  # 5 scopes into 3: sizes 2, 2, 1
    assert sorted(q["qid"] for p in parts for q in p) == sorted(q["qid"] for q in qs)
    assert C.apply_shard(qs, None) is qs
    with pytest.raises(SystemExit):
        C.apply_shard(qs, "1/6")  # more shards than haystacks


def run_shards(tmp_path, lme_file, **kw):
    """Two shards of the LongMemEval fixture, each in its own run directory. Returns (whole summary, [s1, s2])."""
    whole = L.run(args_for(tmp_path, lme_file, out=str(tmp_path / "whole"), systems="bm25,hybrid", granularity="both"))
    dirs = []
    for k in (1, 2):
        d = tmp_path / f"s{k}"
        L.run(args_for(tmp_path, lme_file, out=str(d), systems="bm25,hybrid", granularity="both", shard=f"{k}/2", **kw))
        dirs.append(d)
    return whole, dirs


def test_merged_shards_equal_the_unsharded_run(lme_file, stub_model, tmp_path):
    """The shard directories no longer hold shard_state.json. The scope files under scopes/ carry the exact scores instead."""
    import merge_shards as M

    whole, (s1, s2) = run_shards(tmp_path, lme_file)
    assert not (s1 / "shard_state.json").exists() and len(list((s1 / "scopes").glob("*.json"))) == 2
    assert json.loads((s1 / "summary.json").read_text())["shard"] == "1/2"
    merged = M.merge([s1, s2], tmp_path / "merged")
    assert merged["levels"] == whole["levels"]
    assert "shard" not in merged and merged["merged_scopes"] == 3
    assert (tmp_path / "merged" / "per_question.jsonl").read_bytes() == (tmp_path / "whole" / "per_question.jsonl").read_bytes()
    assert [m["shard"] for m in merged["merged_from"]] == ["1/2", "2/2"]
    for m, d in zip(merged["merged_from"], (s1, s2)):
        assert m["summary_sha256"] == C.sha256_file(d / "summary.json")
        assert m["environment"] == json.loads((d / "summary.json").read_text())["environment"]


def test_shards_can_share_one_scope_directory(lme_file, stub_model, tmp_path):
    import merge_shards as M

    shared = tmp_path / "pool"
    for k in (1, 2):
        L.run(args_for(tmp_path, lme_file, out=str(tmp_path / f"s{k}"), systems="bm25", granularity="session", shard=f"{k}/2", scope_dir=str(shared)))
    whole = L.run(args_for(tmp_path, lme_file, out=str(tmp_path / "whole"), systems="bm25", granularity="session"))
    merged = M.merge([shared], tmp_path / "merged")
    assert merged["levels"] == whole["levels"] and len(merged["merged_from"]) == 1


def tamper_scope(run_dir: Path, fn, which: int = 0) -> None:
    p = sorted((run_dir / "scopes").glob("*.json"))[which]
    d = json.loads(p.read_text())
    fn(d)
    p.write_text(json.dumps(d))


@pytest.mark.parametrize("field", ["benchmark", "dataset_sha256", "systems", "granularity", "depth", "chunking", "rerank", "fusion", "alpha", "embed_profile", "seed", "bootstrap"])
def test_merge_refuses_a_fingerprint_that_differs(field, lme_file, stub_model, tmp_path):
    import merge_shards as M

    _, (s1, s2) = run_shards(tmp_path, lme_file)
    tamper_scope(s1, lambda d: d["fingerprint"].__setitem__(field, "changed"))
    with pytest.raises(SystemExit, match=f"differs from .* in {field}"):
        M.merge([s1, s2], tmp_path / "m")


def test_merge_refuses_different_code(lme_file, stub_model, tmp_path):
    import merge_shards as M

    _, (s1, s2) = run_shards(tmp_path, lme_file)
    tamper_scope(s1, lambda d: d["fingerprint"]["code_sha256"].__setitem__("evals/memory/common.py", "0" * 64))
    with pytest.raises(SystemExit, match="code_sha256.evals/memory/common.py"):
        M.merge([s1, s2], tmp_path / "m")


def test_merge_refuses_a_smoke_shard(lme_file, stub_model, tmp_path):
    import merge_shards as M

    dirs = []
    for k in (1, 2):
        d = tmp_path / f"s{k}"
        L.run(args_for(tmp_path, lme_file, out=str(d), systems="bm25", granularity="session", shard=f"{k}/2", limit_questions=3))
        dirs.append(d)
    with pytest.raises(SystemExit, match="smoke"):
        M.merge(dirs, tmp_path / "m")


def test_merge_refuses_a_missing_scope(lme_file, stub_model, tmp_path):
    import merge_shards as M

    _, (s1, _) = run_shards(tmp_path, lme_file)
    with pytest.raises(SystemExit, match="missing: q3_abs"):
        M.merge([s1], tmp_path / "m")


def test_merge_refuses_an_extra_scope(lme_file, stub_model, tmp_path):
    import merge_shards as M

    _, (s1, s2) = run_shards(tmp_path, lme_file)
    tamper_scope(s2, lambda d: d.__setitem__("scope", "zzz"))
    with pytest.raises(SystemExit, match="extra: zzz"):
        M.merge([s1, s2], tmp_path / "m")


def test_merge_refuses_a_repeated_scope(lme_file, stub_model, tmp_path):
    import merge_shards as M

    _, (s1, s2) = run_shards(tmp_path, lme_file)
    with pytest.raises(SystemExit, match="appears twice"):
        M.merge([s1, s1, s2], tmp_path / "m")


def test_merge_refuses_questions_that_are_not_the_datasets(lme_file, stub_model, tmp_path):
    import merge_shards as M

    _, (s1, s2) = run_shards(tmp_path, lme_file)
    tamper_scope(s1, lambda d: d.__setitem__("qids", d["qids"] + ["bogus"]))
    with pytest.raises(SystemExit, match="does not hold the dataset's questions"):
        M.merge([s1, s2], tmp_path / "m")


def test_merge_refuses_an_unreadable_scope_file(lme_file, stub_model, tmp_path):
    import merge_shards as M

    _, (s1, s2) = run_shards(tmp_path, lme_file)
    sorted((s1 / "scopes").glob("*.json"))[0].write_text('{"format": 1, "fing')
    with pytest.raises(SystemExit, match="not a readable scope file"):
        M.merge([s1, s2], tmp_path / "m")


def test_merge_needs_the_dataset_it_names(lme_file, stub_model, tmp_path):
    import merge_shards as M

    _, (s1, s2) = run_shards(tmp_path, lme_file)
    moved = tmp_path / "moved.json"
    lme_file.rename(moved)
    with pytest.raises(SystemExit, match="cannot find the dataset"):
        M.merge([s1, s2], tmp_path / "m")
    assert M.merge([s1, s2], tmp_path / "m", data=moved)["merged_scopes"] == 3


@pytest.mark.parametrize("path", [("counts",), ("environment", "embed_profile"), ("environment", "code_sha256"), ("config", "seed")])
def test_merge_checks_the_source_summaries_too(path, lme_file, stub_model, tmp_path):
    import merge_shards as M

    _, (s1, s2) = run_shards(tmp_path, lme_file)
    sp = s1 / "summary.json"
    s = json.loads(sp.read_text())
    d = s
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = "changed"
    sp.write_text(json.dumps(s))
    with pytest.raises(SystemExit, match="summary.json differs .* in " + ".".join(path).replace(".", r"\.")):
        M.merge([s1, s2], tmp_path / "m")


def test_merge_refuses_a_foreign_benchmark(lme_file, locomo_file, stub_model, tmp_path):
    import merge_shards as M

    L.run(args_for(tmp_path, lme_file, systems="bm25", granularity="session", shard="1/2", out=str(tmp_path / "s1")))
    K.run(args_for(tmp_path, locomo_file, systems="bm25", granularity="session", shard="2/2", out=str(tmp_path / "s2")))
    with pytest.raises(SystemExit, match="differs from .* in .*benchmark"):
        M.merge([tmp_path / "s1", tmp_path / "s2"], tmp_path / "m")


# ---------------------------------------------------------------- per-scope files, resume, atomic writes

def test_kill_and_resume_equals_an_uninterrupted_run(lme_file, stub_model, tmp_path, monkeypatch):
    whole = L.run(args_for(tmp_path, lme_file, out=str(tmp_path / "whole"), systems="bm25,dense,hybrid", granularity="both"))
    real, built = C.MemoryIndex, []

    class Dies(real):
        def __init__(self, *a, **k):
            if len(built) == 4:  # two scopes are done (two indexes each). The third scope dies on its first index.
                raise RuntimeError("killed")
            built.append(1)
            super().__init__(*a, **k)

    monkeypatch.setattr(C, "MemoryIndex", Dies)
    with pytest.raises(RuntimeError, match="killed"):
        L.run(args_for(tmp_path, lme_file, out=str(tmp_path / "resumed"), systems="bm25,dense,hybrid", granularity="both"))
    assert len(list((tmp_path / "resumed" / "scopes").glob("*.json"))) == 2
    assert not (tmp_path / "resumed" / "summary.json").exists()

    rebuilt = []

    class Counts(real):
        def __init__(self, *a, **k):
            rebuilt.append(1)
            super().__init__(*a, **k)

    monkeypatch.setattr(C, "MemoryIndex", Counts)
    resumed = L.run(args_for(tmp_path, lme_file, out=str(tmp_path / "resumed"), systems="bm25,dense,hybrid", granularity="both"))
    assert len(rebuilt) == 2  # only the third scope, at two granularities
    assert resumed["levels"] == whole["levels"]
    assert (tmp_path / "resumed" / "per_question.jsonl").read_bytes() == (tmp_path / "whole" / "per_question.jsonl").read_bytes()


def test_resume_refuses_files_from_a_different_run_unless_rebuilt(lme_file, stub_model, tmp_path):
    L.run(args_for(tmp_path, lme_file, systems="bm25", granularity="session", depth=20))
    with pytest.raises(SystemExit, match="differs in depth"):
        L.run(args_for(tmp_path, lme_file, systems="bm25", granularity="session", depth=10))
    s = L.run(args_for(tmp_path, lme_file, systems="bm25", granularity="session", depth=10, rebuild=True))
    assert s["config"]["depth_chunks"] == 10
    L.run(args_for(tmp_path, lme_file, systems="bm25", granularity="session", depth=10))  # the rewritten files now match


def test_resume_refuses_a_different_question_list(locomo_file, stub_model, tmp_path):
    K.run(args_for(tmp_path, locomo_file, systems="bm25", granularity="session", conversations=["conv-A"]))
    with pytest.raises(SystemExit, match="differs in .*smoke_subset"):
        K.run(args_for(tmp_path, locomo_file, systems="bm25", granularity="session"))


def test_a_truncated_scope_file_is_recomputed(lme_file, stub_model, tmp_path):
    whole = L.run(args_for(tmp_path, lme_file, systems="bm25", granularity="session"))
    victim = sorted((tmp_path / "out" / "scopes").glob("*.json"))[1]
    victim.write_text(victim.read_text()[:40])
    again = L.run(args_for(tmp_path, lme_file, systems="bm25", granularity="session"))
    assert again["levels"] == whole["levels"] and C.read_scope_file(victim) is not None


def test_scope_names_are_safe_and_distinct():
    assert C.safe_scope_name("conv-26") == "conv-26"
    a, b = C.safe_scope_name("a/b"), C.safe_scope_name("a_b")
    assert "/" not in a and a != b


def test_atomic_write_leaves_the_old_file_when_the_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "summary.json"
    C.atomic_write_text(target, "old\n")

    def crash(src, dst):
        raise OSError("crash between the temp write and the replace")

    monkeypatch.setattr(C.os, "replace", crash)
    with pytest.raises(OSError, match="crash"):
        C.atomic_write_text(target, "new and longer\n")
    monkeypatch.undo()
    assert target.read_text() == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["summary.json"]  # no temp file left behind
    C.atomic_write_text(target, "new\n")
    assert target.read_text() == "new\n"


# ---------------------------------------------------------------- clusters, abstention pool, categories

def test_cluster_bootstrap_resamples_whole_clusters():
    """Six values in three clusters of two: cluster A holds both ones. Whole-cluster draws have mean k/3 for k copies of A, k binomial(3, 1/3).

    P(k=0) is 8/27 and P(k=3) is 1/27, both above 2.5 percent, so the interval is [0, 1]. Resampling the six values one by one reaches a mean of 1 only with probability 3**-6."""
    values, clusters = [1.0, 1.0, 0.0, 0.0, 0.0, 0.0], ["A", "A", "B", "B", "C", "C"]
    assert C.bootstrap_ci(values, clusters=clusters) == [0.0, 1.0]
    assert C.bootstrap_ci(values)[1] < 1.0
    assert C.bootstrap_ci(values, clusters=list("abcdef")) == C.bootstrap_ci(values)  # one value per cluster is the plain bootstrap


def test_cluster_sign_flip_flips_whole_clusters():
    """Six equal differences in two clusters of three. Two signs are drawn, and the flipped mean reaches |1| only when both agree, so p is near 1/2.

    Six separate signs would reach it with probability 2/64."""
    a, b = [1.0] * 6, [0.0] * 6
    by_cluster = C.cluster_permutation_p(a, b, ["X"] * 3 + ["Y"] * 3)
    by_question = C.cluster_permutation_p(a, b, list("abcdef"))
    assert 0.45 < by_cluster["p_value"] < 0.55 and by_cluster["mean_difference"] == 1.0
    assert by_question["p_value"] < 0.05 and by_question == C.stats.permutation_p(a, b)


def test_paired_tests_use_the_row_cluster():
    def rows(vals):
        return [{"qid": f"q{i}", "group": "g", "cluster": "X" if i < 3 else "Y", "scores": {"ndcg@10": v, "recall@10": v}} for i, v in enumerate(vals)]

    out = C.paired_tests({"hybrid": rows([1.0] * 6), "bm25": rows([0.0] * 6)})
    assert 0.45 < out["hybrid_vs_bm25.ndcg@10"]["p_value"] < 0.55


def test_locomo_resamples_conversations_and_lme_resamples_questions(lme_file, locomo_file, stub_model, tmp_path):
    k = K.run(args_for(tmp_path, locomo_file, out=str(tmp_path / "k"), systems="bm25", granularity="session"))
    l = L.run(args_for(tmp_path, lme_file, out=str(tmp_path / "l"), systems="bm25", granularity="session"))
    assert k["metric_notes"]["resampling_unit"].startswith("conversation")
    assert l["metric_notes"]["resampling_unit"].startswith("question")


def test_abstention_pool_drops_missing_scores_and_counts_them():
    out = C.abstention_auroc([5.0, None, 4.0], [1.0, None])
    assert out["n"] == 1 and out["answerable_n"] == 2 and out["auroc"] == 1.0
    assert out["dropped_no_score"] == {"answerable": 1, "abstain": 1}
    # a missing score used to become 0.0 and outrank a negative dense score
    assert C.abstention_auroc([-0.5], [None, -0.9])["auroc"] == 1.0
    assert C.abstention_auroc([1.0], [None])["auroc"] is None


def fake_fallback(query, rows, alias=None, maxchars=None):
    return rows, None, {"fallback": "error"}


def test_rerank_fallback_is_void_and_leaves_the_auroc_pool(lme_file, stub_model, tmp_path, monkeypatch):
    import gestalt_rank

    monkeypatch.setattr(gestalt_rank, "rerank_rows", fake_fallback)
    s = L.run(args_for(tmp_path, lme_file, granularity="session", rerank="on"))
    assert s["rerank_invalid"]["fallbacks"] == 3 and s["config"]["rerank_fallback_queries"] == 3
    lv = s["levels"]["session"]
    assert lv["void_systems"] == ["hybrid_rerank"]
    ab = lv["abstention_auroc"]["hybrid_rerank"]
    assert ab["n"] == 0 and ab["answerable_n"] == 0 and ab["dropped_no_score"] == {"answerable": 2, "abstain": 1}
    assert lv["abstention_auroc"]["hybrid"]["n"] == 1  # the base systems keep their scores


def test_void_rerank_exits_with_code_3_and_the_merge_propagates_it(lme_file, stub_model, tmp_path, monkeypatch):
    import gestalt_rank
    import merge_shards as M

    monkeypatch.setattr(gestalt_rank, "rerank_rows", fake_fallback)
    argv = ["--data", str(lme_file), "--granularity", "session", "--systems", "bm25", "--rerank", "on", "--rerank-depth", "5", "--rerank-model", "stub"]
    assert L.main(argv + ["--out", str(tmp_path / "whole")]) == 3
    for k in (1, 2):
        assert L.main(argv + ["--out", str(tmp_path / f"s{k}"), "--shard", f"{k}/2"]) == 3
    merged = M.merge([tmp_path / "s1", tmp_path / "s2"], tmp_path / "m")
    assert merged["rerank_invalid"]["fallbacks"] == 3 and merged["levels"]["session"]["void_systems"] == ["hybrid_rerank"]
    assert M.main(["--out", str(tmp_path / "m2"), str(tmp_path / "s1"), str(tmp_path / "s2")]) == 3


def test_a_clean_rerank_exits_zero(lme_file, stub_model, tmp_path, monkeypatch):
    import gestalt_rank

    monkeypatch.setattr(gestalt_rank, "rerank_rows", lambda q, rows, alias=None, maxchars=None: (rows, [float(len(rows) - i) for i in range(len(rows))], {"fallback": "none"}))
    argv = ["--data", str(lme_file), "--granularity", "session", "--systems", "bm25", "--rerank", "on", "--rerank-depth", "5", "--out", str(tmp_path / "o")]
    assert L.main(argv) == 0
    assert "rerank_invalid" not in json.loads((tmp_path / "o" / "summary.json").read_text())


def test_base_systems_never_take_the_servers_rerank_rule(lme_file, stub_model, tmp_path, monkeypatch):
    import gestalt_rank

    seen = []
    real = C.harness.search_scored

    def spy(*a, **k):
        seen.append(k.get("rerank", "unset"))
        return real(*a, **k)

    monkeypatch.setattr(C.harness, "search_scored", spy)
    monkeypatch.setattr(gestalt_rank, "rerank_rows", lambda q, rows, alias=None, maxchars=None: (rows, [1.0] * len(rows), {"fallback": "none"}))
    L.run(args_for(tmp_path, lme_file, granularity="session", rerank="on"))
    assert seen and set(seen) == {False}


def test_category_5_is_its_own_group_and_out_of_the_pooled_row(tmp_path, stub_model):
    data = locomo_fixture()
    data[0]["qa"].append({"question": "What did Ann not buy?", "answer": "n", "evidence": ["D1:1"], "category": 5})
    p = tmp_path / "lc.json"
    p.write_text(json.dumps(data))
    default = K.run(args_for(tmp_path, p, out=str(tmp_path / "d"), systems="bm25,hybrid", granularity="session"))
    cells = default["levels"]["session"]["systems"]["bm25"]
    assert cells["5 adversarial"]["n"] == 1 and cells["all"]["n"] == 6
    assert "left out of the pooled" in default["metric_notes"]["adversarial"] and default["config"]["include_adversarial"] is False
    pooled = K.run(args_for(tmp_path, p, out=str(tmp_path / "i"), systems="bm25,hybrid", granularity="session", include_adversarial=True))
    assert pooled["levels"]["session"]["systems"]["bm25"]["all"]["n"] == 7 and pooled["config"]["include_adversarial"] is True


def test_merged_locomo_keeps_the_category_rule(locomo_file, stub_model, tmp_path):
    import merge_shards as M

    whole = K.run(args_for(tmp_path, locomo_file, out=str(tmp_path / "w"), systems="bm25", granularity="session"))
    for k in (1, 2):
        K.run(args_for(tmp_path, locomo_file, out=str(tmp_path / f"s{k}"), systems="bm25", granularity="session", shard=f"{k}/2"))
    merged = M.merge([tmp_path / "s1", tmp_path / "s2"], tmp_path / "m")
    assert merged["levels"] == whole["levels"]
    with pytest.raises(SystemExit, match="include_adversarial"):
        M.merge([tmp_path / "s1", tmp_path / "s2"], tmp_path / "m", include_adversarial=True)


# ---------------------------------------------------------------- the embedding cache

class CountingModel(StubModel):
    def __init__(self):
        super().__init__()
        self.calls = []

    def encode(self, texts, **kw):
        self.calls.append([texts] if isinstance(texts, str) else list(texts))
        return super().encode(texts, **kw)


def test_cached_encoder_keeps_queries_and_drops_documents_per_scope():
    m = CountingModel()
    enc = C.CachedEncoder(m)
    enc.encode(["a b", "c d", "a b"])
    enc.encode("what is a")
    enc.begin_scope()
    enc.encode(["a b"])
    enc.encode("what is a")
    assert m.calls == [["a b", "c d"], ["what is a"], ["a b"]]  # the document is embedded again after the scope change, the query is not
    assert enc.docs.keys() == {"a b"} and enc.queries.keys() == {"what is a"}
