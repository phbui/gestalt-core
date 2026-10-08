#!/usr/bin/env python3
"""Retrieval quality eval for gestalt_search.

The skill evals in evals/configs/ measure whether a PROMPT routes to the right
SKILL. Nothing measured whether a QUESTION retrieves the right KNOWLEDGE — this
does, against evals/retrieval/golden.yaml.

MIRRORS the MCP server's query path (same FTS5 tokenisation, same RRF K=60,
same candidate depth). It is a reimplementation, not a shared call path — the
server module imports the MCP framework at import time. tests/test_harness_fidelity.py
fails loudly if the two drift.

Usage:
    python3 evals/retrieval/run_retrieval_evals.py            # run + report
    python3 evals/retrieval/run_retrieval_evals.py --json out.json
    python3 evals/retrieval/run_retrieval_evals.py --baseline save
    python3 evals/retrieval/run_retrieval_evals.py --baseline check   # exit 1 on a real regression (net loss AND bootstrap bound)
    python3 evals/retrieval/run_retrieval_evals.py --baseline check --strict   # old rule: any drop
    python3 evals/retrieval/run_retrieval_evals.py --mode fts   # leaf: full-text leg only, never banked
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))  # sibling import under any cwd
try:
    import gestalt_embed_config as _ec
except ImportError:  # a lone copy of this file (a test fixture, a stale install): behave as before X14, unpinned
    class _ec:  # noqa: N801
        MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
        MODEL_REVISION = None
        EMBED_DIM = 768
        DOC_PREFIX = "search_document: "
        QUERY_PREFIX = "search_query: "

EVAL_DIR = Path(__file__).resolve().parent
GESTALT_DIR = EVAL_DIR.parent.parent
DB_PATH = GESTALT_DIR / ".search" / "gestalt.db"
GOLDEN = EVAL_DIR / "golden.yaml"
BASELINE = EVAL_DIR / "baseline.json"

K_RRF = 60
DEPTHS = (1, 3, 5)
# gestalt_search's own default. The eval previously fused over a pool of
# limit*2 where limit=max(DEPTHS)=5, i.e. half the candidates a real default
# call sees — measuring a narrower pipeline than the one actually served.
SERVED_DEFAULT_LIMIT = 10

_model = None


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        # Same model, revision and prefixes as the index builder and the server (X14, 2026-10-06).
        kw = {"revision": _ec.MODEL_REVISION} if getattr(_ec, "MODEL_REVISION", None) else {}
        _model = SentenceTransformer(_ec.MODEL_NAME, trust_remote_code=True, **kw)
    return _model


def get_db(need_vec: bool = True) -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    if need_vec:
        import sqlite_vec

        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    return db


def has_vectors(db) -> bool:
    """True when sections_vec exists and holds rows. A leaf's index has none (X6, 2026-10-06)."""
    try:
        return bool(db.execute("SELECT 1 FROM sections_vec LIMIT 1").fetchone())
    except Exception:
        return False


def search(db, query: str, limit: int = 5, mode: str = "hybrid") -> list[sqlite3.Row]:
    """Mirror of gestalt-mcp-server.py gestalt_search — keep in sync with it."""
    tokens = [t for t in re.split(r"\W+", query) if t]
    fts_query = " OR ".join('"' + t + '"' for t in tokens) if tokens else None

    fts_results = []
    if fts_query:
        try:
            fts_results = db.execute(
                "SELECT rowid, slug, heading, block_id, rank AS score "
                "FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit * 2),
            ).fetchall()
        except Exception:
            fts_results = []

    # Heading/anchor leg — mirrors the server's third leg. Targets section identity,
    # which the body-lexical and dense legs both dilute: a heading is ~5 tokens inside
    # a ~2,000-char chunk.
    vec_results = []
    if mode == "hybrid":  # mode "fts" is the leg the per-prompt hook uses; it needs no model and no vectors
        query_emb = get_model().encode(_ec.QUERY_PREFIX + query, convert_to_numpy=True)
        vec_results = db.execute(
            "SELECT id, distance FROM sections_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (query_emb.tobytes(), limit * 2),
        ).fetchall()

    scores: dict[int, float] = {}
    for rank, row in enumerate(fts_results):
        scores[row["rowid"]] = scores.get(row["rowid"], 0) + 1.0 / (K_RRF + rank + 1)
    for rank, row in enumerate(vec_results):
        scores[row["id"]] = scores.get(row["id"], 0) + 1.0 / (K_RRF + rank + 1)

    top_ids = sorted(scores, key=lambda i: scores[i], reverse=True)[:limit]
    if not top_ids:
        return []
    placeholders = ",".join("?" * len(top_ids))
    rows = db.execute(
        f"SELECT id, slug, heading, block_id, anchors FROM sections_meta "
        f"WHERE id IN ({placeholders})",
        top_ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in top_ids if i in by_id]


