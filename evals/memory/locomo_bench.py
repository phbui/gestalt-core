#!/usr/bin/env python3
"""Score gestalt's retrieval stack on LoCoMo-10 (10 long conversations, 1,986 questions).

Each question searches only its own conversation, as the published write-ups do. The 1,982 questions with evidence are scored. Evidence is a list of `dia_id` turn ids. Session level is the session each evidence turn belongs to. All five categories are reported. Category 5 (adversarial) is a group of its own and stays out of the pooled row unless --include-adversarial is given.

The answer key has a documented error rate of about 6.4 percent (Penfield audit). Retrieval recall is not QA accuracy. Never set these numbers beside a vendor QA number. See evals/memory/README.md.

    python evals/memory/locomo_bench.py --out runs/locomo --granularity both --rerank on
    python evals/memory/locomo_bench.py --out runs/smoke --conversations conv-26 --limit-questions 20 --granularity session   # smoke
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common as C

# The dataset stores the category as a number only. These names follow the order in the LoCoMo paper and task_eval/evaluation.py. [INFERRED]
CATEGORY_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}
ADVERSARIAL = "5 adversarial"
PENFIELD_CAVEAT = "The LoCoMo answer key has a documented 6.4 percent error rate (Penfield audit). Recall here is measured against that key as shipped. Treat gaps under a few points with care."
_DIA = re.compile(r"D:?(\d+):(\d+)")


def parse_evidence(evidence: list[str]) -> list[str]:
    """Pull dia_ids out of the evidence strings. The data holds joined and mistyped ones ('D8:6; D9:17', 'D:11:26'), so every D<n>:<m> match counts."""
    return [f"D{a}:{b}" for e in evidence for a, b in _DIA.findall(e)]


def conversation_sessions(conv: dict) -> list[C.Session]:
    """The sessions of one conversation in order. Turn id is the dia_id. The header carries the session date and the speaker."""
    nums = sorted(int(k.split("_")[1]) for k in conv if re.fullmatch(r"session_\d+", k))
    return [C.Session(f"session_{n}", conv.get(f"session_{n}_date_time", ""),
                      [C.Turn(t["dia_id"], t["speaker"], t["text"]) for t in conv[f"session_{n}"]]) for n in nums]


def load_questions(path: Path, conversations: list[str] | None = None, limit: int | None = None) -> tuple[list[dict], dict, dict]:
    raw = json.loads(Path(path).read_text())
    if conversations:
        raw = [c for c in raw if c["sample_id"] in conversations]
    qs, scopes = [], {}
    counts = {"conversations": len(raw), "questions": 0, "no_evidence": 0, "unresolved_evidence_ids": 0, "scored": 0, "dropped_no_resolved_evidence": 0}
    for conv in raw:
        sessions = conversation_sessions(conv["conversation"])
        scopes[conv["sample_id"]] = sessions
        session_of = {t.tid: s.sid for s in sessions for t in s.turns}
        for i, x in enumerate(conv["qa"]):
            counts["questions"] += 1
            if not x.get("evidence"):
                counts["no_evidence"] += 1
                continue
            ids = parse_evidence(x["evidence"])
            gold = {d for d in ids if d in session_of}
            counts["unresolved_evidence_ids"] += len(ids) - len(gold)
            if not gold:
                counts["dropped_no_resolved_evidence"] += 1
                continue
            counts["scored"] += 1
            qs.append({"qid": f"{conv['sample_id']}#{i}", "group": f"{x['category']} {CATEGORY_NAMES.get(x['category'], '?')}", "query": x["question"],
                       "scope": conv["sample_id"], "cluster": conv["sample_id"], "abstain": False, "gold_turn": gold, "gold_session": {session_of[d] for d in gold}})
    if limit:
        qs = qs[:limit]
        counts["scored"] = len(qs)
    return qs, scopes, counts


def ci_method(n_clusters: int) -> str:
    """Names the interval and the test, with the cluster count and the smallest two-sided p the sign-flip test can reach (2 to the power of one minus the cluster count)."""
    return (f"cluster percentile bootstrap, 95 percent, {n_clusters} clusters (conversations). "
            f"With so few clusters the interval may under-cover. Two-sided sign-flip permutation floor: 2^-{n_clusters - 1} = {2.0 ** -(n_clusters - 1):.6f}.")


def build_summary(info: dict, counts: dict, res: dict, fp: dict, env: dict, shard: str | None = None, include_adversarial: bool = False) -> dict:
    """The summary of a run, from the pooled scope results. The merge tool calls this too, so a pooled summary has the shape of an unsharded one."""
    excluded = () if include_adversarial else (ADVERSARIAL,)
    summary = {
        "generated_utc": C.utc_now(),
        "benchmark": "LoCoMo-10 (retrieval only)",
        "smoke_subset": fp["smoke_subset"],
        "dataset": info,
        "honesty_note": C.HONESTY,
        "answer_key_caveat": PENFIELD_CAVEAT,
        "metric_notes": {
            "recall@k": "share of the gold items found in the top k units",
            "any@k": "at least one gold item in the top k",
            "all@k": "every gold item in the top k",
            "turn_gold": "dia_ids in the evidence list, parsed with D<n>:<m> (the data has joined and mistyped strings)",
            "category_names": "the dataset stores numbers only. Names follow the LoCoMo paper order and are inferred.",
            "adversarial": ("category 5 (adversarial) is reported as its own group and is "
                            + ("included in the pooled `all` row because --include-adversarial was given." if include_adversarial
                               else "left out of the pooled `all` row. Pass --include-adversarial to pool it.")),
            "scope": "each question searches only its own conversation",
            "images": "image captions are not indexed. Only the dialogue text is.",
            "levels": "session = session units. turn = turn units. session_from_turns = the turn ranking folded to sessions, a different system from session units.",
            "resampling_unit": "conversation. Questions about one conversation share its history, so the bootstrap interval and the paired sign-flip test resample whole conversations (10 clusters at most). Intervals are wide for that reason.",
        },
        "counts": counts,
        "ci_method": ci_method(counts["conversations"]),
        "config": C.summary_config(fp, res["rerank_fallbacks"], include_adversarial),
        "environment": env,
        "seconds": res["seconds"],
        "levels": C.summarize_levels(res, fp["systems"], exclude_from_all=excluded),
    }
    C.mark_rerank_void(summary, res["rerank_fallbacks"])
    if shard:
        summary["shard"] = shard
        summary["shard_note"] = "One shard. These numbers are not the benchmark result. Pool all shards with evals/memory/merge_shards.py."
    return summary


def run(args) -> dict:
    info = C.dataset_record(Path(args.data), "locomo10") if args.data else C.fetch_dataset("locomo10")
    qs, scopes, counts = load_questions(Path(info["path"]), args.conversations, args.limit_questions)
    shard = getattr(args, "shard", None)
    qs = C.apply_shard(qs, shard)
    systems, rerank, model = C.resolve_run(args)
    out = Path(args.out)
    bench = Path(__file__).resolve()
    fp = C.run_fingerprint("locomo10", info, systems, args, rerank, bench, bool(args.limit_questions or args.conversations))
    env = C.environment(model, rerank, bench)
    res = C.evaluate(qs, lambda scope: scopes[scope], args.granularity, systems, model, args.depth, rerank, args.batch_size, log=lambda m: print(m, file=sys.stderr),
                     scope_dir=Path(getattr(args, "scope_dir", None) or out / "scopes"), fingerprint=fp, rebuild=getattr(args, "rebuild", False),
                     meta={"environment": env, "dataset_path": info["path"]})
    summary = build_summary(info, counts, res, fp, env, shard, getattr(args, "include_adversarial", False))
    C.write_outputs(out, summary, res["lines"])
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    C.add_common_args(ap)
    ap.add_argument("--conversations", nargs="+", default=None, help="smoke test: only these sample ids, for example conv-26")
    ap.add_argument("--include-adversarial", action="store_true", help="pool category 5 (adversarial) into the `all` row. It is always reported as its own group")
    args = ap.parse_args(argv)
    s = run(args)
    print(f"LoCoMo-10{'  [SMOKE SUBSET]' if s['smoke_subset'] else ''}  sha256 {s['dataset']['sha256']}  counts {s['counts']}")
    print("CAVEAT:", s["answer_key_caveat"])
    for lv, body in s["levels"].items():
        print(C.fmt_table(f"level: {lv}", body["systems"], list(body["systems"])))
        for t, v in body["tests"].items():
            print(f"  {t}: {v['mean_difference']:+.4f}  p={v['p_value']}")
    return C.exit_code(s)


if __name__ == "__main__":
    raise SystemExit(main())
