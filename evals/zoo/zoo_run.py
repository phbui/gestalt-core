#!/usr/bin/env python3
"""Run many retrieval systems on one BEIR dataset with the same queries, qrels, scorer and statistics as beir_bench.

    python evals/zoo/zoo_run.py --dataset beir/scifact --systems bm25 nomic qwen3-4b nomic+bm25:rrf --out DIR

A system is a name from zoo.yaml, or an ad hoc fusion `A+B:rrf` or `A+B:convex@0.4` of two such names.
It writes DIR/beir-scifact.json in beir_bench's shape (pair_runs.py and merge_summaries.py read it), with a `cost` block per system,
one TREC run file per system and a summary.json. Exit 0 on success, 3 when a reranker fell back on any query (the run is void).
Work files go to --work-dir (default zoo-work/ beside --out). A rerun resumes: documents, FTS5 and embedding blocks are kept.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import platform
import re
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]  # run as a script, HERE would shadow modules such as cost
sys.path.insert(0, str(REPO))

import beir_bench as bb
import bench_engine as engine
import gestalt_rank
import yaml
from bench_stats import bootstrap_ci, code_hashes, ndcg_at_10, permutation_p
from resumable import atomic_write, atomic_write_json

import evals.zoo  # noqa: F401  puts evals/retrieval and tools on sys.path
from evals.zoo import cost

ADAPTERS = {"bm25": ("evals.zoo.bm25", "Bm25"), "dense_hf": ("evals.zoo.dense_hf", "DenseHF"),
            "fused": ("evals.zoo.fused", "Fused"), "rerank": ("evals.zoo.rerank", "Rerank"),
            "colbert": ("evals.zoo.colbert", "MultiVector")}
REFS = ("a", "b", "c", "inner")  # argument names that hold another system's name
ADHOC = re.compile(r"^(.+)\+(.+):(rrf|convex)(?:@([0-9.]+))?$")


def load_specs(path: Path) -> dict:
    specs = yaml.safe_load(path.read_text())["systems"]
    for name, spec in specs.items():
        if spec.get("adapter") not in ADAPTERS:
            raise SystemExit(f"zoo_run: system {name!r} names adapter {spec.get('adapter')!r}. Known: {sorted(ADAPTERS)}")
    return specs


def resolve(names: list[str], specs: dict) -> None:
    """Add a spec for every ad hoc fusion in `names`. Raises SystemExit on a name that is neither declared nor a fusion of declared names."""
    for n in names:
        if n in specs:
            continue
        m = ADHOC.match(n)
        if not m or m.group(1) not in specs or m.group(2) not in specs:
            raise SystemExit(f"zoo_run: unknown system {n!r}. Declared: {sorted(specs)}. Ad hoc fusion looks like A+B:rrf or A+B:convex@0.4.")
        specs[n] = {"adapter": "fused", "args": {"a": m.group(1), "b": m.group(2), "fusion": m.group(3), **({"alpha": float(m.group(4))} if m.group(4) else {})}}


def deps(name: str, specs: dict) -> set[str]:
    out = {name}
    for ref in REFS:
        if ref in (specs[name].get("args") or {}):
            out |= deps(specs[name]["args"][ref], specs)
    return out


def walk(s):
    """The Searcher and every part under it."""
    yield s
    for p in s.parts:
        yield from walk(p)


class Corpus:
    """The dataset's documents as (doc_id, "title body"), re-iterable, with beir_bench's subset rules (--limit-docs, --max-docs)."""

    def __init__(self, name: str, limit_docs: int | None, max_docs: int | None, qrels: dict, queries: list):
        self.name, self.limit_docs, self.max_docs, self.n = name, limit_docs, max_docs, 0
        self.relevant = {d for q in queries for d, r in qrels[q.query_id].items() if r > 0}

    def __iter__(self):
        import ir_datasets

        docs = ir_datasets.load(self.name).docs_iter()
        if self.max_docs:
            docs = iter(engine.select_capped(docs, self.relevant, self.max_docs))
        n = 0
        for d in docs:
            if self.limit_docs and n >= self.limit_docs:
                break
            n += 1
            yield d.doc_id, engine.doc_text(getattr(d, "title", ""), d.text)
        self.n = n

    def count(self) -> int:
        if not self.n:  # a resumed run never iterates the corpus
            for _ in self:
                pass
        return self.n


class Zoo:
    """Builds and indexes systems on demand, each once, and measures what indexing cost."""

    def __init__(self, specs: dict, corpus: Corpus, work: Path, rebuild: bool):
        self.specs, self.corpus, self.work, self.rebuild = specs, corpus, work, rebuild
        self.built: dict = {}
        self.index_cost: dict[str, dict] = {}

    def build(self, name: str):
        if name in self.built:
            return self.built[name]
        spec = self.specs[name]
        mod, cls = ADAPTERS[spec["adapter"]]
        cls = getattr(importlib.import_module(mod), cls)
        args = dict(spec.get("args") or {})
        for ref in REFS:
            if ref in args:
                args[ref] = self.build(args[ref])
        if getattr(cls, "needs_work_dir", False):
            args["work_dir"] = self.work / re.sub(r"[^A-Za-z0-9._-]", "_", name)
            if self.rebuild:
                shutil.rmtree(args["work_dir"], ignore_errors=True)
        s = cls(name, **args)
        cost.reset_peak()
        t0 = time.perf_counter()
        s.index(self.corpus)
        self.index_cost[name] = {"seconds": time.perf_counter() - t0, "peak": cost.peak_vram_mib()}
        print(f"zoo_run: {name} indexed in {self.index_cost[name]['seconds']:.1f}s", file=sys.stderr, flush=True)
        self.built[name] = s
        return s

    def release(self, keep: set[str]) -> None:
        """Close every built system outside `keep`, so the next system's peak VRAM does not include it."""
        for name in [n for n in self.built if n not in keep]:
            self.built.pop(name).close()


