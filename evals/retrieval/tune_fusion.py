#!/usr/bin/env python3
"""Pick the lexical weight for convex fusion on held-out clusters (spec 9c, 2026-10-08).

Convex fusion scores a chunk as alpha * minmax(bm25) + (1 - alpha) * minmax(similarity) over each leg's pool. This script finds alpha without fitting on the cases it reports.

    python3 evals/retrieval/tune_fusion.py --build pools.npz    # needs the embedding model and a hybrid index: run it on a GPU machine
    python3 evals/retrieval/tune_fusion.py pools.npz            # grid, split, report, no model needed
    python3 evals/retrieval/tune_fusion.py pools.npz --json out.json

--build runs both legs once per golden case and caches the raw pools, the raw leg scores and the accepted slugs in an .npz file, at both pool sizes the server uses: gestalt_rank.pool_size(10, False) with the rerank off and pool_size(10, True) with it on. Each array is keyed by its pool size, so a grid never reads a pool of the wrong depth. Once golden.yaml carries split tags, only dev cases are cached (assign_splits.py), so the test split never informs alpha.

The tuning split is by cluster: the connected components of accepted slugs, the same clusters assign_splits uses (assign_splits.components). A cluster trains when the sha1 of its sorted members, salted with TRAIN_SALT, is below 7 mod 10. The rest is held out, so no query about a held-out cluster informs alpha. Alpha runs 0 to 1 in steps of 0.1 and is chosen on train by recall@3, then MRR, then the lowest alpha. The report shows alpha* and RRF on the held-out clusters with a paired cluster bootstrap, once per pool size. The case and cluster counts are printed, never written here.

The runner's FTS-only mode has no second leg, so this script refuses an index without vectors.

Public benchmarks. --results reads beir_bench result JSONs written with --fusion-grid and picks one fusion setting on a dev half:

    python3 evals/retrieval/tune_fusion.py --results out/beir-scifact.json out/beir-nfcorpus.json --out fusion-choice.json

Each query goes to dev or test by assign_splits.is_dev, the salted hash choose_config's splits come from, over the query id salted with
GRID_SALT and the dataset name. Only dev per-query nDCG@10 is read. GRID_RULE below states the choice and the safeguard. The script
writes the choice with its dev numbers and prints the GESTALT_FUSION lines that reproduce it. It never reads the test half. To spend the
test half once, pair the chosen system with the best single leg on it:

    python3 evals/retrieval/pair_runs.py out/beir-scifact.json out/beir-scifact.json --system-a hybrid:wrrf@0.2 --system-b dense --split test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "tools"))
import assign_splits
import gestalt_rank
import run_retrieval_evals as runner

ALPHAS = [round(0.1 * i, 1) for i in range(11)]
MAX_RANK = 5  # recall and the printed "mrr" are both cut at this rank, so the latter is MRR@5
TRAIN_SALT = "tune-fusion:"  # independent of the dev/test hash, which is unsalted, so train and held-out both exist inside dev
FIELDS = ("queries", "expect", "accept", "f_ids", "f_sc", "f_off", "v_ids", "v_dist", "v_off", "cand_ids", "cand_slugs")


def served_pools(limit: int = runner.SERVED_DEFAULT_LIMIT) -> tuple[int, ...]:
    """The candidate depths a served call reads per leg: rerank off, then rerank on. One value when they agree."""
    return tuple(sorted({gestalt_rank.pool_size(limit, False), gestalt_rank.pool_size(limit, True)}))


def tuning_cases(cases: list[dict]) -> list[dict]:
    """Answerable cases, restricted to dev once the golden set carries split tags. A half-tagged set is refused."""
    tagged = [c for c in cases if c.get("split")]
    if tagged and len(tagged) != len(cases):
        raise SystemExit(f"{len(cases) - len(tagged)} golden cases carry no split. Run evals/retrieval/assign_splits.py first.")
    keep = [c for c in cases if not runner.is_abstain(c)]
    return [c for c in keep if c["split"] == "dev"] if tagged else keep


def build_cache(db, cases: list[dict], families: list[list[str]], embed, accept_set, pool: int) -> dict:
    """Run both legs once per case at one pool size. Returns the arrays that np.savez stores, plus the pool size.

    embed(query) returns the query vector as the server would build it. Flat arrays with offsets keep the file free of pickles. Abstention cases have no gold and are skipped."""
    queries, expect, accept = [], [], []
    f_ids, f_sc, f_off = [], [], [0]
    v_ids, v_dist, v_off = [], [], [0]
    for case in cases:
        if runner.is_abstain(case):
            continue
        q = case["query"]
        fts_query = gestalt_rank.fts_match(q, False)
        rows = []
        if fts_query:
            try:
                rows = db.execute(
                    "SELECT rowid, rank AS score FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, pool),
                ).fetchall()
            except Exception as e:  # noqa: BLE001  a lexical failure must not silently steer the alpha choice
                print(f"tune_fusion: FTS failed for one query ({type(e).__name__}: {e}); its lexical pool is empty", file=sys.stderr)
                rows = []
        vrows = db.execute(
            "SELECT id, distance FROM sections_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (embed(q).tobytes(), pool),
        ).fetchall()
        f_ids += [r[0] for r in rows]
        f_sc += [r[1] for r in rows]
        f_off.append(len(f_ids))
        v_ids += [r[0] for r in vrows]
        v_dist += [r[1] for r in vrows]
        v_off.append(len(v_ids))
        queries.append(q)
        expect.append(case["expect_slug"])
        accept.append("|".join(sorted(accept_set(case, families))))
    all_ids = sorted(set(f_ids) | set(v_ids))
    slug_of = {}
    for i in range(0, len(all_ids), 500):
        chunk = all_ids[i:i + 500]
        for r in db.execute(f"SELECT id, slug FROM sections_meta WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            slug_of[r[0]] = r[1]
    return {
        "queries": np.array(queries), "expect": np.array(expect), "accept": np.array(accept),
        "f_ids": np.array(f_ids, dtype=np.int64), "f_sc": np.array(f_sc, dtype=np.float64), "f_off": np.array(f_off, dtype=np.int64),
        "v_ids": np.array(v_ids, dtype=np.int64), "v_dist": np.array(v_dist, dtype=np.float64), "v_off": np.array(v_off, dtype=np.int64),
        "cand_ids": np.array(all_ids, dtype=np.int64), "cand_slugs": np.array([slug_of.get(i, "") for i in all_ids]),
        "pool": np.array(pool, dtype=np.int64),
    }


def save_caches(path: str, caches: list[dict]) -> None:
    """One .npz holding every pool size, each array keyed p<pool>_<field>. Written to a temp file and moved into place."""
    arrays = {"pools": np.array([int(c["pool"]) for c in caches], dtype=np.int64)}
    for c in caches:
        arrays.update({f"p{int(c['pool'])}_{k}": c[k] for k in FIELDS})
    target = Path(path)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            np.savez(f, **arrays)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_cache(path: str, pool: int | None = None) -> dict:
    """The arrays of one pool size (the smallest when pool is None). Refuses a cache built before pool sizes were recorded."""
    with np.load(path, allow_pickle=False) as z:
        if "pools" not in z.files:
            raise SystemExit(f"{path} records no pool size. Rebuild it with --build.")
        pools = [int(p) for p in z["pools"]]
        pool = pools[0] if pool is None else pool
        if pool not in pools:
            raise SystemExit(f"{path} holds pool sizes {pools}, not {pool}. Rebuild it with --build.")
        return {**{k: z[f"p{pool}_{k}"] for k in FIELDS}, "pool": pool}


def cache_pools(path: str) -> list[int]:
    with np.load(path, allow_pickle=False) as z:
        return [int(p) for p in z["pools"]] if "pools" in z.files else []


def _case_legs(c: dict, i: int):
    fo, vo = c["f_off"], c["v_off"]
    fts = list(zip(c["f_ids"][fo[i]:fo[i + 1]].tolist(), c["f_sc"][fo[i]:fo[i + 1]].tolist()))
    vec = list(zip(c["v_ids"][vo[i]:vo[i + 1]].tolist(), c["v_dist"][vo[i]:vo[i + 1]].tolist()))
    return fts, vec


def rank_of(order: list[int], slug_of: dict, accept: set[str]) -> int | None:
    for pos, rid in enumerate(order[:MAX_RANK]):
        if slug_of.get(rid) in accept:
            return pos + 1
    return None


def rrf_order(fts: list[tuple], vec: list[tuple]) -> list[int]:
    scores = gestalt_rank.rrf_fuse([[rid for rid, _ in fts], [rid for rid, _ in vec]])
    return sorted(scores, key=scores.get, reverse=True)


def convex_order(fts: list[tuple], vec: list[tuple], alpha: float) -> list[int]:
    scores = gestalt_rank.convex_fuse(fts, vec, alpha)
    return sorted(scores, key=scores.get, reverse=True)


def ranks_for(c: dict, order_fn) -> list[int | None]:
    slug_of = dict(zip(c["cand_ids"].tolist(), [str(s) for s in c["cand_slugs"]]))
    out = []
    for i in range(len(c["queries"])):
        fts, vec = _case_legs(c, i)
        out.append(rank_of(order_fn(fts, vec), slug_of, set(str(c["accept"][i]).split("|"))))
    return out


def score(ranks: list[int | None], idx: list[int]) -> tuple[float, float]:
    """(recall@3, MRR) over the cases in idx."""
    if not idx:
        return 0.0, 0.0
    r3 = sum(1 for i in idx if ranks[i] and ranks[i] <= 3) / len(idx)
    m = sum(1.0 / ranks[i] for i in idx if ranks[i]) / len(idx)
    return r3, m


def clusters_of(c: dict) -> tuple[list[str], dict[str, frozenset[str]]]:
    """Each case's cluster label (smallest slug of its component) and each label's members, from the cached accept sets."""
    accepts = [set(str(a).split("|")) for a in c["accept"]]
    comps = assign_splits.components(accepts)
    label = {s: min(comp) for comp in comps for s in comp}
    return [label[str(e)] for e in c["expect"]], {min(comp): comp for comp in comps}


def is_train(members) -> bool:
    """Stable train or held-out side of a cluster, independent of its dev/test side."""
    return assign_splits.is_dev(members, salt=TRAIN_SALT)


def tune(c: dict, iterations: int = runner.BOOTSTRAP_ITERATIONS) -> dict:
    labels, members = clusters_of(c)
    train = [i for i, cl in enumerate(labels) if is_train(members[cl])]
    held = [i for i, cl in enumerate(labels) if not is_train(members[cl])]
    grid = {a: ranks_for(c, lambda f, v, a=a: convex_order(f, v, a)) for a in ALPHAS}
    rrf = ranks_for(c, rrf_order)
    # Best recall@3 on train, then best MRR, then the lowest alpha (the grid order), so the pick is deterministic.
    best = max(ALPHAS, key=lambda a: (round(score(grid[a], train)[0], 12), round(score(grid[a], train)[1], 12), -a))
    out = {
        "pool": int(c.get("pool", 0)) or None,
        "n_cases": len(labels), "train_n": len(train), "held_out_n": len(held),
        "train_clusters": len({labels[i] for i in train}), "held_out_clusters": len({labels[i] for i in held}),
        "alpha_star": best,
        "train": {f"{a}": dict(zip(("recall@3", "mrr"), map(lambda x: round(x, 4), score(grid[a], train)))) for a in ALPHAS},
        "held_out": {
            "alpha_star": dict(zip(("recall@3", "mrr"), map(lambda x: round(x, 4), score(grid[best], held)))),
            "rrf": dict(zip(("recall@3", "mrr"), map(lambda x: round(x, 4), score(rrf, held)))),
        },
    }

    def mk(ranks):
        return {"results": [{"qid": str(i), "query": str(c["queries"][i]), "expect_slug": str(c["expect"][i]),
                             "cluster": labels[i], "rank": ranks[i]} for i in held]}

    out["bootstrap_held_out"] = runner.cluster_bootstrap(mk(rrf), mk(grid[best]), k=3, iterations=iterations, cluster_key="cluster")
    return out


def report(res: dict) -> None:
    print(f"\nConvex fusion, pool {res['pool']} per leg, alpha grid 0 to 1 by 0.1, {res['n_cases']} cases: "
          f"{res['train_n']} train ({res['train_clusters']} clusters), {res['held_out_n']} held out ({res['held_out_clusters']} clusters)")
    for a, m in res["train"].items():
        print(f"  alpha {a:<4} train recall@3 {m['recall@3']:.3f}  MRR {m['mrr']:.3f}" + ("   <- chosen" if float(a) == res["alpha_star"] else ""))
    h = res["held_out"]
    print(f"\n  held out, alpha* = {res['alpha_star']}: recall@3 {h['alpha_star']['recall@3']:.3f}  MRR {h['alpha_star']['mrr']:.3f}")
    print(f"  held out, RRF           : recall@3 {h['rrf']['recall@3']:.3f}  MRR {h['rrf']['mrr']:.3f}")
    b = res["bootstrap_held_out"]
    print("  " + (b["verdict"] if b else "bootstrap: fewer than 3 shared held-out clusters, no verdict"))


# --- the public-benchmark grid -------------------------------------------------------------------------------

GRID_SALT = "tune-fusion-grid:"
LEGS = ("dense", "bm25")
GRID_RULE = (
    "Dev half only. Each candidate (hybrid and every hybrid:* grid system) scores the mean over datasets of its dev mean nDCG@10. "
    "The best candidate wins, ties to the lower lexical weight, then to the name in sort order. It is kept only when the 95 percent "
    "paired bootstrap interval (bench_stats.bootstrap_ci over the pooled dev per-query differences) of it minus the better single leg "
    "(dense or bm25, by the same dev score) lies above zero. Otherwise the choice is the degenerate setting that equals that leg: "
    "wrrf with lexical weight 0 for dense, 1 for bm25.")


def query_split(dataset: str, qid: str) -> str:
    """dev or test for one benchmark query. pair_runs --split reads the same function."""
    return "dev" if assign_splits.is_dev([qid], salt=f"{GRID_SALT}{dataset}:") else "test"


def dev_scores(result: dict) -> dict[str, dict[str, float]]:
    """{system: {qid: nDCG@10}} over the dev queries of one result JSON. Test entries are skipped before they are read."""
    ids = result["query_ids"]
    dev = [j for j, q in enumerate(ids) if query_split(result["dataset"], q) == "dev"]
    out = {}
    for name, v in result["systems"].items():
        pq = v.get("per_query")
        if pq is None or len(pq) != len(ids):
            raise SystemExit(f"{result['dataset']}: system {name} has no per-query scores aligned with query_ids")
        out[name] = {ids[j]: pq[j] for j in dev}
    return out


def lexical_weight(name: str) -> float:
    """The lexical weight a candidate puts on BM25, for the tie-break. rrf and rescue count as 0.5."""
    _, _, w = name.partition("@")
    return float(w) if w else 0.5


def env_lines(name: str, params: dict | None) -> list[str]:
    """The environment that makes gestalt_search rank like the named system."""
    if name == "hybrid":
        return ["GESTALT_FUSION=rrf"]
    method, _, w = name[len("hybrid:"):].partition("@")
    p = params or {}
    if method == "wrrf":
        return ["GESTALT_FUSION=wrrf", f"GESTALT_FUSION_W_BM25={w}"]
    if method == "convex":
        return ["GESTALT_FUSION=convex", f"GESTALT_FUSION_ALPHA={w}", f"GESTALT_FUSION_NORM={p.get('norm', 'minmax')}",
                f"GESTALT_FUSION_MISSING={p.get('missing', 'zero')}"]
    r = p.get("rescue") or {}
    keys = {"max_rank": "RANK", "min_norm_score": "MIN", "dense_window": "WINDOW", "insert_at": "AT", "max_rescues": "MAX"}
    return ["GESTALT_FUSION=rescue", f"GESTALT_FUSION_NORM={p.get('norm', 'minmax')}"] + [f"GESTALT_RESCUE_{v}={r[k]}" for k, v in keys.items() if k in r]


def tune_grid(results: list[dict]) -> dict:
    """The choice on the dev half of every result, as a JSON-ready dict. See GRID_RULE."""
    from bench_stats import bootstrap_ci

    params = {json.dumps((r.get("query_log_header") or {}).get("fusion_params"), sort_keys=True) for r in results}
    if len(params) != 1:
        raise SystemExit("the results ran under different fusion knobs (query_log_header.fusion_params). Rerun them under one setting")
    fparams = json.loads(params.pop())
    views = [(r["dataset"], dev_scores(r)) for r in results]
    names = set.intersection(*(set(v) for _, v in views))
    if not set(LEGS) <= names:
        raise SystemExit("every result needs the bm25 and dense systems")
    cands = sorted(n for n in names if n.startswith("hybrid:") or (n == "hybrid" and all(
        (r.get("query_log_header") or {}).get("fusion") == "rrf" for r in results)))
    if not any(n.startswith("hybrid:") for n in cands):
        raise SystemExit("no hybrid:* systems. Run beir_bench.py with --fusion-grid first")

    for d, v in views:
        if any(len(v[n]) == 0 for n in names):
            raise SystemExit(f"{d}: the dev split holds no queries for a system, so there is nothing to tune on. Use a larger dataset or a different salt.")

    def score(n: str) -> float:
        return sum(sum(v[n].values()) / len(v[n]) for _, v in views) / len(views)

    dev = {n: round(score(n), 4) for n in sorted(names)}
    best = min(cands, key=lambda n: (-round(score(n), 4), lexical_weight(n), n))  # ties at the printed precision go to the lower lexical weight, as the rule says
    leg = min(LEGS, key=lambda n: (-score(n), n))
    diffs = [v[best][q] - v[leg][q] for _, v in views for q in sorted(v[leg])]
    ci = bootstrap_ci(diffs)
    passed = ci[0] > 0
    chosen = best if passed else f"hybrid:wrrf@{0 if leg == 'dense' else 1}"
    return {
        "rule": GRID_RULE, "split": {"helper": "assign_splits.is_dev", "salt": GRID_SALT + "<dataset>:", "dev_share": assign_splits.DEV_BELOW / 10},
        "datasets": [d for d, _ in views], "n_dev": {d: len(v[leg]) for d, v in views},
        "dev_ndcg10": dev, "best_candidate": best, "best_single_leg": leg,
        "safeguard": {"mean_difference": round(sum(diffs) / len(diffs), 4), "ci95_bootstrap": ci, "passed": passed},
        "chosen": chosen, "env": env_lines(chosen, fparams), "fusion_params": fparams,
        "note": "Every number here comes from the dev half. The test half was not read.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cache", nargs="?", help="pools .npz written by --build")
    ap.add_argument("--results", nargs="+", metavar="JSON", help="beir_bench result JSONs written with --fusion-grid: choose on their dev half")
    ap.add_argument("--out", metavar="PATH", help="with --results, write the choice here")
    ap.add_argument("--build", metavar="NPZ", help="run both legs for every dev case at both served pool sizes and write the cache here (needs the model)")
    ap.add_argument("--json", metavar="PATH")
    ap.add_argument("--iterations", type=int, default=runner.BOOTSTRAP_ITERATIONS)
    args = ap.parse_args()

    if args.results:
        res = tune_grid([json.loads(Path(p).read_text()) for p in args.results])
        print("Rule: " + GRID_RULE)
        for n, v in res["dev_ndcg10"].items():
            print(f"  {n:22s} dev nDCG@10 {v:.4f}" + ("   <- chosen" if n == res["chosen"] else ""))
        g = res["safeguard"]
        print(f"  {res['best_candidate']} minus {res['best_single_leg']}: {g['mean_difference']:+.4f}, 95% CI {g['ci95_bootstrap']}, "
              + ("kept" if g["passed"] else "not above noise, so the single leg stays"))
        print("\n".join(res["env"]))
        if args.out:
            runner.atomic_write_text(args.out, json.dumps(res, indent=2))
            print(f"Wrote {args.out}")
        return
    if args.build:
        if not runner.DB_PATH.exists():
            sys.exit("Search index missing.")
        db = runner.get_db(need_vec=True)
        if not runner.has_vectors(db):
            sys.exit("This index has no vectors. Convex fusion needs both legs: run --build on a machine that has the embedding model.")
        cases, families = runner.load_cases()
        cases = tuning_cases(cases)
        save_caches(args.build, [build_cache(db, cases, families, runner._embed_query, runner.accept_set, p) for p in served_pools()])
        print(f"Wrote {args.build} at pool sizes {list(served_pools())} over {len(cases)} cases")
        return
    if not args.cache:
        ap.error("give a pools .npz, or --build NPZ")
    results = [tune(load_cache(args.cache, p), args.iterations) for p in cache_pools(args.cache) or [None]]
    for res in results:
        report(res)
    if args.json:
        runner.atomic_write_text(args.json, json.dumps({"by_pool": results}, indent=2))
        print(f"Wrote {args.json}")


if __name__ == "__main__":
    main()
