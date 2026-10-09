#!/usr/bin/env python3
"""Retrieval quality eval for gestalt_search.

The skill evals in evals/configs/ measure whether a PROMPT routes to the right
SKILL. Nothing measured whether a QUESTION retrieves the right KNOWLEDGE — this
does, against evals/retrieval/golden.yaml.

Ranks through the same code as the MCP server (tools/gestalt-mcp-server.py): both call
gestalt_rank.hybrid_search, so the FTS5 tokenisation, the RRF constant, the candidate depth,
the fusion, the rerank and the slug dedup have one definition. A knob left unset resolves as
the server resolves it, so rerank "auto" turns on at the hub with CUDA here too. A caller that
wants the base system passes rerank=False. tests/test_harness_fidelity.py runs both paths on
one index and fails if they disagree.

Usage:
    python3 evals/retrieval/run_retrieval_evals.py            # run + report
    python3 evals/retrieval/run_retrieval_evals.py --json out.json
    python3 evals/retrieval/run_retrieval_evals.py --split dev --json dev.json   # dev cases only (see assign_splits.py)
    python3 evals/retrieval/run_retrieval_evals.py --baseline bank --config-name default   # rerank off, the shipped default
    python3 evals/retrieval/run_retrieval_evals.py --baseline bank --config-name hub       # rerank on, as the hub serves it
    python3 evals/retrieval/run_retrieval_evals.py --baseline check   # gate every banked configuration, exit 1 on a regression or when not comparable
    python3 evals/retrieval/run_retrieval_evals.py --baseline check --strict   # old rule: any drop
    python3 evals/retrieval/run_retrieval_evals.py --mode fts   # leaf: full-text leg only, never banked
    python3 evals/retrieval/run_retrieval_evals.py --rerank on --compare before.json   # paired bootstrap against a saved run

Knobs: --rerank off|on, --dedup DECAY (1 = off), --stopwords on|off, --fusion rrf|convex, --alpha A.
--baseline save is the old spelling of --baseline bank --config-name default.

Exit codes: 0 pass, 1 regression or not comparable (the gate fails closed), 2 usage,
3 a run asked for the rerank and the reranker fell back to the fusion order on some query.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))  # sibling import under any cwd
import gestalt_rank  # noqa: E402  the ranking stages the server also uses
gestalt_rank.LOG_PREFIX = "gestalt-eval"  # the shared module's stderr lines carry the runner's name
try:
    import gestalt_embed_config as _ec
except ImportError:  # a lone copy of this file (a test fixture, a stale install): behave as before X14, unpinned
    class _ec:  # noqa: N801
        MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
        MODEL_REVISION = None
        EMBED_DIM = 768
        DOC_PREFIX = "search_document: "
        QUERY_PREFIX = "search_query: "

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))  # assign_splits, imported lazily where the clusters are needed
GESTALT_DIR = EVAL_DIR.parent.parent
DEFAULT_DB_PATH = GESTALT_DIR / ".search" / "gestalt.db"
# GESTALT_SEARCH_DIR points the runner at a side index. resolve_db_path is the config module's rule for it.
DB_PATH = getattr(_ec, "resolve_db_path", lambda default: default)(DEFAULT_DB_PATH)
GOLDEN = EVAL_DIR / "golden.yaml"
BASELINE = EVAL_DIR / "baseline.json"

K_RRF = gestalt_rank.RRF_K  # an alias, kept because evals/retrieval/beir_bench.py records it
DEPTHS = (1, 3, 5)
# gestalt_search's own default. The eval previously fused over a pool of
# limit*2 where limit=max(DEPTHS)=5, i.e. half the candidates a real default
# call sees — measuring a narrower pipeline than the one actually served.
SERVED_DEFAULT_LIMIT = 10
SPLITS = ("dev", "test", "all")
BOOTSTRAP_ITERATIONS = 10000
BOOTSTRAP_SEED = 12345
EXIT_RERANK_FALLBACK = 3
EXIT_QUERY_STALL = 4
PROGRESS_EVERY_CASES = 10
PROGRESS_EVERY_SECONDS = 60.0

_model = None


def get_model():
    """The embedding model for this run, loaded once and cached. The hub relay path never calls it."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        # Same model, revision and prefixes as the index builder and the server (X14, 2026-10-06).
        kw = {"revision": _ec.MODEL_REVISION} if getattr(_ec, "MODEL_REVISION", None) else {}
        # The device follows the builder's rule, not the server's. GESTALT_EMBED_DEVICE set means that device.
        # Unset means sentence-transformers picks the GPU when it sees one. The server's cpu default is for a
        # shared MCP process and would put every benchmark on the CPU (2026-10-08: a 2 minute SciFact took 48).
        device = os.environ.get("GESTALT_EMBED_DEVICE", "").strip()
        if device:
            kw["device"] = device
        if getattr(_ec, "MODEL_KWARGS", None):
            kw["model_kwargs"] = _ec.MODEL_KWARGS
        if getattr(_ec, "TOKENIZER_KWARGS", None):
            kw["tokenizer_kwargs"] = _ec.TOKENIZER_KWARGS
        if hasattr(_ec, "apply_tf32"):
            _ec.apply_tf32()
        set_model(SentenceTransformer(_ec.MODEL_NAME, trust_remote_code=getattr(_ec, "TRUST_REMOTE_CODE", True), **kw))
    return _model


def set_model(model) -> None:
    """Use this query encoder from now on. Its encode() gets the GPU duty-cycle throttle (gestalt_rank.throttle_calls, a no-op at GESTALT_GPU_DUTY=1.0). The memory harnesses call it to share one loaded model."""
    global _model
    gestalt_rank.throttle_calls(model, "encode")
    _model = model


def get_db(need_vec: bool = True) -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    if need_vec:
        import sqlite_vec

        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    return db


def has_vectors(db) -> bool:
    """True when sections_vec exists and holds rows. A leaf's index has none (X6, 2026-10-06)."""
    try:
        return bool(db.execute("SELECT 1 FROM sections_vec LIMIT 1").fetchone())
    except Exception:
        return False


def _embed_query(query: str):
    """Same as the server's: the profile prefix, then the profile post-processing."""
    vec = get_model().encode(_ec.QUERY_PREFIX + query, convert_to_numpy=True)
    post = getattr(_ec, "postprocess", None)
    return post(vec) if post else vec


def atomic_write_text(path: Path | str, text: str) -> None:
    """Write text so a reader never sees a half-written file: a temp file in the same directory, fsync, then os.replace."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def search_result(db, query: str, limit: int = 5, mode: str = "hybrid", rerank: bool | None = None,
                  decay: float | None = None, stopwords: bool | None = None, fusion: str | None = None,
                  alpha: float | None = None, rerank_alias: str | None = None) -> gestalt_rank.SearchResult:
    """The served ranking path, gestalt_rank.hybrid_search, with this runner's query encoder.

    rerank None resolves as the server resolves it (gestalt_rank.rerank_for). True forces the rerank and False forbids it. Other knobs left as None are read from the environment, as the server reads them."""
    return gestalt_rank.hybrid_search(db, query, limit, embed_query=_embed_query, mode=mode, rerank=rerank, decay=decay,
                                      stopwords=stopwords, fusion=fusion, alpha=alpha, rerank_alias=rerank_alias)


def search_scored(db, query: str, limit: int = 5, mode: str = "hybrid", rerank: bool | None = None,
                  decay: float | None = None, stopwords: bool | None = None, fusion: str | None = None,
                  alpha: float | None = None, rerank_alias: str | None = None) -> tuple[list[sqlite3.Row], float | None]:
    """(rows, top_score) from search_result. top_score is SearchResult.top_score: the rerank score if the rerank ran, else the fused score, else FTS5's negated bm25 in fts mode. None when nothing came back."""
    found = search_result(db, query, limit, mode, rerank=rerank, decay=decay, stopwords=stopwords, fusion=fusion,
                          alpha=alpha, rerank_alias=rerank_alias)
    return found.rows, found.top_score


