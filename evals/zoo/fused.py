"""Fusion of two Searchers by bench_engine.fuse_pools, the engine's own fusion (rrf at K=60, or convex).

A third part `c` is allowed with rrf. Three legs go through gestalt_rank.rrf_fuse, which takes any number of ranked lists."""
from __future__ import annotations

import gestalt_rank
from bench_engine import fuse_pools

from evals.zoo.adapter import combine_describe, describe_fields
from evals.zoo.bm25 import Bm25


class Fused:
    """Each part searches `depth` hits (default 2 * k, gestalt's pool). Convex fusion puts weight `alpha` on part `a`.

    fuse_pools reads its two legs as FTS5 ranks and distances, and both are smaller-is-better. A Searcher score is
    larger-is-better, so each score is negated on the way in. With a third part `c` the legs are ranked in the order a, b, c.

    The engine fuses the lexical leg first. rrf breaks a score tie by the leg that listed the document first, and convex puts alpha on the
    first leg. With lexical_first (the default) a Bm25 part therefore goes before the other parts whatever order the names came in, so
    `nomic+bm25:rrf` and `bm25+nomic:rrf` score alike and alpha always weights the lexical leg. Pass lexical_first=False to keep the order given."""

    def __init__(self, name: str, a, b, fusion: str = "rrf", alpha: float = 0.5, depth: int | None = None, c=None, lexical_first: bool = True):
        if fusion not in ("rrf", "convex"):
            raise ValueError(f"fusion must be rrf or convex, not {fusion!r}")
        if c is not None and fusion != "rrf":
            raise ValueError("three-leg fusion supports rrf only")
        self.name, self.a, self.b, self.c, self.fusion, self.alpha, self.depth = name, a, b, c, fusion, alpha, depth
        self.parts = (a, b) if c is None else (a, b, c)
        self.legs = tuple(sorted(self.parts, key=lambda p: not isinstance(p, Bm25))) if lexical_first else self.parts  # a stable sort
        self.indexed = False

    def index(self, docs) -> None:
        for part in self.parts:
            if not part.indexed:
                part.index(docs)
        self.indexed = True

    def search(self, queries, k):
        depth = self.depth or 2 * k
        if self.c is not None:
            runs = [part.search(queries, depth) for part in self.legs]
            out = {}
            for qid, _ in queries:
                scores = gestalt_rank.rrf_fuse([[d for d, _ in r[qid]] for r in runs])
                out[qid] = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:k]  # a stable sort keeps first-appearance order on ties
            return out
        ra, rb = (leg.search(queries, depth) for leg in self.legs)
        return {qid: fuse_pools([(d, -s) for d, s in ra[qid]], [(d, -s) for d, s in rb[qid]], self.fusion, self.alpha)[:k]
                for qid, _ in queries}

    def close(self) -> None:
        pass  # the parts belong to whoever built them

    def describe(self) -> dict:
        return combine_describe(describe_fields(), self.parts)
