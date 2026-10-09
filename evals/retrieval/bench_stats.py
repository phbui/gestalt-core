"""Statistics, code hashes and the environment record shared by the retrieval and memory benchmarks.

Importing this module has no side effects. It reads no argv, writes no os.environ entry and loads no model.
beir_bench.py re-exports every public name here, and evals/memory/common.py imports from here so that it no
longer pulls in beir_bench (which reads the system flags from argv at import).

SEED, BOOTSTRAP and PERMUTATIONS fix the seeded bootstrap interval and the paired sign-flip test.
ndcg_at_10 scores one ranking. permutation_p and bootstrap_ci reproduce the stream of random.Random(SEED).
code_hashes names the files behind a number. environment records the stack that produced it.
split_text is the index builder's own chunker, loaded by path because its file name has a hyphen.
"""
from __future__ import annotations

import hashlib
import math
import os
import platform
import random
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_BLOCK_SIZE = 20_000  # equals resumable.DEFAULT_BLOCK_SIZE (tests/test_bench_stats.py checks it), repeated so this module imports nothing of ours

SEED = 12345
BOOTSTRAP = 10_000
PERMUTATIONS = 20_000
TOPK = 10


def ndcg_at_10(ranked: list[str], rel: dict[str, int]) -> float:
    dcg = sum(rel.get(d, 0) / math.log2(i + 2) for i, d in enumerate(ranked[:TOPK]))
    ideal = sorted(rel.values(), reverse=True)[:TOPK]
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def _python_stream(seed: int):
    """A numpy RandomState that yields the exact stream of random.Random(seed).random().

    Both are MT19937 and both build a double from two 32-bit draws the same way (genrand_res53), so copying
    the state makes them agree draw for draw. The vectorised tests below therefore reproduce every p-value
    and interval the pure-Python loops gave."""
    import numpy as np

    state = random.Random(seed).getstate()[1]
    rs = np.random.RandomState()
    rs.set_state(("MT19937", np.array(state[:-1], dtype=np.uint32), state[-1]))
    return rs


