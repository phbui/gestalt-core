"""Shared ranking stages for the gestalt search server and the retrieval eval runner.

The server (tools/gestalt-mcp-server.py) and the runner (evals/retrieval/run_retrieval_evals.py) both import this module. That is how the eval measures the path that is served. Before this module the runner kept a mirror of the server logic, and tests/test_harness_fidelity.py had to catch drift.

Every knob is read from the environment at call time. With every knob unset, each function reproduces the behaviour from before this module existed.

    GESTALT_RERANK          off | auto | on      cross-encoder rerank after fusion (default auto)
    GESTALT_RERANK_MODEL    bge | qwen3-4b | qwen3-0.6b, or a Hugging Face id (default bge)
    GESTALT_RERANK_DEPTH    candidates the reranker sees (default 40)
    GESTALT_RERANK_MAXCHARS characters of chunk text the reranker sees (default 2000)
    GESTALT_RERANK_BATCH    pairs per forward pass in the reranker, a throughput knob only (default 8)
  GESTALT_GPU_DUTY          share of wall time the GPU may stay busy, 0.05 to 1.0, default 1.0; below 1.0 every model call is followed by a proportional pause (a clamp-avoidance experiment)
    GESTALT_RERANK_DEVICE   auto | cpu | cuda | cuda:N, where the reranker loads (default auto, which means CUDA when torch sees it)
    GESTALT_RERANK_ALLOW_ANY  1 lets GESTALT_RERANK_MODEL name a Hugging Face id outside RERANK_REVISIONS, which loads unpinned (default off, an unknown alias is rejected)
    GESTALT_RERANK_DTYPE    float16 | bfloat16 | float32, the reranker's weights (default unset, which means float16 on CUDA and the library default on cpu). A bad value raises ValueError
  GESTALT_TF32            0 | 1, allow TF32 matmuls when a model loads (default 0). A bad value raises ValueError
  GESTALT_ATTN_IMPL       sdpa | eager | flash_attention_2, passed as attn_implementation (default unset, the library's choice). A bad value raises ValueError
  GESTALT_RERANK_INSTRUCTION  task line the Qwen rerankers prepend (default names the personal knowledge base)
    GESTALT_SLUG_DECAY      1.0 keeps today's order, 0.5 demotes a repeated slug, 0 is strict dedup (default 1.0)
    GESTALT_FTS_STOPWORDS   on | off             drop closed-class words from the FTS query (default off)
    GESTALT_FUSION          rrf | convex | wrrf | rescue   how the two legs fuse (default rrf, see fuse below)
    GESTALT_FUSION_ALPHA    weight of the lexical leg, the first leg, under convex fusion (default 0.5)
    GESTALT_FUSION_W_BM25   0 to 1, the lexical weight under wrrf. The dense leg gets 1 minus it, and both are doubled, so 0.5 gives the plain RRF scores (default 0.5)
    GESTALT_FUSION_NORM     minmax | zscore | tmin, how convex and rescue normalise a leg (default minmax). tmin divides by the pool max over a declared floor: 0 for BM25, -2 for the negated L2 distance of unit vectors
    GESTALT_FUSION_MISSING  zero | leg_min, what convex gives an id that a leg lacks (default zero)
    GESTALT_RESCUE_RANK     rescue: a lexical hit must rank within this many (default 3)
    GESTALT_RESCUE_MIN      rescue: and have at least this normalised score (default 0.5)
    GESTALT_RESCUE_WINDOW   rescue: and be absent from this many dense hits (default 20)
    GESTALT_RESCUE_AT       rescue: rescued hits go in at this position from 0, and the dense hits above it never move (default 5)
    GESTALT_RESCUE_MAX      rescue: at most this many hits are rescued (default 2)
    GESTALT_ABSTAIN         off | on, add confidence and abstain to the result when the reranker ran (default off). Rows are never reordered or dropped
    GESTALT_ABSTAIN_MIN     abstain when confidence is below this (default 0.5). A probability with GESTALT_CALIB, else a raw reranker score
    GESTALT_CALIB           path to a JSON written by evals/retrieval/calibration.py. Its top1 Platt fit maps the top score to a probability (default unset, the identity map)
    GESTALT_LINK_EXPAND     off | on, after fusion and before the rerank, append the best section of each note linked to or from the top fused notes (default off)
    GESTALT_LINK_EXPAND_MAX   most rows one expansion appends (default 10)
  GESTALT_LINK_EXPAND_K   how many top fused notes the expansion starts from (default 3)

A knob with a value this module does not recognise logs one line on stderr, once per variable, and keeps its default. The exceptions are the newer knobs that raise ValueError naming the variable: GESTALT_RERANK_DTYPE, GESTALT_TF32, GESTALT_ATTN_IMPL, GESTALT_FUSION_W_BM25, GESTALT_FUSION_NORM, GESTALT_FUSION_MISSING, the GESTALT_RESCUE_* knobs, and the abstention and link expansion knobs.

The fusion arithmetic lives in evals/retrieval/fusion.py, which bench_engine calls too, so there is one copy of it.

This module is stdlib only, and so is fusion.py. The CrossEncoder import is lazy and sits inside a try, so a node without sentence-transformers still imports it.
"""

import importlib.util
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


