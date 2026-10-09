#!/usr/bin/env python3
"""Score gestalt's retrieval stack on public BEIR datasets.

The corpus is stored by bench_engine.py: a file-backed, contentless FTS5 table (porter unicode61 tokenizer, the columns of build_db below)
and float32 embedding blocks of EMBED_DIM floats (768 for the default nomic profile). Dense search uses sqlite-vec up to 300,000 documents
and exact blocked search above. The query builder (gestalt_rank.fts_match) and the leg depths (gestalt_rank.pool_size) are gestalt's own.
The fusion is NOT a call into gestalt_rank.hybrid_search. bench_engine.fuse_pools calls gestalt_rank.fuse, the fusion step the server runs,
whose arithmetic lives in evals/retrieval/fusion.py. hybrid is reciprocal rank fusion at K=60 unless --fusion says otherwise. tests/test_beir_bench.py
checks that the rankings equal the old in-memory path. The embedding model, its pinned revision and its prefixes come from tools/gestalt_embed_config.py.
The reranker instruction is dataset-specific: the Qwen rerankers prepend GESTALT_RERANK_INSTRUCTION, and its default names a personal knowledge
base. A public set needs its own task line, so a Qwen number carries that choice. Nothing else is tuned on the test data.
Each result records eight hashed files (code_hashes) and the rerank instruction and max chars (environment.rerank).

For each dataset it reports nDCG@10 for three systems, with a seeded 95% bootstrap interval:
  bm25    the FTS5 leg alone
  dense   the sqlite-vec leg alone
  hybrid  both legs fused by RRF (what gestalt_search returns)
and two seeded paired sign-flip permutation tests (hybrid against each single leg).
It writes one JSON file and one TREC-style run file per system under --out. With --rerank on there is a fourth system, hybrid_rerank.
The run file's score column holds the system's own score, oriented so higher is better: the negated FTS5 rank for bm25, the negated L2
distance for dense, the fused score for hybrid and the grid systems, the reranker score for hybrid_rerank. Rows are written in rank order.

Run on a GPU machine. Embedding about 9,000 abstracts on a laptop CPU takes tens of minutes.

    python evals/retrieval/beir_bench.py --datasets beir/scifact beir/nfcorpus --out evals/retrieval/results
    python evals/retrieval/beir_bench.py --datasets beir/scifact --limit-docs 300 --out /tmp/smoke   # smoke test
    python evals/retrieval/beir_bench.py --all-public --max-docs 200000 --out evals/retrieval/results-subset   # every public set, capped

Any public BEIR set runs by its ir_datasets id (--datasets beir/fiqa beir/scidocs ...). --all-public expands to the 15 public sets. CQADupStack is
one entry that runs its 12 sub-forums and reports their mean, the way BEIR does. --max-docs N caps the corpus for the million-document sets:
the relevant documents of the test queries are always kept and the rest is filled in corpus order. A capped run is labelled "subset" in
summary.json and in the stdout line. It is easier than the real benchmark and must not be quoted as the benchmark.

Flags that change the system under test:
    --embed-profile nomic|qwen3-4b   the embedding profile from tools/gestalt_embed_config.py (sets GESTALT_EMBED_PROFILE)
    --rerank on                      add a fourth system, hybrid_rerank: the top --rerank-depth hybrid hits re-scored by tools/gestalt_rank.rerank_rows
    --fusion convex --alpha 0.4      score the hybrid system with convex fusion instead of RRF (sets GESTALT_FUSION and GESTALT_FUSION_ALPHA)
    --fusion-grid wrrf=0,0.1,0.2 convex=0,0.1 rescue
                                     add one system per setting, fused from the same two pools as hybrid: hybrid:wrrf@W (weighted RRF,
                                     lexical weight W), hybrid:convex@A (convex fusion, lexical weight A) and hybrid:rescue. hybrid stays
                                     as it is. Each gets a run file and per-query nDCG for evals/retrieval/tune_fusion.py --results. Their
                                     query log is a separate file, so adding a grid never invalidates the plain run's log

Resumable runs. Every phase of a dataset lives in a work directory (--work-dir, default `work/` beside --out)
and resumes where a kill stopped it: the stored documents, the FTS5 index, the embeddings in blocks of
--block-size documents, and every finished query. bench_engine.py describes the phases. A second run of the
same dataset embeds nothing that is already embedded. Corpora above 300,000 documents use exact blocked
search instead of sqlite-vec (GESTALT_BENCH_DENSE=vec|exact|auto). work/<dataset>/status.json names the phase.

Needs: pip install -r evals/retrieval/requirements-bench.txt
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "tools"))


def _export_flag_env(argv: list[str]) -> None:
    """Turn --embed-profile, --fusion and --alpha into the environment variables the shared modules read at import.

    gestalt_embed_config and the runner decide their profile and fusion once, when they are imported. The
    imports below run before argparse does, so this reads the three flags first. argparse still validates them."""
    names = {"--embed-profile": "GESTALT_EMBED_PROFILE", "--fusion": "GESTALT_FUSION", "--alpha": "GESTALT_FUSION_ALPHA"}
    for i, arg in enumerate(argv):
        flag, eq, val = arg.partition("=")
        if flag in names:
            val = val if eq else (argv[i + 1] if i + 1 < len(argv) else "")
            if val:
                os.environ[names[flag]] = val


if __name__ == "__main__":  # only the script reads its own argv; an importer (tests, the memory harness) never has its environment changed
    _export_flag_env(sys.argv[1:])

import bench_engine as engine  # noqa: E402
import gestalt_embed_config as ec  # noqa: E402
import run_retrieval_evals as harness  # noqa: E402
from bench_engine import select_capped  # noqa: E402,F401  the one subset rule, shared with mteb_bench
from resumable import DEFAULT_BLOCK_SIZE, QueryLog, atomic_write, atomic_write_json  # noqa: E402
import heldout_datasets  # noqa: E402

heldout_datasets.register()  # the sealed held-out sets (litsearch, techqa) become dataset names; they refuse to read until opened

# The statistics, code hashes and environment record live in bench_stats (no argv or os.environ side effects at import).
# They are re-exported here so every existing `beir_bench.X` keeps working.
from bench_stats import (BOOTSTRAP, PERMUTATIONS, SEED, TOPK, _python_stream, _rows_for, _split_text, bootstrap_ci, code_hashes,  # noqa: E402,F401
                         environment, ndcg_at_10, permutation_p, split_text)

# The 15 public BEIR sets as ir_datasets ids. CQADupStack is a group of 12 sub-forums (CQADUPSTACK below).
PUBLIC_BEIR = [
    "beir/msmarco", "beir/trec-covid", "beir/nfcorpus", "beir/nq", "beir/hotpotqa", "beir/fiqa", "beir/arguana",
    "beir/webis-touche2020", "beir/cqadupstack", "beir/quora", "beir/dbpedia-entity", "beir/scidocs", "beir/fever",
    "beir/climate-fever", "beir/scifact",
]
CQADUPSTACK = ["android", "english", "gaming", "gis", "mathematica", "physics", "programmers", "stats", "tex", "unix", "webmasters", "wordpress"]
# Where the test qrels live, as a suffix on the dataset id. The default is /test. These sets keep them elsewhere.
QRELS_SUFFIX = {
    "beir/msmarco": "/dev", "beir/webis-touche2020": "/v2", "beir/arguana": "", "beir/climate-fever": "",
    "beir/scidocs": "", "beir/trec-covid": "", **{f"beir/cqadupstack/{c}": "" for c in CQADUPSTACK},
}
LEGACY_DATASETS = ("beir/scifact", "beir/nfcorpus")


def expand_datasets(names: list[str], all_public: bool) -> list[str]:
    """The dataset ids to run. --all-public adds the 15 public sets after any named ones, without repeats."""
    out: list[str] = []
    for n in list(names) + (PUBLIC_BEIR if all_public else []):
        if n not in out:
            out.append(n)
    return out


def build_db(ids: list[str], texts: list[str], model, batch_size: int) -> sqlite3.Connection:
    import sqlite_vec

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(
        f"""
        CREATE VIRTUAL TABLE sections_fts USING fts5(slug, title, heading, block_id, content, tokenize='porter unicode61');
        CREATE TABLE sections_meta (
            id INTEGER PRIMARY KEY, slug TEXT NOT NULL, heading TEXT NOT NULL, block_id TEXT, content TEXT NOT NULL,
            file_path TEXT NOT NULL, content_hash TEXT, anchors TEXT, sensitivity TEXT NOT NULL DEFAULT 'unpublished',
            title TEXT NOT NULL DEFAULT ''
        );
        CREATE VIRTUAL TABLE sections_vec USING vec0(id INTEGER PRIMARY KEY, embedding FLOAT[{ec.EMBED_DIM}]);
        """
    )
    vectors = ec.postprocess(model.encode([ec.DOC_PREFIX + t for t in texts], batch_size=batch_size, convert_to_numpy=True, show_progress_bar=False))
    for i, (doc_id, text, vec) in enumerate(zip(ids, texts, vectors)):
        slug = f"d{i}"  # a token no query contains, so the slug column cannot match lexically
        db.execute("INSERT INTO sections_meta (id, slug, heading, block_id, content, file_path, anchors) VALUES (?, ?, '', NULL, ?, ?, '')", (i, slug, text, doc_id))
        db.execute("INSERT INTO sections_fts (rowid, slug, heading, block_id, content) VALUES (?, ?, '', '', ?)", (i, slug, text))
        db.execute("INSERT INTO sections_vec (id, embedding) VALUES (?, ?)", (i, vec.astype("float32").tobytes()))
    db.commit()
    return db


def rerank_ids(db: sqlite3.Connection, query: str, ids: list[str], depth: int, alias: str) -> tuple[list[str], dict]:
    """The hybrid pool re-scored by tools/gestalt_rank.rerank_rows, as (BEIR doc ids best first, the call's info dict).

    gestalt_rank is imported here so this file loads without it. The pool is the top `depth` hybrid hits.
    Rows carry title, heading and content, the three fields the reranker reads. BEIR text already holds the
    title, so the title field is empty."""
    import gestalt_rank

    pool = harness.search(db, query, limit=depth, mode="hybrid", rerank=False)  # the base pool; this function reranks it itself
    rows = []
    for r in pool:
        row = dict(r)
        if "content" not in row:
            row["content"] = db.execute("SELECT content FROM sections_meta WHERE slug = ?", (row["slug"],)).fetchone()["content"]
        row.setdefault("title", "")
        row.setdefault("heading", "")
        rows.append(row)
    ranked, _scores, info = gestalt_rank.rerank_rows(query, rows, alias)
    return [ids[int(r["slug"][1:])] for r in ranked][:TOPK], info


# The header keys that decide a ranking and that a finished dataset JSON must repeat. corpus_fingerprint is left out
# because load_finished does not open the corpus. The dataset name and the subset labels cover it.
IDENTITY_KEYS = ("fusion", "alpha", "stopwords", "rrf_k", "rerank", "code_sha256")


def parse_fusion_grid(items: list[str] | None) -> list[str]:
    """Grid system names from --fusion-grid items: wrrf=W,W,... convex=A,A,... and rescue. Weights run from 0 to 1. Order kept, repeats dropped."""
    out: list[str] = []
    for item in items or []:
        method, eq, weights = item.partition("=")
        if method == "rescue" and not eq:
            names = ["hybrid:rescue"]
        elif method in ("wrrf", "convex") and weights:
            names = []
            for w in weights.split(","):
                try:
                    v = float(w)
                except ValueError:
                    v = float("nan")
                if not 0.0 <= v <= 1.0:
                    raise SystemExit(f"--fusion-grid {item}: {w!r} is not a weight from 0 to 1")
                names.append(f"hybrid:{method}@{v:g}")
        else:
            raise SystemExit(f"--fusion-grid takes wrrf=W,..., convex=A,... or rescue, got {item!r}")
        for n in names:
            if n not in out:
                out.append(n)
    return out


def run_identity(rerank: dict | None, fusion: str, alpha: float, systems: list[str] | None = None) -> dict:
    """What a dataset JSON records so a later --resume can tell whether this run would reproduce it.

    query_log_header is the engine's header (fusion, alpha, stopwords, RRF K, and for a reranked run the alias,
    depth, max chars and instruction). model names the embedding model, its pinned revision, profile and load
    path, and the reranker's alias and the model id that alias resolves to on this machine."""
    header = engine.query_log_header(fingerprint="", systems=systems or list(engine.BASE_SYSTEMS), limit=TOPK, rerank=rerank,
                                     fusion=fusion, alpha=alpha, fill_from_hybrid=False)
    header["code_sha256"] = code_hashes()  # a result or a query log from other code is never reused, however equal its knobs
    alias = harness.gestalt_rank.resolve_alias(rerank["model"]) if rerank else None
    model = {"embedding_model": ec.MODEL_NAME, "embedding_revision": ec.MODEL_REVISION, "embed_profile": ec.PROFILE,
             "embedding_load_path": ec.LOAD_PATH,
             "reranker_alias": rerank["model"] if rerank else None,
             "reranker_model_id": harness.gestalt_rank.RERANK_MODELS.get(alias, alias) if rerank else None}
    return {"query_log_header": header, "model": model}