def _base_block(block_id: str | None) -> str:
    """Strip the `-pN` suffix added when a section is split into sub-chunks."""
    return re.sub(r"-p\d+$", "", block_id or "")


def load_cases() -> list[dict]:
    import yaml

    data = yaml.safe_load(GOLDEN.read_text())
    return data["cases"]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k hits in n trials. Wald is wrong near 0 and 1 and at small n."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def recall_at(rs, d):
    return sum(1 for r in rs if r["rank"] and r["rank"] <= d) / len(rs) if rs else 0.0


def mrr(rs):
    return sum(1.0 / r["rank"] for r in rs if r["rank"]) / len(rs) if rs else 0.0


def bucket_of(case: dict) -> str:
    return case.get("bucket") or ("hard" if case.get("hard") else "lookup")


def metrics(rs: list[dict]) -> dict:
    """n, distinct targets, recall at each depth with a Wilson interval, MRR."""
    out = {"n": len(rs), "targets": len({r["expect_slug"] for r in rs})}
    for d in DEPTHS:
        k = sum(1 for r in rs if r["rank"] and r["rank"] <= d)
        lo, hi = wilson(k, len(rs))
        out[f"recall@{d}"] = round(k / len(rs), 4) if rs else 0.0
        out[f"recall@{d}_ci"] = [round(lo, 3), round(hi, 3)]
    out["mrr"] = round(mrr(rs), 4)
    return out


def run_cases(db, cases: list[dict], mode: str) -> list[dict]:
    max_depth = max(DEPTHS)
    results = []
    for case in cases:
        hits = search(db, case["query"], limit=SERVED_DEFAULT_LIMIT, mode=mode)[:max_depth]
        slugs = [h["slug"] for h in hits]
        want = case["expect_slug"]
        # expect_any lists every slug that answers the question equally well (X6, 2026-10-06).
        accept = set(case.get("expect_any") or []) | {want}

        rank = next((i + 1 for i, s in enumerate(slugs) if s in accept), None)
        block_ok = None
        if case.get("expect_block"):
            # Oversized sections are split into `<block>-p2`, `-p3`... Comparing
            # raw block_ids scores a correct retrieval as a miss whenever the
            # answer lands in a later part of a split section, which biases this
            # metric downward precisely as entries grow. Compare the base id.
            # A block hit means the retrieved chunk CONTAINS the answer, which is
            # what the consumer actually reads. Two ways that is true:
            #   - the chunk is named by that anchor (block_id), or
            #   - the anchor is defined somewhere inside the chunk.
            # The second case is not a concession. Entries mark sub-facts with a
            # trailing inline `^anchor` mid-section, and the chunker derives block_id
            # only from headings and standalone anchor lines — so 12 of 48 anchored
            # cases had NO chunk whose block_id could ever match, and were scored as
            # misses even when retrieval returned the chunk holding the answer. That
            # was a measurement artifact, not a retrieval failure.
            want_block = case["expect_block"]
            block_ok = any(
                h["slug"] == want
                and (
                    _base_block(h["block_id"]) == want_block
                    or want_block in ((h["anchors"] or "").split(","))
                )
                for h in hits
            )

        results.append(
            {
                "query": case["query"],
                "expect_slug": want,
                "expect_block": case.get("expect_block"),
                "hard": bool(case.get("hard")),
                "bucket": bucket_of(case),
                "rank": rank,
                "block_hit": block_ok,
                "got": slugs[:3],
            }
        )
    return results