def _rows_for(n: int, total: int) -> int:
    return max(1, min(total, 2_000_000 // max(1, n)))  # keeps one draw matrix near 16 MB


def bootstrap_ci(values: list[float]) -> list[float]:
    """Seeded 95% percentile bootstrap of the mean, BOOTSTRAP resamples.

    Identical to drawing with random.Random(SEED).choices(values, k=n) per resample and summing left to
    right, the original pure-Python loop, which tests/test_beir_bench.py keeps as the reference."""
    import numpy as np

    v = np.asarray(values, dtype=np.float64)
    n = len(v)
    rs = _python_stream(SEED)
    means = []
    step = _rows_for(n, BOOTSTRAP)
    for s0 in range(0, BOOTSTRAP, step):
        m = min(step, BOOTSTRAP - s0)
        picks = v[np.floor(rs.random_sample(m * n).reshape(m, n) * float(n)).astype(np.int64)]
        acc = np.zeros(m)
        for j in range(n):  # left to right, as Python's sum adds
            acc += picks[:, j]
        means.append(acc / n)
    allm = np.sort(np.concatenate(means))
    return [round(float(allm[int(0.025 * BOOTSTRAP)]), 4), round(float(allm[int(0.975 * BOOTSTRAP) - 1]), 4)]


def permutation_p(a: list[float], b: list[float]) -> dict:
    """Seeded paired sign-flip permutation test, PERMUTATIONS draws, two-sided, with the +1 correction.

    Vectorised over the permutations with the same random stream and the same summation order as the
    original loop, so the p-value is bit-identical to it."""
    import numpy as np

    diffs = [x - y for x, y in zip(a, b)]
    n = len(diffs)
    obs = sum(diffs) / n
    d = np.asarray(diffs, dtype=np.float64)
    rs = _python_stream(SEED)
    extreme = 0
    step = _rows_for(n, PERMUTATIONS)
    for s0 in range(0, PERMUTATIONS, step):
        m = min(step, PERMUTATIONS - s0)
        keep = rs.random_sample(m * n).reshape(m, n) < 0.5
        acc = np.zeros(m)
        for j in range(n):
            acc += np.where(keep[:, j], d[j], -d[j])
        extreme += int((np.abs(acc / n) >= abs(obs) - 1e-15).sum())
    return {"mean_difference": round(obs, 4), "p_value": round((extreme + 1) / (PERMUTATIONS + 1), 5), "permutations": PERMUTATIONS}


def code_hashes() -> dict[str, str]:
    """SHA-256 of the files that decide the numbers, so a result names the exact code behind it.

    They are beir_bench.py, bench_stats.py, bench_engine.py, fusion.py, resumable.py, run_retrieval_evals.py,
    tools/gestalt_embed_config.py and, when present, tools/gestalt_rank.py. That is eight files."""
    files = {
        "evals/retrieval/beir_bench.py": HERE / "beir_bench.py",
        "evals/retrieval/bench_stats.py": HERE / "bench_stats.py",
        "evals/retrieval/bench_engine.py": HERE / "bench_engine.py",
        "evals/retrieval/fusion.py": HERE / "fusion.py",
        "evals/retrieval/resumable.py": HERE / "resumable.py",
        "evals/retrieval/run_retrieval_evals.py": HERE / "run_retrieval_evals.py",
        "tools/gestalt_embed_config.py": REPO / "tools" / "gestalt_embed_config.py",
    }
    rank = REPO / "tools" / "gestalt_rank.py"
    if rank.exists():
        files["tools/gestalt_rank.py"] = rank
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}


def environment(model, rerank: dict | None = None, block_size: int = DEFAULT_BLOCK_SIZE, batch_size: int | None = None,
                dense_mode: str | None = None) -> dict:
    """The software and configuration behind a result. The shared modules load here, at call time, not at import."""
    import sys

    import numpy
    import sentence_transformers
    import sqlite_vec
    import torch

    for p in (str(HERE), str(REPO / "tools")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import bench_engine as engine
    import gestalt_embed_config as ec
    import run_retrieval_evals as harness

    try:
        import ir_datasets

        ird = ir_datasets.__version__
    except Exception:
        ird = None
    rank = harness.gestalt_rank
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "sqlite_vec": getattr(sqlite_vec, "__version__", None),
        "numpy": numpy.__version__,
        "torch": torch.__version__,
        "sentence_transformers": sentence_transformers.__version__,
        "ir_datasets": ird,
        "device": str(model.device),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "embedding_model": ec.MODEL_NAME,
        "embedding_revision": ec.MODEL_REVISION,
        "embedding_load_path": ec.LOAD_PATH,
        "embed_profile": ec.PROFILE,
        "embed_dim": ec.EMBED_DIM,
        # Precision. embed_dtype and rerank_dtype are the resolved values, not the raw env. Documents are encoded longest first
        # in sub-batches (bench_engine.plan_subbatches), so the batch size and the sort order both shape TF32 and fp16 rounding.
        "embed_dtype": ec.EMBED_DTYPE,
        "embed_normalize": bool(getattr(ec, "EMBED_NORMALIZE", False)),
        "rerank_dtype": rank.rerank_dtype(),
        "tf32": ec.TF32,
        "attn_impl": ec.ATTN_IMPL,
        "embed_batch_size": batch_size,
        "embed_sort_order": "length-sorted",
        "doc_prefix": ec.DOC_PREFIX,
        "query_prefix": ec.QUERY_PREFIX,
        "rrf_k": harness.K_RRF,
        # mode and alpha are the raw environment, as before. resolved is what gestalt_rank.fusion_settings reads: the method, the
        # convex alpha (lexical leg), the wrrf lexical weight, the normaliser, the missing policy, the rescue knobs and the RRF K.
        # mode and alpha are the validated values the ranker uses, the same ones resolved below, so the record cannot disagree
        # with the run (before 2026-10-09 they were the raw environment strings).
        "fusion": {"mode": rank.fusion_settings()["method"], "alpha": rank.fusion_settings()["alpha"],
                   "call_site": "bench_engine.fuse_pools, through gestalt_rank.fuse and evals/retrieval/fusion.py, the fusion the server runs",
                   "resolved": rank.fusion_settings()},
        "rerank": {"on": bool(rerank), "model": rerank["model"] if rerank else None, "depth": rerank["depth"] if rerank else None,
                   "instruction": rank.qwen_instruction() if rerank else None, "maxchars": rank.rerank_maxchars() if rerank else None,
                   "instruction_note": "The Qwen rerankers prepend this instruction. It is dataset-specific tuning, set per dataset with "
                                       "GESTALT_RERANK_INSTRUCTION. The default names a personal knowledge base."},
        "engine": {
            "block_size": block_size, "dtype": "float32", "dense": dense_mode or os.environ.get("GESTALT_BENCH_DENSE") or "auto",
            "dense_threshold": engine.DENSE_THRESHOLD,
            "dense_note": f"sqlite-vec up to {engine.DENSE_THRESHOLD} documents, exact blocked search above",
            "fts": "file-backed FTS5, contentless, porter unicode61",
            "encode_budgets": engine.budgets(), "query_encode": "one call per query", "stopwords": rank.stopwords_on(),
        },
        "code_sha256": code_hashes(),
    }


_builder = None


def _index_builder():
    global _builder
    if _builder is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("gestalt_index_builder_for_bench", REPO / "tools" / "gestalt-index-builder.py")
        _builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_builder)
    return _builder


def split_text(body: str, limit: int | None = None, overlap: int | None = None) -> list[str]:
    """The index builder's own `_split_text`. The limits default to its MAX_CHUNK_CHARS and SUBCHUNK_OVERLAP."""
    b = _index_builder()
    return b._split_text(body, b.MAX_CHUNK_CHARS if limit is None else limit, b.SUBCHUNK_OVERLAP if overlap is None else overlap)


_split_text = split_text  # the old private name, kept so earlier callers still import
