"""Measurement helpers for the cost block of a zoo result.

Everything here takes plain callables and objects, so a fake model tests it. torch is imported only when asked about the GPU.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np


def _torch_cuda():
    """The torch module when CUDA is usable, else None."""
    try:
        import torch
    except ImportError:
        return None
    return torch if torch.cuda.is_available() else None


def gpu_name() -> str | None:
    torch = _torch_cuda()
    return torch.cuda.get_device_name(0) if torch else None


def reset_peak() -> None:
    torch = _torch_cuda()
    if torch:
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()


def peak_vram_mib() -> float | None:
    """Peak torch allocation since reset_peak(), in MiB. None on a CPU."""
    torch = _torch_cuda()
    if not torch:
        return None
    torch.cuda.synchronize()
    return round(torch.cuda.max_memory_allocated() / 2**20, 1)


def param_count(model) -> int | None:
    """Parameters of a torch module, or of its .model attribute (a CrossEncoder keeps its network there). None when neither has any."""
    for obj in (model, getattr(model, "model", None)):
        params = getattr(obj, "parameters", None)
        if callable(params):
            return int(sum(p.numel() for p in params()))
    return None


def dir_bytes(path: str | Path) -> int:
    """Bytes of a file, or of every file under a directory."""
    p = Path(path)
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def batch1_latencies_ms(search: Callable[[list, int], object], queries: Sequence, k: int, n: int) -> list[float]:
    """Wall time in ms of `search([q], k)` for each of the first n queries, one query per call."""
    out = []
    for q in list(queries)[:n]:
        t0 = time.perf_counter()
        search([q], k)
        out.append((time.perf_counter() - t0) * 1000)
    return out


def percentile(values: Sequence[float], p: float) -> float | None:
    return round(float(np.percentile(values, p)), 3) if len(values) else None


def cost_block(*, index_seconds: float, n_docs: int, search_seconds: float, n_queries: int, latencies_ms: Sequence[float],
               peak_vram: float | None, index_bytes: int | None, parameters: int | None, dtype: str | None, gpu: str | None) -> dict:
    """The cost block of one system. search_seconds is the batched run over all queries. latencies_ms are the batch-1 runs."""
    return {
        "index_seconds": round(index_seconds, 3),
        "docs_per_second": round(n_docs / index_seconds, 1) if index_seconds > 0 else None,
        "query_latency_ms_p50": percentile(latencies_ms, 50),
        "query_latency_ms_p95": percentile(latencies_ms, 95),
        "latency_queries": len(latencies_ms),
        "queries_per_second": round(n_queries / search_seconds, 2) if search_seconds > 0 else None,
        "peak_vram_mib": peak_vram,
        "index_bytes": index_bytes,
        "parameters": parameters,
        "dtype": dtype,
        "gpu": gpu,
    }