def evaluate(mode: str = "auto") -> dict:
    if not DB_PATH.exists():
        sys.exit("Search index missing. Run: python3 tools/gestalt-index-builder.py")

    cases = load_cases()
    db = None
    if mode in ("auto", "hybrid"):
        try:
            db = get_db(need_vec=True)
        except ImportError:
            db = None
        if db is None or not has_vectors(db):
            if mode == "hybrid":
                sys.exit("--mode hybrid needs sqlite_vec and a populated sections_vec. This index has none.")
            db = get_db(need_vec=False)
            mode = "fts"
        else:
            mode = "hybrid"
    else:
        db = get_db(need_vec=False)

    results = run_cases(db, cases, mode)
    # The per-prompt hook uses only the full-text leg, so it gets its own row (X6, 2026-10-06).
    fts_results = results if mode == "fts" else run_cases(db, cases, "fts")

    hard = [r for r in results if r["hard"]]
    easy = [r for r in results if not r["hard"]]
    block_cases = [r for r in results if r["expect_block"]]

    summary = {
        "mode": mode,
        "embed": {"model": _ec.MODEL_NAME, "revision": getattr(_ec, "MODEL_REVISION", None)},
        "n_cases": len(results),
        "recall": {f"@{d}": round(recall_at(results, d), 4) for d in DEPTHS},
        "mrr": round(mrr(results), 4),
        "hard": {
            "n": len(hard),
            "recall@3": round(recall_at(hard, 3), 4),
            "mrr": round(mrr(hard), 4),
        },
        "easy": {
            "n": len(easy),
            "recall@3": round(recall_at(easy, 3), 4),
            "mrr": round(mrr(easy), 4),
        },
        "block_precision": (
            round(sum(1 for r in block_cases if r["block_hit"]) / len(block_cases), 4)
            if block_cases
            else None
        ),
        "overall": metrics(results),
        "buckets": {b: metrics([r for r in results if r["bucket"] == b]) for b in sorted({r["bucket"] for r in results})},
        "fts_only": {
            "overall": metrics(fts_results),
            "buckets": {b: metrics([r for r in fts_results if r["bucket"] == b]) for b in sorted({r["bucket"] for r in fts_results})},
        },
    }
    return {"summary": summary, "results": results, "fts_results": fts_results}


def significance(before: dict, after: dict) -> dict | None:
    """Exact two-sided sign test on queries that changed hit/miss state.

    N here is small enough that a raw percentage delta is not evidence. Two
    flips in the same direction give p=0.5 — a coin flip. Reporting this next
    to the delta is the difference between a measurement and a claim.
    """
    b = {r["query"]: bool(r["rank"] and r["rank"] <= 3) for r in before.get("results", [])}
    a = {r["query"]: bool(r["rank"] and r["rank"] <= 3) for r in after.get("results", [])}
    shared = set(b) & set(a)
    if not shared:
        return None
    gained = sum(1 for q in shared if a[q] and not b[q])
    lost = sum(1 for q in shared if b[q] and not a[q])
    n = gained + lost
    if n == 0:
        return {"gained": 0, "lost": 0, "p_value": 1.0, "verdict": "no change"}
    # Exact two-sided binomial p at q=0.5.
    from math import comb

    k = min(gained, lost)
    p = min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / (2**n))
    return {
        "gained": gained,
        "lost": lost,
        "p_value": round(p, 4),
        "verdict": "significant at 0.05" if p < 0.05 else "NOT significant — report as noise",
    }