def search(db, query: str, limit: int = 5, mode: str = "hybrid", **knobs) -> list[sqlite3.Row]:
    """Rows only. See search_result for the knobs."""
    return search_scored(db, query, limit, mode, **knobs)[0]


def _base_block(block_id: str | None) -> str:
    """Strip the `-pN` suffix added when a section is split into sub-chunks."""
    return re.sub(r"-p\d+$", "", block_id or "")


def load_cases() -> tuple[list[dict], list[list[str]]]:
    """The golden cases and the top-level `families:` lists (slugs that answer the same question equally well). A file without `families:` gives an empty list."""
    import yaml

    data = yaml.safe_load(GOLDEN.read_text())
    return data["cases"], [list(f) for f in (data.get("families") or [])]


def accept_set(case: dict, families: list[list[str]] | None = None) -> set[str]:
    """Every slug that counts as a hit: the target, its expect_any list, and any family that shares a member with them."""
    acc = set(case.get("expect_any") or [])
    if case.get("expect_slug"):
        acc.add(case["expect_slug"])
    for fam in families or ():
        if acc & set(fam):
            acc |= set(fam)
    return acc


def is_abstain(case: dict) -> bool:
    return bool(case.get("expect_none")) or case.get("bucket") == "abstain"


def case_id(case: dict) -> str:
    """A stable id for a golden case: its `id` field, else a hash of what it asks and what it accepts.

    Two cases with one query and different targets get different ids, so pairing two runs by case id never merges them."""
    if case.get("id"):
        return str(case["id"])
    key = [case.get("query"), case.get("expect_slug"), sorted(case.get("expect_any") or []), bool(case.get("expect_none"))]
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:12]


def golden_sha256(cases: list[dict], families: list[list[str]] | None = None) -> str:
    """sha256 over the canonical case list. Any change to what a case asks, accepts, or how it is scored or split changes it.

    Per case: qid, query, targets, expect_any, expect_none, bucket, the families that touch its targets, split, expect_block and hard. Order of cases, of families and inside a family does not change it."""
    canon = []
    for c in cases:
        base = accept_set(c)
        canon.append({
            "qid": case_id(c), "query": c.get("query"), "targets": [c["expect_slug"]] if c.get("expect_slug") else [],
            "expect_any": sorted(c.get("expect_any") or []), "expect_none": bool(c.get("expect_none")), "bucket": bucket_of(c),
            "families": sorted(sorted(f) for f in (families or []) if base & set(f)), "split": c.get("split"),
            "expect_block": c.get("expect_block"), "hard": bool(c.get("hard")),
        })
    canon.sort(key=lambda r: r["qid"])
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


def select_split(cases: list[dict], split: str) -> list[dict]:
    """The cases of one split. "all" returns every case. dev or test needs every case tagged (evals/retrieval/assign_splits.py)."""
    if split == "all":
        return list(cases)
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}, got {split!r}")
    untagged = [c["query"] for c in cases if c.get("split") not in ("dev", "test")]
    if untagged:
        raise ValueError(f"{len(untagged)} cases carry no split tag. Run evals/retrieval/assign_splits.py first.")
    return [c for c in cases if c["split"] == split]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k hits in n trials. Wald is wrong near 0 and 1 and at small n."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def recall_at(rs, d):
    return sum(1 for r in rs if r["rank"] and r["rank"] <= d) / len(rs) if rs else 0.0


def mrr(rs):
    return sum(1.0 / r["rank"] for r in rs if r["rank"]) / len(rs) if rs else 0.0


def bucket_of(case: dict) -> str:
    if case.get("expect_none"):
        return "abstain"
    return case.get("bucket") or ("hard" if case.get("hard") else "lookup")


def _answerable(rs: list[dict]) -> list[dict]:
    return [r for r in rs if not r.get("abstain")]


def metrics(rs: list[dict]) -> dict:
    """n, distinct targets, recall at each depth with a Wilson interval, MRR. Abstention rows have no target and are left out."""
    rs = _answerable(rs)
    out = {"n": len(rs), "targets": len({r["expect_slug"] for r in rs})}
    for d in DEPTHS:
        k = sum(1 for r in rs if r["rank"] and r["rank"] <= d)
        lo, hi = wilson(k, len(rs))
        out[f"recall@{d}"] = round(k / len(rs), 4) if rs else 0.0
        out[f"recall@{d}_ci"] = [round(lo, 3), round(hi, 3)]
    out["mrr"] = round(mrr(rs), 4)
    return out


def ci90(values: list[float]) -> tuple[float, float]:
    """The 5th and 95th percentile of bootstrap values, by the index rule every gate here uses."""
    v = sorted(values)
    return v[int(0.05 * len(v))], v[int(0.95 * len(v))]


def _top(r: dict) -> float:
    """A row's top score for the AUROC. A query that returned nothing ranks below every query that returned something."""
    return float("-inf") if r.get("top_score") is None else r["top_score"]


def auroc_unusable(rows: list[dict], score_kind: str | None = None) -> str | None:
    """Why the top-1 scores of these rows cannot be ranked against each other, or None when they can.

    FTS5's bm25 is not normalised across queries, so a raw bm25 from one query says nothing about another. A run where the rerank fell back on some queries mixes reranker logits with fused scores. score_kind overrides the per-row field for files written before rows carried it."""
    kinds = {score_kind} if score_kind else {r.get("score_kind") for r in rows if r.get("top_score") is not None}
    if "bm25" in kinds:
        return "fts mode: the top score is raw bm25, which is not comparable across queries"
    kinds.discard(None)
    if len(kinds) > 1:
        return f"mixed score kinds {sorted(kinds)}: the rerank fell back on some queries"
    return None


def _cluster_of(r: dict) -> str:
    """The bootstrap unit of a row. Rows that carry a cluster (evaluate labels them) use it. An abstention row is its own unit."""
    if r.get("cluster"):
        return r["cluster"]
    if r.get("abstain"):
        return "abstain:" + (r.get("qid") or r["query"])
    return r["expect_slug"]


def auroc_bootstrap(pos_rows: list[dict], neg_rows: list[dict], iterations: int = BOOTSTRAP_ITERATIONS,
                    seed: int = BOOTSTRAP_SEED) -> list[float] | None:
    """90 percent cluster-bootstrap interval of the AUROC. Whole clusters are resampled, and each abstention row is its own cluster. None with fewer than 3 clusters."""
    import random

    groups: dict[str, tuple[list[float], list[float]]] = {}
    for r in pos_rows:
        groups.setdefault(_cluster_of(r), ([], []))[0].append(_top(r))
    for r in neg_rows:
        groups.setdefault(_cluster_of(r), ([], []))[1].append(_top(r))
    keys = sorted(groups)
    if len(keys) < 3:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(iterations):
        pos, neg = [], []
        for _ in keys:
            p, n = groups[keys[rng.randrange(len(keys))]]
            pos += p
            neg += n
        a = gestalt_rank.auroc(pos, neg)
        if a is not None:
            vals.append(a)
    if not vals:
        return None
    lo, hi = ci90(vals)
    return [round(lo, 4), round(hi, 4)]


