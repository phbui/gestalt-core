"""evals/retrieval/fusion.py and the fusion knobs in tools/gestalt_rank.py. Pure Python, no model, no index."""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "tools"))
import fusion  # noqa: E402
import gestalt_rank  # noqa: E402

pytest.importorskip("numpy")
import bench_engine  # noqa: E402


# --- the code this module replaced, kept verbatim as the reference ----------------------------------------------------

def legacy_rrf_fuse(legs, k=60):
    scores = {}
    for leg in legs:
        for rank, key in enumerate(leg):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return scores


def legacy_fuse_pools_rrf(fts_rows, vec_rows):
    scores = {}
    for rank, (i, _) in enumerate(fts_rows):
        scores[i] = scores.get(i, 0) + 1.0 / (60 + rank + 1)
    for rank, (i, _) in enumerate(vec_rows):
        scores[i] = scores.get(i, 0) + 1.0 / (60 + rank + 1)
    return [(i, scores[i]) for i in sorted(scores, key=lambda i: scores[i], reverse=True)]


def random_pools(rng, depth=None, universe=None):
    depth = depth or rng.randint(0, 40)
    universe = universe or rng.randint(max(depth, 1), 120)
    fts = rng.sample(range(universe), min(depth, universe))
    vec = rng.sample(range(universe), min(rng.randint(0, 40), universe))
    fts_rows = [(i, -float(depth - r) - rng.choice([0.0, 0.5])) for r, i in enumerate(fts)]  # FTS5 rank: negative, best first
    vec_rows = sorted(((i, rng.choice([0.1, 0.2, 0.3, rng.random()])) for i in vec), key=lambda x: x[1])  # distances, ties common
    return fts_rows, vec_rows


@pytest.mark.parametrize("seed", range(200))
def test_rrf_reproduces_the_old_rrf_fuse_and_fuse_pools_exactly(seed, monkeypatch):
    monkeypatch.delenv("GESTALT_FUSION", raising=False)
    rng = random.Random(seed)
    fts_rows, vec_rows = random_pools(rng, depth=rng.choice([10, 20, 40, 80]), universe=rng.choice([30, 90, 400]))
    legs = [[i for i, _ in fts_rows], [i for i, _ in vec_rows]]
    old = legacy_rrf_fuse(legs)
    new = fusion.rrf(legs)
    assert list(new.items()) == list(old.items())  # same scores, same insertion order
    assert list(gestalt_rank.rrf_fuse(legs).items()) == list(old.items())
    assert bench_engine.fuse_pools(fts_rows, vec_rows, "rrf", 0.5) == legacy_fuse_pools_rrf(fts_rows, vec_rows)
    assert sorted(new, key=new.get, reverse=True) == sorted(old, key=old.get, reverse=True)


@pytest.mark.parametrize("seed", range(50))
def test_weights_one_one_reproduce_rrf_and_one_zero_the_first_leg(seed):
    rng = random.Random(seed)
    fts_rows, vec_rows = random_pools(rng)
    dense, lex = [i for i, _ in vec_rows], [i for i, _ in fts_rows]
    ref = fusion.rrf([lex, dense])
    w = fusion.weighted_rrf([lex, dense], (1, 1))
    assert list(w.items()) == [(i, ref[i]) for i in sorted(ref, key=ref.get, reverse=True)]
    only = fusion.weighted_rrf([dense, lex], (1, 0))
    assert list(only)[:len(dense)] == dense
    assert list(gestalt_rank.fuse(fts_rows, vec_rows, "wrrf", w_bm25=0.5).items()) == list(w.items())


