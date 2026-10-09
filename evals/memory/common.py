"""Shared pieces for the LongMemEval-S and LoCoMo-10 retrieval harnesses.

These harnesses score retrieval only. They ask whether the evidence turn or session lands in the top k. They never ask a model to answer. Retrieval recall is not QA accuracy, so no number from here may sit beside a vendor QA number.

The index is gestalt's own. `beir_bench.build_db` builds it (FTS5 porter, sqlite-vec). It is imported when the first index is built, not at module import. `run_retrieval_evals.search_scored` runs the two legs and the fusion. Text is cut by the index builder's own splitter (2,000 chars, 150 overlap). Every chunk carries a date and speaker header, the way gestalt puts a title and heading on a section.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import platform
import re
import sqlite3
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "tools"))

import bench_stats as stats
import gestalt_embed_config as ec
import run_retrieval_evals as harness

SEED = stats.SEED
BOOTSTRAP = stats.BOOTSTRAP
KS = (1, 3, 5, 10)
SYSTEMS = ("bm25", "dense", "hybrid", "hybrid_rerank")
CHUNKING = "tools/gestalt-index-builder.py _split_text, 2000 chars, 150 overlap, date and speaker header on every chunk"
SCOPE_FORMAT = 1
CACHE_DIR = Path(os.environ.get("GESTALT_BENCH_CACHE", Path.home() / ".cache" / "gestalt-bench"))

# Pinned sources. A cached file whose sha256 differs from the pin is an error.
DATASETS = {
    "longmemeval_s": {
        "file": "longmemeval_s_cleaned.json",
        "url": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/98d7416c24c778c2fee6e6f3006e7a073259d48f/longmemeval_s_cleaned.json",
        "revision": "huggingface xiaowu0162/longmemeval-cleaned @ 98d7416c24c778c2fee6e6f3006e7a073259d48f (the cleaned 2025-09 release the LongMemEval README names)",
        "licence": "MIT (github.com/xiaowu0162/LongMemEval and the Hugging Face dataset card)",
        "sha256": "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442",
    },
    "locomo10": {
        "file": "locomo10.json",
        "url": "https://raw.githubusercontent.com/snap-research/locomo/3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376/data/locomo10.json",
        "revision": "github snap-research/locomo @ 3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376",
        "licence": "CC BY-NC 4.0 (LICENSE.txt in the snap-research/locomo repository). Non-commercial use only.",
        "sha256": "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4",
    },
}

HONESTY = (
    "Retrieval recall is not QA accuracy. Never compare these numbers to a vendor QA number. "
    "Turn level and session level are reported separately. Every question type or category is reported. "
    "The dataset version and sha256 are in this file."
)


# ---------------------------------------------------------------- dataset download

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def atomic_write_text(path: Path | str, text: str) -> None:
    """Write `text` so a reader sees the old file or the whole new one, never half. Temp file in the same directory, fsync, os.replace, fsync of the directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


DOWNLOAD_TIMEOUT_S = 60  # per read, not per file; a stalled mirror fails instead of hanging the shard for ever


def fetch_dataset(name: str, cache_dir: Path | None = None, download: bool = True) -> dict:
    """Return {path, sha256, pinned_sha256, verified, licence, url, revision, bytes}. Downloads once into the cache, never into the repo."""
    spec = DATASETS[name]
    cache = Path(cache_dir) if cache_dir else CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / spec["file"]
    if not path.exists():
        if not download:
            raise SystemExit(f"{path} is missing and downloads are off")
        fresh = True
        tmp = path.with_suffix(".part")
        with urllib.request.urlopen(spec["url"], timeout=DOWNLOAD_TIMEOUT_S) as resp, open(tmp, "wb") as out:
            out.writelines(iter(lambda: resp.read(1 << 20), b""))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    else:
        fresh = False
    digest = sha256_file(path)
    if digest != spec["sha256"]:
        if fresh:
            raise SystemExit(f"{path}: the download's sha256 {digest} does not match the pin {spec['sha256']}. The upstream file changed or the transfer was corrupted. Refusing to score a different file.")
        aside = path.with_name(f"{path.name}.bad-{digest[:12]}")
        os.replace(path, aside)
        raise SystemExit(f"{path}: cached sha256 {digest} does not match the pin {spec['sha256']}. Moved it to {aside.name}. Run again to download a fresh copy, or pass --no-download to stop here.")
    return {"name": name, "path": str(path), "sha256": digest, "pinned_sha256": spec["sha256"], "verified": True,
            "licence": spec["licence"], "url": spec["url"], "revision": spec["revision"], "bytes": path.stat().st_size}