def abstain_metrics(rs: list[dict], score_kind: str | None = None, iterations: int = BOOTSTRAP_ITERATIONS) -> dict | None:
    """AUROC of the top-1 score for telling an answerable query from an abstention query, by rank sum, with a 90 percent cluster-bootstrap interval. None without abstention rows.

    The score is a ranking signal and never a threshold (^bm25-thresholds). The AUROC says how well it orders the two groups. 0.5 is chance. A query with no result scores minus infinity. The AUROC is None, with the reason in auroc_note, when the scores are not comparable across queries (see auroc_unusable)."""
    ab = [r for r in rs if r.get("abstain")]
    if not ab:
        return None
    ans = _answerable(rs)
    out = {"n": len(ab), "answerable_n": len(ans)}
    why = auroc_unusable(ab + ans, score_kind)
    if why:
        return {**out, "auroc": None, "auroc_ci90": None, "auroc_note": why}
    a = gestalt_rank.auroc([_top(r) for r in ans], [_top(r) for r in ab])
    return {**out, "auroc": round(a, 4) if a is not None else None,
            "auroc_ci90": auroc_bootstrap(ans, ab, iterations) if a is not None else None}


def bucket_metrics(rs: list[dict]) -> dict:
    out = {}
    for b in sorted({r["bucket"] for r in rs}):
        rows = [r for r in rs if r["bucket"] == b]
        out[b] = abstain_metrics(rows + _answerable(rs)) if b == "abstain" else metrics(rows)
    return out


def _score_kind(found: gestalt_rank.SearchResult, mode: str) -> str | None:
    if not found.rows:
        return None
    if found.rows[0]["id"] in found.rerank_scores:
        return "rerank"
    return "bm25" if mode == "fts" else "fused"


def query_timeout() -> float:
    """GESTALT_EVAL_QUERY_TIMEOUT seconds one search may take before the process aborts (default 300, 0 turns the guard off).
    The first search also loads the embedding model and the reranker, so raise the knob for a first run that downloads them."""
    raw = os.environ.get("GESTALT_EVAL_QUERY_TIMEOUT", "").strip()
    if not raw:
        return 300.0
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not value >= 0:
        raise SystemExit(f"GESTALT_EVAL_QUERY_TIMEOUT={raw!r} must be a number of seconds, 0 or more")
    return value


class StallWatchdog:
    """Abort the process when one unit of work (one search) runs past `timeout` seconds.

    Seen 2026-10-08: a golden-set run sat 18 minutes with one thread at 100 percent CPU, no GPU work and no output, where the same
    command in a fresh process took 270 seconds. The stuck thread is inside native code, so an exception cannot reach it. A watcher
    thread writes the message and ends the process with `exit_fn` (os._exit), and the driver's retry starts a fresh process."""

    def __init__(self, timeout: float, what: str = "search", prefix: str = "retrieval-eval", exit_fn=os._exit, poll: float | None = None):
        self.timeout, self.what, self.prefix, self.exit_fn = timeout, what, prefix, exit_fn
        self._poll = poll if poll is not None else max(0.05, min(1.0, timeout / 4)) if timeout else 1.0
        self._label = None
        self._t0 = 0.0
        self._stop = threading.Event()
        self._thread = None

    def arm(self, label: str) -> None:
        self._t0 = time.monotonic()
        self._label = label
        if self.timeout and self._thread is None:
            self._thread = threading.Thread(target=self._watch, name="stall-watchdog", daemon=True)
            self._thread.start()

    def disarm(self) -> None:
        self._label = None

    def close(self) -> None:
        self._label = None
        self._stop.set()

    def _watch(self) -> None:
        while not self._stop.wait(self._poll):
            label, elapsed = self._label, time.monotonic() - self._t0
            if label is not None and elapsed > self.timeout:
                print(f"{self.prefix}: ABORT {self.what} {label} ran {elapsed:.0f}s, past GESTALT_EVAL_QUERY_TIMEOUT={self.timeout:g}s. "
                      f"The process is stuck, so it exits with code {EXIT_QUERY_STALL}. Run the same command again for a fresh process.",
                      file=sys.stderr, flush=True)
                self.exit_fn(EXIT_QUERY_STALL)
                return


class Progress:
    """A stderr line every `every` items and every `seconds`: "retrieval-eval: 40/151 cases, 95 s elapsed, about 260 s left"."""

    def __init__(self, total: int, noun: str = "cases", prefix: str = "retrieval-eval", every: int = PROGRESS_EVERY_CASES,
                 seconds: float = PROGRESS_EVERY_SECONDS, clock=time.monotonic):
        self.total, self.noun, self.prefix, self.every, self.seconds, self.clock = total, noun, prefix, every, seconds, clock
        self.t0 = self.last = clock()

    def tick(self, done: int) -> None:
        now = self.clock()
        if done and (done % self.every == 0 or now - self.last >= self.seconds or done == self.total):
            elapsed = now - self.t0
            left = elapsed / done * (self.total - done)
            print(f"{self.prefix}: {done}/{self.total} {self.noun}, {elapsed:.0f} s elapsed, about {left:.0f} s left", file=sys.stderr, flush=True)
            self.last = now


def run_cases(db, cases: list[dict], mode: str, families: list[list[str]] | None = None, **knobs) -> list[dict]:
    max_depth = max(DEPTHS)
    results = []
    dog = StallWatchdog(query_timeout())
    progress = Progress(len(cases))
    try:
        return _run_cases(db, cases, mode, families, knobs, max_depth, results, dog, progress)
    finally:
        dog.close()


def _run_cases(db, cases, mode, families, knobs, max_depth, results, dog, progress) -> list[dict]:
    for n, case in enumerate(cases):
        progress.tick(n)
        dog.arm(f"case {case_id(case)}")
        found = search_result(db, case["query"], limit=SERVED_DEFAULT_LIMIT, mode=mode, **knobs)
        dog.disarm()
        hits = found.rows[:max_depth]
        slugs = [h["slug"] for h in hits]
        common = {
            "qid": case_id(case),
            "query": case["query"],
            "split": case.get("split"),
            "got": slugs[:3],
            "top_score": found.top_score,
            "score_kind": _score_kind(found, mode),
            "rerank_fallback": found.rerank_fallback,
            "fts_failed": found.fts_failed,
        }
        if is_abstain(case):
            # An abstention query has no right answer. Whatever comes back is wrong, so there is no rank and no block to score.
            # Its top score feeds the AUROC.
            results.append({**common, "expect_slug": None, "expect_block": None, "hard": False, "bucket": "abstain",
                            "rank": None, "block_hit": None, "abstain": True})
            continue
        want = case["expect_slug"]
        # expect_any lists every slug that answers the question equally well (X6, 2026-10-06). A family in golden.yaml adds its members.
        accept = accept_set(case, families)
        base_accept = accept_set(case)

        rank = next((i + 1 for i, s in enumerate(slugs) if s in accept), None)
        rank_without_families = next((i + 1 for i, s in enumerate(slugs) if s in base_accept), None)
        block_ok = None
        if case.get("expect_block"):
            # Oversized sections are split into `<block>-p2`, `-p3`... Comparing
            # raw block_ids scores a correct retrieval as a miss whenever the
            # answer lands in a later part of a split section, which biases this
            # metric downward precisely as entries grow. Compare the base id.
            # A block hit means the retrieved chunk CONTAINS the answer, which is
            # what the consumer actually reads. Two ways that is true:
            #   - the chunk is named by that anchor (block_id), or
            #   - the anchor is defined somewhere inside the chunk.
            # The second case is not a concession. Entries mark sub-facts with a
            # trailing inline `^anchor` mid-section, and the chunker derives block_id
            # only from headings and standalone anchor lines — so 12 of 48 anchored
            # cases had NO chunk whose block_id could ever match, and were scored as
            # misses even when retrieval returned the chunk holding the answer. That
            # was a measurement artifact, not a retrieval failure.
            want_block = case["expect_block"]
            block_ok = any(
                h["slug"] == want
                and (
                    _base_block(h["block_id"]) == want_block
                    or want_block in ((h["anchors"] or "").split(","))
                )
                for h in hits
            )

        results.append({**common, "expect_slug": want, "expect_block": case.get("expect_block"), "hard": bool(case.get("hard")),
                        "bucket": bucket_of(case), "rank": rank, "block_hit": block_ok,
                        "family_rescued": rank is not None and rank_without_families is None})
    progress.tick(len(cases))
    return results