def test_normalisers_degenerate_empty_nan_and_negative_pools():
    for f in (fusion.minmax, fusion.zscore, lambda v: fusion.theoretical_min(v, -100.0)):
        assert f([]) == []
        assert f([3.0, 3.0]) == [0.5, 0.5] and f([7.0]) == [0.5]
        assert f([float("nan"), 2.0, 2.0]) == [0.0, 0.5, 0.5]
        assert f([float("nan")]) == [0.0]
    assert fusion.minmax([-10.0, -5.0, 0.0]) == [0.0, 0.5, 1.0]
    assert fusion.minmax([-3.0, float("nan"), -1.0]) == [0.0, 0.0, 1.0]
    z = fusion.zscore([1.0, 2.0, 3.0])
    assert z[1] == 0.0 and z[0] == -z[2]
    assert fusion.theoretical_min([4.0, 2.0, -1.0], 0.0) == [1.0, 0.5, 0.0]  # below the floor clips to 0
    assert fusion.theoretical_min([-4.0, -2.0], 0.0) == [0.0, 0.0]  # every score under the floor
    assert fusion.theoretical_min([-1.0, -2.0], -2.0) == [1.0, 0.0]


def test_convex_alpha_is_the_first_leg_weight_and_missing_policy_matters():
    lex = [(1, 9.0), (2, 5.0), (3, 1.0)]
    den = [(3, -0.1), (4, -0.5), (1, -0.9)]
    assert list(fusion.convex([lex, den], 1.0))[:3] == [1, 2, 3]
    assert list(fusion.convex([lex, den], 0.0))[:3] == [3, 4, 1]
    z = fusion.convex([lex, den], 0.5, missing="zero")
    assert z[2] == pytest.approx(0.25) and z[4] == pytest.approx(0.25)
    zs = fusion.convex([lex, den], 0.5, normaliser="zscore", missing="leg_min")
    zz = fusion.convex([lex, den], 0.5, normaliser="zscore", missing="zero")
    assert zs[4] < zz[4]  # the leg's minimum z-score is below 0
    assert fusion.convex([[], []], 0.5) == {}
    assert fusion.convex([lex, []], 0.5, missing="leg_min") == fusion.convex([lex, []], 0.5)  # an empty leg gives 0 under both
    with pytest.raises(ValueError):
        fusion.convex([lex, den], 1.5)
    with pytest.raises(ValueError):
        fusion.convex([lex, den], 0.5, missing="worst")
    with pytest.raises(ValueError):
        fusion.convex([lex, den], 0.5, normaliser="rank")


@pytest.mark.parametrize("seed", range(100))
def test_convex_tie_break_is_stable_under_leg_swap_and_input_order(seed):
    rng = random.Random(seed)
    lex = [(i, float(rng.choice([1, 2, 3]))) for i in rng.sample(range(30), 12)]
    lex.sort(key=lambda x: -x[1])
    den = [(i, float(rng.choice([1, 2]))) for i in rng.sample(range(30), 12)]
    den.sort(key=lambda x: -x[1])
    a = rng.choice([0.0, 0.3, 0.5, 0.7, 1.0])
    one = fusion.convex([lex, den], a)
    two = fusion.convex([den, lex], 1.0 - a)
    assert list(one) == list(two)
    for k, v in one.items():
        assert two[k] == pytest.approx(v)
    best = {}
    for leg in (lex, den):
        for r, (i, _) in enumerate(leg):
            best[i] = min(best.get(i, r), r)
    assert list(one) == sorted(one, key=lambda i: (-one[i], best[i], i))


@pytest.mark.parametrize("seed", range(1000))
def test_rescue_never_displaces_the_dense_top_above_insert_at(seed):
    rng = random.Random(seed)
    dense_ids = rng.sample(range(60), rng.randint(0, 30))
    dense = [(i, -float(r)) for r, i in enumerate(dense_ids)]
    lex_ids = rng.sample(range(60), rng.randint(0, 30))
    lex = [(i, float(30 - r) * rng.random()) for r, i in enumerate(lex_ids)]
    lex.sort(key=lambda x: -x[1])
    kw = dict(max_rank=rng.randint(0, 6), min_norm_score=rng.random(), dense_window=rng.randint(0, 25),
              insert_at=rng.randint(0, 10), max_rescues=rng.randint(0, 4))
    out = list(fusion.dense_first_rescue(dense, lex, **kw))
    at = kw["insert_at"]
    assert out[:min(at, len(dense_ids))] == dense_ids[:at]
    assert len([i for i in out if i not in dense_ids]) <= kw["max_rescues"]
    assert len(out) == len(set(out)) == len(set(dense_ids) | set(out))
    scores = list(fusion.dense_first_rescue(dense, lex, **kw).values())
    assert scores == sorted(scores, reverse=True)


