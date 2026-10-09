#!/usr/bin/env python3
"""Run gestalt's search pipeline through the mteb library.

mteb accepts three kinds of retrieval model: an encoder, a cross encoder, and a search model. A search model has
`index(corpus, ...)` and `search(queries, ..., top_k)` and returns ranked results directly (SearchProtocol,
mteb/models/models_protocols.py:24). That is the door the hybrid pipeline uses. It does not need to hide behind `encode`.

GestaltHybridSearch runs on the same resumable engine as beir_bench.py (bench_engine.py): the documents, a file-backed
FTS5 index with gestalt's schema, float32 embedding blocks, and one query log per task split. It answers each query
with gestalt's hybrid search, both legs fused by RRF or convex fusion, with an optional rerank. A kill inside a task
loses at most one embedding block and no finished query. mteb's own cache still resumes whole tasks.
The embedding model, its revision and its prefixes come from tools/gestalt_embed_config.py.

    python evals/retrieval/mteb_bench.py --smoke                                   # SciFact, 200 documents, a smoke only
    python evals/retrieval/mteb_bench.py --list-tasks                              # print the task list and stop
    python evals/retrieval/mteb_bench.py --output-folder out/mteb/results  # the full MTEB(eng, v2) retrieval run, on a GPU

Flags that change the system under test: --embed-profile, --rerank on, --fusion convex --alpha 0.4. They behave as in beir_bench.py.

A run with --max-docs or --smoke is a subset. It skips the mteb cache, so nothing partial lands in a results folder
you might submit. It writes SUBSET.txt beside the output, and a full run removes it. Never quote it, never submit it.
The subset keeps every relevant document and fills the rest in corpus order, the rule beir_bench.py uses.

With --rerank on, a task where the reranker fell back on any query is void: its cached result is renamed to
<file>.void-rerank so the next run recomputes it, RERANK_INVALID.json names the tasks, and the exit code is 3.

Needs: pip install -r evals/retrieval/requirements-bench.txt
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import beir_bench as bb

ec, harness, engine = bb.ec, bb.harness, bb.engine
from resumable import DEFAULT_BLOCK_SIZE, QueryLog, atomic_write, atomic_write_json

BENCHMARK = "MTEB(eng, v2)"
SMOKE_TASK = "SciFact"
SMOKE_DOCS = 200
# mteb requires organization/model_name, so the bare name gestalt-hybrid gets the repo owner as its organization.
MODEL_NAME = "phbui/gestalt-hybrid"
REFERENCE = "https://github.com/phbui/gestalt-core"


def default_tasks() -> list[str]:
    """The retrieval tasks of MTEB(eng, v2), as the installed mteb defines them."""
    import mteb

    return [t.metadata.name for t in mteb.get_benchmark(BENCHMARK).tasks if t.metadata.type == "Retrieval"]


def model_meta():
    """The ModelMeta for the entrant. Every field is stated, none is guessed.

    training_datasets stays empty. Gestalt trains nothing. The zero-shot claim rests on the nomic model card and
    nothing in this tree can check it. The embedding model may have seen text from the benchmark sets.
    public_training_code and public_training_data are None because the entrant is a pipeline of a pretrained model
    and rule-based fusion. Nothing here was trained."""
    from mteb.models.model_meta import ModelMeta

    return ModelMeta(
        loader=None,
        name=MODEL_NAME,
        revision=ec.MODEL_REVISION,
        release_date="2026-10-08",
        languages=["eng-Latn"],
        n_parameters=None,
        memory_usage_mb=None,
        max_tokens=None,
        embed_dim=ec.EMBED_DIM,
        license=None,
        open_weights=None,
        public_training_code=None,
        public_training_data=None,
        framework=["Sentence Transformers"],
        reference=REFERENCE,
        similarity_fn_name=None,
        use_instructions=False,
        training_datasets=set(),
        adapted_from=ec.MODEL_NAME,
        model_type=["dense"],
    )


class GestaltHybridSearch:
    """An mteb search model: gestalt's BM25 leg and dense leg, fused, optionally reranked.

    `work_dir` holds one subdirectory per task, subset and split. Without it a temporary directory is used and
    nothing resumes. `relevant` maps (task, subset, split) to the relevant document ids, which a --max-docs
    subset always keeps. `rerank_fallbacks` counts, per task, the queries whose rerank fell back."""

    def __init__(self, model=None, rerank: dict | None = None, max_docs: int | None = None, batch_size: int = 64,
                 work_dir: Path | None = None, relevant: dict | None = None, block_size: int = DEFAULT_BLOCK_SIZE, rebuild: bool = False):
        self.model = model
        self.rerank = rerank
        self.max_docs = max_docs
        self.batch_size = batch_size
        self.work_dir = Path(work_dir) if work_dir else None
        self.relevant = relevant or {}
        self.block_size = block_size
        self.rebuild = rebuild
        self.fts = None
        self.dense = None
        self.wd: Path | None = None
        self.task_name = ""
        self.rerank_fallbacks: dict[str, int] = {}
        self.mteb_model_meta = model_meta()

    @property
    def ids(self) -> list[str]:
        """The indexed document ids in corpus order."""
        return [r[0] for r in self.fts.db.execute("SELECT doc_id FROM docs ORDER BY ord")] if self.fts else []

    def index(self, corpus, *, task_metadata=None, hf_split=None, hf_subset=None, encode_kwargs=None, num_proc=None) -> None:
        self.task_name = getattr(task_metadata, "name", None) or "task"
        key = "-".join(str(x) for x in (self.task_name, hf_subset or "default", hf_split or "test")) + (f"-max{self.max_docs}" if self.max_docs else "")
        root = self.work_dir or Path(tempfile.mkdtemp(prefix="gestalt-mteb-"))
        self.wd = root / key.replace("/", "-")
        relevant = self.relevant.get((self.task_name, hf_subset, hf_split), set())
        max_docs = self.max_docs

        def stream():
            rows = engine.select_capped(corpus, relevant, max_docs, doc_id=lambda r: str(r["id"])) if max_docs else corpus
            for row in rows:
                yield str(row["id"]), engine.doc_text(row.get("title"), row.get("text") or row.get("body"))

        if self.fts is not None:
            self.fts.close()
        model = self.model or harness.get_model()
        self.fts, store = engine.prepare_corpus(self.wd, key, stream, model=model, ec=ec, batch_size=self.batch_size,
                                                block_size=self.block_size, rebuild=self.rebuild,
                                                status=engine.Status(self.wd / "status.json", f"mteb_bench: {key}"))
        self.dense = engine.open_dense(store, self.fts.n_docs(), ec.EMBED_DIM)

    def search(self, queries, *, task_metadata=None, hf_split=None, hf_subset=None, top_k: int = 10, encode_kwargs=None,
               top_ranked=None, num_proc=None) -> dict[str, dict[str, float]]:
        if self.fts is None:
            raise ValueError("Corpus must be indexed before searching.")
        model = self.model or harness.get_model()
        systems = ["hybrid"] + (["hybrid_rerank"] if self.rerank else [])
        fusion, alpha = harness.gestalt_rank.fusion_mode(), harness.gestalt_rank.fusion_alpha()
        header = engine.query_log_header(fingerprint=self.fts.get("fingerprint"), systems=systems, limit=top_k, rerank=self.rerank,
                                         fusion=fusion, alpha=alpha, fill_from_hybrid=True)
        qs = [(str(q["id"]), q["text"]) for q in queries]
        with QueryLog(self.wd / f"queries-top{top_k}.jsonl", header, rebuild=self.rebuild) as log:
            engine.retrieve(qs, fts=self.fts, dense=self.dense,
                            embed=lambda texts: engine.encode_queries(model, texts, ec.QUERY_PREFIX, ec.postprocess),
                            log=log, systems=systems, limit=top_k, rerank=self.rerank, fusion=fusion, alpha=alpha,
                            fill_from_hybrid=True, status=engine.Status(self.wd / "status.json", f"mteb_bench: {self.task_name}"))
            runs, fallbacks = engine.runs_from_log(log, systems, [q for q, _ in qs])
        if self.rerank:
            self.rerank_fallbacks[self.task_name] = self.rerank_fallbacks.get(self.task_name, 0) + fallbacks
        final = runs["hybrid_rerank" if self.rerank else "hybrid"]
        # the score is rank-derived, search() returns an order
        return {qid: {d: float(len(ranked) - i) for i, d in enumerate(ranked)} for qid, ranked in final.items()}


def subset_relevant(tasks) -> dict:
    """(task, subset, split) -> relevant document ids, so a --max-docs subset keeps them as beir_bench does.

    mteb hands index() the corpus without its judgements, so they are read here from each task's data."""
    out: dict = {}
    for task in tasks:
        task.load_data()
        for subset, splits in (getattr(task, "dataset", None) or {}).items():
            for split, data in splits.items():
                rel = data.get("relevant_docs") or {}
                out[(task.metadata.name, subset, split)] = {d for q in rel.values() for d, s in q.items() if s > 0}
    return out


