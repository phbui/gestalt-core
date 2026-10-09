"""The Searcher protocol every zoo system implements.

A Searcher indexes (doc_id, text) pairs once and answers queries with (doc_id, score) lists. A larger score is better.
`docs` must be a re-iterable (a list, or an object whose __iter__ starts a fresh pass), because a composite hands it to several parts.

Optional attributes the runner reads when present:
    parts             tuple of the Searchers this one is built from. Empty for a leaf.
    indexed           True once index() has run. Composites skip parts that are already indexed.
    needs_work_dir    True when the constructor takes work_dir. The runner passes one directory per system.
    fallbacks         for a reranker: the number of queries whose reranker did not run. Any fallback voids the run.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

DESCRIBE_KEYS = ("model_id", "revision", "dtype", "parameters", "licence", "index_bytes")


@runtime_checkable
class Searcher(Protocol):
    name: str

    def index(self, docs: Iterable[tuple[str, str]]) -> None: ...

    def search(self, queries: list[tuple[str, str]], k: int) -> dict[str, list[tuple[str, float]]]: ...

    def close(self) -> None: ...

    def describe(self) -> dict: ...


def describe_fields(**given) -> dict:
    """A describe() dict with every key in DESCRIBE_KEYS. Keys not given are None."""
    unknown = set(given) - set(DESCRIBE_KEYS)
    if unknown:
        raise ValueError(f"unknown describe keys {sorted(unknown)}")
    return {k: given.get(k) for k in DESCRIBE_KEYS}


def combine_describe(own: dict, parts) -> dict:
    """The describe() of a composite: its own fields, plus the parts' parameters and index bytes added, ids joined with '+'.

    `own` may leave a field None. Parameters and index bytes are None only when the composite and every part leave them None."""
    infos = [own] + [p.describe() for p in parts]

    def total(key):
        vals = [i[key] for i in infos if i.get(key) is not None]
        return sum(vals) if vals else None

    def joined(key, sep):
        vals = []
        for i in infos:
            if i.get(key) and i[key] not in vals:
                vals.append(i[key])
        return sep.join(vals) if vals else None

    return describe_fields(model_id=joined("model_id", "+"), revision=None, dtype=joined("dtype", "/"), parameters=total("parameters"),
                           licence=joined("licence", "/"), index_bytes=total("index_bytes"))
