"""A cross-encoder reranker over any Searcher, through gestalt_rank.rerank_rows.

A query whose reranker did not run keeps the inner order and is counted in `fallbacks`. The runner voids the run on any fallback,
as beir_bench does: that row would be the inner system under another name."""
from __future__ import annotations

from pathlib import Path

import gestalt_rank
from bench_engine import FtsIndex, rerank_failed

from evals.zoo.adapter import combine_describe, describe_fields
from evals.zoo.cost import dir_bytes, param_count


class Rerank:
    needs_work_dir = True

    def __init__(self, name: str, inner, model: str, work_dir: str | Path, depth: int = 40):
        self.name, self.inner, self.model, self.depth, self.dir = name, inner, model, depth, Path(work_dir)
        self.parts = (inner,)
        self.indexed = False
        self.fallback_qids: set[str] = set()
        self._model_info: dict = {}

    @property
    def fallbacks(self) -> int:
        return len(self.fallback_qids)

    def index(self, docs) -> None:
        if not self.inner.indexed:
            self.inner.index(docs)
        self.docs = FtsIndex(self.dir / "docs.sqlite")  # the texts the reranker reads
        self.docs.load_docs(docs)
        self.indexed = True

    def search(self, queries, k):
        text = dict(queries)
        out = {}
        for qid, pool in self.inner.search(queries, max(self.depth, k)).items():
            head, tail = pool[:self.depth], pool[self.depth:]
            found = self.docs.lookup([d for d, _ in head])
            rows = [{"id": d, "slug": d, "title": "", "heading": "", "content": found[d][1]} for d, _ in head]
            ranked, scores, info = gestalt_rank.rerank_rows(text[qid], rows, self.model)
            if rerank_failed(info):
                self.fallback_qids.add(qid)
            if scores is None:
                inner_score = dict(head)
                scores = [inner_score[r["id"]] for r in ranked]
            elif not self._model_info:
                self._read_model()
            out[qid] = ([(r["id"], s) for r, s in zip(ranked, scores)] + tail)[:k]
        return out

    def _read_model(self) -> None:
        try:
            net = gestalt_rank.get_reranker(self.model)
            dtype = str(getattr(getattr(net, "model", None), "dtype", "")).replace("torch.", "")
            self._model_info = {"parameters": param_count(net), "dtype": dtype or None}
        except Exception:  # a cost field must never fail a run
            self._model_info = {"parameters": None, "dtype": None}

    def close(self) -> None:
        if hasattr(self, "docs"):
            self.docs.close()

    def describe(self) -> dict:
        hub_id, revision = gestalt_rank.RERANK_REVISIONS.get(self.model, (self.model, None))
        own = describe_fields(model_id=hub_id, dtype=self._model_info.get("dtype"), parameters=self._model_info.get("parameters"),
                              index_bytes=dir_bytes(self.dir) if self.dir.exists() else None)
        out = combine_describe(own, self.parts)
        out["revision"] = revision
        return out