def measure(zoo: Zoo, name: str, queries: list[tuple[str, str]], k: int, n_latency: int) -> tuple[dict, dict, object, float]:
    """Search with one system. Returns (ranked lists, cost block, searcher, seconds of the batched search)."""
    s = zoo.build(name)
    s.search(queries[:1], k)  # warm up: a reranker loads on its first call and must not count against throughput
    cost.reset_peak()
    t0 = time.perf_counter()
    ranked = s.search(queries, k)
    t_search = time.perf_counter() - t0
    lat = cost.batch1_latencies_ms(s.search, queries, k, n_latency)
    peaks = [zoo.index_cost[p.name]["peak"] for p in walk(s)] + [cost.peak_vram_mib()]
    peaks = [p for p in peaks if p is not None]
    d = s.describe()
    block = cost.cost_block(
        index_seconds=sum(zoo.index_cost[p.name]["seconds"] for p in walk(s)), n_docs=zoo.corpus.count(), search_seconds=t_search, n_queries=len(queries),
        latencies_ms=lat, peak_vram=max(peaks) if peaks else None, index_bytes=d["index_bytes"], parameters=d["parameters"],
        dtype=d["dtype"], gpu=cost.gpu_name())
    return ranked, block, s, t_search


def environment() -> dict:
    import numpy
    import sentence_transformers
    import torch

    try:
        import ir_datasets
        ird = ir_datasets.__version__
    except Exception:
        ird = None
    return {"python": platform.python_version(), "platform": platform.platform(), "numpy": numpy.__version__, "torch": torch.__version__,
            "sentence_transformers": sentence_transformers.__version__, "ir_datasets": ird, "gpu": cost.gpu_name(), "engine": "evals/zoo"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, help="an ir_datasets BEIR id such as beir/scifact")
    ap.add_argument("--systems", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=str(HERE / "zoo.yaml"))
    ap.add_argument("--work-dir", default=None, help="default: zoo-work/ beside --out")
    ap.add_argument("--limit-docs", type=int, default=None, help="smoke test: first N documents and at most 50 queries. Labelled smoke_subset")
    ap.add_argument("--max-docs", type=int, default=None, help="cap the corpus at N documents, relevant ones kept. Labelled subset")
    ap.add_argument("--latency-queries", type=int, default=100, help="queries timed one at a time for p50 and p95")
    ap.add_argument("--rebuild", action="store_true", help="discard each system's work directory first")
    args = ap.parse_args(argv)
    if args.dataset == "beir/cqadupstack":
        raise SystemExit("zoo_run: run the 12 sub-forums (beir/cqadupstack/android and so on) one at a time")

    specs = load_specs(Path(args.config))
    resolve(args.systems, specs)
    out = Path(args.out)
    work = Path(args.work_dir) if args.work_dir else out.parent / "zoo-work"
    bb.ensure_work_root(work)
    qrels, queries, excluded = bb.scorable_queries(args.dataset)
    corpus = Corpus(args.dataset, args.limit_docs, args.max_docs, qrels, queries)
    if args.limit_docs:
        held = {d for d, _ in corpus}
        queries = [q for q in queries if any(d in held for d in qrels[q.query_id])][:50]
    if not queries:
        raise SystemExit(f"zoo_run: {args.dataset} has no scorable query over the documents kept")
    qlist = [(q.query_id, q.text) for q in queries]
    zoo = Zoo(specs, corpus, work / bb.dataset_key(args.dataset, args.limit_docs, args.max_docs, "nomic"), args.rebuild)

    k, runs, costs, searchers, t_total = bb.TOPK, {}, {}, {}, 0.0
    for i, name in enumerate(args.systems):
        runs[name], costs[name], searchers[name], t = measure(zoo, name, qlist, k, args.latency_queries)
        t_total += t
        zoo.release(set().union(*(deps(n, specs) for n in args.systems[i + 1:])))

    per_query = {n: [ndcg_at_10([d for d, _ in runs[n][q]], qrels[q]) for q, _ in qlist] for n in args.systems}
    safe = args.dataset.replace("/", "-")
    out.mkdir(parents=True, exist_ok=True)
    for n, run in runs.items():
        atomic_write(out / f"{safe}.{n}.run", "".join(f"{q} Q0 {d} {r} {s:.6f} zoo-{n}\n" for q, ranked in run.items() for r, (d, s) in enumerate(ranked, 1)))
    tests = {f"{n}_vs_{p.name}": permutation_p(per_query[n], per_query[p.name]) for n, s in searchers.items() for p in s.parts if p.name in per_query}
    rerankers = {id(p): p for s in searchers.values() for p in walk(s) if hasattr(p, "fallbacks")}.values()
    fallbacks = sum(p.fallbacks for p in rerankers)
    rr_spec = specs[next(iter(rerankers)).name]["args"] if rerankers else None
    result = {
        "dataset": args.dataset, "split": "test", "documents": corpus.count(), "queries": len(qlist), "queries_excluded_no_positive": excluded,
        "smoke_subset": bool(args.limit_docs), **({"subset": True, "max_docs": args.max_docs} if args.max_docs else {}),
        "metric": "nDCG@10",
        "query_log_header": {
            "zoo": True, "systems": list(args.systems), "limit": k, "specs": {n: specs[n] for n in set().union(*(deps(n, specs) for n in args.systems))},
            "rerank": {"model": rr_spec["model"], "depth": rr_spec.get("depth", 40), "maxchars": gestalt_rank.rerank_maxchars(),
                       "instruction": gestalt_rank.qwen_instruction()} if rr_spec else None,
            "fusion_params": gestalt_rank.fusion_settings(),
            "code_sha256": code_hashes(),
            "zoo_code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(HERE.glob("*.py"))}},
        "model": {"systems": {n: s.describe() for n, s in searchers.items()}},
        "systems": {n: {"ndcg10": round(sum(v) / len(v), 4), "ci95_bootstrap": bootstrap_ci(v), "per_query": [round(x, 5) for x in v], "cost": costs[n]}
                    for n, v in per_query.items()},
        "tests": tests,
        **({"rerank_fallback_queries": fallbacks} if rerankers else {}),
        "seconds": {"index_and_embed": round(sum(c["seconds"] for c in zoo.index_cost.values()), 1), "search_all_queries": round(t_total, 1)},
        "query_ids": [q for q, _ in qlist],
    }
    atomic_write_json(out / f"{safe}.json", result)
    summary = {"generated_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "environment": environment(),
               "smoke_subset": bool(args.limit_docs),
               "results": {args.dataset: {k_: v for k_, v in result.items() if k_ in ("documents", "queries", "queries_excluded_no_positive", "smoke_subset",
                                                                                      "subset", "max_docs", "tests", "rerank_fallback_queries")}
                           | {"systems": {n: {x: y for x, y in s.items() if x != "per_query"} for n, s in result["systems"].items()}}}}
    if fallbacks:
        summary["rerank_invalid"] = {args.dataset: fallbacks}
        print(f"zoo_run: FAIL the reranker fell back on {fallbacks} queries. The run is void. Do not quote it.", file=sys.stderr)
    atomic_write_json(out / "summary.json", summary)
    zoo.release(set())
    for n in args.systems:
        print(f"  {n:40s} nDCG@10 {result['systems'][n]['ndcg10']:.4f}  p50 {costs[n]['query_latency_ms_p50']} ms  index {costs[n]['index_seconds']} s")
    return 3 if fallbacks else 0


if __name__ == "__main__":
    raise SystemExit(main())