def cluster_bootstrap(
    before: dict, after: dict, k: int = 3, iterations: int = 10000, seed: int = 12345
) -> dict | None:
    """Paired bootstrap on recall@k, resampling by TARGET DOCUMENT, not by query.

    Replaces the sign test as the primary signal. Two reasons, both from the IR
    evaluation literature:

    1. Sign and Wilcoxon are the two tests that literature explicitly recommends
       AGAINST. Smucker, Allan & Carterette (CIKM 2007,
       dl.acm.org/doi/10.1145/1321440.1321528) found "the randomization, bootstrap,
       and t-tests all agreed with each other while the Wilcoxon and sign tests
       neither agreed with the other tests nor each other". Ihemelandu & Ekstrand
       (arxiv.org/abs/2305.02461) add that sign/Wilcoxon false-positive rates *rise*
       with sample size. A sign test also throws away magnitude, keeping only
       direction, which is most of the available information at this scale.

    2. The trials are CLUSTERED: 62 queries target only ~21 documents, so queries
       sharing a target are correlated. Resampling queries independently understates
       variance and overstates significance. The standard remedy is to resample whole
       clusters as atomic units (block bootstrap) — here, a document together with
       every query aimed at it.

    Reported WITH the effect size, not as a bare pass/fail: Ihemelandu & Ekstrand
    recommend "both the p-value, and effect size be reported ... and used for decision
    making". A single gate cannot distinguish "no signal" from "underpowered", which
    is exactly the confusion this eval hit at 25 queries.
    """
    import random

    def by_cluster(run: dict) -> dict[str, list[bool]]:
        out: dict[str, list[bool]] = {}
        for r in run.get("results", []):
            hit = bool(r["rank"] and r["rank"] <= k)
            out.setdefault(r["expect_slug"], []).append(hit)
        return out

    b, a = by_cluster(before), by_cluster(after)
    clusters = sorted(set(b) & set(a))
    # Only clusters with the same number of queries in both runs are comparable;
    # a changed golden set makes the two runs different experiments, not two
    # measurements of one.
    clusters = [c for c in clusters if len(b[c]) == len(a[c])]
    if len(clusters) < 3:
        return None

    def recall(d: dict[str, list[bool]], keys: list[str]) -> float:
        flat = [h for c in keys for h in d[c]]
        return sum(flat) / len(flat) if flat else 0.0

    observed = recall(a, clusters) - recall(b, clusters)

    rng = random.Random(seed)
    n = len(clusters)
    ge = 0
    for _ in range(iterations):
        draw = [clusters[rng.randrange(n)] for _ in range(n)]
        # Centre the resampled difference on zero to test the null that the two runs
        # are the same; count how often |centred| reaches |observed|.
        d = (recall(a, draw) - recall(b, draw)) - observed
        if abs(d) >= abs(observed):
            ge += 1
    p = (ge + 1) / (iterations + 1)  # add-one, so p is never reported as exactly 0

    return {
        "metric": f"recall@{k}",
        "effect_size": round(observed, 4),
        "clusters": n,
        "p_value": round(p, 4),
        "iterations": iterations,
        "verdict": (
            f"recall@{k} moved {observed:+.1%} (p={p:.3f}, "
            f"{n} document clusters) — "
            + ("distinguishable from noise" if p < 0.05 else "NOT distinguishable from noise")
        ),
    }


def effective_n(results: list[dict]) -> int:
    """Distinct target documents. Multiple queries on one document are not
    independent trials, so this is the honest denominator for confidence."""
    return len({r["expect_slug"] for r in results})


def paired_drop(before: dict, after: dict, k: int = 3, iterations: int = 10000, seed: int = 12345) -> dict | None:
    """Regression rule from RC-ai-research section 1 (X6, 2026-10-06).

    A drop is real only when BOTH hold: the net number of cases lost at recall@k reaches
    max(2, 3 percent of n), and the 95th percentile of the paired cluster-bootstrap
    difference (after minus before) is still below zero. The run is deterministic for a fixed
    index and fixed cases, so the noise is sampling noise across cases. A size test alone
    flags one unlucky flip. A bound alone flags one lost case in a large cluster.
    Returns None when the two runs share too few queries to compare.
    """
    import math
    import random

    b = {r["query"]: (r["expect_slug"], bool(r["rank"] and r["rank"] <= k)) for r in before.get("results", [])}
    a = {r["query"]: (r["expect_slug"], bool(r["rank"] and r["rank"] <= k)) for r in after.get("results", [])}
    shared = sorted(set(b) & set(a))
    clusters: dict[str, list[tuple[bool, bool]]] = {}
    for q in shared:
        clusters.setdefault(b[q][0], []).append((b[q][1], a[q][1]))
    if len(clusters) < 3:
        return None
    n = len(shared)
    net_lost = sum(1 for q in shared if b[q][1] and not a[q][1]) - sum(1 for q in shared if a[q][1] and not b[q][1])
    threshold = max(2, math.ceil(0.03 * n))
    keys = sorted(clusters)
    rng = random.Random(seed)
    diffs = []
    for _ in range(iterations):
        draw = [clusters[keys[rng.randrange(len(keys))]] for _ in keys]
        flat = [p for c in draw for p in c]
        diffs.append(sum(y for _, y in flat) / len(flat) - sum(x for x, _ in flat) / len(flat))
    diffs.sort()
    lo, hi = diffs[int(0.05 * iterations)], diffs[int(0.95 * iterations)]
    return {
        "metric": f"recall@{k}",
        "n_shared": n,
        "net_lost": net_lost,
        "threshold": threshold,
        "ci90": [round(lo, 4), round(hi, 4)],
        "real_drop": net_lost >= threshold and hi < 0,
    }


