"""The one fusion implementation behind gestalt search and the benchmarks.

tools/gestalt_rank.py and evals/retrieval/bench_engine.py both call into this module. It is stdlib only and reads no environment. The knobs that pick a method live in gestalt_rank.

Every leg is ranked best first. rrf and weighted_rrf take a leg as a list of ids. convex and dense_first_rescue take a leg as a list of (id, score) pairs where a higher score is better, so a caller negates an FTS5 rank or an L2 distance before it passes them in.

rrf keeps the order of the code it replaced. Its dict comes back in first-appearance order, the first leg first, and every caller sorts it with a stable sort, so an exact tie keeps that order. weighted_rrf breaks ties the same way, so weights (1, 1) give rrf's scores and rrf's order. convex and dense_first_rescue return their dict already in final order, and their ties go to the better best-leg rank, then to the smaller id. That rule does not depend on which leg comes first, so swapping the legs and alpha for 1 - alpha gives the same order. Every method returns its dict in final order, so a caller's stable sort by score keeps it.

The normalisers map a leg's scores to comparable values. Each has the same degenerate policy. A pool with one distinct finite score gives 0.5 to every item. An empty leg gives an empty list. A NaN or infinite score counts as missing and gets 0.0.
"""
from __future__ import annotations

import math
from collections.abc import Hashable, Sequence

RRF_K = 60
NORMALISERS = ("minmax", "zscore", "tmin")
MISSING = ("zero", "leg_min")
DEGENERATE = 0.5


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _id_key(i: Hashable) -> tuple:
    """Sort key for the last tie-break. Numbers sort by value, anything else by its string, numbers first."""
    if isinstance(i, (int, float)) and not isinstance(i, bool):
        return (0, i, "")
    return (1, 0, str(i))


# --- rank fusion -----------------------------------------------------------------------------------


def rrf(legs: Sequence[Sequence[Hashable]], k: int = RRF_K) -> dict:
    """Reciprocal rank fusion. An id scores the sum of 1/(k + rank + 1) over the legs that hold it, rank from 0.

    The dict is in first-appearance order, the first leg first. Callers sort it by score with a stable sort. This reproduces gestalt_rank.rrf_fuse and bench_engine.fuse_pools as they were before this module, tie order included."""
    scores: dict = {}
    for leg in legs:
        for rank, key in enumerate(leg):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _best_ranks(legs: Sequence[Sequence[Hashable]]) -> dict:
    best: dict = {}
    for leg in legs:
        for rank, key in enumerate(leg):
            if rank < best.get(key, rank + 1):
                best[key] = rank
    return best


def _ordered(scores: dict, best: dict) -> dict:
    """scores in final order: score descending, then the better best-leg rank, then the smaller id."""
    return {i: scores[i] for i in sorted(scores, key=lambda i: (-scores[i], best[i], _id_key(i)))}


def _check_weights(weights: Sequence[float], n: int) -> list[float]:
    if len(weights) != n:
        raise ValueError(f"weighted_rrf: {len(weights)} weights for {n} legs")
    out = []
    for w in weights:
        if not _finite(w) or w < 0:
            raise ValueError(f"weighted_rrf: weight {w!r} is not a finite number at or above 0")
        out.append(float(w))
    return out


def weighted_rrf(legs: Sequence[Sequence[Hashable]], weights: Sequence[float], k: int = RRF_K) -> dict:
    """RRF where leg j contributes weights[j]/(k + rank + 1). Weights (1, 1) give exactly the rrf scores and the rrf order.

    A leg with weight 0 adds nothing, so weights (1, 0) keep the first leg's order. Its ids still appear, after the ids of the weighted legs. The dict is in final order: score descending, an exact tie in first-appearance order as rrf has it."""
    w = _check_weights(weights, len(legs))
    scores: dict = {}
    for leg, wj in zip(legs, w):
        for rank, key in enumerate(leg):
            scores[key] = scores.get(key, 0.0) + wj / (k + rank + 1)
    return {i: scores[i] for i in sorted(scores, key=lambda i: -scores[i])}  # stable, so ties keep first-appearance order


# --- normalisers -----------------------------------------------------------------------------------


def _degenerate(values: Sequence[float], fin: list[float]) -> list[float] | None:
    """The shared policy. None when the pool has two or more distinct finite scores."""
    if not values:
        return []
    if not fin:
        return [0.0] * len(values)
    if len(set(fin)) == 1:
        return [DEGENERATE if _finite(v) else 0.0 for v in values]
    return None


def minmax(values: Sequence[float]) -> list[float]:
    """(v - min) / (max - min) over the finite scores of the pool."""
    fin = [float(v) for v in values if _finite(v)]
    deg = _degenerate(values, fin)
    if deg is not None:
        return deg
    lo, hi = min(fin), max(fin)
    return [(v - lo) / (hi - lo) if _finite(v) else 0.0 for v in values]


def zscore(values: Sequence[float]) -> list[float]:
    """(v - mean) / population standard deviation over the finite scores of the pool. The result is not bounded."""
    fin = [float(v) for v in values if _finite(v)]
    deg = _degenerate(values, fin)
    if deg is not None:
        return deg
    mean = sum(fin) / len(fin)
    sd = math.sqrt(sum((v - mean) ** 2 for v in fin) / len(fin))
    if sd == 0.0:  # distinct values whose spread underflows
        return [DEGENERATE if _finite(v) else 0.0 for v in values]
    return [(v - mean) / sd if _finite(v) else 0.0 for v in values]


