"""BM25 through gestalt's own FTS5 index: bench_engine.FtsIndex and gestalt_rank.fts_match, the same tokenizer and query builder."""
from __future__ import annotations

from pathlib import Path

import gestalt_rank
from bench_engine import FtsIndex

from evals.zoo.adapter import describe_fields
from evals.zoo.cost import dir_bytes


class Bm25:
    needs_work_dir = True
    parts = ()

    def __init__(self, name: str, work_dir: str | Path):
        self.name, self.dir = name, Path(work_dir)
        self.indexed = False

    def index(self, docs) -> None:
        self.fts = FtsIndex(self.dir / "corpus.sqlite")
        self.fts.load_docs(docs)
        self.fts.build_fts()
        self.indexed = True

    def search(self, queries, k):
        out = {}
        for qid, text in queries:
            rows = self.fts.search(gestalt_rank.fts_match(text), k)  # (ord, FTS5 rank), rank negative and better when lower
            out[qid] = list(zip(self.fts.doc_ids([o for o, _ in rows]), [-r for _, r in rows]))
        return out

    def close(self) -> None:
        if hasattr(self, "fts"):
            self.fts.close()

    def describe(self) -> dict:
        return describe_fields(model_id="fts5-bm25-porter", index_bytes=dir_bytes(self.dir) if self.dir.exists() else None)