# The shipped default configuration. A run whose config differs is a different experiment, so the gate refuses it.
DEFAULT_CONFIG = {"rerank": "off", "dedup": 1.0, "stopwords": False, "fusion": "rrf", "search_dir": "default"}
# Named configurations a bank can hold. default is the shipped off configuration. hub is the configuration the server
# runs on the hub: the same knobs with the rerank on, at the model and depth the environment names.
BANK_CONFIGS = {
    "default": {"rerank": False, "decay": 1.0, "stopwords": False, "fusion": "rrf"},
    "hub": {"rerank": True, "decay": 1.0, "stopwords": False, "fusion": "rrf"},
}


def families_hash(families: list[list[str]] | None) -> str:
    """A stable short hash of the family lists. Order inside a family and between families does not change it."""
    canon = sorted(sorted(f) for f in (families or []))
    return hashlib.sha1(json.dumps(canon).encode()).hexdigest()[:12]


def run_config(opts: dict, families: list[list[str]] | None = None) -> dict:
    """Every ranking knob this run used, plus the index it read and the families it accepted. Stored as summary["config"]."""
    on = opts["rerank"]
    return {
        "rerank": "on" if on else "off",
        "rerank_env": gestalt_rank.rerank_mode(),
        "families": families_hash(families),
        "rerank_model": (opts.get("rerank_alias") or gestalt_rank.rerank_alias()) if on else None,
        "rerank_depth": gestalt_rank.rerank_depth() if on else None,
        "rerank_maxchars": gestalt_rank.rerank_maxchars() if on else None,
        "dedup": opts["decay"],
        "stopwords": opts["stopwords"],
        "fusion": opts["fusion"],
        "alpha": opts["alpha"] if opts["fusion"] == "convex" else None,
        "search_dir": "default" if DB_PATH == DEFAULT_DB_PATH else str(DB_PATH),
        "embed_profile": getattr(_ec, "PROFILE", None),
        "text_format": getattr(_ec, "TEXT_FORMAT", None),
    }


def config_diff(cur: dict | None, banked: dict | None) -> list[str]:
    """Knob names where cur differs from the banked config. A bank with no config was taken at DEFAULT_CONFIG."""
    banked_in = banked
    cur, banked = cur or {}, {**DEFAULT_CONFIG, **(banked or {})}
    keys = ["rerank", "dedup", "stopwords", "fusion", "search_dir"]
    if "families" in (banked_in or {}):  # a bank taken before families were recorded has nothing to compare
        keys.append("families")
    if cur.get("rerank") == "on" or banked.get("rerank") == "on":
        keys += ["rerank_model", "rerank_depth", "rerank_maxchars"]
    if cur.get("fusion") == "convex" or banked.get("fusion") == "convex":
        keys.append("alpha")
    return [k for k in keys if cur.get(k, DEFAULT_CONFIG.get(k)) != banked.get(k)]


def resolve_opts(rerank=None, decay=None, stopwords=None, fusion=None, alpha=None, rerank_alias=None) -> dict:
    """Fill every knob left as None from the environment, the way the server reads them. rerank None is gestalt_rank.rerank_for, the server's rule."""
    return {
        "rerank": (gestalt_rank.rerank_for() is not None) if rerank is None else rerank,
        "decay": gestalt_rank.slug_decay() if decay is None else decay,
        "stopwords": gestalt_rank.stopwords_on() if stopwords is None else stopwords,
        "fusion": fusion or gestalt_rank.fusion_mode(),
        "alpha": gestalt_rank.fusion_alpha() if alpha is None else alpha,
        "rerank_alias": rerank_alias,
    }


def label_clusters(results: list[dict], cases: list[dict], families: list[list[str]]) -> dict:
    """Write each row's bootstrap cluster (the connected component of accepted slugs, assign_splits.case_clusters) and return the population numbers the summary prints."""
    import assign_splits

    clusters = assign_splits.case_clusters(cases, families)
    for r in results:
        r["cluster"] = clusters.get(r["qid"])
    ans = _answerable(results)
    n_clusters = len({r["cluster"] for r in ans})
    return {
        "answerable": len(ans),
        "targets": len({r["expect_slug"] for r in ans}),
        "clusters": n_clusters,
        "mean_queries_per_cluster": round(len(ans) / n_clusters, 2) if n_clusters else None,
    }


def evaluate(mode: str = "auto", split: str = "all", **knobs) -> dict:
    """Run every case through the shared search path and return the per-case rows, the bucket metrics and the abstention AUROC for one configuration."""
    if not DB_PATH.exists():
        sys.exit("Search index missing. Run: python3 tools/gestalt-index-builder.py")

    opts = resolve_opts(**knobs)
    all_cases, families = load_cases()
    try:
        cases = select_split(all_cases, split)
    except ValueError as e:
        sys.exit(str(e))
    db = None
    if mode in ("auto", "hybrid"):
        try:
            db = get_db(need_vec=True)
        except ImportError:
            db = None
        if db is None or not has_vectors(db):
            if mode == "hybrid":
                sys.exit("--mode hybrid needs sqlite_vec and a populated sections_vec. This index has none.")
            db = get_db(need_vec=False)
            mode = "fts"
        else:
            mode = "hybrid"
    else:
        db = get_db(need_vec=False)

    results = run_cases(db, cases, mode, families, **opts)
    # The per-prompt hook uses only the full-text leg, so it gets its own row (X6, 2026-10-06).
    fts_results = results if mode == "fts" else run_cases(db, cases, "fts", families, **opts)
    population = label_clusters(results, cases, families)
    if fts_results is not results:
        label_clusters(fts_results, cases, families)

    answerable = _answerable(results)
    hard = [r for r in answerable if r["hard"]]
    easy = [r for r in answerable if not r["hard"]]
    block_cases = [r for r in answerable if r["expect_block"]]

    summary = {
        "mode": mode,
        "embed": {"model": _ec.MODEL_NAME, "revision": getattr(_ec, "MODEL_REVISION", None)},
        "config": run_config(opts, families),
        "split": split,
        "golden_sha256": golden_sha256(all_cases, families),
        "n_cases": len(results),
        "n_answerable": len(answerable),
        "n_abstain": len(results) - len(answerable),
        "population": population,
        "rerank_fallbacks": sum(1 for r in results if r.get("rerank_fallback")),
        "recall": {f"@{d}": round(recall_at(answerable, d), 4) for d in DEPTHS},
        "mrr": round(mrr(answerable), 4),
        "hard": {
            "n": len(hard),
            "recall@3": round(recall_at(hard, 3), 4),
            "mrr": round(mrr(hard), 4),
        },
        "easy": {
            "n": len(easy),
            "recall@3": round(recall_at(easy, 3), 4),
            "mrr": round(mrr(easy), 4),
        },
        "block_precision": (
            round(sum(1 for r in block_cases if r["block_hit"]) / len(block_cases), 4)
            if block_cases
            else None
        ),
        "family_rescued": sum(1 for r in results if r.get("family_rescued")),
        "overall": metrics(results),
        "buckets": bucket_metrics(results),
        "fts_only": {
            "overall": metrics(fts_results),
            "buckets": bucket_metrics(fts_results),
            "family_rescued": sum(1 for r in fts_results if r.get("family_rescued")),
        },
    }
    if opts["decay"] < 1.0 and block_cases:
        # Cases whose block hit at decay 1 and no longer hits. The dedup gate reads this.
        undeduped = run_cases(db, cases, mode, families, **{**opts, "decay": 1.0})
        was = {r["qid"] for r in undeduped if r.get("block_hit")}
        summary["dedup_block_displaced"] = sum(1 for r in results if r["qid"] in was and r.get("expect_block") and not r["block_hit"])
    return {"summary": summary, "results": results, "fts_results": fts_results}


