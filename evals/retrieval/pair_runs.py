"""pair_runs.py: compare one system from two benchmark runs on the queries they share.

Two dataset JSONs from beir_bench.py come from separate runs. Nothing pairs them. This script does.
It checks that both name the same dataset, metric and subset settings, keeps the query ids they share and
reports how many it dropped. Over those queries it reports the two means, the mean difference, a 95% bootstrap
interval of the difference and a paired sign-flip permutation p. The seed and counts are bench_stats's own.

Per-query scores come from the JSON when it holds them. Otherwise they are recomputed from the .run file beside
each JSON and the qrels, with beir_bench's own scorer. The output says which path was used.

    python evals/retrieval/pair_runs.py A.json B.json --system-a hybrid_rerank --system-b dense [--json OUT] [--split dev|test]

--split keeps the queries on one side of tune_fusion.query_split, the dev and test halves tune_fusion --results uses.

Exit 0 on success, 2 when dataset, metric or subset settings differ (or an input is unusable), 3 when either run
records a reranker fallback.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "tools"))

from bench_stats import BOOTSTRAP, PERMUTATIONS, SEED, bootstrap_ci, permutation_p

SUBSET_KEYS = ("smoke_subset", "subset", "max_docs")


class Refusal(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def load(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise Refusal(2, f"cannot read {path}: {e}")


def check_comparable(a: dict, b: dict) -> None:
    if a.get("dataset") != b.get("dataset"):
        raise Refusal(2, f"dataset differs: {a.get('dataset')!r} vs {b.get('dataset')!r}")
    if a.get("metric") != b.get("metric"):
        raise Refusal(2, f"metric differs: {a.get('metric')!r} vs {b.get('metric')!r}")
    for k in SUBSET_KEYS:
        x, y = a.get(k) or None, b.get(k) or None
        if x != y:
            raise Refusal(2, f"subset setting {k} differs: {x!r} vs {y!r}")
    for label, r in (("A", a), ("B", b)):
        if r.get("rerank_fallback_queries", 0) > 0:
            raise Refusal(3, f"run {label} fell back to the hybrid order on {r['rerank_fallback_queries']} queries")


def read_run(path: Path) -> dict[str, list[str]]:
    """A TREC run file as written by beir_bench: `qid Q0 doc rank score tag`, in rank order."""
    out: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            out.setdefault(parts[0], []).append(parts[2])
    return out


def per_query_scores(path: str, r: dict, system: str) -> tuple[dict[str, float], str]:
    """Map query id to nDCG@10 for one system, and the path used: 'json' or 'run-files'."""
    if system not in r.get("systems", {}):
        raise Refusal(2, f"{path} has no system {system!r}. It has {sorted(r.get('systems', {}))}")
    stored = r["systems"][system].get("per_query")
    ids = r.get("query_ids")
    if stored is not None and ids is not None:
        if len(stored) != len(ids):
            raise Refusal(2, f"{path}: per_query has {len(stored)} scores for {len(ids)} query ids")
        return dict(zip(ids, stored)), "json"
    run_file = Path(path).with_name(f"{r['dataset'].replace('/', '-')}.{system}.run")
    if not run_file.exists():
        raise Refusal(2, f"{path} holds no per-query scores and {run_file} does not exist")
    import beir_bench as bb  # heavy, so only the fallback path pays for it

    qrels, queries, _ = bb.scorable_queries(r["dataset"])
    wanted = ids if ids is not None else [q.query_id for q in queries]
    run = read_run(run_file)
    return {q: bb.ndcg_at_10(run.get(q, []), qrels[q]) for q in wanted if q in qrels}, "run-files"


def identity(r: dict) -> dict:
    m, h = r.get("model") or {}, r.get("query_log_header") or {}
    rr = h.get("rerank") or {}
    return {"embed_profile": m.get("embed_profile"), "embedding_revision": m.get("embedding_revision"),
            "embedding_model": m.get("embedding_model"),
            "reranker_alias": m.get("reranker_alias") or rr.get("model"), "rerank_depth": rr.get("depth"),
            "rerank_instruction": rr.get("instruction")}


def pair(a_path: str, b_path: str, system_a: str, system_b: str, split: str | None = None) -> dict:
    a, b = load(a_path), load(b_path)
    check_comparable(a, b)
    sa, path_a = per_query_scores(a_path, a, system_a)
    sb, path_b = per_query_scores(b_path, b, system_b)
    shared = [q for q in sa if q in sb]
    if split:
        from tune_fusion import (
            query_split,  # loads the runner, so only a split pays for it
        )

        shared = [q for q in shared if query_split(a["dataset"], q) == split]
    if not shared:
        raise Refusal(2, "the two runs share no query ids")
    xa, xb = [sa[q] for q in shared], [sb[q] for q in shared]
    diffs = [x - y for x, y in zip(xa, xb)]
    n = len(shared)
    perm = permutation_p(xa, xb)
    return {
        "dataset": a["dataset"], "metric": a.get("metric"), "system_a": system_a, "system_b": system_b,
        "paired_queries": n, "dropped_from_a": len(sa) - n, "dropped_from_b": len(sb) - n,
        "score_source": {"a": path_a, "b": path_b},
        "mean_a": round(sum(xa) / n, 4), "mean_b": round(sum(xb) / n, 4), "mean_difference": round(sum(diffs) / n, 4),
        "ci95_bootstrap_difference": bootstrap_ci(diffs), "bootstrap": BOOTSTRAP,
        "p_value": perm["p_value"], "permutations": PERMUTATIONS, "seed": SEED,
        "identity_a": identity(a), "identity_b": identity(b),
    }


def render(res: dict) -> str:
    lo, hi = res["ci95_bootstrap_difference"]
    lines = [f"{res['dataset']}  {res['metric']}  paired queries {res['paired_queries']}  "
             f"dropped: {res['dropped_from_a']} from A, {res['dropped_from_b']} from B",
             f"scores from: A {res['score_source']['a']}, B {res['score_source']['b']}", "",
             f"{'':8s}{'system':18s}{'mean':>8s}",
             f"{'A':8s}{res['system_a']:18s}{res['mean_a']:8.4f}",
             f"{'B':8s}{res['system_b']:18s}{res['mean_b']:8.4f}",
             f"{'A - B':8s}{'':18s}{res['mean_difference']:+8.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]  p = {res['p_value']}",
             "", "run identity:"]
    for label in ("a", "b"):
        lines.append(f"  {label.upper()}: " + ", ".join(f"{k}={v}" for k, v in res[f"identity_{label}"].items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a_json")
    ap.add_argument("b_json")
    ap.add_argument("--system-a", required=True, help="system to read from A, for example hybrid_rerank")
    ap.add_argument("--system-b", required=True, help="system to read from B, for example dense")
    ap.add_argument("--json", metavar="OUT", help="also write the result here")
    ap.add_argument("--split", choices=["dev", "test"], default=None, help="keep one half of tune_fusion's dev/test split")
    args = ap.parse_args(argv)
    try:
        res = pair(args.a_json, args.b_json, args.system_a, args.system_b, args.split)
    except Refusal as e:
        print(f"pair_runs: {e}", file=sys.stderr)
        return e.code
    print(render(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