def load_finished(out: Path, name: str, rerank_on: bool, limit_docs: int | None, max_docs: int | None, expected: dict) -> dict | None:
    """A dataset result already on disk that this run would reproduce, or None.

    It must hold the systems this run asks for (hybrid_rerank only with --rerank on), carry the same subset labels, and record no reranker
    fallback. It must also record the configuration of `expected` (run_identity): reranker alias and resolved model id, depth, fusion,
    alpha, rerank instruction, max chars, embedding profile and model revision or load path. A file that lacks those fields is a
    mismatch. A file from another configuration is never reused, because a stale number under a fresh label is worse than a rerun."""
    path = out / f"{name.replace('/', '-')}.json"
    try:
        r = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if r.get("dataset") != name or bool(r.get("smoke_subset")) != bool(limit_docs) or r.get("max_docs") != max_docs:
        return None
    if ("hybrid_rerank" in r.get("systems", {})) != rerank_on or r.get("rerank_fallback_queries"):
        return None
    if not {"bm25", "dense", "hybrid"} <= set(r.get("systems", {})) or not r.get("tests"):
        return None
    header = r.get("query_log_header")
    if not isinstance(header, dict) or r.get("model") != expected["model"]:
        return None
    if any(key not in header or header[key] != expected["query_log_header"][key] for key in IDENTITY_KEYS):
        return None
    # A grid run needs its grid systems and the fusion knobs they ran under. A plain RRF run has neither, as before.
    grid = [s for s in expected["query_log_header"]["systems"] if engine.grid_system(s)]
    if not set(grid) <= set(r.get("systems", {})) or header.get("fusion_params") != expected["query_log_header"].get("fusion_params"):
        return None
    return r