def _key_fn(*runs: dict):
    """Pair rows by case id. Runs saved before rows carried a qid pair by query text, as they always did."""
    rows = [r for run in runs for r in run.get("results", [])]
    if rows and all(r.get("qid") for r in rows):
        return lambda r: r["qid"]
    return lambda r: r["query"]


def paired_clusters(before: dict, after: dict, hit, rows=_answerable, cluster_key: str | None = None) -> tuple[dict, int]:
    """Clusters of paired (before hit, after hit) tuples, keyed by cluster, and the number of clusters dropped.

    A cluster is kept only when both runs hold exactly the same cases for it. A cluster whose cases differ between the runs (a changed golden set) is a different experiment and is dropped, and the count says how many. cluster_key names the row field that groups cases, else _cluster_of without the evaluate labels, which is the target slug."""
    key = _key_fn(before, after)
    group = (lambda r: r.get(cluster_key)) if cluster_key else (lambda r: r["expect_slug"])
    b = {key(r): r for r in rows(before.get("results", []))}
    a = {key(r): r for r in rows(after.get("results", []))}
    members_b: dict = {}
    members_a: dict = {}
    for k, r in b.items():
        members_b.setdefault(group(r), set()).add(k)
    for k, r in a.items():
        members_a.setdefault(group(r), set()).add(k)
    kept, dropped = {}, 0
    for c in sorted(set(members_b) | set(members_a), key=str):
        if members_b.get(c) != members_a.get(c):
            dropped += 1
            continue
        kept[c] = [(hit(b[k]), hit(a[k])) for k in sorted(members_b[c])]
    return kept, dropped


def significance(before: dict, after: dict) -> dict | None:
    """Exact two-sided sign test on queries that changed hit/miss state.

    N here is small enough that a raw percentage delta is not evidence. Two
    flips in the same direction give p=0.5 — a coin flip. Reporting this next
    to the delta is the difference between a measurement and a claim.
    """
    key = _key_fn(before, after)
    b = {key(r): bool(r["rank"] and r["rank"] <= 3) for r in _answerable(before.get("results", []))}
    a = {key(r): bool(r["rank"] and r["rank"] <= 3) for r in _answerable(after.get("results", []))}
    shared = set(b) & set(a)
    if not shared:
        return None
    gained = sum(1 for q in shared if a[q] and not b[q])
    lost = sum(1 for q in shared if b[q] and not a[q])
    n = gained + lost
    if n == 0:
        return {"gained": 0, "lost": 0, "p_value": 1.0, "verdict": "no change"}
    # Exact two-sided binomial p at q=0.5.
    from math import comb

    k = min(gained, lost)
    p = min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / (2**n))
    return {
        "gained": gained,
        "lost": lost,
        "p_value": round(p, 4),
        "verdict": "significant at 0.05" if p < 0.05 else "NOT significant — report as noise",
    }


def cluster_bootstrap(
    before: dict, after: dict, k: int = 3, iterations: int = BOOTSTRAP_ITERATIONS, seed: int = BOOTSTRAP_SEED,
    cluster_key: str | None = None,
) -> dict | None:
    """Paired bootstrap on recall@k, resampling by TARGET DOCUMENT, not by query.

    Replaces the sign test as the primary signal. Two reasons, both from the IR
    evaluation literature:

    1. Sign and Wilcoxon are the two tests that literature explicitly recommends
       AGAINST. Smucker, Allan & Carterette (CIKM 2007,
       dl.acm.org/doi/10.1145/1321440.1321528) found "the randomization, bootstrap,
       and t-tests all agreed with each other while the Wilcoxon and sign tests
       neither agreed with the other tests nor each other". Ihemelandu & Ekstrand
       (arxiv.org/abs/2305.02461) add that sign/Wilcoxon false-positive rates *rise*
       with sample size. A sign test also throws away magnitude, keeping only
       direction, which is most of the available information at this scale.

    2. The trials are CLUSTERED: several queries target one document, so queries
       sharing a target are correlated. The summary's `population` block prints the
       current case, target and cluster counts. Resampling queries independently
       understates variance and overstates significance. The standard remedy is to
       resample whole clusters as atomic units (block bootstrap) — here, a document
       together with every query aimed at it.

    Reported WITH the effect size, not as a bare pass/fail: Ihemelandu & Ekstrand
    recommend "both the p-value, and effect size be reported ... and used for decision
    making". A single gate cannot distinguish "no signal" from "underpowered", which
    is exactly the confusion this eval hit at 25 queries.

    Uses the same clusters as paired_drop (paired_clusters), and reports how many were dropped.
    """
    import random

    clusters, dropped = paired_clusters(before, after, lambda r: bool(r["rank"] and r["rank"] <= k), cluster_key=cluster_key)
    keys = list(clusters)
    if len(keys) < 3:
        return None

    def diff(draw: list) -> float:
        flat = [p for c in draw for p in clusters[c]]
        return sum(y for _, y in flat) / len(flat) - sum(x for x, _ in flat) / len(flat) if flat else 0.0

    observed = diff(keys)
    rng = random.Random(seed)
    n = len(keys)
    ge = 0
    for _ in range(iterations):
        draw = [keys[rng.randrange(n)] for _ in range(n)]
        # Centre the resampled difference on zero to test the null that the two runs
        # are the same; count how often |centred| reaches |observed|.
        if abs(diff(draw) - observed) >= abs(observed):
            ge += 1
    p = (ge + 1) / (iterations + 1)  # add-one, so p is never reported as exactly 0

    return {
        "metric": f"recall@{k}",
        "effect_size": round(observed, 4),
        "clusters": n,
        "dropped_clusters": dropped,
        "p_value": round(p, 4),
        "iterations": iterations,
        "verdict": (
            f"recall@{k} moved {observed:+.1%} (p={p:.3f}, "
            f"{n} document clusters, {dropped} dropped as not paired) — "
            + ("distinguishable from noise" if p < 0.05 else "NOT distinguishable from noise")
        ),
    }


def effective_n(results: list[dict]) -> int:
    """Distinct target documents. Multiple queries on one document are not
    independent trials, so this is the honest denominator for confidence."""
    return len({r["expect_slug"] for r in _answerable(results)})


def paired_drop(before: dict, after: dict, k: int = 3, iterations: int = BOOTSTRAP_ITERATIONS, seed: int = BOOTSTRAP_SEED,
                metric: str = "recall", cluster_key: str | None = None) -> dict | None:
    """Regression rule from RC-ai-research section 1 (X6, 2026-10-06).

    A drop is real only when BOTH hold: the net number of cases lost at recall@k reaches
    max(2, 3 percent of n), and the 95th percentile of the paired cluster-bootstrap
    difference (after minus before) is still below zero. The run is deterministic for a fixed
    index and fixed cases, so the noise is sampling noise across cases. A size test alone
    flags one unlucky flip. A bound alone flags one lost case in a large cluster.
    metric="block" runs the same rule on block hits instead of recall@k, over the cases that carry an expect_block. The dedup gate reads it.
    Uses the same clusters as cluster_bootstrap. Returns None when the two runs share fewer than 3 clusters.
    """
    import math
    import random

    def hit(r: dict) -> bool:
        return bool(r.get("block_hit")) if metric == "block" else bool(r["rank"] and r["rank"] <= k)

    def rows(rs: list[dict]) -> list[dict]:
        rs = _answerable(rs)
        return [r for r in rs if r.get("expect_block")] if metric == "block" else rs

    clusters, dropped = paired_clusters(before, after, hit, rows, cluster_key=cluster_key)
    if len(clusters) < 3:
        return None
    pairs = [p for c in clusters.values() for p in c]
    n = len(pairs)
    net_lost = sum(1 for x, y in pairs if x and not y) - sum(1 for x, y in pairs if y and not x)
    threshold = max(2, math.ceil(0.03 * n))
    keys = list(clusters)
    rng = random.Random(seed)
    diffs = []
    for _ in range(iterations):
        draw = [clusters[keys[rng.randrange(len(keys))]] for _ in keys]
        flat = [p for c in draw for p in c]
        diffs.append(sum(y for _, y in flat) / len(flat) - sum(x for x, _ in flat) / len(flat))
    lo, hi = ci90(diffs)
    return {
        "metric": "block precision" if metric == "block" else f"recall@{k}",
        "n_shared": n,
        "dropped_clusters": dropped,
        "net_lost": net_lost,
        "threshold": threshold,
        "ci90": [round(lo, 4), round(hi, 4)],
        "real_drop": net_lost >= threshold and hi < 0,
    }


