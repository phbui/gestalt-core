#!/usr/bin/env python3
"""Score gestalt's retrieval stack on public BEIR datasets.

The index uses gestalt's own schema (FTS5 with the porter unicode61 tokenizer, a vec0 table of 768-float
vectors) and the search calls gestalt's own `search()` from run_retrieval_evals.py: the same query
builder, the same two legs, the same reciprocal rank fusion with K=60. The embedding model, its pinned
revision and its prefixes come from tools/gestalt_embed_config.py. Nothing is tuned on the test data.

For each dataset it reports nDCG@10 for three systems, with a seeded 95% bootstrap interval:
  bm25    the FTS5 leg alone
  dense   the sqlite-vec leg alone
  hybrid  both legs fused by RRF (what gestalt_search returns)
and two seeded paired sign-flip permutation tests (hybrid against each single leg).
It writes one JSON file and one TREC-style run file per system under --out.

Run on a GPU machine. Embedding about 9,000 abstracts on a laptop CPU takes tens of minutes.

    python evals/retrieval/beir_bench.py --datasets beir/scifact beir/nfcorpus --out evals/retrieval/results
    python evals/retrieval/beir_bench.py --datasets beir/scifact --limit-docs 300 --out /tmp/smoke   # smoke test

Needs: pip install -r evals/retrieval/requirements-bench.txt
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import platform
import random
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "tools"))

import gestalt_embed_config as ec  # noqa: E402
import run_retrieval_evals as harness  # noqa: E402

SEED = 12345
BOOTSTRAP = 10_000
PERMUTATIONS = 20_000
TOPK = 10


def ndcg_at_10(ranked: list[str], rel: dict[str, int]) -> float:
    dcg = sum(rel.get(d, 0) / math.log2(i + 2) for i, d in enumerate(ranked[:TOPK]))
    ideal = sorted(rel.values(), reverse=True)[:TOPK]
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def build_db(ids: list[str], texts: list[str], model, batch_size: int) -> sqlite3.Connection:
    import sqlite_vec

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(
        """
        CREATE VIRTUAL TABLE sections_fts USING fts5(slug, heading, block_id, content, tokenize='porter unicode61');
        CREATE TABLE sections_meta (
            id INTEGER PRIMARY KEY, slug TEXT NOT NULL, heading TEXT NOT NULL, block_id TEXT, content TEXT NOT NULL,
            file_path TEXT NOT NULL, content_hash TEXT, anchors TEXT, sensitivity TEXT NOT NULL DEFAULT 'unpublished'
        );
        CREATE VIRTUAL TABLE sections_vec USING vec0(id INTEGER PRIMARY KEY, embedding FLOAT[768]);
        """
    )
    vectors = model.encode([ec.DOC_PREFIX + t for t in texts], batch_size=batch_size, convert_to_numpy=True, show_progress_bar=False)
    for i, (doc_id, text, vec) in enumerate(zip(ids, texts, vectors)):
        slug = f"d{i}"  # a token no query contains, so the slug column cannot match lexically
        db.execute("INSERT INTO sections_meta (id, slug, heading, block_id, content, file_path, anchors) VALUES (?, ?, '', NULL, ?, ?, '')", (i, slug, text, doc_id))
        db.execute("INSERT INTO sections_fts (rowid, slug, heading, block_id, content) VALUES (?, ?, '', '', ?)", (i, slug, text))
        db.execute("INSERT INTO sections_vec (id, embedding) VALUES (?, ?)", (i, vec.astype("float32").tobytes()))
    db.commit()
    return db


def dense_only(db: sqlite3.Connection, model, query: str, k: int) -> list[int]:
    qv = model.encode(ec.QUERY_PREFIX + query, convert_to_numpy=True)
    rows = db.execute("SELECT id FROM sections_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance", (qv.astype("float32").tobytes(), k)).fetchall()
    return [r["id"] for r in rows]


def bootstrap_ci(values: list[float]) -> list[float]:
    rng = random.Random(SEED)
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(BOOTSTRAP))
    return [round(means[int(0.025 * BOOTSTRAP)], 4), round(means[int(0.975 * BOOTSTRAP) - 1], 4)]


def permutation_p(a: list[float], b: list[float]) -> dict:
    diffs = [x - y for x, y in zip(a, b)]
    obs = sum(diffs) / len(diffs)
    rng = random.Random(SEED)
    extreme = 0
    for _ in range(PERMUTATIONS):
        flipped = sum(d if rng.random() < 0.5 else -d for d in diffs) / len(diffs)
        if abs(flipped) >= abs(obs) - 1e-15:
            extreme += 1
    return {"mean_difference": round(obs, 4), "p_value": round((extreme + 1) / (PERMUTATIONS + 1), 5), "permutations": PERMUTATIONS}


def code_hashes() -> dict[str, str]:
    """SHA-256 of the three files that decide the numbers, so a result names the exact code behind it."""
    import hashlib

    files = {
        "evals/retrieval/beir_bench.py": HERE / "beir_bench.py",
        "evals/retrieval/run_retrieval_evals.py": HERE / "run_retrieval_evals.py",
        "tools/gestalt_embed_config.py": HERE.parent.parent / "tools" / "gestalt_embed_config.py",
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}


def environment(model) -> dict:
    import numpy
    import sentence_transformers
    import sqlite_vec
    import torch

    try:
        import ir_datasets

        ird = ir_datasets.__version__
    except Exception:
        ird = None
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "sqlite_vec": getattr(sqlite_vec, "__version__", None),
        "numpy": numpy.__version__,
        "torch": torch.__version__,
        "sentence_transformers": sentence_transformers.__version__,
        "ir_datasets": ird,
        "device": str(model.device),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "embedding_model": ec.MODEL_NAME,
        "embedding_revision": ec.MODEL_REVISION,
        "doc_prefix": ec.DOC_PREFIX,
        "query_prefix": ec.QUERY_PREFIX,
        "rrf_k": harness.K_RRF,
        "code_sha256": code_hashes(),
    }


def run_dataset(name: str, out: Path, limit_docs: int | None, batch_size: int, model) -> dict:
    import ir_datasets

    corpus = list(ir_datasets.load(name).docs_iter())
    test = ir_datasets.load(name + "/test")
    qrels: dict[str, dict[str, int]] = {}
    for q in test.qrels_iter():
        qrels.setdefault(q.query_id, {})[q.doc_id] = q.relevance
    queries = [q for q in test.queries_iter() if q.query_id in qrels]
    if limit_docs:
        corpus = corpus[:limit_docs]
        keep = {d.doc_id for d in corpus}
        queries = [q for q in queries if any(d in keep for d in qrels[q.query_id])][:50]
    ids = [d.doc_id for d in corpus]
    texts = [((getattr(d, "title", "") or "") + " " + d.text).strip() for d in corpus]

    t0 = time.time()
    db = build_db(ids, texts, model, batch_size)
    t_index = time.time() - t0

    runs: dict[str, dict[str, list[str]]] = {"bm25": {}, "dense": {}, "hybrid": {}}
    t0 = time.time()
    for q in queries:
        runs["bm25"][q.query_id] = [ids[int(r["slug"][1:])] for r in harness.search(db, q.text, limit=TOPK, mode="fts")]
        runs["hybrid"][q.query_id] = [ids[int(r["slug"][1:])] for r in harness.search(db, q.text, limit=TOPK, mode="hybrid")]
        runs["dense"][q.query_id] = [ids[i] for i in dense_only(db, model, q.text, TOPK)]
    t_search = time.time() - t0

    per_query = {s: [ndcg_at_10(r[q.query_id], qrels[q.query_id]) for q in queries] for s, r in runs.items()}
    safe = name.replace("/", "-")
    out.mkdir(parents=True, exist_ok=True)
    for system, run in runs.items():
        with open(out / f"{safe}.{system}.run", "w") as fh:
            for qid, ranked in run.items():
                for rank, doc_id in enumerate(ranked, 1):
                    fh.write(f"{qid} Q0 {doc_id} {rank} {TOPK - rank + 1} gestalt-{system}\n")  # score is rank-derived

    result = {
        "dataset": name,
        "split": "test",
        "documents": len(ids),
        "queries": len(queries),
        "smoke_subset": bool(limit_docs),
        "metric": "nDCG@10",
        "systems": {
            s: {"ndcg10": round(sum(v) / len(v), 4), "ci95_bootstrap": bootstrap_ci(v), "per_query": [round(x, 5) for x in v]}
            for s, v in per_query.items()
        },
        "tests": {
            "hybrid_vs_bm25": permutation_p(per_query["hybrid"], per_query["bm25"]),
            "hybrid_vs_dense": permutation_p(per_query["hybrid"], per_query["dense"]),
        },
        "seconds": {"index_and_embed": round(t_index, 1), "search_all_queries": round(t_search, 1)},
        "query_ids": [q.query_id for q in queries],
    }
    (out / f"{safe}.json").write_text(json.dumps(result, indent=1) + "\n")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", nargs="+", default=["beir/scifact", "beir/nfcorpus"])
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--limit-docs", type=int, default=None, help="smoke test: first N documents only")
    ap.add_argument("--batch-size", type=int, default=64)
    args = ap.parse_args()

    model = harness.get_model()
    out = Path(args.out)
    env = environment(model)
    (out).mkdir(parents=True, exist_ok=True)
    summary = {"generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "environment": env, "results": {}}
    for name in args.datasets:
        r = run_dataset(name, out, args.limit_docs, args.batch_size, model)
        summary["results"][name] = {k: r[k] for k in ("documents", "queries", "systems", "tests")}
        s = r["systems"]
        print(f"{name}: docs={r['documents']} queries={r['queries']}" + ("  [SMOKE SUBSET]" if r["smoke_subset"] else ""))
        for sysname in ("bm25", "dense", "hybrid"):
            print(f"  {sysname:7s} nDCG@10 {s[sysname]['ndcg10']:.4f}  95% CI {s[sysname]['ci95_bootstrap']}")
        for t, v in r["tests"].items():
            print(f"  {t}: {v['mean_difference']:+.4f}  p={v['p_value']}")
    # drop the bulky per-query arrays from the summary file
    for v in summary["results"].values():
        for s in v["systems"].values():
            s.pop("per_query", None)
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