def stale_query_log(path: Path, header: dict) -> bool:
    """True when `path` holds a query log whose header equals `header` apart from code_sha256: same knobs, other code."""
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.loads(fh.readline()).get("header") or {}
    except (OSError, ValueError):
        return False
    strip = lambda h: {k: v for k, v in h.items() if k != "code_sha256"}  # noqa: E731
    if strip(old) == strip(header) and old.get("code_sha256") != header.get("code_sha256"):
        print(f"beir_bench: {path.name} was written by other code, re-querying instead of replaying it", file=sys.stderr, flush=True)
        return True
    return False


def default_work_dir(out: Path) -> Path:
    """`work/` beside --out. Corpora there are shared by every --out directory that sits beside it."""
    return out.parent / "work"


def dataset_key(name: str, limit_docs: int | None, max_docs: int | None, profile: str | None = None) -> str:
    """The work subdirectory of one dataset under one subset setting and one embedding profile.

    A capped corpus is another corpus. So is a corpus embedded by another profile: the nomic default keeps the bare name, and any
    other profile gets its own directory, so two profiles under one --work-dir never collide. On 2026-10-08 the 4B runs shared the
    nomic work directory and were refused for a fingerprint mismatch twice before anyone pointed --work-dir elsewhere."""
    profile = ec.PROFILE if profile is None else profile
    tag = "" if profile in (None, "", "nomic") else f"-{profile}"
    return name.replace("/", "-") + tag + (f"-max{max_docs}" if max_docs else "") + (f"-limit{limit_docs}" if limit_docs else "")