def test_rescue_inserts_a_strong_lexical_miss_at_insert_at():
    dense = [(i, -float(i)) for i in range(10)]
    lex = [(99, 10.0), (3, 9.0), (98, 1.0)]
    out = fusion.dense_first_rescue(dense, lex, max_rank=3, min_norm_score=0.5, dense_window=5, insert_at=2, max_rescues=2)
    assert list(out)[:4] == [0, 1, 99, 2]  # 98 is too weak, 3 is in the dense window
    assert out[99] == out[1]


KNOBS = [("GESTALT_FUSION_W_BM25", "1.5", gestalt_rank.fusion_w_bm25), ("GESTALT_FUSION_W_BM25", "x", gestalt_rank.fusion_w_bm25),
         ("GESTALT_FUSION_NORM", "rank", gestalt_rank.fusion_norm), ("GESTALT_FUSION_MISSING", "worst", gestalt_rank.fusion_missing),
         ("GESTALT_RESCUE_RANK", "0", gestalt_rank.rescue_params), ("GESTALT_RESCUE_MIN", "nan", gestalt_rank.rescue_params),
         ("GESTALT_RESCUE_WINDOW", "-1", gestalt_rank.rescue_params), ("GESTALT_RESCUE_AT", "two", gestalt_rank.rescue_params),
         ("GESTALT_RESCUE_MAX", "1.5", gestalt_rank.rescue_params)]


@pytest.mark.parametrize("name,value,read", KNOBS)
def test_every_new_knob_raises_on_a_bad_value_and_names_the_variable(monkeypatch, name, value, read):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        read()


def test_the_knob_defaults_and_the_recorded_settings(monkeypatch):
    for n in ("GESTALT_FUSION", "GESTALT_FUSION_ALPHA", "GESTALT_FUSION_W_BM25", "GESTALT_FUSION_NORM", "GESTALT_FUSION_MISSING",
              "GESTALT_RESCUE_RANK", "GESTALT_RESCUE_MIN", "GESTALT_RESCUE_WINDOW", "GESTALT_RESCUE_AT", "GESTALT_RESCUE_MAX"):
        monkeypatch.delenv(n, raising=False)
    assert gestalt_rank.fusion_settings() == {
        "method": "rrf", "alpha": 0.5, "w_bm25": 0.5, "norm": "minmax", "missing": "zero", "rrf_k": 60,
        "rescue": {"max_rank": 3, "min_norm_score": 0.5, "dense_window": 20, "insert_at": 5, "max_rescues": 2}}
    monkeypatch.setenv("GESTALT_FUSION", "wrrf")
    monkeypatch.setenv("GESTALT_FUSION_W_BM25", "0.2")
    assert gestalt_rank.fusion_mode() == "wrrf" and gestalt_rank.fusion_w_bm25() == 0.2
    monkeypatch.setenv("GESTALT_FUSION", "rescue")
    assert gestalt_rank.fusion_mode() == "rescue"


def test_unknown_fusion_name_in_a_call_raises():
    with pytest.raises(ValueError):
        gestalt_rank.fuse([(1, -1.0)], [(2, 0.1)], "rff")


def test_rescue_without_a_dense_leg_keeps_the_lexical_order():
    assert list(gestalt_rank.fuse([(1, -3.0), (2, -2.0)], [], "rescue")) == [1, 2]
    assert math.isfinite(sum(gestalt_rank.fuse([(1, -3.0)], [(2, 0.1)], "rescue").values()))
