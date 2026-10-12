"""A cross-encoder reranker over any Searcher, through gestalt_rank.rerank_rows.

A query whose reranker did not run keeps the inner order and is counted in `fallbacks`. The runner voids the run on any fallback,
as beir_bench does: that row would be the inner system under another name.

`blend` (wave 2, 2026-10-09) mixes the reranker's score with the inner system's fused score over the reranked head instead of
replacing the order: final = (1 - blend) * z(reranker) + blend * z(inner), both z-scored within the head. blend 0 is the shipped
behaviour. The papers only test this on MS MARCO, where interpolation with BM25 hurt (arXiv 2301.09728); BEIR is untested."""
from __future__ import annotations

from pathlib import Path

import gestalt_rank
from bench_engine import FtsIndex, rerank_failed

from evals.zoo.adapter import combine_describe, describe_fields
from evals.zoo.cost import dir_bytes, param_count


class Rerank:
    needs_work_dir = True

    def __init__(self, name: str, inner, model: str, work_dir: str | Path, depth: int = 40, blend: float = 0.0):
        if not 0.0 <= blend < 1.0:
            raise ValueError(f"blend must be in [0, 1), not {blend!r}")
        self.name, self.inner, self.model, self.depth, self.dir, self.blend = name, inner, model, depth, Path(work_dir), float(blend)
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
            head_scores = [(r["id"], s) for r, s in zip(ranked, scores)]
            if self.blend and scores is not None and not rerank_failed(info):
                head_scores = blend_scores(head_scores, dict(head), self.blend)
            out[qid] = (head_scores + tail)[:k]
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
        gestalt_rank.unload_reranker(force=True)  # the next system's VRAM baseline must not include this one's reranker (2026-10-11)

    def describe(self) -> dict:
        hub_id, revision = gestalt_rank.RERANK_REVISIONS.get(self.model, (self.model, None))
        own = describe_fields(model_id=hub_id, dtype=self._model_info.get("dtype"), parameters=self._model_info.get("parameters"),
                              index_bytes=dir_bytes(self.dir) if self.dir.exists() else None)
        out = combine_describe(own, self.parts)
        out["revision"] = revision
        if self.blend:
            out["blend"] = self.blend
        return out


def blend_scores(reranked: list[tuple[str, float]], inner_score: dict[str, float], blend: float) -> list[tuple[str, float]]:
    """Reorder the reranked head by (1 - blend) * z(reranker score) + blend * z(inner fused score); ties keep the reranker order.

    The arithmetic is gestalt_rank.blend_scores, the product's own, so the zoo measures what ships."""
    ids = [d for d, _ in reranked]
    return [(i, float(s)) for i, s in gestalt_rank.blend_scores(ids, dict(reranked), inner_score, blend)]