# FEVER and Climate-FEVER ship as two corpora of the same Wikipedia abstracts, 25 documents apart in size
# (ir_datasets etc/metadata.json). Each may copy the other's vectors for documents with equal id and text.
CORPUS_DONORS = {"beir/fever": "beir/climate-fever", "beir/climate-fever": "beir/fever"}


def ensure_work_root(work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    ignore = work_dir / ".gitignore"
    if not ignore.exists():  # vectors and indexes are tens of gigabytes and never belong in a commit
        atomic_write(ignore, "*\n")


def scorable_queries(name: str):
    """Load the test qrels and the queries that can be scored.

    Returns `(qrels, queries, excluded)`.
    """
    import ir_datasets

    test = ir_datasets.load(name + QRELS_SUFFIX.get(name, "/test"))
    qrels: dict[str, dict[str, int]] = {}
    for q in test.qrels_iter():
        qrels.setdefault(q.query_id, {})[q.doc_id] = max(q.relevance, 0)  # BEIR scores a negative judgement as 0
    judged = [q for q in test.queries_iter() if q.query_id in qrels]
    # A query without a positive judgement has no ideal ranking, so nDCG is undefined for it. pytrec_eval
    # drops such queries and so does mteb (_filter_queries_without_positives). They are counted, not scored.
    queries = [q for q in judged if any(r > 0 for r in qrels[q.query_id].values())]
    return qrels, queries, len(judged) - len(queries)


def _build_corpus(name: str, key: str, wd: Path, work_dir: Path, qrels: dict, queries: list, limit_docs: int | None,
                  max_docs: int | None, batch_size: int, model, block_size: int, rebuild: bool, status, dense_mode: str | None):
    """Build or resume the corpus index and open the dense search.

    Returns `(fts, dense, queries, n_docs, t_index)`. Raises `SystemExit` when no query is scorable.
    """
    import ir_datasets

    if max_docs:
        relevant = {d for q in queries for d, r in qrels[q.query_id].items() if r > 0}

    def stream():
        docs = ir_datasets.load(name).docs_iter()
        if max_docs:
            docs = iter(select_capped(docs, relevant, max_docs))
        for n, d in enumerate(docs):
            if limit_docs and n >= limit_docs:
                break
            yield d.doc_id, engine.doc_text(getattr(d, "title", ""), d.text)

    t0 = time.time()
    donors = [work_dir / dataset_key(CORPUS_DONORS[name], limit_docs, max_docs)] if name in CORPUS_DONORS else []
    fts, store = engine.prepare_corpus(wd, key, stream, model=model, ec=ec, batch_size=batch_size, block_size=block_size,
                                       rebuild=rebuild, status=status, donors=donors)
    n_docs = fts.n_docs()
    if max_docs:
        print(f"beir_bench: NOTE {name} corpus capped at --max-docs {max_docs} ({n_docs} documents kept). This is a SUBSET run, not the benchmark. Do not quote it.", file=sys.stderr)
    if limit_docs:
        held = set(fts.lookup(sorted({d for q in queries for d in qrels[q.query_id]})))
        queries = [q for q in queries if any(d in held for d in qrels[q.query_id])][:50]
    if not queries:
        raise SystemExit(f"beir_bench: {name} has no scorable query over the {n_docs} documents kept. Raise --limit-docs.")
    t_index = time.time() - t0
    dense = engine.open_dense(store, n_docs, ec.EMBED_DIM, dense_mode)
    print(f"beir_bench: {name} {n_docs} documents ready in {t_index:.0f}s, dense search {dense.name}, {len(queries)} queries", file=sys.stderr, flush=True)
    return fts, dense, queries, n_docs, t_index


def _run_systems(name: str, out: Path, wd: Path, fts, dense, queries: list, model, rerank: dict | None, fusion, alpha,
                 rebuild: bool, status, grid: list[str] = ()):
    """Run every system over every query, with the query log resuming.

    Returns `(runs, rerank_fallbacks, header, t_search, scores)`. scores are the logged scores, as bench_engine.scores_from_log gives them.
    """
    systems = list(engine.BASE_SYSTEMS) + (["hybrid_rerank"] if rerank else []) + list(grid)
    header = engine.query_log_header(fingerprint=fts.get("fingerprint"), systems=systems, limit=TOPK, rerank=rerank,
                                     fusion=fusion, alpha=alpha, fill_from_hybrid=False)
    header["code_sha256"] = code_hashes()
    out_tag = hashlib.sha256(str(out.resolve()).encode()).hexdigest()[:8]
    if grid:  # its own log, so a grid never invalidates the log of the plain run in the same --out
        out_tag += "-grid-" + hashlib.sha256("\n".join(grid).encode()).hexdigest()[:8]
    # A query log written by other code is re-queried, not replayed. On 2026-10-08 a rerun meant to put the final code's
    # hashes on the headline numbers replayed 623 logged queries in 67 s, so the recorded hashes would have named code
    # that never ran them. Any other header difference is still a refusal, handled by QueryLog itself.
    rebuild = rebuild or stale_query_log(wd / f"queries-{out.name}-{out_tag}.jsonl", header)
    t0 = time.time()
    # Stall guard (GESTALT_EVAL_QUERY_TIMEOUT, default 300 s): the status callback runs once per finished query, so a gap
    # longer than the timeout between two calls is one stuck query batch. The process then exits with code 4.
    dog = harness.StallWatchdog(harness.query_timeout(), what="query batch", prefix=f"beir_bench: {name}")
    dog.arm("after 0 finished queries")
    beats = [0]

    def beat_status(*a, **k):
        beats[0] += 1
        dog.arm(f"after {beats[0]} finished queries")
        if status:
            status(*a, **k)

    try:
        with QueryLog(wd / f"queries-{out.name}-{out_tag}.jsonl", header, rebuild=rebuild) as log:
            engine.retrieve([(q.query_id, q.text) for q in queries], fts=fts, dense=dense,
                            embed=lambda texts: engine.encode_queries(model, texts, ec.QUERY_PREFIX, ec.postprocess),
                            log=log, systems=systems, limit=TOPK, rerank=rerank, fusion=fusion, alpha=alpha, status=beat_status)
            runs, rerank_fallbacks = engine.runs_from_log(log, systems, [q.query_id for q in queries])
            scores = engine.scores_from_log(log, systems, [q.query_id for q in queries])
    finally:
        dog.close()
    fts.close()
    t_search = time.time() - t0
    if rerank and rerank_fallbacks:
        print(f"beir_bench: WARNING rerank fell back to the hybrid order on {rerank_fallbacks} of {len(queries)} queries for {name}", file=sys.stderr)
    return runs, rerank_fallbacks, header, t_search, scores


# The systems whose logged score is smaller-is-better: bm25 logs FTS5's rank and dense logs the L2 distance.
LOWER_IS_BETTER = ("bm25", "dense")


def run_score(system: str, score: float) -> float:
    """The score a run file carries, oriented so higher is better."""
    return 0.0 - score if system in LOWER_IS_BETTER else score


def _score_and_write(name: str, out: Path, wd: Path, runs: dict, rerank_fallbacks: int, header: dict, queries: list, qrels: dict,
                     excluded: int, n_docs: int, t_index: float, t_search: float, limit_docs: int | None,
                     max_docs: int | None, rerank: dict | None, identity: dict, scores: dict) -> dict:
    """Score the runs, write the TREC run files and the result JSON, and mark the status done.

    A run file line is `qid Q0 doc rank score tag`, in rank order, with the system's real score (run_score). Tools that sort by
    score, as trec_eval does, can reorder exact ties. Every reader in this directory reads the lines in rank order."""
    per_query = {s: [ndcg_at_10(r[q.query_id], qrels[q.query_id]) for q in queries] for s, r in runs.items()}
    safe = name.replace("/", "-")
    out.mkdir(parents=True, exist_ok=True)
    for system, run in runs.items():
        lines = [f"{qid} Q0 {doc_id} {rank} {run_score(system, sc)!r} gestalt-{system}\n"
                 for qid, ranked in run.items() for rank, (doc_id, sc) in enumerate(zip(ranked, scores[system][qid]), 1)]
        atomic_write(out / f"{safe}.{system}.run", "".join(lines))

    result = {
        "dataset": name,
        "split": "test",
        "documents": n_docs,
        "queries": len(queries),
        "queries_excluded_no_positive": excluded,
        "smoke_subset": bool(limit_docs),
        **({"subset": True, "max_docs": max_docs} if max_docs else {}),
        "metric": "nDCG@10",
        "query_log_header": header,
        "model": identity["model"],
        "systems": {
            s: {"ndcg10": round(sum(v) / len(v), 4), "ci95_bootstrap": bootstrap_ci(v), "per_query": [round(x, 5) for x in v]}
            for s, v in per_query.items()
        },
        "tests": {
            "hybrid_vs_bm25": permutation_p(per_query["hybrid"], per_query["bm25"]),
            "hybrid_vs_dense": permutation_p(per_query["hybrid"], per_query["dense"]),
            **({"rerank_vs_hybrid": permutation_p(per_query["hybrid_rerank"], per_query["hybrid"])} if rerank else {}),
        },
        **({"rerank_fallback_queries": rerank_fallbacks} if rerank else {}),
        "seconds": {"index_and_embed": round(t_index, 1), "search_all_queries": round(t_search, 1)},
        "query_ids": [q.query_id for q in queries],
    }
    atomic_write_json(out / f"{safe}.json", result)
    atomic_write_json(wd / "status.json", {"phase": "done", "done": len(queries), "total": len(queries),
                                           "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return result


def refuse_cpu(model, smoke: bool = False) -> None:
    """Refuse to score on the CPU. A run that lost its GPU, or never saw one, would report numbers from a CPU fallback.

    A subset run (--limit-docs or --max-docs) is exempt, because the clean-clone reproduction runs one on a CPU and a subset's
    numbers are never published. GESTALT_BENCH_ALLOW_CPU=1 or an explicit GESTALT_EMBED_DEVICE (for example cpu) allows a full run. A model that
    names no device is not checked."""
    if smoke or os.environ.get("GESTALT_BENCH_ALLOW_CPU", "").strip() == "1" or os.environ.get("GESTALT_EMBED_DEVICE", "").strip():
        return
    device = str(getattr(model, "device", "") or "")
    if device.startswith("cpu"):
        raise SystemExit("beir_bench: the embedding model is on the CPU, so this run would score on a CPU fallback. "
                         "Fix the GPU, or set GESTALT_BENCH_ALLOW_CPU=1 (or GESTALT_EMBED_DEVICE=cpu) to run on the CPU on purpose.")


def run_dataset(name: str, out: Path, limit_docs: int | None, batch_size: int, model, rerank: dict | None = None,
                max_docs: int | None = None, resume: bool = False, work_dir: Path | None = None,
                block_size: int = DEFAULT_BLOCK_SIZE, rebuild: bool = False, dense_mode: str | None = None, grid: list[str] = ()) -> dict:
    """Score one BEIR dataset. Every phase resumes from the work directory (see bench_engine). grid names the --fusion-grid systems."""
    refuse_cpu(model, smoke=bool(limit_docs or max_docs))
    fusion = harness.gestalt_rank.fusion_mode()
    alpha = harness.gestalt_rank.fusion_alpha()
    identity = run_identity(rerank, fusion, alpha, list(engine.BASE_SYSTEMS) + list(grid) if grid else None)
    if resume:
        done = load_finished(out, name, bool(rerank), limit_docs, max_docs, identity)
        if done is not None:
            print(f"beir_bench: {name} resumed from {out}/{name.replace('/', '-')}.json, not recomputed", file=sys.stderr, flush=True)
            return done
    work_dir = Path(work_dir) if work_dir else default_work_dir(out)
    ensure_work_root(work_dir)
    key = dataset_key(name, limit_docs, max_docs)
    wd = work_dir / key
    status = engine.Status(wd / "status.json", f"beir_bench: {name}")

    qrels, queries, excluded = scorable_queries(name)
    fts, dense, queries, n_docs, t_index = _build_corpus(name, key, wd, work_dir, qrels, queries, limit_docs, max_docs,
                                                         batch_size, model, block_size, rebuild, status, dense_mode)
    runs, rerank_fallbacks, header, t_search, scores = _run_systems(name, out, wd, fts, dense, queries, model, rerank, fusion, alpha,
                                                                    rebuild, status, grid)
    return _score_and_write(name, out, wd, runs, rerank_fallbacks, header, queries, qrels, excluded, n_docs, t_index, t_search,
                            limit_docs, max_docs, rerank, identity, scores)


def finish_summary(summary: dict, names: list[str], max_docs: int | None) -> dict:
    """Add the mean nDCG@10 over the sets run and the subset label. The legacy two-set run gets neither, so its file is unchanged."""
    if not max_docs and all(n in LEGACY_DATASETS for n in names):
        return summary
    means = {}
    for r in summary["results"].values():
        for s, v in r["systems"].items():
            means.setdefault(s, []).append(v["ndcg10"])
    summary["mean_ndcg10"] = {s: round(sum(v) / len(v), 4) for s, v in means.items()}
    summary["datasets_run"] = len(summary["results"])
    summary["subset"] = bool(max_docs)
    if max_docs:
        summary["max_docs"] = max_docs
        summary["note"] = "Corpus capped. This is a SUBSET run. It is easier than the benchmark and must not be quoted as the benchmark."
    return summary


def cqadupstack_aggregate(subs: dict[str, dict], limit_docs: int | None, max_docs: int | None, rerank_on: bool) -> dict:
    """The CQADupStack entry from its 12 forum results.

    The headline ndcg10 of each system is the unweighted mean of the 12 forum values, the BEIR convention.
    The interval and the paired tests run on the pooled per-query scores of all forums, because a mean of
    means has no per-query sample of its own. ndcg10_pooled is that pooled mean, for reference. Reranker
    fallbacks add up over the forums, so one failing forum voids the aggregate row."""
    first = next(iter(subs.values()))
    pooled = {s: [x for c in CQADUPSTACK if c in subs for x in subs[c]["systems"][s]["per_query"]] for s in first["systems"]}
    systems = {s: {"ndcg10": round(sum(r["systems"][s]["ndcg10"] for r in subs.values()) / len(subs), 4),
                   "ndcg10_pooled": round(sum(v) / len(v), 4), "ci95_bootstrap": bootstrap_ci(v)}
               for s, v in pooled.items()}
    tests = {"hybrid_vs_bm25": permutation_p(pooled["hybrid"], pooled["bm25"]),
             "hybrid_vs_dense": permutation_p(pooled["hybrid"], pooled["dense"]),
             **({"rerank_vs_hybrid": permutation_p(pooled["hybrid_rerank"], pooled["hybrid"])} if rerank_on else {})}
    return {"dataset": "beir/cqadupstack", "documents": sum(r["documents"] for r in subs.values()), "queries": sum(r["queries"] for r in subs.values()),
            "queries_excluded_no_positive": sum(r.get("queries_excluded_no_positive", 0) for r in subs.values()),
            "smoke_subset": bool(limit_docs), "systems": systems, "tests": tests,
            "ci_basis": "The interval and tests use the pooled per-query scores of the forums. ndcg10 is the unweighted mean of the forum means.",
            **({"rerank_fallback_queries": sum(r.get("rerank_fallback_queries", 0) for r in subs.values())} if rerank_on else {}),
            "subforums": sorted(subs), **({"subset": True, "max_docs": max_docs} if max_docs else {})}


def run_cqadupstack(out: Path, limit_docs: int | None, batch_size: int, model, rerank: dict | None, max_docs: int | None, resume: bool = False,
                    **work) -> dict:
    """CQADupStack as BEIR reports it: run the 12 sub-forums and average their nDCG@10 per system."""
    subs = {c: run_dataset(f"beir/cqadupstack/{c}", out, limit_docs, batch_size, model, rerank, max_docs, resume, **work) for c in CQADUPSTACK}
    result = cqadupstack_aggregate(subs, limit_docs, max_docs, bool(rerank))
    atomic_write_json(out / "beir-cqadupstack.json", result)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", nargs="+", default=["beir/scifact", "beir/nfcorpus"])
    ap.add_argument("--all-public", action="store_true", help="also run the 15 public BEIR sets (PUBLIC_BEIR above)")
    ap.add_argument("--max-docs", type=int, default=None,
                    help="cap each corpus at N documents (relevant ones always kept). The run is labelled subset and must not be quoted as the benchmark")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--limit-docs", type=int, default=None, help="smoke test: first N documents only")
    ap.add_argument("--batch-size", type=int, default=64, help="largest encode batch. Long texts get smaller batches (GESTALT_BENCH_TOKEN_BUDGET, GESTALT_BENCH_ATTN_BUDGET)")
    ap.add_argument("--embed-profile", choices=sorted(ec.PROFILES), default=None,
                    help="embedding profile (read before the imports above, so it applies to the whole run)")
    ap.add_argument("--rerank", choices=["off", "on"], default="off", help="add the hybrid_rerank system (needs tools/gestalt_rank.py)")
    ap.add_argument("--rerank-model", default=os.environ.get("GESTALT_RERANK_MODEL", "bge"), help="reranker alias: bge, qwen3-4b or qwen3-0.6b")
    ap.add_argument("--rerank-depth", type=int, default=40, help="hybrid hits handed to the reranker")
    ap.add_argument("--fusion", choices=["rrf", "convex"], default=None, help="hybrid fusion (sets GESTALT_FUSION)")
    ap.add_argument("--alpha", type=float, default=None, help="convex fusion weight on the lexical leg (sets GESTALT_FUSION_ALPHA)")
    ap.add_argument("--fusion-grid", nargs="+", default=None, metavar="SPEC",
                    help="extra systems fused from the same pools as hybrid: wrrf=W,... convex=A,... rescue. hybrid itself is unchanged")
    ap.add_argument("--resume", action="store_true",
                    help="reuse a dataset result already in --out when it matches this run (same subset labels, same systems, no reranker fallback). "
                         "Lets a run that a GPU fault cut short continue with the next dataset")
    ap.add_argument("--work-dir", default=None, help="where documents, indexes, embeddings and finished queries persist (default: work/ beside --out)")
    ap.add_argument("--engine", choices=["auto", "blocks"], default="auto",
                    help="both use the resumable block engine. auto searches with sqlite-vec up to 300,000 documents and exactly above. "
                         "blocks searches exactly for every corpus. GESTALT_BENCH_DENSE=vec|exact overrides both")
    ap.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE, help="documents per embedding block, the unit a restart redoes")
    ap.add_argument("--rebuild", action="store_true",
                    help="discard work-dir state built under another configuration instead of refusing it. Matching state is kept")
    args = ap.parse_args()
    if args.embed_profile and args.embed_profile != ec.PROFILE:
        raise SystemExit(f"--embed-profile {args.embed_profile} did not take effect (module loaded profile {ec.PROFILE})")
    rerank = {"model": args.rerank_model, "depth": args.rerank_depth} if args.rerank == "on" else None
    grid = parse_fusion_grid(args.fusion_grid)
    import gestalt_rank  # fail now, not after an hour of embedding; the engine and the environment record need it with or without a reranker

    print(f"beir_bench: profile={ec.PROFILE} dim={ec.EMBED_DIM} rerank={args.rerank} fusion={gestalt_rank.fusion_settings()['method']}", file=sys.stderr)
    engine.cap_gpu_memory()
    model = harness.get_model()
    refuse_cpu(model, smoke=bool(args.limit_docs or args.max_docs))
    out = Path(args.out)
    dense_mode = os.environ.get("GESTALT_BENCH_DENSE") or ("exact" if args.engine == "blocks" else "auto")
    env = environment(model, rerank, block_size=args.block_size, batch_size=args.batch_size, dense_mode=dense_mode)
    out.mkdir(parents=True, exist_ok=True)
    summary = {"generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "environment": env,
               "smoke_subset": bool(args.limit_docs), "results": {}}
    work = {"work_dir": Path(args.work_dir) if args.work_dir else default_work_dir(out), "block_size": args.block_size, "rebuild": args.rebuild,
            "dense_mode": dense_mode}
    if grid:
        work["grid"] = grid
    names = expand_datasets(args.datasets, args.all_public)
    for name in names:
        if name == "beir/cqadupstack":
            r = run_cqadupstack(out, args.limit_docs, args.batch_size, model, rerank, args.max_docs, args.resume, **work)
        else:
            r = run_dataset(name, out, args.limit_docs, args.batch_size, model, rerank, args.max_docs, args.resume, **work)
        summary["results"][name] = {k: r[k] for k in ("documents", "queries", "queries_excluded_no_positive", "smoke_subset", "subset", "max_docs",
                                                      "systems", "tests", "rerank_fallback_queries") if k in r}
        s = r["systems"]
        print(f"{name}: docs={r['documents']} queries={r['queries']}" + ("  [SMOKE SUBSET]" if r["smoke_subset"] else "") + ("  [SUBSET: --max-docs, not the benchmark]" if args.max_docs else ""))
        for sysname in s:
            print(f"  {sysname:7s} nDCG@10 {s[sysname]['ndcg10']:.4f}  95% CI {s[sysname]['ci95_bootstrap']}")
        for t, v in r["tests"].items():
            print(f"  {t}: {v['mean_difference']:+.4f}  p={v['p_value']}")
    # drop the bulky per-query arrays from the summary file
    for v in summary["results"].values():
        for s in v["systems"].values():
            s.pop("per_query", None)
    summary = finish_summary(summary, names, args.max_docs)
    fallbacks = {n: r["rerank_fallback_queries"] for n, r in summary["results"].items() if r.get("rerank_fallback_queries")}
    if fallbacks:
        # A hybrid_rerank row where the reranker fell back is the hybrid order under another name. The files are
        # kept for inspection, the summary says the row is void, and the exit code makes a driver retry the step.
        # Seen 2026-10-08: 273 of 300 SciFact queries fell back on CUDA errors while the GPU was failing, and the
        # run read as "no gain from the reranker" until the fallback count was noticed. A retry reruns only the
        # queries that fell back, because the query log does not count them as finished.
        summary["rerank_invalid"] = fallbacks
        print(f"beir_bench: FAIL the hybrid_rerank rows are void, the reranker fell back on {fallbacks} queries. Do not quote them.", file=sys.stderr)
    atomic_write_json(out / "summary.json", summary)
    return 3 if fallbacks else 0


if __name__ == "__main__":
    raise SystemExit(main())