def _bucket_line(name: str, m: dict) -> str:
    lo, hi = m["recall@5_ci"]
    return (f"  {name:<10} n={m['n']:<3} targets={m['targets']:<3} r@1 {m['recall@1']:.1%}  r@3 {m['recall@3']:.1%}  "
            f"r@5 {m['recall@5']:.1%} [{lo:.0%}-{hi:.0%}]  MRR {m['mrr']:.3f}")


def report(out: dict) -> None:
    s = out["summary"]
    print(f"\nRetrieval eval — {s['n_cases']} golden queries, mode={s.get('mode', 'hybrid')}\n")
    if s.get("mode") == "fts":
        print("  FTS-only run: no vectors in this index. These are NOT hybrid numbers and must not be banked.\n")
    for d in DEPTHS:
        print(f"  recall@{d}: {s['recall'][f'@{d}']:.1%}")
    print(f"  MRR:       {s['mrr']:.3f}")
    print(f"  block precision: "
          f"{s['block_precision']:.1%}" if s["block_precision"] is not None else "  block precision: n/a")
    print(f"  effective N: {effective_n(out['results'])} distinct target documents "
          f"across {s['n_cases']} queries (clustered trials are not independent)")
    print(f"\n  easy (n={s['easy']['n']}): recall@3 {s['easy']['recall@3']:.1%}  MRR {s['easy']['mrr']:.3f}")
    print(f"  hard (n={s['hard']['n']}): recall@3 {s['hard']['recall@3']:.1%}  MRR {s['hard']['mrr']:.3f}")
    if "buckets" in s:
        print("\n  per bucket (r@5 carries a 95% Wilson interval):")
        print(_bucket_line("ALL", s["overall"]))
        for name, m in s["buckets"].items():
            print(_bucket_line(name, m))
        print("\n  FTS-only leg (what the per-prompt hook uses):")
        print(_bucket_line("ALL", s["fts_only"]["overall"]))
        for name, m in s["fts_only"]["buckets"].items():
            print(_bucket_line(name, m))

    misses = [r for r in out["results"] if not r["rank"] or r["rank"] > 3]
    if misses:
        print(f"\n  MISSES (not in top 3) — {len(misses)}:")
        for m in misses:
            got = ", ".join(m["got"]) or "nothing"
            tag = " [hard]" if m["hard"] else ""
            print(f"    - {m['query'][:62]!r}{tag}")
            print(f"        want {m['expect_slug']}, got: {got}")

    block_misses = [r for r in out["results"] if r["expect_block"] and not r["block_hit"]]
    if block_misses:
        print(f"\n  BLOCK MISSES (right doc, wrong section) — {len(block_misses)}:")
        for m in block_misses:
            print(f"    - {m['query'][:62]!r} → wanted ^{m['expect_block']}")
    print()