def dataset_record(path: Path, name: str) -> dict:
    """The same record for a file given by path (a fixture or a local copy). `verified` says whether it matches the pin."""
    spec = DATASETS[name]
    digest = sha256_file(path)
    return {"name": name, "path": str(path), "sha256": digest, "pinned_sha256": spec["sha256"], "verified": digest == spec["sha256"],
            "licence": spec["licence"], "url": spec["url"], "revision": spec["revision"], "bytes": path.stat().st_size}


# ---------------------------------------------------------------- units

@dataclass
class Turn:
    tid: str
    speaker: str
    text: str


@dataclass
class Session:
    sid: str
    date: str
    turns: list[Turn] = field(default_factory=list)


@dataclass
class Unit:
    uid: str
    session: str
    header: str
    body: str


def session_units(sessions: list[Session]) -> list[Unit]:
    """One unit per session. The body is the turns in order, one 'speaker: text' paragraph each. The header holds the date only.

    The session id never enters the text. LongMemEval names its evidence sessions 'answer_...', so an id in the text would leak the answer."""
    return [Unit(s.sid, s.sid, f"{s.date}", "\n\n".join(f"{t.speaker}: {t.text}" for t in s.turns)) for s in sessions if s.turns]


def turn_units(sessions: list[Session]) -> list[Unit]:
    """One unit per turn. The header holds the date and the speaker."""
    return [Unit(t.tid, s.sid, f"{s.date} — {t.speaker}", t.text) for s in sessions for t in s.turns if t.text.strip()]


def build_units(sessions: list[Session], granularity: str) -> list[Unit]:
    if granularity == "session":
        return session_units(sessions)
    if granularity == "turn":
        return turn_units(sessions)
    raise ValueError(granularity)


def chunk_units(units: list[Unit]) -> tuple[list[int], list[str]]:
    """Cut each unit into chunks of at most MAX_CHUNK_CHARS. Returns (unit index per chunk, chunk text). Every chunk repeats the header."""
    owner: list[int] = []
    texts: list[str] = []
    for i, u in enumerate(units):
        for part in stats.split_text(u.body):
            if part.strip():
                owner.append(i)
                texts.append(f"{u.header}\n\n{part}")
    return owner, texts


# ---------------------------------------------------------------- models

class CachedEncoder:
    """Wraps an encoder so one text is embedded once per scope, and one query once per run.

    A query arrives as a single string and a document batch as a list. Query vectors are few and live for the whole run, because every system and level asks the same queries. Document vectors live for one scope (a haystack or a conversation) and `begin_scope` drops them. Measured on the pinned LongMemEval-S file, no chunk text repeats across haystacks, at session or turn granularity (every chunk carries its session date), so keeping document vectors longer would only hold memory."""

    def __init__(self, model):
        self.model = model
        self.queries: dict[str, object] = {}
        self.docs: dict[str, object] = {}
        self.device = getattr(model, "device", "cpu")

    def begin_scope(self) -> None:
        self.docs.clear()

    def encode(self, texts, batch_size: int = 64, **kw):
        import numpy as np

        single = isinstance(texts, str)
        items = [texts] if single else list(texts)
        cache = self.queries if single else self.docs
        todo = list(dict.fromkeys(t for t in items if t not in cache))
        if todo:
            vecs = self.model.encode(todo, batch_size=batch_size, **kw)
            for t, v in zip(todo, vecs):
                cache[t] = v
        out = np.stack([cache[t] for t in items])
        return out[0] if single else out


class NullEncoder:
    """For FTS-only runs. It returns one constant unit vector. The dense leg is never read."""
    device = "none"

    def encode(self, texts, **kw):
        import numpy as np

        n = 1 if isinstance(texts, str) else len(texts)
        v = np.full((n, ec.FULL_DIM), 1.0 / math.sqrt(ec.FULL_DIM), dtype="float32")
        return v[0] if isinstance(texts, str) else v


def _install_model(model) -> None:
    """Make the retrieval harness embed queries through `model`, so queries share the cache and the model of the index."""
    harness.set_model(model)


# ---------------------------------------------------------------- index

