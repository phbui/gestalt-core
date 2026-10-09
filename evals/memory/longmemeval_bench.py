#!/usr/bin/env python3
"""Score gestalt's retrieval stack on LongMemEval-S (500 questions, one chat haystack of about 50 sessions each).

Each question searches only its own haystack. Evidence is `answer_session_ids` at session level and the turns flagged `has_answer` at turn level. The 30 abstention questions (ids ending `_abs`) are searched but left out of every recall metric. They feed one separate number: the AUROC of the top-1 score for telling answerable from abstention questions.

Retrieval recall is not QA accuracy. Never set these numbers beside a vendor QA number. See evals/memory/README.md for the rules and the commands.

    python evals/memory/longmemeval_bench.py --out runs/lme --granularity both --rerank on
    python evals/memory/longmemeval_bench.py --out runs/smoke --systems bm25 --granularity session --limit-questions 20   # smoke
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common as C


def load_questions(path: Path, limit: int | None = None) -> tuple[list[dict], dict, dict]:
    """Parse the JSON into (questions, sessions by question id, counts). The session id never reaches the indexed text."""
    raw = json.loads(Path(path).read_text())
    raw = raw[:limit] if limit else raw
    qs, haystacks = [], {}
    counts = {"questions": len(raw), "abstention": 0, "scored_session": 0, "scored_turn": 0, "no_turn_gold": 0}
    for x in raw:
        sessions, gold_turn = [], set()
        for sid, date, turns in zip(x["haystack_session_ids"], x["haystack_dates"], x["haystack_sessions"]):
            ts = []
            for i, t in enumerate(turns):
                tid = f"{sid}#{i}"
                ts.append(C.Turn(tid, t["role"], t["content"]))
                if t.get("has_answer"):
                    gold_turn.add(tid)
            sessions.append(C.Session(sid, date, ts))
        abstain = x["question_id"].endswith("_abs")
        haystacks[x["question_id"]] = sessions
        qs.append({"qid": x["question_id"], "group": x["question_type"], "query": x["question"], "scope": x["question_id"], "abstain": abstain,
                   "gold_session": set(x["answer_session_ids"]), "gold_turn": gold_turn})
        if abstain:
            counts["abstention"] += 1
        else:
            counts["scored_session"] += bool(x["answer_session_ids"])
            counts["scored_turn"] += bool(gold_turn)
            counts["no_turn_gold"] += not gold_turn
    return qs, haystacks, counts


def build_summary(info: dict, counts: dict, res: dict, fp: dict, env: dict, shard: str | None = None) -> dict:
    """The summary of a run, from the pooled scope results. The merge tool calls this too, so a pooled summary has the shape of an unsharded one."""
    summary = {
        "generated_utc": C.utc_now(),
        "benchmark": "LongMemEval-S (retrieval only)",
        "smoke_subset": fp["smoke_subset"],
        "dataset": info,
        "honesty_note": C.HONESTY,
        "metric_notes": {
            "recall@k": "share of the gold items found in the top k units",
            "any@k": "at least one gold item in the top k",
            "all@k": "every gold item in the top k (LongMemEval's recall_all)",
            "turn_gold": "turns flagged has_answer. Questions with none are left out of turn metrics.",
            "session_gold": "answer_session_ids",
            "abstention": "30 questions ending _abs are skipped in recall. Their AUROC compares top-1 scores across different haystacks, so read it as a signal check only. Queries with no comparable score (nothing returned, or the reranker fell back) are dropped from the pool and counted in dropped_no_score.",
            "levels": "session = session units. turn = turn units. session_from_turns = the turn ranking folded to sessions, a different system from session units.",
            "resampling_unit": "question. Each question has its own haystack, so the bootstrap interval and the paired sign-flip test resample whole questions.",
        },
        "counts": counts,
        "config": C.summary_config(fp, res["rerank_fallbacks"]),
        "environment": env,
        "seconds": res["seconds"],
        "levels": C.summarize_levels(res, fp["systems"]),
    }
    C.mark_rerank_void(summary, res["rerank_fallbacks"])
    if shard:
        summary["shard"] = shard
        summary["shard_note"] = "One shard. These numbers are not the benchmark result. Pool all shards with evals/memory/merge_shards.py."
    return summary


def run(args) -> dict:
    info = C.dataset_record(Path(args.data), "longmemeval_s") if args.data else C.fetch_dataset("longmemeval_s")
    qs, haystacks, counts = load_questions(Path(info["path"]), args.limit_questions)
    shard = getattr(args, "shard", None)
    qs = C.apply_shard(qs, shard)
    systems, rerank, model = C.resolve_run(args)
    out = Path(args.out)
    bench = Path(__file__).resolve()
    fp = C.run_fingerprint("longmemeval_s", info, systems, args, rerank, bench, bool(args.limit_questions))
    env = C.environment(model, rerank, bench)
    res = C.evaluate(qs, lambda scope: haystacks[scope], args.granularity, systems, model, args.depth, rerank, args.batch_size, log=lambda m: print(m, file=sys.stderr),
                     scope_dir=Path(getattr(args, "scope_dir", None) or out / "scopes"), fingerprint=fp, rebuild=getattr(args, "rebuild", False),
                     meta={"environment": env, "dataset_path": info["path"]})
    summary = build_summary(info, counts, res, fp, env, shard)
    C.write_outputs(out, summary, res["lines"])
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    C.add_common_args(ap)
    args = ap.parse_args(argv)
    s = run(args)
    print(f"LongMemEval-S{'  [SMOKE SUBSET]' if s['smoke_subset'] else ''}  sha256 {s['dataset']['sha256']}  counts {s['counts']}")
    for lv, body in s["levels"].items():
        print(C.fmt_table(f"level: {lv}", body["systems"], list(body["systems"])))
        if body["abstention_auroc"]:
            print("  abstention AUROC:", body["abstention_auroc"])
        for t, v in body["tests"].items():
            print(f"  {t}: {v['mean_difference']:+.4f}  p={v['p_value']}")
    return C.exit_code(s)


if __name__ == "__main__":
    raise SystemExit(main())