def check_baseline(out: dict, base: dict, prev_run: dict | None, strict: bool = False) -> list[str]:
    """Return the regression lines. Empty means pass. Prints the interval it used."""
    s = out["summary"]
    cur, ref = s, base
    if s.get("mode") == "fts":
        # An FTS-only run compares only with a banked FTS-only row. Never with hybrid numbers.
        ref = base.get("fts_only", {}).get("overall")
        if not ref:
            print("  FTS-only run and no banked fts_only row: nothing to compare. Skipping the gate.")
            return []
        cur = s["fts_only"]["overall"]
        cur = {"recall": {f"@{d}": cur[f"recall@{d}"] for d in DEPTHS}, "mrr": cur["mrr"]}
        ref = {"recall": {f"@{d}": ref[f"recall@{d}"] for d in DEPTHS}, "mrr": ref["mrr"]}
    regressed = []
    if strict:
        for d in DEPTHS:
            k = f"@{d}"
            if cur["recall"][k] < ref["recall"][k] - 1e-9:
                regressed.append(f"recall{k}: {ref['recall'][k]:.1%} → {cur['recall'][k]:.1%}")
        if cur["mrr"] < ref["mrr"] - 1e-9:
            regressed.append(f"MRR: {ref['mrr']:.3f} → {cur['mrr']:.3f}")
        return regressed
    if prev_run is None:
        print("  no baseline-results.json: the paired rule needs per-case results. No gate. Use --strict to compare headlines.")
        return []
    runs = (prev_run, out)
    if s.get("mode") == "fts":
        runs = (prev_run.get("fts_results") or [], out["fts_results"])
        runs = ({"results": runs[0]}, {"results": runs[1]})
        if not runs[0]["results"]:
            print("  banked results hold no fts_results. No gate.")
            return []
    for d in DEPTHS:
        pd = paired_drop(runs[0], runs[1], k=d)
        if pd is None:
            print(f"  recall@{d}: not comparable (golden set changed, or <3 shared clusters). Treat as a new baseline.")
            continue
        print(f"  recall@{d}: net {-pd['net_lost']:+d} cases of {pd['n_shared']} shared, "
              f"bootstrap 90% interval of the change [{pd['ci90'][0]:+.3f}, {pd['ci90'][1]:+.3f}], "
              f"fail needs net loss >= {pd['threshold']} and upper bound < 0")
        if pd["real_drop"]:
            regressed.append(f"recall@{d}: lost {pd['net_lost']} net cases, upper bound {pd['ci90'][1]:+.3f} < 0")
    return regressed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="write full results as JSON")
    ap.add_argument("--baseline", choices=["save", "check"])
    ap.add_argument("--mode", choices=["auto", "hybrid", "fts"], default="auto",
                    help="auto uses the hybrid path when the index has vectors, else the FTS leg only")
    ap.add_argument("--strict", action="store_true",
                    help="old rule: fail on any drop in a headline number (default: net loss AND bootstrap bound)")
    args = ap.parse_args()

    out = evaluate(args.mode)
    report(out)

    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2))
        print(f"Wrote {args.json}")

    if args.baseline == "save":
        if out["summary"]["mode"] != "hybrid":
            print("Refusing to bank an FTS-only run as the baseline. Run this on the hub, where the index has vectors.")
            sys.exit(1)
        BASELINE.write_text(json.dumps(out["summary"], indent=2))
        BASELINE.with_name("baseline-results.json").write_text(json.dumps(out, indent=2))
        print(f"Baseline saved to {BASELINE}")
    elif args.baseline == "check":
        if not BASELINE.exists():
            # Exit non-zero: a CI step gating on the exit code alone would otherwise
            # read "no baseline" as "no regression". Matches run_evals.baseline_check.
            print("No baseline yet — run --baseline save first.")
            sys.exit(1)
        base = json.loads(BASELINE.read_text())
        prev = BASELINE.with_name("baseline-results.json")
        prev_run = json.loads(prev.read_text()) if prev.exists() else None
        if prev_run and out["summary"]["mode"] == "hybrid":  # an FTS run against hybrid results is not a paired comparison
            sig = significance(prev_run, out)
            if sig:
                print(f"\n  flips vs previous run: +{sig['gained']} / -{sig['lost']} "
                      f"(sign-test p={sig['p_value']}, secondary — see cluster_bootstrap)")
            boot = cluster_bootstrap(prev_run, out)
            if boot:
                print(f"  {boot['verdict']}")
        regressed = check_baseline(out, base, prev_run, strict=args.strict)
        if regressed:
            print("REGRESSION:")
            for r in regressed:
                print(f"  - {r}")
            sys.exit(1)
        print("No retrieval regression.")


if __name__ == "__main__":
    main()