def _bucket_line(name: str, m: dict) -> str:
    if "recall@5_ci" not in m:  # the abstain bucket carries n and AUROC, not recall
        a, ci = m.get("auroc"), m.get("auroc_ci90")
        tail = (f"{a:.3f}" + (f" [{ci[0]:.3f}-{ci[1]:.3f}]" if ci else "")) if a is not None else f"n/a ({m.get('auroc_note', 'no score')})"
        return f"  {name:<10} n={m['n']:<3} AUROC of top-1 score, answerable vs abstain: " + tail
    lo, hi = m["recall@5_ci"]
    return (f"  {name:<10} n={m['n']:<3} targets={m['targets']:<3} r@1 {m['recall@1']:.1%}  r@3 {m['recall@3']:.1%}  "
            f"r@5 {m['recall@5']:.1%} [{lo:.0%}-{hi:.0%}]  MRR {m['mrr']:.3f}")


def report(out: dict) -> None:
    s = out["summary"]
    n_ab = s.get("n_abstain", 0)
    print(f"\nRetrieval eval — {s['n_cases'] - n_ab} golden queries"
          + (f" and {n_ab} abstention queries" if n_ab else "") + f", mode={s.get('mode', 'hybrid')}, split={s.get('split', 'all')}\n")
    cfg = s.get("config") or {}
    off = config_diff(cfg, None)
    if off:
        shown = ", ".join(f"{k}={cfg.get(k)}" for k in off)
        print(f"  config differs from the banked default: {shown}. Not gated against the default bank.\n")
    if s.get("mode") == "fts":
        print("  FTS-only run: no vectors in this index. These are NOT hybrid numbers and must not be banked.\n")
    if s.get("rerank_fallbacks"):
        print(f"  RERANK FELL BACK on {s['rerank_fallbacks']} queries: those rows are the fusion order, not the rerank.\n")
    for d in DEPTHS:
        print(f"  recall@{d}: {s['recall'][f'@{d}']:.1%}")
    print(f"  MRR:       {s['mrr']:.3f}")
    print(f"  block precision: "
          f"{s['block_precision']:.1%}" if s["block_precision"] is not None else "  block precision: n/a")
    pop = s.get("population")
    if pop:
        print(f"  population: {pop['answerable']} answerable cases over {pop['targets']} distinct targets in "
              f"{pop['clusters']} clusters, {pop['mean_queries_per_cluster']} queries per cluster "
              "(clustered trials are not independent)")
    else:
        print(f"  effective N: {effective_n(out['results'])} distinct target documents "
              f"across {s.get('n_answerable', s['n_cases'])} queries (clustered trials are not independent)")
    if s.get("family_rescued"):
        print(f"  family rescued: {s['family_rescued']} cases hit only because a slug family accepts a sibling entry")
    if "dedup_block_displaced" in s:
        print(f"  dedup_block_displaced: {s['dedup_block_displaced']} block cases hit at decay 1 and miss at decay {cfg.get('dedup')}")
    print(f"\n  easy (n={s['easy']['n']}): recall@3 {s['easy']['recall@3']:.1%}  MRR {s['easy']['mrr']:.3f}")
    print(f"  hard (n={s['hard']['n']}): recall@3 {s['hard']['recall@3']:.1%}  MRR {s['hard']['mrr']:.3f}")
    if "buckets" in s:
        print("\n  per bucket (r@5 carries a 95% Wilson interval, the AUROC a 90% cluster-bootstrap interval):")
        print(_bucket_line("ALL", s["overall"]))
        for name, m in s["buckets"].items():
            if m:
                print(_bucket_line(name, m))
        print("\n  FTS-only leg (what the per-prompt hook uses):")
        print(_bucket_line("ALL", s["fts_only"]["overall"]))
        for name, m in s["fts_only"]["buckets"].items():
            if m:
                print(_bucket_line(name, m))

    misses = [r for r in _answerable(out["results"]) if not r["rank"] or r["rank"] > 3]
    if misses:
        print(f"\n  MISSES (not in top 3) — {len(misses)}:")
        for m in misses:
            got = ", ".join(m["got"]) or "nothing"
            tag = " [hard]" if m["hard"] else ""
            print(f"    - {m['query'][:62]!r}{tag}")
            print(f"        want {m['expect_slug']}, got: {got}")

    block_misses = [r for r in _answerable(out["results"]) if r["expect_block"] and not r["block_hit"]]
    if block_misses:
        print(f"\n  BLOCK MISSES (right doc, wrong section) — {len(block_misses)}:")
        for m in block_misses:
            print(f"    - {m['query'][:62]!r} → wanted ^{m['expect_block']}")
    print()


# --- the bank ------------------------------------------------------------------------------------


def results_path(name: str) -> Path:
    """Per-case results of a banked configuration. default keeps the old file name, which the floor tests read."""
    return BASELINE.with_name("baseline-results.json" if name == "default" else f"baseline-results-{name}.json")


def bank_configs(bank: dict | None) -> dict[str, dict]:
    """The banked summaries by configuration name. A bank written before named configurations is the default configuration."""
    if not bank:
        return {}
    if "configs" in bank:
        return dict(bank["configs"])
    return {"default": bank}


def load_bank() -> dict[str, dict] | None:
    return bank_configs(json.loads(BASELINE.read_text())) if BASELINE.exists() else None


def not_comparable(out: dict, base: dict, prev_run: dict | None, *, golden_sha: str, n_golden: int, strict: bool = False) -> list[str]:
    """Every reason this run cannot be gated against this banked configuration. Empty means comparable. Each reason says what to do."""
    s = out["summary"]
    why = []
    if not base.get("golden_sha256"):
        why.append("the bank has no golden_sha256, so it may hold another case list. Re-bank it with --baseline bank")
    elif base["golden_sha256"] != golden_sha:
        why.append("golden.yaml changed since the bank (golden_sha256 differs). Re-bank after an intended edit")
    if base.get("n_cases") != n_golden:
        why.append(f"the bank holds {base.get('n_cases')} cases and golden.yaml holds {n_golden}. Re-bank with --baseline bank")
    if s.get("mode") != base.get("mode", "hybrid"):
        why.append(f"run mode {s.get('mode')} differs from the banked mode {base.get('mode', 'hybrid')}. Run where the index has vectors")
    if s.get("split", "all") != base.get("split", "all"):
        why.append(f"split {s.get('split', 'all')} differs from the banked split {base.get('split', 'all')}. Drop --split")
    diff = config_diff(s.get("config"), base.get("config"))
    if diff:
        shown = ", ".join(f"{k}={(s.get('config') or {}).get(k)}" for k in diff)
        why.append(f"config drift: {shown} differ from the bank. Unset the knob, or bank this configuration")
    if not strict and prev_run is None:
        why.append("the banked per-case results file is missing, and the paired rule needs it. Re-bank, or pass --strict")
    return why