def _load_freshness():
    """tools/gestalt_freshness.py, loaded by path beside this file. Supersession and age demotion, all off until GESTALT_FRESHNESS=on."""
    import importlib.util

    path = Path(__file__).resolve().parent / "gestalt_freshness.py"
    spec = importlib.util.spec_from_file_location("gestalt_freshness", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_FRESHNESS = None


def _freshness():
    global _FRESHNESS
    if _FRESHNESS is None:
        _FRESHNESS = _load_freshness()
    return _FRESHNESS


def freshness_prefix(result, section_id) -> str:
    """The "[updated 3 days ago, supersedes x] " text for a snippet, or "" when GESTALT_FRESHNESS is off."""
    fr = getattr(result, "freshness", None) or {}
    return _freshness().snippet_prefix({"freshness": fr.get(section_id, {})})


def _load_fusion():
    """evals/retrieval/fusion.py, loaded by path so no other module named fusion can stand in for it. bench_engine reaches it through gestalt_rank.fuse."""
    path = Path(__file__).resolve().parent.parent / "evals" / "retrieval" / "fusion.py"
    mod = sys.modules.get("fusion")
    if mod is not None and Path(getattr(mod, "__file__", "") or "").resolve() == path:
        return mod
    spec = importlib.util.spec_from_file_location("fusion", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules.setdefault("fusion", mod)
    return mod


fusion_lib = _load_fusion()

# --- knobs ---------------------------------------------------------------------------------------


_warned: set = set()


def _warn_once(name: str, raw: str, why: str) -> None:
    """One stderr line per variable, so a typo is visible without flooding a long-lived server."""
    if name not in _warned:
        _warned.add(name)
        _log(f"{name}={raw!r} {why}, using the default")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        v = math.nan
    if not math.isfinite(v):
        _warn_once(name, raw, "is not a finite number")
        return default
    return min(hi, max(lo, v))


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return min(hi, max(lo, int(raw)))
    except ValueError:
        _warn_once(name, raw, "is not an integer")
        return default


def _choice_env(name: str, default: str, choices: tuple[str, ...]) -> str:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in choices:
        return raw
    _warn_once(name, raw, f"is not one of {'|'.join(choices)}")
    return default


def rerank_mode() -> str:
    return _choice_env("GESTALT_RERANK", "auto", ("off", "auto", "on"))


def is_hub() -> bool:
    """True on the fleet hub. The hub is FLEET_HUB_NAME, else the first label of FLEET_HUB, else the literal name "hub".

    The fleet sets FLEET_HUB in ~/.fleet/fleet.env, so the fallback only matters on a machine with no fleet config, where it is
    false by construction. This file is hashed into every benchmark result and ships as is, so it names no private host."""
    import socket
    hub = os.environ.get("FLEET_HUB_NAME") or (os.environ.get("FLEET_HUB", "").split(".")[0]) or "hub"
    return socket.gethostname().split(".")[0].lower() == hub.lower()


def cuda_available() -> bool:
    """True when torch imports and sees a GPU. Any failure counts as no GPU."""
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def rerank_resolved(is_hub: bool, cuda: bool) -> str | None:
    """The reranker alias this process will use, or None when the rerank is off. The server and the eval runner both call this.

    off never reranks and on always does. auto reranks only on the hub, and only with CUDA."""
    mode = rerank_mode()
    if mode == "off" or (mode == "auto" and not (is_hub and cuda)):
        return None
    return rerank_alias()


def rerank_for(hub: bool | None = None) -> str | None:
    """rerank_resolved for this process. A leaf resolves auto to off without importing torch, so CUDA is only probed on the hub under auto.

    hub defaults to is_hub(). The server passes its own answer so a test can stand in for the hub."""
    if hub is None:
        hub = is_hub()
    cuda = cuda_available() if (hub and rerank_mode() == "auto") else False
    return rerank_resolved(hub, cuda)


def rerank_alias() -> str:
    return _env("GESTALT_RERANK_MODEL", "bge") or "bge"


def rerank_depth() -> int:
    return _int_env("GESTALT_RERANK_DEPTH", 40, 1, 200)


def rerank_maxchars() -> int:
    return _int_env("GESTALT_RERANK_MAXCHARS", 2000, 100, 20000)


def gpu_duty() -> float:
    """Share of wall time the GPU may stay busy, 0.05 to 1.0. GESTALT_GPU_DUTY, default 1.0 (no pause).

    The author's laptop 4090 clamps its power limit to 10 W after minutes of draw at the 80 W ceiling (2026-10-08: five clamps in one
    day, each 3 to 40 minutes into sustained load, each one costing the running step). The card refuses a lower power limit, so the
    only lever left is a duty cycle: after a batch that kept the GPU busy for t seconds, sleep t * (1 / duty - 1). At 0.7 the average
    draw stays near 56 W for a 43 percent slowdown. Whether that keeps the clamp away is the open test."""
    return _float_env("GESTALT_GPU_DUTY", 1.0, 0.05, 1.0)


def duty_sleep(busy_s: float, duty: float | None = None) -> float:
    """Sleep the pause that `duty` requires after `busy_s` seconds of GPU work and return the seconds slept."""
    duty = gpu_duty() if duty is None else duty
    if duty >= 1.0 or busy_s <= 0:
        return 0.0
    pause = busy_s * (1.0 / duty - 1.0)
    time.sleep(pause)
    return pause


def throttle_calls(obj, method: str) -> None:
    """Wrap `obj.<method>` so each call is followed by the pause `gpu_duty()` requires. A no-op at duty 1.0, and never applied twice."""
    if gpu_duty() >= 1.0 or getattr(getattr(obj, method), "_gestalt_throttled", False):
        return
    inner = getattr(obj, method)

    def throttled(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            return inner(*args, **kwargs)
        finally:
            duty_sleep(time.perf_counter() - t0)

    throttled._gestalt_throttled = True  # type: ignore[attr-defined]
    setattr(obj, method, throttled)


def rerank_batch() -> int:
    """Pairs per forward pass in CrossEncoder.predict. Throughput knob only, it does not change which documents win.

    Measured 2026-10-08 on the author's laptop 4090, Qwen3-Reranker-0.6B in fp16, 40 pairs of up to 2000 characters, GPU otherwise idle:
    batch 8 took 0.80 s per query, 16 took 0.87 s, 32 (the library default) 1.12 s, 40 1.54 s, 64 1.32 s. Smaller batches pad less
    because predict sorts the pairs by length first. Pooling the pairs of four queries into one call was no faster (1.04 s per query)."""
    return _int_env("GESTALT_RERANK_BATCH", 8, 1, 256)


def slug_decay() -> float:
    return _float_env("GESTALT_SLUG_DECAY", 1.0, 0.0, 1.0)


_TRUE = ("on", "1", "true", "yes")
_FALSE = ("off", "0", "false", "no")


def stopwords_on() -> bool:
    raw = _env("GESTALT_FTS_STOPWORDS").lower()
    if raw and raw not in _TRUE + _FALSE:
        _warn_once("GESTALT_FTS_STOPWORDS", raw, "is not on or off")
    return raw in _TRUE


FUSION_METHODS = ("rrf", "convex", "wrrf", "rescue")


def fusion_mode() -> str:
    return _choice_env("GESTALT_FUSION", "rrf", FUSION_METHODS)


def fusion_alpha() -> float:
    """The weight on the lexical leg, which is the first leg, under convex fusion."""
    return _float_env("GESTALT_FUSION_ALPHA", 0.5, 0.0, 1.0)


def _strict_float(name: str, default: float, lo: float, hi: float) -> float:
    """A float env var from lo to hi. Unset or empty gives the default. Anything else raises ValueError naming the variable."""
    raw = _env(name)
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        v = math.nan
    if not (math.isfinite(v) and lo <= v <= hi):
        raise ValueError(f"{name}={raw!r} is not valid. Use a number from {lo:g} to {hi:g}, or leave it unset.")
    return v


def _strict_int(name: str, default: int, lo: int) -> int:
    """An integer env var at or above lo. Unset or empty gives the default. Anything else raises ValueError naming the variable."""
    raw = _env(name)
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        v = lo - 1
    if v < lo:
        raise ValueError(f"{name}={raw!r} is not valid. Use an integer at or above {lo}, or leave it unset.")
    return v


def fusion_w_bm25() -> float:
    """The lexical weight under wrrf, 0 to 1. 0.5 gives the plain RRF scores."""
    return _strict_float("GESTALT_FUSION_W_BM25", 0.5, 0.0, 1.0)


def fusion_norm() -> str:
    return _strict_env("GESTALT_FUSION_NORM", fusion_lib.NORMALISERS) or "minmax"


def fusion_missing() -> str:
    return _strict_env("GESTALT_FUSION_MISSING", fusion_lib.MISSING) or "zero"


def rescue_params() -> dict:
    """The dense_first_rescue arguments the GESTALT_RESCUE_* knobs set."""
    return {
        "max_rank": _strict_int("GESTALT_RESCUE_RANK", 3, 1),
        "min_norm_score": _strict_float("GESTALT_RESCUE_MIN", 0.5, -math.inf, math.inf),
        "dense_window": _strict_int("GESTALT_RESCUE_WINDOW", 20, 0),
        "insert_at": _strict_int("GESTALT_RESCUE_AT", 5, 0),
        "max_rescues": _strict_int("GESTALT_RESCUE_MAX", 2, 0),
    }


def fusion_settings() -> dict:
    """Every fusion knob as the next fusion will read it. bench_stats.environment records it."""
    return {"method": fusion_mode(), "alpha": fusion_alpha(), "w_bm25": fusion_w_bm25(), "norm": fusion_norm(),
            "missing": fusion_missing(), "rescue": rescue_params(), "rrf_k": RRF_K}


def _on_off(name: str) -> bool:
    """off, on or unset. Anything else raises, as gestalt_freshness.enabled does, so config() and the search agree."""
    raw = _env(name, "off").strip().lower()
    if raw in ("", "off"):
        return False
    if raw == "on":
        return True
    raise ValueError(f"{name} must be off or on, got {raw!r}")


def config() -> dict:
    """Every ranking knob as the next search will read it. The eval runner stores this in summary["config"]."""
    return {
        "rerank": rerank_mode(),
        "rerank_model": rerank_alias(),
        "rerank_instruction": qwen_instruction(),
        "rerank_depth": rerank_depth(),
        "rerank_maxchars": rerank_maxchars(),
        "dedup": slug_decay(),
        "stopwords": stopwords_on(),
        "fusion": fusion_mode(),
        "alpha": fusion_alpha(),
        "w_bm25": fusion_w_bm25(),
        "norm": fusion_norm(),
        "missing": fusion_missing(),
        "abstain": abstain_on(),
        "link_expand": link_expand_on(),
        "freshness": _on_off("GESTALT_FRESHNESS"),
    }


# --- full-text query -----------------------------------------------------------------------------

# About sixty closed-class English words. FTS5 ships no stopword list. A word here carries no topic, so as one OR term in a natural-language question it only adds noise. A token with a digit is never dropped.
STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have how i if in into is it its "
    "me my of on or our should so than that the their them then there these they this to us was we were what "
    "when where which who whom why will with would you your about".split()
)

_MAX_TOKENS = 32


def fts_match(query: str, stopwords: bool | None = None) -> str | None:
    """Build a safe FTS5 MATCH expression: per-token OR, never a phrase query.

    Quoting the whole string makes it a phrase query, so a multi-word natural-language search would require that exact contiguous run of words and match nothing. Per-token quoting still escapes FTS5 operators (AND/OR/NEAR/*/^). Returns None when the query holds no word characters, since an empty MATCH is a syntax error.

    With stopwords off the output is the plain token list. With stopwords on, the list is lowercased for the comparison, deduplicated and capped at 32 tokens. A token with a digit stays. If the filter would leave nothing, the unfiltered tokens are used.
    """
    tokens = [t for t in re.split(r"\W+", query) if t]
    if not tokens:
        return None
    if stopwords is None:
        stopwords = stopwords_on()
    if stopwords:
        seen: set[str] = set()
        uniq = []
        for t in tokens:
            low = t.lower()
            if low not in seen:
                seen.add(low)
                uniq.append(t)
        kept = [t for t in uniq if t.lower() not in STOPWORDS or any(c.isdigit() for c in t)]
        tokens = (kept or uniq)[:_MAX_TOKENS]
    return " OR ".join('"' + t + '"' for t in tokens)


def idf_top_tokens(db, query: str, k: int = 6) -> str:
    """The k rarest non-stopword tokens of a query that the index holds, joined by spaces. Used by the prompt hook for long prompts.

    Document frequency comes from one FTS5 count per token on the open connection, so the porter stemmer applies as it does at query time. Returns the stopword-filtered query unchanged if the index cannot be read.
    """
    tokens = []
    for t in re.split(r"\W+", query):
        if t and (t.lower() not in STOPWORDS or any(c.isdigit() for c in t)) and t.lower() not in {x.lower() for x in tokens}:
            tokens.append(t)
    tokens = tokens[:_MAX_TOKENS]
    if len(tokens) <= k:
        return " ".join(tokens)
    try:
        n = db.execute("SELECT count(*) FROM sections_meta").fetchone()[0]
        scored = []
        for i, t in enumerate(tokens):
            df = db.execute("SELECT count(*) FROM sections_fts WHERE sections_fts MATCH ?", ('"' + t + '"',)).fetchone()[0]
            if df:  # a token the index has never seen matches nothing, so it cannot be one of the best k
                scored.append((-math.log((n + 1) / (df + 1)), i, t))
        if not scored:
            return " ".join(tokens)
        top = sorted(scored)[:k]
        return " ".join(t for _, _, t in sorted(top, key=lambda x: x[1]))
    except Exception:
        return " ".join(tokens)


# --- abstention ----------------------------------------------------------------------------------


def abstain_on() -> bool:
    """GESTALT_ABSTAIN as a bool. Unset is off. A value other than off or on raises ValueError."""
    return _strict_env("GESTALT_ABSTAIN", ("off", "on")) == "on"


def load_calib(path: str | None = None) -> tuple[float, float] | None:
    """The (scale, offset) Platt fit in the calibration JSON at GESTALT_CALIB, or None when it is unset.

    It reads the top1 signal, the reranker's top score. A missing file, bad JSON or a fit that is not ok raises ValueError."""
    path = _env("GESTALT_CALIB") if path is None else path
    return calib_fit(path, "top1") if path else None


def calib_fit(json_path, signal: str = "top1") -> tuple[float, float]:
    """(scale, offset) of the Platt fit for one signal in a calibration.py JSON. Raises ValueError when there is none.

    evals/retrieval/calibration.py fit_for is this function, so the eval and the ranker share one reader."""
    try:
        blob = json.loads(Path(json_path).read_text())
        cal = blob["signals"][signal]["calibration"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"{json_path}: no calibration for signal {signal!r} ({type(e).__name__}: {str(e)[:80]})") from e
    if not isinstance(cal, dict) or cal.get("status") != "ok":
        raise ValueError(f"{json_path}: the calibration fit for {signal!r} is not ok")
    a, b = float(cal["scale"]), float(cal["offset"])
    if not (math.isfinite(a) and math.isfinite(b)):
        raise ValueError(f"{json_path}: the calibration fit for {signal!r} is not finite")
    return a, b


def confidence(top_score: float | None, calib: tuple[float, float] | None = None) -> float | None:
    """Probability that the query is answerable. It is sigmoid(scale * top_score + offset) for a Platt fit (scale, offset).

    With calib None it is the identity map on the raw score. A top_score of None gives None, never a guess."""
    if top_score is None:
        return None
    if calib is None:
        return float(top_score)
    z = calib[0] * float(top_score) + calib[1]
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def abstain_min(calibrated: bool) -> float:
    """GESTALT_ABSTAIN_MIN. Default 0.5. With a calibration it is a probability from 0 to 1, else any raw score. A bad value raises ValueError."""
    return _strict_float("GESTALT_ABSTAIN_MIN", 0.5, 0.0, 1.0) if calibrated else _strict_float("GESTALT_ABSTAIN_MIN", 0.5, -math.inf, math.inf)


def abstain_decision(top_score: float | None, calib: tuple[float, float] | None = None, minimum: float | None = None) -> tuple[float | None, bool]:
    """(confidence, abstain). Abstain is True when the confidence is below the minimum. A confidence equal to it does not abstain.

    A top_score of None gives (None, False)."""
    conf = confidence(top_score, calib)
    if conf is None:
        return None, False
    minimum = abstain_min(calib is not None) if minimum is None else minimum
    return conf, conf < minimum


# --- wikilink expansion --------------------------------------------------------------------------

_WIKILINK_SLUG_RE = re.compile(r"\[\[\s*([^\]#|\[]+?)\s*(?:[#|][^\]]*)?\]\]")


def link_expand_on() -> bool:
    """GESTALT_LINK_EXPAND as a bool. Unset is off. A value other than off or on raises ValueError."""
    return _strict_env("GESTALT_LINK_EXPAND", ("off", "on")) == "on"


def link_expand_k() -> int:
    return _strict_int("GESTALT_LINK_EXPAND_K", 3, 1)


def link_expand_max() -> int:
    """GESTALT_LINK_EXPAND_MAX, the most rows one expansion may append (default 10). A hub note with many links adds that many rerank pairs."""
    return _strict_int("GESTALT_LINK_EXPAND_MAX", 10, 1)


def wikilink_slugs(text: str) -> list[str]:
    """The note slugs a text links to, in order and without repeats. `[[slug#^block]]`, `[[slug#heading]]` and `[[slug|alias]]` all name `slug`."""
    return list(dict.fromkeys(m.group(1) for m in _WIKILINK_SLUG_RE.finditer(text or "")))


def expand_links(db, metas: list, fused: dict, k: int) -> list:
    """The rows to append to the pool: the best section of each note linked to or from the top k notes of `metas`.

    The index has no links table. The links are read from the section text of sections_meta with wikilink_slugs. A note is skipped when any of its sections is already in `metas`. The best section is the one with the highest fused score, else the lowest id. Each row is a dict with expanded_from set to the top note that led to it. Outgoing links come first, then incoming ones."""
    top: list = []
    for m in metas:
        slug = _field(m, "slug")
        if slug and slug not in top:
            top.append(slug)
        if len(top) == k:
            break
    present = {_field(m, "slug") for m in metas}
    source: dict = {}
    for slug in top:
        for (content,) in db.execute("SELECT content FROM sections_meta WHERE slug = ? ORDER BY id", (slug,)).fetchall():
            for target in wikilink_slugs(content):
                if target not in present and target != slug:
                    source.setdefault(target, slug)
    for slug in top:
        like = "%[[" + slug.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        for other, content in db.execute("SELECT slug, content FROM sections_meta WHERE slug != ? AND content LIKE ? ESCAPE '\\' ORDER BY id", (slug, like)).fetchall():
            if other not in present and slug in wikilink_slugs(content):
                source.setdefault(other, slug)
    out = []
    for target, origin in source.items():
        rows = db.execute("SELECT * FROM sections_meta WHERE slug = ? ORDER BY id", (target,)).fetchall()
        if not rows:
            continue
        best = max(rows, key=lambda r: (fused.get(r["id"], -math.inf), -r["id"]))
        out.append({**dict(zip(best.keys(), tuple(best))), "expanded_from": origin})
    return out


# --- candidate depth -----------------------------------------------------------------------------


def pool_size(limit: int, rerank_on: bool, depth: int | None = None) -> int:
    """How many candidates each leg fetches before fusion. Twice the limit, or the rerank depth if that is larger."""
    if not rerank_on:
        return limit * 2
    return max(limit * 2, depth if depth is not None else rerank_depth())


# --- reciprocal rank fusion ----------------------------------------------------------------------

# The RRF constant. It is fixed on purpose: a relevance gate compared against a score ceiling of N_LEGS/(RRF_K+1) is how four unreachable gates once shipped.
RRF_K = fusion_lib.RRF_K


def rrf_fuse(legs: list[list], k: int = RRF_K) -> dict:
    """Reciprocal Rank Fusion over ranked legs of ids. An id's score is the sum of 1/(k + rank + 1) over the legs that hold it. It is fusion.rrf."""
    return fusion_lib.rrf(legs, k)


# --- slug-level dedup ----------------------------------------------------------------------------


def _field(row, key: str, default=""):
    """Read a column from a sqlite3.Row or a dict. A missing column or a NULL gives the default."""
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def slug_decay_select(rows: list, limit: int, decay: float, slug_key: str = "slug") -> list:
    """Greedy selection with slug identity as the similarity, a cheap form of MMR.

    Row i has base score 1/(RRF_K+i+1), where i is its position in the input order. Its effective score is base * decay ** (rows already chosen from its slug). Pick the highest, ties to the earlier row, stop at limit. Decay 1 returns the input order. Decay 0 takes one row per slug first, then backfills from the skipped rows when fewer than limit slugs exist.
    """
    if decay >= 1.0 or limit <= 0:
        return list(rows[:limit]) if limit > 0 else []
    base = [1.0 / (RRF_K + i + 1) for i in range(len(rows))]
    taken: dict = {}
    left = list(range(len(rows)))
    out = []
    while left and len(out) < limit:
        best, best_eff = None, -1.0
        for i in left:
            eff = base[i] * (decay ** taken.get(_field(rows[i], slug_key), 0))
            if eff > best_eff:
                best, best_eff = i, eff
        left.remove(best)
        slug = _field(rows[best], slug_key)
        taken[slug] = taken.get(slug, 0) + 1
        out.append(rows[best])
    return out


def decay_stats(pool: list, picked: list, limit: int, slug_key: str = "slug") -> dict:
    """Numbers for the dedup log line: distinct slugs in the picked rows, and how many of the pool's top-limit rows were displaced."""
    top = {id(r) for r in pool[:limit]}
    return {
        "pool": len(pool),
        "slugs_distinct": len({_field(r, slug_key) for r in picked}),
        "demoted": len(top - {id(r) for r in picked}),
    }


# --- convex fusion -------------------------------------------------------------------------------


_minmax = fusion_lib.minmax  # the name the unit tests and older callers use

# The declared floors tmin divides over. The lexical leg is FTS5's negated rank, a BM25 score, which is never below 0. The dense
# leg is the negated L2 distance, and two unit vectors are at most 2 apart.
LEXICAL_FLOOR = 0.0
DENSE_FLOOR = -2.0


def convex_fuse(fts_rows: list[tuple], vec_rows: list[tuple], alpha: float, norm: str | None = None, missing: str | None = None) -> dict:
    """score = alpha * norm(lexical) + (1 - alpha) * norm(dense), through fusion.convex. alpha weights the lexical leg, which is the first leg.

    fts_rows are (id, rank) pairs where rank is FTS5's bm25 rank: negative, more negative is better. vec_rows are (id, distance) pairs from sqlite-vec's vec0 table. The table is declared without a distance_metric, so the distance is L2 and a smaller value is closer. Both are negated so a larger value is better. norm and missing default to GESTALT_FUSION_NORM (minmax) and GESTALT_FUSION_MISSING (zero, an id missing from a leg gets 0 from it).

    The returned dict is already in final order: score descending, then the better leg rank, then the smaller id. Callers sort it again with a stable sort and keep that order.
    """
    return fusion_lib.convex([[(i, -s) for i, s in fts_rows], [(i, -d) for i, d in vec_rows]], alpha,
                             normaliser=norm or fusion_norm(), missing=missing or fusion_missing(), lows=(LEXICAL_FLOOR, DENSE_FLOOR))


def fuse(fts_rows: list[tuple], vec_rows: list[tuple], fusion: str | None = None, alpha: float | None = None,
         w_bm25: float | None = None) -> dict:
    """Fuse an FTS5 pool of (id, rank) and a vec0 pool of (id, L2 distance). The server, the eval runner and bench_engine all call this.

    fusion is rrf, convex, wrrf or rescue, else GESTALT_FUSION. A parameter left as None is read from its knob.
      rrf     plain reciprocal rank fusion, K=60. The dict is in first-appearance order, lexical leg first, for a stable sort.
      convex  convex_fuse with alpha on the lexical leg.
      wrrf    weighted RRF with weights (2 * w_bm25, 2 * (1 - w_bm25)) on (lexical, dense). w_bm25 0.5 gives the rrf scores, 0 the dense order.
      rescue  the dense order with up to GESTALT_RESCUE_MAX lexical hits inserted (fusion.dense_first_rescue).
    Every method but rrf returns its dict in final order. A stable sort by score keeps that order."""
    fusion = fusion or fusion_mode()
    if fusion == "rrf":
        return rrf_fuse([[i for i, _ in fts_rows], [i for i, _ in vec_rows]])
    if fusion == "convex":
        return convex_fuse(fts_rows, vec_rows, fusion_alpha() if alpha is None else alpha)
    if fusion == "wrrf":
        w = fusion_w_bm25() if w_bm25 is None else w_bm25
        if not (isinstance(w, (int, float)) and 0.0 <= w <= 1.0):
            raise ValueError(f"w_bm25 {w!r} is not a number from 0 to 1")
        return fusion_lib.weighted_rrf([[i for i, _ in fts_rows], [i for i, _ in vec_rows]], (2.0 * w, 2.0 * (1.0 - w)), RRF_K)
    if fusion == "rescue":
        if not vec_rows:  # an FTS-only search has no dense order to keep, so it keeps the lexical order as rrf does
            return rrf_fuse([[i for i, _ in fts_rows]])
        return fusion_lib.dense_first_rescue([(i, -d) for i, d in vec_rows], [(i, -s) for i, s in fts_rows], normaliser=fusion_norm(),
                                             lexical_low=LEXICAL_FLOOR, **rescue_params())
    raise ValueError(f"unknown fusion {fusion!r}. Use one of: {', '.join(FUSION_METHODS)}")


# --- cross-encoder rerank ------------------------------------------------------------------------

RERANK_MODELS = {
    "bge": "BAAI/bge-reranker-v2-m3",
    "qwen3-4b": "Qwen/Qwen3-Reranker-4B",
    "qwen3-0.6b": "Qwen/Qwen3-Reranker-0.6B",
}
DEFAULT_QWEN_INSTRUCTION = "Given a question about a personal engineering knowledge base, retrieve the note section that answers it"


def qwen_instruction() -> str:
    """The task line the Qwen rerankers prepend. GESTALT_RERANK_INSTRUCTION overrides it, because the default names a personal knowledge base and a public benchmark such as SciFact needs its own task (a scientific claim against abstracts)."""
    return _env("GESTALT_RERANK_INSTRUCTION", DEFAULT_QWEN_INSTRUCTION) or DEFAULT_QWEN_INSTRUCTION


# Each known alias maps to its Hub id and a pinned commit, looked up on huggingface.co/api/models/<id> on 2026-10-08. The load passes revision= so a moved branch cannot swap the weights.
RERANK_REVISIONS = {
    "bge": ("BAAI/bge-reranker-v2-m3", "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"),
    "qwen3-4b": ("Qwen/Qwen3-Reranker-4B", "22e683669bc0f0bd69640a1354a6d0aebcfeede5"),
    "qwen3-0.6b": ("Qwen/Qwen3-Reranker-0.6B", "e61197ed45024b0ed8a2d74b80b4d909f1255473"),
}


def pinned_model(alias: str) -> tuple[str, str | None]:
    """(hub id, revision) for an alias or a known Hub id. Anything else raises ValueError unless GESTALT_RERANK_ALLOW_ANY=1, which loads it unpinned."""
    if alias in RERANK_REVISIONS:
        return RERANK_REVISIONS[alias]
    for hub_id, rev in RERANK_REVISIONS.values():
        if alias == hub_id:
            return hub_id, rev
    if _env("GESTALT_RERANK_ALLOW_ANY") in _TRUE:
        return alias, None
    raise ValueError(f"unknown reranker {alias!r}: known aliases are {', '.join(RERANK_REVISIONS)}. Set GESTALT_RERANK_ALLOW_ANY=1 to load an unpinned Hub id")


QWEN_4B_MIN_FREE_BYTES = 9 * 1024**3

_rerank_lock = threading.Lock()
_rerankers: dict = {}
_rerank_last_used = 0.0
_load_failed: dict = {}
LOAD_RETRY_S = 300


LOAD_SLOW_S = 120  # a model load that holds its lock longer than this logs one line


def load_watchdog(what: str) -> threading.Timer:
    """Start a daemon timer that logs when a model load under a lock runs past LOAD_SLOW_S. The caller cancels it when the load ends. It never interrupts the load."""
    t = threading.Timer(LOAD_SLOW_S, lambda: _log(f"{what} load still holds its lock after {LOAD_SLOW_S}s, other searches wait on it"))
    t.daemon = True
    t.start()
    return t


LOG_PREFIX = "gestalt"  # the caller sets it: the server "gestalt-mcp", the eval runner "gestalt-eval"


def _log(msg: str) -> None:
    print(f"{LOG_PREFIX}: {msg}", file=sys.stderr)  # stdout is the MCP transport, so never print there


ALIAS_TTL_S = 300
_alias_memo: dict = {}  # alias -> (monotonic time, resolved alias)


def resolve_alias(alias: str | None = None) -> str:
    """The alias that will load. qwen3-4b needs 9 GB of free VRAM, else it falls back to bge with one stderr line.

    The answer is kept for ALIAS_TTL_S seconds, so a low-VRAM hub logs the fallback once per window and not once per query."""
    alias = alias or rerank_alias()
    hit = _alias_memo.get(alias)
    if hit and time.monotonic() - hit[0] < ALIAS_TTL_S:
        return hit[1]
    resolved = _resolve_alias_uncached(alias)
    _alias_memo[alias] = (time.monotonic(), resolved)
    return resolved


def _resolve_alias_uncached(alias: str) -> str:
    if alias == "qwen3-4b":
        try:
            import torch

            free = torch.cuda.mem_get_info()[0] if torch.cuda.is_available() else 0
        except Exception:
            free = 0
        if free < QWEN_4B_MIN_FREE_BYTES:
            _log(f"rerank model qwen3-4b needs {QWEN_4B_MIN_FREE_BYTES // 1024**3} GB free VRAM, found {free / 1024**3:.1f} GB — using bge")
            return "bge"
    return alias


def rerank_device_kwargs() -> tuple[str, dict]:
    """The device the reranker loads on and the constructor kwargs that go with it.

    GESTALT_RERANK_DEVICE names it outright. Otherwise CUDA wins when torch sees it, else cpu. On CUDA the weights load in float16, which halves the memory and the time. The library default is not trusted here, because a reranker that lands on the CPU of the hub costs ten cores for minutes per eval run."""
    device = _env("GESTALT_RERANK_DEVICE", "auto").lower()
    if device == "auto":
        device = "cuda" if cuda_available() else "cpu"
    model_kwargs: dict = {}
    dtype = _strict_env("GESTALT_RERANK_DTYPE", ("float16", "bfloat16", "float32"))
    if dtype:
        model_kwargs["torch_dtype"] = dtype
    elif device.startswith("cuda"):
        model_kwargs["torch_dtype"] = "float16"  # a string, which sentence-transformers resolves, so no torch import here
    attn = _strict_env("GESTALT_ATTN_IMPL", ("sdpa", "eager", "flash_attention_2"))
    if attn:
        model_kwargs["attn_implementation"] = attn
    return device, ({"model_kwargs": model_kwargs} if model_kwargs else {})


def _strict_env(name: str, choices: tuple[str, ...]) -> str:
    """An env var that must be one of `choices`. Unset or empty gives "". Anything else raises ValueError."""
    raw = _env(name).lower()
    if raw and raw not in choices:
        raise ValueError(f"{name}={raw!r} is not valid. Use one of: {', '.join(choices)}, or leave it unset.")
    return raw


def tf32_on() -> bool:
    """GESTALT_TF32 as a bool. 0 or unset is False."""
    return _strict_env("GESTALT_TF32", ("0", "1")) == "1"


def apply_tf32() -> None:
    """Set the two TF32 switches in torch to GESTALT_TF32. Called when the reranker loads."""
    import torch

    torch.backends.cuda.matmul.allow_tf32 = tf32_on()
    torch.backends.cudnn.allow_tf32 = tf32_on()


def rerank_dtype() -> str:
    """The reranker's weights dtype as it resolves now: the knob, else float16 on CUDA, else "default" (the library's own)."""
    _, kwargs = rerank_device_kwargs()
    return kwargs.get("model_kwargs", {}).get("torch_dtype", "default")


def get_reranker(alias: str | None = None):
    """Load a CrossEncoder once and keep it. The import is lazy and may raise ImportError or a load error. Callers catch it."""
    global _rerank_last_used
    alias = alias or rerank_alias()
    name, revision = pinned_model(alias)  # an unknown alias is rejected before any load
    with _rerank_lock:
        model = _rerankers.get(alias)
        if model is None:
            if time.monotonic() - _load_failed.get(alias, -1e9) < LOAD_RETRY_S:
                raise RuntimeError(f"reranker {alias} failed to load less than {LOAD_RETRY_S}s ago")  # a broken load is not retried on every query
            watchdog = load_watchdog(f"reranker {alias}")
            try:
                from sentence_transformers import CrossEncoder

                device, kwargs = rerank_device_kwargs()
                apply_tf32()
                _log(f"loading reranker {name} revision={revision or 'unpinned'} device={device}")
                model = CrossEncoder(name, trust_remote_code=False, device=device, revision=revision, **kwargs)
            except Exception:
                _load_failed[alias] = time.monotonic()
                raise
            finally:
                watchdog.cancel()
            throttle_calls(model, "predict")  # GESTALT_GPU_DUTY, a no-op at the default
            _rerankers[alias] = model
        _rerank_last_used = time.monotonic()
    return model


def unload_reranker(idle_s: int = 0, force: bool = False) -> bool:
    """Drop every loaded reranker if idle for idle_s seconds, or at once when forced. True when something was released."""
    global _rerankers
    with _rerank_lock:
        if not _rerankers:
            return False
        if not force and (idle_s <= 0 or time.monotonic() - _rerank_last_used < idle_s):
            return False
        _rerankers = {}
    _log("reranker unloaded after idle")
    return True


def rerank_loaded() -> bool:
    return bool(_rerankers)


def rerank_text(row, maxchars: int) -> str:
    title = _field(row, "title") or _field(row, "slug")
    return f"{title} — {_field(row, 'heading')}\n{str(_field(row, 'content'))[:maxchars]}"


def _is_cuda_error(e: Exception) -> bool:
    return type(e).__name__ == "AcceleratorError" or (isinstance(e, RuntimeError) and "cuda" in str(e).lower())


def _rerank_fallback(rows: list, info: dict, t0: float, e: Exception) -> tuple[list, None, dict]:
    info["fallback"] = type(e).__name__
    info["ms"] = int((time.perf_counter() - t0) * 1000)
    _log(f"rerank fell back to fusion order ({type(e).__name__}: {str(e)[:120]})")
    return list(rows), None, info


def rerank_rows(query: str, rows: list, alias: str | None = None, maxchars: int | None = None) -> tuple[list, list | None, dict]:
    """Reorder rows by cross-encoder score, best first. Returns (rows, scores, info).

    scores is aligned with the returned rows, or None when the rerank did not run. Any import, CUDA or load error keeps the input order and says so in info["fallback"]. The sort is stable, so equal scores keep their fusion order.
    """
    t0 = time.perf_counter()
    alias = resolve_alias(alias)
    maxchars = maxchars or rerank_maxchars()
    info = {"model": alias, "n": len(rows), "ms": 0, "top1_changed": False, "fallback": "none"}
    if len(rows) < 2:
        return list(rows), None, {**info, "fallback": "too-few"}
    try:
        model = get_reranker(alias)
    except Exception as e:
        return _rerank_fallback(rows, info, t0, e)
    try:
        pairs = [(query, rerank_text(r, maxchars)) for r in rows]
        kw = {"prompt": qwen_instruction()} if alias.startswith("qwen3") else {}
        scores = [float(s) for s in model.predict(pairs, batch_size=rerank_batch(), **kw)]
    except Exception as e:
        if _is_cuda_error(e):
            unload_reranker(force=True)  # a CUDA fault leaves the weights unusable, so the next call reloads
        return _rerank_fallback(rows, info, t0, e)
    if not all(math.isfinite(sc) for sc in scores):
        # fp16 overflow gives NaN or inf. NaN sorts arbitrarily, so the whole call keeps the fusion order.
        info["fallback"] = "nonfinite-score"
        info["ms"] = int((time.perf_counter() - t0) * 1000)
        _log("rerank fell back to fusion order (non-finite score)")
        return list(rows), None, info
    order = sorted(range(len(rows)), key=lambda i: -scores[i])
    info["top1_changed"] = order[0] != 0
    info["ms"] = int((time.perf_counter() - t0) * 1000)
    return [rows[i] for i in order], [scores[i] for i in order], info


def rerank_log_line(info: dict) -> str:
    return (f"rerank model={info['model']} n={info['n']} ms={info['ms']} "
            f"top1_changed={info['top1_changed']} fallback={info['fallback']}")


# --- hybrid search -------------------------------------------------------------------------------

_default_alias = rerank_alias  # hybrid_search has a parameter of the same name


@dataclass
class SearchResult:
    """What hybrid_search found.

    rows are sqlite3.Row objects from sections_meta in final order. fused_scores and rerank_scores are keyed by row id and hold the rows that have one. scores is the score each row is ranked by: the rerank score when the rerank ran, else the fused score. top_score is the score that put row 0 first: the rerank score if the rerank ran, else the fused score, else FTS5's negated bm25 in fts mode (a one-leg RRF score is rank only, so it cannot separate anything). It is None when nothing came back.

    confidence and abstain are set only when GESTALT_ABSTAIN is on and the rerank ran (see abstain_decision). Otherwise confidence is None and abstain is False. expanded is how many rows the link expansion appended to the pool.

    rerank is the rerank_rows info dict, or None when no rerank was requested. rerank_fallback is True when a rerank was requested and did not run. A pool of fewer than two rows has nothing to order and does not count. decay holds the dedup numbers, or None when dedup is off.
    """

    rows: list = field(default_factory=list)
    scores: dict = field(default_factory=dict)
    fused_scores: dict = field(default_factory=dict)
    rerank_scores: dict = field(default_factory=dict)
    top_score: Optional[float] = None
    rerank: Optional[dict] = None
    rerank_fallback: bool = False
    decay: Optional[dict] = None
    fusion: str = "rrf"
    alpha: float = 0.5
    pool: int = 0
    fts_failed: bool = False
    confidence: Optional[float] = None
    abstain: bool = False
    expanded: int = 0


_LIGHT_COLUMNS = "id, slug, heading, block_id, anchors"
_IN_CHUNK = 500  # stays under SQLite's 999-variable limit on old builds


def _fetch_meta(db, ids: list, columns: str) -> list:
    by_id: dict = {}
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = ids[i:i + _IN_CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in db.execute(f"SELECT {columns} FROM sections_meta WHERE id IN ({marks})", chunk).fetchall():
            by_id[r["id"]] = r
    return [by_id[i] for i in ids if i in by_id]


def hybrid_search(db, query: str, limit: int, *, embed_query: Callable, mode: str = "hybrid", rerank: bool | None = None,
                  decay: float | None = None, stopwords: bool | None = None, fusion: str | None = None,
                  alpha: float | None = None, rerank_alias: str | None = None, full: bool = False) -> SearchResult:
    """The whole ranking path in one place: FTS leg, dense leg, fusion, rerank, slug dedup, cut to limit. The server and the eval runner both call it.

    db needs row_factory sqlite3.Row and the tables sections_fts, sections_vec and sections_meta. embed_query(query) returns the query vector as bytes or as an object with tobytes(). It is not called in mode "fts", which is the leg the per-prompt hook uses.

    rerank True forces the rerank, False forbids it, and None resolves it as the server does (rerank_for). rerank_alias names the reranker, else GESTALT_RERANK_MODEL. The rerank only runs in hybrid mode. A knob left as None is read from the environment. full=True fetches every sections_meta column for every row, else only the light ones plus the chunk text when the rerank runs.
    """
    if mode == "hybrid" and rerank is None:
        alias = rerank_for()
        if alias is not None and rerank_alias:
            alias = rerank_alias
    elif mode == "hybrid" and rerank:
        alias = rerank_alias or _default_alias()
    else:
        alias = None
    decay = slug_decay() if decay is None else decay
    fusion = fusion or fusion_mode()
    alpha = fusion_alpha() if alpha is None else alpha
    depth = rerank_depth()
    pool = pool_size(limit, alias is not None, depth)

    fts_rows: list = []
    fts_failed = False
    fts_query = fts_match(query, stopwords)
    if fts_query:
        try:
            fts_rows = db.execute(
                "SELECT rowid, rank AS score FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit * 3 if (mode == "fts" and decay < 1.0) else pool),  # an FTS-only dedup reads more rows, as the FTS-only server path does
            ).fetchall()
        except sqlite3.Error as e:
            fts_rows = []
            fts_failed = True
            _log(f"FTS leg failed, searching without it ({type(e).__name__}: {str(e)[:120]})")

    vec_rows: list = []
    if mode == "hybrid":
        vec = embed_query(query)
        vec_rows = db.execute(
            "SELECT id, distance FROM sections_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (vec if isinstance(vec, (bytes, bytearray)) else vec.tobytes(), pool),
        ).fetchall()

    fused = fuse([(r["rowid"], r["score"]) for r in fts_rows], [(r["id"], r["distance"]) for r in vec_rows], fusion, alpha)
    result = SearchResult(fusion=fusion, alpha=alpha, pool=pool, fts_failed=fts_failed)
    ordered = sorted(fused, key=fused.get, reverse=True)
    if not ordered:
        return result

    # Off, the pool is the top `limit`. A rerank reads `depth` rows. A dedup reads every fused row.
    keep = max(limit, depth) if alias else (len(ordered) if decay < 1.0 else limit)
    metas = _fetch_meta(db, ordered[:keep], "*" if (full or alias) else _LIGHT_COLUMNS)

    if link_expand_on() and metas:
        extra = expand_links(db, metas, fused, link_expand_k())[:link_expand_max()]
        if extra:
            floor = min(fused[m["id"]] for m in metas)
            for row in extra:
                floor = math.nextafter(floor, -math.inf)
                fused[row["id"]] = floor
            metas = list(metas) + extra
            result.expanded = len(extra)

    rr_by_id: dict = {}
    if alias and metas:
        metas, rr_scores, info = rerank_rows(query, metas, alias)
        result.rerank = info
        result.rerank_fallback = rr_scores is None and info["fallback"] != "too-few"
        if rr_scores is not None:
            rr_by_id = {m["id"]: sc for m, sc in zip(metas, rr_scores)}
    if decay < 1.0:
        picked = slug_decay_select(metas, limit, decay)
        result.decay = decay_stats(metas, picked, limit)
        metas = picked
    metas = metas[:limit]
    if not metas:
        return result

    result.rows = metas
    result.fused_scores = {m["id"]: fused[m["id"]] for m in metas}
    result.rerank_scores = {i: rr_by_id[i] for i in result.fused_scores if i in rr_by_id}
    result.scores = {i: result.rerank_scores.get(i, f) for i, f in result.fused_scores.items()}
    top = metas[0]["id"]
    if top in rr_by_id:
        result.top_score = rr_by_id[top]
    elif mode == "fts":
        result.top_score = next((-r["score"] for r in fts_rows if r["rowid"] == top), None)
    else:
        result.top_score = fused.get(top)
    if abstain_on() and top in rr_by_id:
        result.confidence, result.abstain = abstain_decision(result.top_score, load_calib())
    _freshness().apply_to_result(db, result)  # a no-op until GESTALT_FRESHNESS=on; then demotes superseded and expired sections
    return result


def auroc(pos: list[float], neg: list[float]) -> float | None:
    """P(score of a positive > score of a negative), ties count half. Rank-sum form of the Mann-Whitney statistic. None if either side is empty."""
    if not pos or not neg:
        return None
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    ranks = [0.0] * len(allv)
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    rank_sum_pos = sum(r for r, (_, lab) in zip(ranks, allv) if lab == 1)
    n_pos, n_neg = len(pos), len(neg)
    return (rank_sum_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