class MemoryIndex:
    """One in-memory gestalt index over the units of one haystack or one conversation."""

    def __init__(self, units: list[Unit], model, batch_size: int = 64):
        self.units = units
        self.owner, texts = chunk_units(units)
        import beir_bench

        self.db = beir_bench.build_db([str(i) for i in range(len(texts))], texts, model, batch_size)
        self.model = model
        self.n_chunks = len(texts)
        self.rerank_fallbacks = 0  # queries where the reranker failed and the row is just the hybrid order

    def _units_of(self, chunk_ids: list[int]) -> list[str]:
        seen, out = set(), []
        for c in chunk_ids:
            u = self.owner[c]
            if u not in seen:
                seen.add(u)
                out.append(self.units[u].uid)
        return out

    def dense_scored(self, query: str, k: int) -> tuple[list[int], float | None]:
        qv = ec.postprocess(self.model.encode(ec.QUERY_PREFIX + query, convert_to_numpy=True))
        rows = self.db.execute("SELECT id, distance FROM sections_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance", (qv.astype("float32").tobytes(), k)).fetchall()
        return [r["id"] for r in rows], (-rows[0]["distance"] if rows else None)

    def rank(self, query: str, system: str, depth: int, rerank: dict | None = None) -> tuple[list[str], float | None]:
        """Unit ids best first, and the top-1 score for the abstention test (None when there is no score on a comparable scale). `depth` is the chunk candidate pool.

        The base systems pass `rerank=False` to the retrieval harness, so the server's rerank rule never reaches them. The reranked system builds its pool with `rerank=False` as well and re-scores the head itself with the alias from its own config."""
        if system == "dense":
            ids, top = self.dense_scored(query, depth)
            return self._units_of(ids), top
        mode = "fts" if system == "bm25" else "hybrid"
        if system == "hybrid_rerank":
            if not rerank:
                raise ValueError("hybrid_rerank needs a rerank config")
            return self._rank_rerank(query, max(depth, rerank["depth"]), rerank)
        rows, top = harness.search_scored(self.db, query, limit=depth, mode=mode, rerank=False, decay=1.0)
        if system == "bm25":
            top = None  # raw bm25 scores have no comparable scale, and the golden runner refuses them as unusable for AUROC, so none is reported
        return self._units_of([r["id"] for r in rows]), top

    def _rank_rerank(self, query: str, depth: int, rerank: dict) -> tuple[list[str], float | None]:
        """The hybrid pool, its first `rerank depth` hits re-scored by gestalt_rank.rerank_rows, then the rest in fusion order.

        When the reranker returns no scores the order is the fusion order and the top score is None, because a fused score and a reranker score are on different scales."""
        import gestalt_rank

        rows, top = harness.search_scored(self.db, query, limit=depth, mode="hybrid", rerank=False, decay=1.0)
        head, tail = rows[: rerank["depth"]], rows[rerank["depth"]:]
        ids = [r["id"] for r in head]
        found = {r["id"]: dict(r) for r in self.db.execute(f"SELECT * FROM sections_meta WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()} if ids else {}
        ranked, scores, info = gestalt_rank.rerank_rows(query, [found[i] for i in ids], rerank["model"])
        if info.get("fallback") not in ("none", "too-few"):
            self.rerank_fallbacks += 1
        top = scores[0] if scores is not None else None
        return self._units_of([r["id"] for r in ranked] + [r["id"] for r in tail]), top


# ---------------------------------------------------------------- metrics

def score_ranked(ranked: list[str], gold: set[str]) -> dict:
    """recall@k (share of gold found), any@k (at least one found), all@k (every one found), MRR and nDCG@10. Binary relevance."""
    out: dict[str, float] = {}
    for k in KS:
        hit = len(gold & set(ranked[:k]))
        out[f"recall@{k}"] = hit / len(gold)
        out[f"any@{k}"] = float(hit > 0)
        out[f"all@{k}"] = float(hit == len(gold))
    first = next((i for i, u in enumerate(ranked, 1) if u in gold), None)
    out["mrr"] = 1.0 / first if first else 0.0
    out["ndcg@10"] = stats.ndcg_at_10(ranked, {g: 1 for g in gold})
    return out


CORE = ("recall@1", "recall@3", "recall@5", "recall@10", "mrr", "ndcg@10")


def _singletons(clusters: list | None, n: int) -> bool:
    """True when every item is its own cluster, so cluster resampling is plain resampling."""
    return clusters is None or len(set(clusters)) == n


def bootstrap_ci(values: list[float], seed: int = SEED, n_boot: int = BOOTSTRAP, clusters: list | None = None) -> list[float]:
    """Seeded 95% percentile bootstrap of the mean. Same seed and resample count as beir_bench. The generator is numpy's, so the draws differ from beir_bench's and the intervals agree only to about three decimals.

    With `clusters` (one label per value) whole clusters are resampled, and the mean is the mean of all values in the drawn clusters. Questions about one LoCoMo conversation share a history, so they are not independent draws. With no clusters, or one value per cluster, items are resampled one by one."""
    import numpy as np

    a = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    if _singletons(clusters, len(a)):
        means = np.concatenate([a[rng.integers(0, len(a), (min(1000, n_boot - s), len(a)))].mean(axis=1) for s in range(0, n_boot, 1000)])
    else:
        labels = list(dict.fromkeys(clusters))
        pos = {c: i for i, c in enumerate(labels)}
        sums = np.zeros(len(labels))
        counts = np.zeros(len(labels))
        for v, c in zip(a, clusters):
            sums[pos[c]] += v
            counts[pos[c]] += 1
        k = len(labels)
        parts = []
        for s in range(0, n_boot, 1000):
            idx = rng.integers(0, k, (min(1000, n_boot - s), k))
            parts.append(sums[idx].sum(axis=1) / counts[idx].sum(axis=1))
        means = np.concatenate(parts)
    means.sort()
    return [round(float(means[int(0.025 * n_boot)]), 4), round(float(means[int(0.975 * n_boot) - 1]), 4)]


def aggregate(rows: list[dict], with_ci: bool = True, exclude_from_all: frozenset | set | tuple = ()) -> dict:
    """rows: {group, scores, cluster?}. Returns {'all': ..., group: ...}. Each cell holds n, the mean of every metric, and a CI on the core metrics.

    Groups named in `exclude_from_all` get their own cell and stay out of the pooled `all` cell."""
    pooled = [r for r in rows if str(r["group"]) not in exclude_from_all]
    groups: dict[str, list[dict]] = {"all": pooled}
    for r in rows:
        groups.setdefault(str(r["group"]), []).append(r)
    out = {}
    for g, rs in groups.items():
        if not rs:
            continue
        cell: dict = {"n": len(rs)}
        clusters = [r["cluster"] for r in rs] if all("cluster" in r for r in rs) else None
        for m in rs[0]["scores"]:
            vals = [r["scores"][m] for r in rs]
            cell[m] = round(sum(vals) / len(vals), 4)
            if with_ci and m in CORE:
                cell[m + "_ci95"] = bootstrap_ci(vals, clusters=clusters)
        out[g] = cell
    return out


def cluster_permutation_p(a: list[float], b: list[float], clusters: list) -> dict:
    """Seeded paired sign-flip test that flips whole clusters. One sign per cluster, drawn with the seed and the permutation count of beir_bench.permutation_p.

    One value per cluster gives exactly beir_bench.permutation_p."""
    import random

    if _singletons(clusters, len(a)):
        return stats.permutation_p(a, b)
    sums: dict = {}
    for x, y, c in zip(a, b, clusters):
        sums[c] = sums.get(c, 0.0) + (x - y)
    parts = list(sums.values())
    n = len(a)
    obs = sum(parts) / n
    rng = random.Random(SEED)
    extreme = 0
    for _ in range(stats.PERMUTATIONS):
        flipped = sum(d if rng.random() < 0.5 else -d for d in parts) / n
        if abs(flipped) >= abs(obs) - 1e-15:
            extreme += 1
    return {"mean_difference": round(obs, 4), "p_value": round((extreme + 1) / (stats.PERMUTATIONS + 1), 5), "permutations": stats.PERMUTATIONS}


def paired_tests(per_system: dict[str, list[dict]], metrics=("ndcg@10", "recall@10"), exclude_from_all: frozenset | set | tuple = ()) -> dict:
    """Seeded paired sign-flip permutation tests on the pooled rows. Rows must be in the same question order, and a row's `cluster` is its resampling unit."""
    pairs = [("hybrid", "bm25"), ("hybrid", "dense"), ("hybrid_rerank", "hybrid")]
    pooled = {s: [r for r in rs if str(r["group"]) not in exclude_from_all] for s, rs in per_system.items()}
    out = {}
    for a, b in pairs:
        if a in pooled and b in pooled and pooled[a]:
            clusters = [r.get("cluster", r["qid"]) for r in pooled[a]]
            for m in metrics:
                out[f"{a}_vs_{b}.{m}"] = cluster_permutation_p([r["scores"][m] for r in pooled[a]], [r["scores"][m] for r in pooled[b]], clusters)
    return out


def abstention_auroc(answerable_top: list[float | None], abstain_top: list[float | None]) -> dict | None:
    """AUROC of the top-1 score for telling an answerable question from an abstention one, as `abstain_metrics` does. None without abstention rows.

    A query with no score (nothing came back, or the reranker fell back and only a fused score exists) is dropped from its pool and counted in `dropped_no_score`. A zero in its place would outrank every negative dense score."""
    if not abstain_top:
        return None
    import gestalt_rank

    pos = [s for s in answerable_top if s is not None]
    neg = [s for s in abstain_top if s is not None]
    a = gestalt_rank.auroc(pos, neg) if pos and neg else None
    return {"n": len(neg), "answerable_n": len(pos), "auroc": round(a, 4) if a is not None else None,
            "dropped_no_score": {"answerable": len(answerable_top) - len(pos), "abstain": len(abstain_top) - len(neg)}}


# ---------------------------------------------------------------- summary

def code_hashes(bench_file: Path) -> dict[str, str]:
    """SHA-256 of every file that decides the numbers. The bench_stats file names are kept and the memory files and the chunker are added."""
    files = {
        "evals/memory/common.py": HERE / "common.py",
        f"evals/memory/{bench_file.name}": bench_file,
        "tools/gestalt-index-builder.py": REPO / "tools" / "gestalt-index-builder.py",
    }
    out = {name: sha256_file(p) for name, p in files.items()}
    out.update(stats.code_hashes())
    return out


def environment(model, rerank: dict | None, bench_file: Path) -> dict:
    try:
        env = stats.environment(model, rerank)
    except Exception as e:  # a stub run or a missing package must not lose the code hashes
        env = {"python": platform.python_version(), "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
               "embed_profile": ec.PROFILE, "embed_dim": ec.EMBED_DIM, "environment_error": f"{type(e).__name__}: {e}"}
    env["code_sha256"] = code_hashes(bench_file)
    return env


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_common_args(ap) -> None:
    ap.add_argument("--out", required=True, help="output directory (summary.json, per_question.jsonl and scopes/)")
    ap.add_argument("--granularity", choices=["session", "turn", "both"], default="both")
    ap.add_argument("--systems", default="bm25,dense,hybrid", help="comma list from bm25,dense,hybrid (hybrid_rerank is added by --rerank on)")
    ap.add_argument("--depth", type=int, default=50, help="chunk candidates each system returns before units are deduplicated")
    ap.add_argument("--limit-questions", type=int, default=None, help="smoke test: score only the first N questions")
    ap.add_argument("--shard", default=None, metavar="K/N",
                    help="score only shard K of N (1-based), whole haystacks together. The scope files go to the shared scope directory and the shard's own summary says it is a shard. "
                         "Pool the shards with evals/memory/merge_shards.py")
    ap.add_argument("--scope-dir", default=None, help="directory of per-scope result files (default: OUT/scopes). Point several shard processes at one directory to pool them without copying")
    ap.add_argument("--rebuild", action="store_true", help="recompute every scope and overwrite its file. Without it, a scope file from a run with a different setting is an error")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--data", default=None, help="path to the dataset JSON (default: download into ~/.cache/gestalt-bench)")
    ap.add_argument("--embed-profile", choices=sorted(ec.PROFILES), default=None, help="embedding profile (read from argv before the imports)")
    ap.add_argument("--rerank", choices=["off", "on"], default="off", help="add the hybrid_rerank system. Required to rerank on any host that is not the maintainer's hub")
    ap.add_argument("--rerank-model", default=os.environ.get("GESTALT_RERANK_MODEL", "bge"), help="reranker alias: bge, qwen3-4b or qwen3-0.6b")
    ap.add_argument("--rerank-depth", type=int, default=40, help="hybrid hits handed to the reranker")
    ap.add_argument("--fusion", choices=["rrf", "convex"], default=None, help="hybrid fusion (sets GESTALT_FUSION)")
    ap.add_argument("--alpha", type=float, default=None, help="convex fusion weight on the lexical leg (sets GESTALT_FUSION_ALPHA)")


def resolve_run(args) -> tuple[list[str], dict | None, object]:
    """Check the flags took effect and return (systems, rerank config, the model to index with)."""
    if args.embed_profile and args.embed_profile != ec.PROFILE:
        raise SystemExit(f"--embed-profile {args.embed_profile} did not take effect (module loaded profile {ec.PROFILE})")
    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    bad = [s for s in systems if s not in ("bm25", "dense", "hybrid")]
    if bad:
        raise SystemExit(f"unknown systems {bad}")
    rerank = {"model": args.rerank_model, "depth": args.rerank_depth} if args.rerank == "on" else None
    if rerank:
        import gestalt_rank  # noqa: F401  fail now, not after an hour of embedding

        systems.append("hybrid_rerank")
        if "hybrid" not in systems:
            systems.insert(0, "hybrid")
    needs_model = any(s in systems for s in ("dense", "hybrid", "hybrid_rerank"))
    if needs_model:
        model = CachedEncoder(harness.get_model())
        _install_model(model)  # query embeddings go through the same cache and the same model
    else:
        model = NullEncoder()
    return systems, rerank, model


def parse_shard(spec: str | None) -> tuple[int, int] | None:
    """'K/N' with 1 <= K <= N, or None. Anything else is an error, because a typo here would silently score the wrong slice."""
    if not spec:
        return None
    try:
        k, n = (int(x) for x in str(spec).split("/"))
    except ValueError:
        raise SystemExit(f"--shard wants K/N, for example 2/4, not {spec!r}")
    if not 1 <= k <= n:
        raise SystemExit(f"--shard {spec}: K must be between 1 and N")
    return k, n


def apply_shard(questions: list[dict], spec: str | None) -> list[dict]:
    """The questions of shard K of N. Whole scopes (haystacks, conversations) go together, in dataset order, so each index is built once.

    The scopes are cut into N contiguous groups whose sizes differ by at most one. A shard can come out empty when N exceeds the number of scopes, which is an error."""
    sh = parse_shard(spec)
    if sh is None:
        return questions
    k, n = sh
    scopes = list(dict.fromkeys(q["scope"] for q in questions))
    if n > len(scopes):
        raise SystemExit(f"--shard {spec}: only {len(scopes)} haystacks to split, so N may not exceed that")
    base, extra = divmod(len(scopes), n)
    start = (k - 1) * base + min(k - 1, extra)
    mine = set(scopes[start:start + base + (1 if k <= extra else 0)])
    return [q for q in questions if q["scope"] in mine]


def write_outputs(out: Path, summary: dict, per_question: list[dict]) -> None:
    """summary.json and per_question.jsonl, each written atomically."""
    out = Path(out)
    atomic_write_text(out / "per_question.jsonl", "".join(json.dumps(r) + "\n" for r in per_question))
    atomic_write_text(out / "summary.json", json.dumps(summary, indent=1) + "\n")


def fmt_table(title: str, agg: dict, systems: list[str], metrics=("recall@5", "recall@10", "mrr", "ndcg@10")) -> str:
    lines = [title]
    if not agg:
        return title + "\n  (no scored rows)"
    for g in sorted(next(iter(agg.values()))):
        lines.append(f"  {g}  (n={next(iter(agg.values()))[g]['n']})")
        for s in systems:
            c = agg[s][g]
            lines.append(f"    {s:14s} " + "  ".join(f"{m} {c[m]:.3f}" + (f" {c[m + '_ci95']}" if m + "_ci95" in c and g == 'all' else "") for m in metrics))
    return "\n".join(lines)


# ---------------------------------------------------------------- the run fingerprint and the scope files

def run_fingerprint(benchmark: str, info: dict, systems: list[str], args, rerank: dict | None, bench_file: Path, smoke: bool) -> dict:
    """Everything that decides a scope's numbers. A scope file is reused only when its fingerprint equals this one."""
    import gestalt_rank

    fusion = gestalt_rank.fusion_mode()
    return {
        "benchmark": benchmark,
        "dataset_sha256": info["sha256"],
        "systems": list(systems),
        "granularity": args.granularity,
        "depth": args.depth,
        "chunking": CHUNKING,
        "rerank": rerank,
        "fusion": fusion,
        "alpha": gestalt_rank.fusion_alpha() if fusion == "convex" else None,
        "embed_profile": ec.PROFILE,
        "code_sha256": code_hashes(bench_file),
        "seed": SEED,
        "bootstrap": BOOTSTRAP,
        "smoke_subset": bool(smoke),
    }


def fingerprint_diff(a: dict, b: dict) -> list[str]:
    """Names of the fields in which two fingerprints differ. Nested code hashes are named per file."""
    out = []
    for k in sorted(set(a) | set(b)):
        if k == "code_sha256" and isinstance(a.get(k), dict) and isinstance(b.get(k), dict):
            out += [f"code_sha256.{f}" for f in sorted(set(a[k]) | set(b[k])) if a[k].get(f) != b[k].get(f)]
        elif a.get(k) != b.get(k):
            out.append(k)
    return out


def levels_for(granularity: str) -> list[str]:
    grans = ["session", "turn"] if granularity == "both" else [granularity]
    return [lv for g in grans for lv in (("session",) if g == "session" else ("turn", "session_from_turns"))]


def safe_scope_name(scope: str) -> str:
    """A file name for a scope id. An id with unusual characters gets a short hash, so two ids never share a file."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", scope)
    return safe if safe == scope else f"{safe}-{hashlib.sha1(scope.encode()).hexdigest()[:8]}"


def scope_path(scope_dir: Path, scope: str) -> Path:
    return Path(scope_dir) / f"{safe_scope_name(scope)}.json"


def log_line(msg: str) -> None:
    """One line on stderr about a scope file that is resumed, discarded or merged."""
    print(f"[memory-bench] {msg}", file=sys.stderr)


def read_scope_file(path: Path) -> dict | None:
    """The parsed scope file, or None when it is missing, truncated, unparseable or not shaped like one."""
    try:
        d = json.loads(Path(path).read_text())
        ok = (d["format"] == SCOPE_FORMAT and isinstance(d["fingerprint"], dict) and isinstance(d["scope"], str) and isinstance(d["qids"], list)
              and isinstance(d["rows"], dict) and isinstance(d["abstain"], dict) and isinstance(d["lines"], list)
              and isinstance(d["rerank_fallbacks"], int) and isinstance(d["seconds"], (int, float)))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        log_line(f"discarding {path}: it is unreadable ({type(e).__name__})")
        return None
    if not ok:
        log_line(f"discarding {path}: it is not shaped like a scope file")
        return None
    return d


def write_scope_file(scope_dir: Path, fingerprint: dict, result: dict, meta: dict) -> None:
    atomic_write_text(scope_path(scope_dir, result["scope"]), json.dumps({"format": SCOPE_FORMAT, "fingerprint": fingerprint, "meta": meta, **result}) + "\n")


def load_scope(scope_dir: Path, scope: str, fingerprint: dict, qids: list[str], systems: list[str], levels: list[str]) -> dict | None:
    """The finished result of a scope from an earlier run, or None when the scope must be computed.

    A damaged file counts as absent. A file from a run with another fingerprint, or another question list, is an error that names the field."""
    path = scope_path(scope_dir, scope)
    d = read_scope_file(path)
    if d is None or d["scope"] != scope:
        if d is not None:
            log_line(f"discarding {path}: it holds scope {d['scope']!r}, not {scope!r}")
        return None
    diff = fingerprint_diff(d["fingerprint"], fingerprint)
    if not diff and d["qids"] != qids:
        diff = ["questions"]
    if diff:
        raise SystemExit(f"{path}: written by a different run, it differs in {', '.join(diff)}. Pass --rebuild to overwrite it or choose another --scope-dir.")
    if set(d["rows"]) != set(levels) or any(set(d["rows"][lv]) != set(systems) for lv in levels):
        log_line(f"discarding {path}: it holds other levels or systems than this run")
        return None
    if d["rerank_fallbacks"] > 0 and "hybrid_rerank" in systems:
        log_line(f"discarding {path}: the reranker fell back on {d['rerank_fallbacks']} queries, so the scope is recomputed")
        return None
    log_line(f"resuming from {path}: the scope is finished and its fingerprint matches")
    return d


def assemble(results: list[dict], systems: list[str], levels: list[str]) -> dict:
    """Pool scope results, in the order given, into the shape `summarize_levels` reads."""
    rows = {lv: {s: [] for s in systems} for lv in levels}
    tops = {lv: {s: ([], []) for s in systems} for lv in levels}
    lines: list[dict] = []
    fallbacks, seconds = 0, 0.0
    for r in results:
        for lv in levels:
            for s in systems:
                rows[lv][s].extend(r["rows"][lv][s])
                ans, ab = r["abstain"][lv][s]
                tops[lv][s][0].extend(ans)
                tops[lv][s][1].extend(ab)
        lines.extend(r["lines"])
        fallbacks += r["rerank_fallbacks"]
        seconds += r["seconds"]
    return {"rows": rows, "abstain": tops, "lines": lines, "rerank_fallbacks": fallbacks, "seconds": round(seconds, 1), "scopes": [r["scope"] for r in results]}


# ---------------------------------------------------------------- the scoring loop

def _score_scope(scope: str, qs: list[dict], make_sessions, grans: list[str], systems: list[str], model, depth: int, rerank: dict | None, batch_size: int) -> dict:
    t0 = time.time()
    levels = levels_for("both" if len(grans) == 2 else grans[0])
    rows = {lv: {s: [] for s in systems} for lv in levels}
    tops = {lv: {s: [[], []] for s in systems} for lv in levels}
    lines: list[dict] = []
    if hasattr(model, "begin_scope"):
        model.begin_scope()
    sessions = make_sessions(scope)
    indexes = {g: MemoryIndex(build_units(sessions, g), model, batch_size) for g in grans}
    for q in qs:
        for g in grans:
            idx = indexes[g]
            session_of = {u.uid: u.session for u in idx.units}
            for s in systems:
                ranked, top = idx.rank(q["query"], s, depth, rerank)
                views = [("session", ranked, q["gold_session"])] if g == "session" else [
                    ("turn", ranked, q["gold_turn"]),
                    ("session_from_turns", list(dict.fromkeys(session_of[u] for u in ranked)), q["gold_session"])]
                for lv, rk, gold in views:
                    if q["abstain"]:
                        tops[lv][s][1].append(top)
                        continue
                    if not gold:
                        continue
                    sc = score_ranked(rk, gold)
                    rows[lv][s].append({"qid": q["qid"], "group": q["group"], "cluster": q.get("cluster", q["qid"]), "scores": sc})
                    tops[lv][s][0].append(top)
                    lines.append({"level": lv, "system": s, "qid": q["qid"], "group": q["group"], "gold_n": len(gold), "top10": rk[:10], "scores": {k: round(v, 4) for k, v in sc.items()}})
    return {"scope": scope, "qids": [q["qid"] for q in qs], "rows": rows, "abstain": tops, "lines": lines,
            "rerank_fallbacks": sum(i.rerank_fallbacks for i in indexes.values()), "seconds": round(time.time() - t0, 1)}


def evaluate(questions: list[dict], make_sessions, granularity: str, systems: list[str], model, depth: int,
             rerank: dict | None, batch_size: int = 64, log=None, scope_dir: Path | None = None,
             fingerprint: dict | None = None, rebuild: bool = False, meta: dict | None = None) -> dict:
    """Score every question at the chosen granularity, one scope at a time.

    A question is {qid, group, query, scope, abstain, gold_session: set, gold_turn: set, cluster?}. `make_sessions(scope)` returns the
    Session list to search for that scope (one haystack for LongMemEval, one conversation for LoCoMo). Each scope's index is built once
    and dropped. Abstention questions are searched but not scored.

    With `scope_dir` and `fingerprint`, each finished scope is written to `scope_dir/<scope>.json` at once, and a scope whose file
    matches the fingerprint is loaded instead of computed. A kill therefore loses at most the scope in flight. The result is always
    assembled from the scope results in question order, so a resumed run equals an uninterrupted one.

    Returns {'rows': {level: {system: [row]}}, 'abstain': {level: {system: ([answerable tops], [abstain tops])}}, 'lines': [...],
    'rerank_fallbacks': n, 'seconds': s, 'scopes': [...]}. Levels are 'session' (session units), 'turn' (turn units) and
    'session_from_turns' (the turn ranking folded to sessions)."""
    grans = ["session", "turn"] if granularity == "both" else [granularity]
    levels = levels_for(granularity)
    by_scope: dict[str, list[dict]] = {}
    for q in questions:
        by_scope.setdefault(q["scope"], []).append(q)
    results, reused = [], 0
    for n, (scope, qs) in enumerate(by_scope.items(), 1):
        qids = [q["qid"] for q in qs]
        res = None
        if scope_dir is not None and not rebuild:
            res = load_scope(scope_dir, scope, fingerprint, qids, systems, levels)
        if res is not None:
            reused += 1
        else:
            res = _score_scope(scope, qs, make_sessions, grans, systems, model, depth, rerank, batch_size)
            if scope_dir is not None:
                write_scope_file(scope_dir, fingerprint, res, meta or {})
        results.append(res)
        if log and (n % 25 == 0 or n == len(by_scope)):
            log(f"  {n}/{len(by_scope)} scopes ({reused} reused from earlier files)")
    return assemble(results, systems, levels)


def summarize_levels(result: dict, systems: list[str], exclude_from_all: frozenset | set | tuple = ()) -> dict:
    """Aggregate every level and system, add the paired tests and the abstention AUROC.

    When the reranker fell back on any query, every level lists `hybrid_rerank` under `void_systems`. Its rows are the hybrid order under another name."""
    out = {}
    for lv, per_sys in result["rows"].items():
        paired = {s: r for s, r in per_sys.items() if r}
        out[lv] = {
            "systems": {s: aggregate(r, exclude_from_all=exclude_from_all) for s, r in paired.items()},
            "tests": paired_tests(paired, exclude_from_all=exclude_from_all) if paired else {},
            "abstention_auroc": {s: abstention_auroc(*result["abstain"][lv][s]) for s in systems if s != "bm25" and result["abstain"][lv][s][1]} or None,
        }
        if result.get("rerank_fallbacks") and "hybrid_rerank" in paired:
            out[lv]["void_systems"] = ["hybrid_rerank"]
    return out


def summary_config(fp: dict, fallbacks: int, include_adversarial: bool | None = None) -> dict:
    cfg = {"systems": fp["systems"], "granularity": fp["granularity"], "depth_chunks": fp["depth"], "k": list(KS), "bootstrap": fp["bootstrap"], "seed": fp["seed"],
           "chunking": fp["chunking"], "rerank": fp["rerank"], "rerank_fallback_queries": fallbacks, "fusion": fp["fusion"], "alpha": fp["alpha"], "embed_profile": fp["embed_profile"]}
    if include_adversarial is not None:
        cfg["include_adversarial"] = include_adversarial
    return cfg


def mark_rerank_void(summary: dict, fallbacks: int) -> None:
    """Record that the hybrid_rerank rows are void. The caller turns this into exit code 3, as beir_bench does."""
    if fallbacks:
        summary["rerank_invalid"] = {"fallbacks": fallbacks}


def exit_code(summary: dict) -> int:
    """3 when the reranker fell back on any query (the hybrid_rerank rows are void), else 0."""
    if summary.get("rerank_invalid"):
        print(f"FAIL the hybrid_rerank rows are void, the reranker fell back on {summary['rerank_invalid']['fallbacks']} queries. Do not quote them.", file=sys.stderr)
        return 3
    return 0