def check_baseline(out: dict, base: dict, prev_run: dict | None, strict: bool = False, allow_rebank: bool = False, *,
                   golden_sha: str, n_golden: int) -> list[str]:
    """Return the failure lines. Empty means pass. Prints the interval it used.

    The gate fails closed. A run that cannot be compared with the bank is a failure, because a stray knob, a changed golden set or a missing file would otherwise read as a pass. allow_rebank prints what it skipped and returns no failure, for a deliberate re-bank."""
    s = out["summary"]
    why = not_comparable(out, base, prev_run, golden_sha=golden_sha, n_golden=n_golden, strict=strict)
    pds = {}
    if not why and not strict:
        for d in DEPTHS:
            pds[d] = paired_drop(prev_run, out, k=d)
            if pds[d] is None:
                why.append(f"recall@{d}: fewer than 3 clusters pair between the bank and this run. Re-bank")
    if why:
        for w in why:
            print(f"  not comparable: {w}.")
        if allow_rebank:
            print("  --allow-rebank is set: the gate is SKIPPED for a deliberate re-bank. Bank this configuration next.")
            return []
        return [f"not comparable: {w}" for w in why]
    regressed = []
    if strict:
        for d in DEPTHS:
            k = f"@{d}"
            if s["recall"][k] < base["recall"][k] - 1e-9:
                regressed.append(f"recall{k}: {base['recall'][k]:.1%} → {s['recall'][k]:.1%}")
        if s["mrr"] < base["mrr"] - 1e-9:
            regressed.append(f"MRR: {base['mrr']:.3f} → {s['mrr']:.3f}")
        return regressed
    for d, pd in pds.items():
        print(f"  recall@{d}: net {-pd['net_lost']:+d} cases of {pd['n_shared']} shared, "
              f"bootstrap 90% interval of the change [{pd['ci90'][0]:+.3f}, {pd['ci90'][1]:+.3f}], "
              f"fail needs net loss >= {pd['threshold']} and upper bound < 0")
        if pd["real_drop"]:
            regressed.append(f"recall@{d}: lost {pd['net_lost']} net cases, upper bound {pd['ci90'][1]:+.3f} < 0")
    return regressed


def refuse_fallbacks(summary: dict, label: str) -> None:
    """Exit EXIT_RERANK_FALLBACK when a run asked for the rerank and it fell back on any query. That run is the fusion order under another name."""
    if (summary.get("config") or {}).get("rerank") != "on":
        return
    n = summary.get("rerank_fallbacks")
    if n is None:
        print(f"  note: {label} predates the fallback count, so its rerank cannot be confirmed.")
        return
    if n > 0:
        print(f"Refusing {label}: the rerank was requested and fell back to the fusion order on {n} queries. "
              "Those rows measure fusion, not the rerank. Fix the reranker (check the model loads and CUDA is free) and run again.")
        sys.exit(EXIT_RERANK_FALLBACK)


def compare_runs(saved_path: str, out: dict) -> None:
    """Paired cluster bootstrap and paired drop of this run against a saved --json run, at each depth, and on block hits."""
    saved = json.loads(Path(saved_path).read_text())
    refuse_fallbacks(saved.get("summary") or {}, saved_path)
    if saved.get("summary", {}).get("mode") == out["summary"]["mode"]:
        runs = (saved, out)
        which = f"{out['summary']['mode']} results"
    else:
        runs = ({"results": saved.get("fts_results") or []}, {"results": out["fts_results"]})
        which = "FTS-only results (the saved run was taken in another mode)"
    print(f"\n  compare vs {saved_path} on {which}:")
    sc, cc = (saved.get("summary") or {}).get("config"), out["summary"].get("config")
    print(f"    saved config {json.dumps(sc)}\n    this config  {json.dumps(cc)}")
    gs, gc = (saved.get("summary") or {}).get("golden_sha256"), out["summary"].get("golden_sha256")
    if gs != gc:
        print("    the two runs used different golden sets: only clusters whose cases match in both are compared.")
    for d in DEPTHS:
        boot = cluster_bootstrap(runs[0], runs[1], k=d)
        pd = paired_drop(runs[0], runs[1], k=d)
        if boot is None or pd is None:
            print(f"    recall@{d}: not comparable (golden set changed, or <3 shared clusters)")
            continue
        print(f"    {boot['verdict']}")
        print(f"      net {-pd['net_lost']:+d} of {pd['n_shared']} shared, 90% interval [{pd['ci90'][0]:+.3f}, {pd['ci90'][1]:+.3f}], "
              f"real_drop={pd['real_drop']}, {pd['dropped_clusters']} clusters dropped as not paired")
    pb = paired_drop(runs[0], runs[1], metric="block")
    if pb is None:
        print("    block precision: not comparable")
    else:
        print(f"    block precision: net {-pb['net_lost']:+d} of {pb['n_shared']} block cases, "
              f"fail needs net loss >= {pb['threshold']} and upper bound < 0, real_drop={pb['real_drop']}")


def _reranker_unavailable(alias: str | None) -> str | None:
    """Why the hub configuration cannot run here, or None when it can. It needs CUDA and a reranker that loads."""
    if not gestalt_rank.cuda_available():
        return "no CUDA device: the hub configuration reranks in fp16 on a GPU"
    try:
        gestalt_rank.get_reranker(gestalt_rank.resolve_alias(alias))
    except Exception as e:
        return f"the reranker does not load ({type(e).__name__}: {str(e)[:120]})"
    return None


def bank_opts(name: str, cli: dict) -> dict:
    """The knobs a configuration is banked at. Knobs given on the command line must agree, or banking refuses."""
    opts = dict(BANK_CONFIGS[name])
    for k, v in cli.items():
        if v is not None and k in opts and v != opts[k]:
            raise ValueError(f"--{k} conflicts with configuration {name}, which banks {k}={opts[k]}")
    return opts


def check_opts(banked: dict, cli: dict) -> dict:
    """The knobs that reproduce a banked configuration, overridden by any knob given on the command line (which then reads as drift)."""
    cfg = {**DEFAULT_CONFIG, **(banked or {})}
    opts = {"rerank": cfg["rerank"] == "on", "decay": cfg["dedup"], "stopwords": cfg["stopwords"], "fusion": cfg["fusion"],
            "alpha": cfg.get("alpha"), "rerank_alias": cfg.get("rerank_model")}
    opts.update({k: v for k, v in cli.items() if v is not None})
    return opts


def write_json(path: Path | str | None, out: dict) -> None:
    if path:
        atomic_write_text(path, json.dumps(out, indent=2))
        print(f"Wrote {path}")