def void_reranked_results(cache, meta, fallbacks: dict[str, int], out: Path) -> None:
    """Record the tasks whose rerank fell back and move their cached results aside, so a rerun recomputes them."""
    atomic_write_json(out / "RERANK_INVALID.json", {"rerank_fallback_queries": fallbacks,
                                                    "note": "These tasks' hybrid_rerank results are void. Rerun to recompute them."})
    if cache is None:
        return
    for task in fallbacks:
        try:
            path = Path(cache.get_task_result_path(task, meta))
        except Exception:
            continue
        if path.exists():
            os.replace(path, path.with_name(path.name + ".void-rerank"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", nargs="+", default=None, help=f"mteb task names. Default: the retrieval tasks of {BENCHMARK}")
    ap.add_argument("--list-tasks", action="store_true", help="print the default task list and stop")
    ap.add_argument("--smoke", action="store_true", help=f"one small task ({SMOKE_TASK}), {SMOKE_DOCS} documents, no cache. A smoke, not a result")
    ap.add_argument("--max-docs", type=int, default=None, help="cap each corpus at N documents (a subset run, never quotable)")
    ap.add_argument("--output-folder", default=str(HERE / "results-mteb"))
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--embed-profile", choices=sorted(ec.PROFILES), default=None, help="embedding profile (read before the imports)")
    ap.add_argument("--rerank", choices=["off", "on"], default="off")
    ap.add_argument("--rerank-model", default=os.environ.get("GESTALT_RERANK_MODEL", "bge"), help="reranker alias: bge, qwen3-4b or qwen3-0.6b")
    ap.add_argument("--rerank-depth", type=int, default=40)
    ap.add_argument("--fusion", choices=["rrf", "convex"], default=None)
    ap.add_argument("--alpha", type=float, default=None)
    ap.add_argument("--work-dir", default=None, help="documents, indexes, embeddings and finished queries per task (default: <output-folder>/work)")
    ap.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE, help="documents per embedding block, the unit a restart redoes")
    ap.add_argument("--rebuild", action="store_true", help="discard work-dir state built under another configuration instead of refusing it")
    args = ap.parse_args()
    if args.embed_profile and args.embed_profile != ec.PROFILE:
        raise SystemExit(f"--embed-profile {args.embed_profile} did not take effect (module loaded profile {ec.PROFILE})")

    tasks = [SMOKE_TASK] if args.smoke else (args.tasks or default_tasks())
    if args.list_tasks:
        print("\n".join(default_tasks()))
        return 0
    max_docs = SMOKE_DOCS if args.smoke and not args.max_docs else args.max_docs
    subset = bool(max_docs)
    print(f"mteb_bench: tasks={tasks}", file=sys.stderr)
    if args.smoke:
        print(f"mteb_bench: SMOKE RUN. {SMOKE_TASK} capped at {max_docs} documents. This proves the plumbing. It is not a result.", file=sys.stderr)
    elif subset:
        print(f"mteb_bench: SUBSET RUN. Corpus capped at {max_docs} documents. Do not quote it or submit it.", file=sys.stderr)

    import mteb

    rerank = {"model": args.rerank_model, "depth": args.rerank_depth} if args.rerank == "on" else None
    if rerank:
        import gestalt_rank  # noqa: F401  fail now, not after an hour of embedding
    out = Path(args.output_folder).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    work = Path(args.work_dir).expanduser() if args.work_dir else out / "work"
    bb.ensure_work_root(work)
    task_objs = mteb.get_tasks(tasks=tasks)
    relevant = subset_relevant(task_objs) if subset else {}
    search_model = GestaltHybridSearch(harness.get_model(), rerank, max_docs, args.batch_size, work_dir=work, relevant=relevant,
                                       block_size=args.block_size, rebuild=args.rebuild)
    marker = out / "SUBSET.txt"
    if subset:
        atomic_write(marker, f"Capped at {max_docs} documents per corpus. Not a benchmark result. Do not submit.\n")
    elif marker.exists():
        marker.unlink()  # a full run's folder must not carry a stale subset marker
    cache = None if subset else mteb.ResultCache(out)
    result = mteb.evaluate(search_model, task_objs, cache=cache, show_progress_bar=False)
    fallbacks = {t: n for t, n in search_model.rerank_fallbacks.items() if n}
    for tr in result.task_results:
        void = "  [VOID: rerank fell back]" if tr.task_name in fallbacks else ""
        print(f"{tr.task_name}: main score {tr.get_score():.4f}" + ("  [SUBSET, not the benchmark]" if subset else "") + void)
    if fallbacks:
        void_reranked_results(cache, search_model.mteb_model_meta, fallbacks, out)
        print(f"mteb_bench: FAIL the reranked rows are void, the reranker fell back on {fallbacks} queries. Do not quote them.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