def theoretical_min(values: Sequence[float], lo: float = 0.0) -> list[float]:
    """(v - lo) / (max - lo), where lo is a declared lower bound such as 0 for BM25 and max is the pool's best score.

    A score below lo is clipped to lo. When every finite score sits at or below lo the leg gives 0.0."""
    if not _finite(lo):
        raise ValueError(f"theoretical_min: lower bound {lo!r} is not finite")
    fin = [float(v) for v in values if _finite(v)]
    deg = _degenerate(values, fin)
    if deg is not None:
        return deg
    hi = max(fin)
    if hi <= lo:
        return [0.0] * len(values)
    return [(max(v, lo) - lo) / (hi - lo) if _finite(v) else 0.0 for v in values]


def normalise(values: Sequence[float], method: str, lo: float = 0.0) -> list[float]:
    """Apply the named normaliser. lo is only read by tmin."""
    if method == "minmax":
        return minmax(values)
    if method == "zscore":
        return zscore(values)
    if method == "tmin":
        return theoretical_min(values, lo)
    raise ValueError(f"unknown normaliser {method!r}. Use one of: {', '.join(NORMALISERS)}")


def _leg_norm(leg: Sequence[tuple], method: str, lo: float) -> dict:
    """id -> normalised score. A repeated id keeps its first, best placed, score."""
    out: dict = {}
    for (i, _), n in zip(leg, normalise([s for _, s in leg], method, lo)):
        out.setdefault(i, n)
    return out


# --- convex combination ----------------------------------------------------------------------------


def convex(legs: Sequence[Sequence[tuple]], alpha: float, normaliser: str = "minmax", missing: str = "zero",
           lows: Sequence[float] = (0.0, 0.0)) -> dict:
    """alpha * norm(first leg) + (1 - alpha) * norm(second leg). alpha is the weight on the FIRST leg.

    legs are two lists of (id, score), best first, higher better. normaliser is minmax, zscore or tmin. lows are the declared lower bounds tmin reads, one per leg. missing decides what an id absent from a leg gets from that leg. zero gives 0.0. leg_min gives the lowest normalised score in that leg, or 0.0 when the leg is empty. The dict is in final order."""
    if len(legs) != 2:
        raise ValueError(f"convex fuses two legs, got {len(legs)}")
    if not _finite(alpha) or not 0.0 <= alpha <= 1.0:
        raise ValueError(f"convex: alpha {alpha!r} is not a number from 0 to 1")
    if missing not in MISSING:
        raise ValueError(f"unknown missing policy {missing!r}. Use one of: {', '.join(MISSING)}")
    norms = [_leg_norm(leg, normaliser, lo) for leg, lo in zip(legs, lows)]
    fill = [min(n.values()) if (missing == "leg_min" and n) else 0.0 for n in norms]
    ids = [[i for i, _ in leg] for leg in legs]
    best = _best_ranks(ids)
    scores = {i: alpha * norms[0].get(i, fill[0]) + (1.0 - alpha) * norms[1].get(i, fill[1]) for i in best}
    return _ordered(scores, best)


# --- dense first, lexical rescue -------------------------------------------------------------------


def dense_first_rescue(dense: Sequence[tuple], lexical: Sequence[tuple], max_rank: int = 3, min_norm_score: float = 0.5,
                       dense_window: int = 20, insert_at: int = 5, max_rescues: int = 2, normaliser: str = "minmax",
                       lexical_low: float = 0.0) -> dict:
    """Keep the dense order and insert a few strong lexical hits the dense leg missed.

    dense and lexical are lists of (id, score), best first, higher better. A lexical hit is rescued when its rank is within the first max_rank, its normalised score is at least min_norm_score, and it is absent from the dense top max(dense_window, insert_at). At most max_rescues are rescued, in lexical order, and they go in at position insert_at (from 0). The dense items above insert_at never move.

    The dict is in final order. A dense item keeps its dense score. A rescued item has no score of its own, so it takes the score of the item just above it (the first dense score when insert_at is 0), and the scores never rise down the list."""
    for name, v in (("max_rank", max_rank), ("dense_window", dense_window), ("insert_at", insert_at), ("max_rescues", max_rescues)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError(f"dense_first_rescue: {name} {v!r} is not an integer at or above 0")
    if not _finite(min_norm_score):
        raise ValueError(f"dense_first_rescue: min_norm_score {min_norm_score!r} is not finite")
    order: list = []
    score: dict = {}
    for i, s in dense:
        if i not in score:
            order.append(i)
            score[i] = s
    protected = set(order[:max(dense_window, insert_at)])
    head = lexical[:max_rank]
    norm = normalise([s for _, s in lexical], normaliser, lexical_low)[:max_rank]
    rescued: list = []
    for (i, _), n in zip(head, norm):
        if len(rescued) >= max_rescues:
            break
        if n >= min_norm_score and i not in protected and i not in rescued:
            rescued.append(i)
    if not rescued:
        return {i: score[i] for i in order}
    rest = [i for i in order if i not in rescued]
    at = min(insert_at, len(rest))
    above = score[rest[at - 1]] if at > 0 else (score[rest[0]] if rest else 0.0)
    final = rest[:at] + rescued + rest[at:]
    return {i: (above if i in rescued else score[i]) for i in final}