def do_bank(args, cli: dict) -> None:
    name = args.config_name or "default"
    try:
        opts = bank_opts(name, cli)
    except ValueError as e:
        print(f"Refusing to bank: {e}.")
        sys.exit(1)
    if name == "hub":
        why = _reranker_unavailable(None)
        if why:
            print(f"Refusing to bank the hub configuration: {why}. Bank it on the hub.")
            sys.exit(1)
    if args.split != "all":
        print("Refusing to bank a split. The bank holds every case. Drop --split.")
        sys.exit(1)
    out = evaluate(args.mode, split="all", **opts)
    report(out)
    write_json(args.json, out)
    s = out["summary"]
    refuse_fallbacks(s, f"banking configuration {name}")
    if s["mode"] != "hybrid":
        print("Refusing to bank an FTS-only run as the baseline. Run this on the hub, where the index has vectors.")
        sys.exit(1)
    cfg = s.get("config") or {}
    if name == "default":
        diff = config_diff(cfg, None)
        if "rerank" in diff:
            print(f"Refusing to bank configuration default with the rerank on (GESTALT_RERANK={cfg.get('rerank_env')}). "
                  "The default configuration is rerank off. Bank the rerank as --config-name hub.")
            sys.exit(1)
        if diff:
            print(f"Refusing to bank a run with ranking knobs on or a side index ({', '.join(diff)}). Bank at the default config.")
            sys.exit(1)
    elif cfg.get("rerank") != "on" or config_diff({**cfg, "rerank": "off"}, None) != []:
        print("Refusing to bank configuration hub: it is the default configuration with the rerank on, and this run is not.")
        sys.exit(1)
    cases, families = load_cases()
    entry = {**s, "config_name": name, "golden_sha256": golden_sha256(cases, families)}
    bank = load_bank() or {}
    bank[name] = entry
    atomic_write_text(BASELINE, json.dumps({"schema": 2, "configs": bank}, indent=2))
    atomic_write_text(results_path(name), json.dumps(out, indent=2))
    print(f"Configuration {name} banked to {BASELINE} and {results_path(name).name}")


def do_check(args, cli: dict) -> int:
    """Gate every banked configuration this machine can reproduce. Returns the exit code."""
    bank = load_bank()
    if not bank:
        # Exit non-zero: a CI step gating on the exit code alone would otherwise
        # read "no baseline" as "no regression". Matches run_evals.baseline_check.
        print("No baseline yet. Bank one first: --baseline bank --config-name default, on a machine whose index has vectors.")
        return 1
    cases, families = load_cases()
    g, n_golden = golden_sha256(cases, families), len(cases)
    names = [args.config_name] if args.config_name else sorted(bank)
    failures: list[str] = []
    gated = 0  # configurations actually evaluated, so a run that skipped them all cannot pass
    for name in names:
        print(f"\n== configuration {name} ==")
        base = bank.get(name)
        if base is None:
            failures.append(f"{name}: no banked configuration of that name (banked: {', '.join(sorted(bank))})")
            continue
        opts = check_opts(base.get("config"), cli)
        if opts["rerank"]:
            why = _reranker_unavailable(opts.get("rerank_alias"))
            if why:
                if args.allow_missing_hub_config:
                    print(f"  SKIPPED: {why}. --allow-missing-hub-config is set.")
                    continue
                failures.append(f"{name}: cannot run here: {why}. Run the check on the hub, or pass --allow-missing-hub-config")
                continue
        out = evaluate(args.mode, split=args.split, **opts)
        gated += 1
        report(out)
        if args.json:
            p = Path(args.json)
            write_json(p if len(names) == 1 else p.with_name(f"{p.stem}-{name}{p.suffix}"), out)
        refuse_fallbacks(out["summary"], f"the check of configuration {name}")
        prev = results_path(name)
        prev_run = json.loads(prev.read_text()) if prev.exists() else None
        if prev_run and out["summary"]["mode"] == base.get("mode", "hybrid"):
            sig = significance(prev_run, out)
            if sig:
                print(f"\n  flips vs the banked run: +{sig['gained']} / -{sig['lost']} "
                      f"(sign-test p={sig['p_value']}, secondary — see cluster_bootstrap)")
            boot = cluster_bootstrap(prev_run, out)
            if boot:
                print(f"  {boot['verdict']}")
        lines = check_baseline(out, base, prev_run, strict=args.strict, allow_rebank=args.allow_rebank,
                               golden_sha=g, n_golden=n_golden)
        failures += [f"{name}: {line}" for line in lines]
    if failures:
        print("REGRESSION OR NOT COMPARABLE:")
        for f in failures:
            print(f"  - {f}")
        return 1
    if gated == 0:
        print("NOTHING GATED: every configuration was skipped, so no regression check ran. Run the check on the hub.")
        return 1
    print(f"No retrieval regression ({gated} of {len(names)} configurations gated).")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="write full results as JSON")
    ap.add_argument("--baseline", choices=["save", "bank", "check"], help="save is the old spelling of bank --config-name default")
    ap.add_argument("--config-name", choices=sorted(BANK_CONFIGS), default=None,
                    help="with --baseline bank: the configuration to bank (default: default). With check: gate only this one (default: every banked one)")
    ap.add_argument("--split", choices=SPLITS, default="all", help="dev or test cases only (tags from assign_splits.py). Default all")
    ap.add_argument("--mode", choices=["auto", "hybrid", "fts"], default="auto",
                    help="auto uses the hybrid path when the index has vectors, else the FTS leg only")
    ap.add_argument("--strict", action="store_true",
                    help="old rule: fail on any drop in a headline number (default: net loss AND bootstrap bound)")
    ap.add_argument("--rerank", choices=["off", "on"], default=None,
                    help="cross-encoder rerank after fusion (hybrid mode only). Default: as the server resolves GESTALT_RERANK")
    ap.add_argument("--dedup", type=float, metavar="DECAY", default=None, help="slug decay: 1 is off, 0.5 demotes a repeated slug, 0 is strict")
    ap.add_argument("--stopwords", choices=["on", "off"], default=None, help="drop closed-class words from the FTS query")
    ap.add_argument("--fusion", choices=["rrf", "convex"], default=None)
    ap.add_argument("--alpha", type=float, default=None, help="lexical weight under --fusion convex")
    ap.add_argument("--compare", metavar="PATH", help="paired bootstrap against a run saved with --json")
    ap.add_argument("--compare-files", nargs=2, metavar=("BEFORE", "AFTER"), help="paired bootstrap between two runs already saved with --json. Nothing is evaluated and no model loads")
    ap.add_argument("--allow-rebank", action="store_true",
                    help="with --baseline check: print what is not comparable and skip the gate, for a deliberate re-bank run")
    ap.add_argument("--allow-config-drift", action="store_true", dest="allow_rebank", help="the old name of --allow-rebank")
    ap.add_argument("--allow-missing-hub-config", action="store_true",
                    help="with --baseline check: skip, not fail, a rerank configuration this machine cannot run (no CUDA or no reranker)")
    args = ap.parse_args()
    if args.compare_files:
        before, after = args.compare_files
        after_run = json.loads(Path(after).read_text())
        refuse_fallbacks(after_run.get("summary") or {}, after)
        compare_runs(before, after_run)
        return
    if args.dedup is not None and not 0.0 <= args.dedup <= 1.0:
        ap.error("--dedup must be in [0, 1]")
    if args.alpha is not None and not 0.0 <= args.alpha <= 1.0:
        ap.error("--alpha must be in [0, 1]")
    if args.config_name and not args.baseline:
        ap.error("--config-name needs --baseline bank or --baseline check")

    cli = {
        "rerank": None if args.rerank is None else args.rerank == "on",
        "decay": args.dedup,
        "stopwords": None if args.stopwords is None else args.stopwords == "on",
        "fusion": args.fusion,
        "alpha": args.alpha,
    }
    if args.baseline in ("save", "bank"):
        if args.baseline == "save" and args.config_name not in (None, "default"):
            ap.error("--baseline save banks the default configuration. Use --baseline bank --config-name NAME")
        do_bank(args, cli)
        return
    if args.baseline == "check":
        sys.exit(do_check(args, cli))

    out = evaluate(args.mode, split=args.split, **cli)
    if out["summary"]["config"]["rerank"] == "on" and out["summary"]["mode"] != "hybrid":
        print("  note: --rerank is ignored in fts mode, as on the server (the rerank only runs on the hybrid path).")
    report(out)

    write_json(args.json, out)

    if args.compare:
        refuse_fallbacks(out["summary"], "this run")
        compare_runs(args.compare, out)


if __name__ == "__main__":
    main()
