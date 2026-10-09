#!/usr/bin/env python3
"""Preflight for the retrieval benchmarks: run this before beir_bench.py, mteb_bench.py or the memory harnesses.

It prints the facts a run depends on and ends with GO or NO-GO. It downloads nothing and loads no model. Exit 0 on GO, 2 on NO-GO.

Checks, each from a failure seen on the author's hub on 2026-10-08:
  versions     python, torch, transformers, sentence-transformers against evals/retrieval/requirements-bench.txt (a clean clone once
               resolved transformers 5.19, where nomic's remote code fails)
  cuda         CUDA present and which card (a run that lost its GPU scores a CPU fallback; beir_bench refuses that unless told)
  vram         free VRAM, with a GESTALT_BENCH_VRAM_FRACTION suggestion under 12 GB (an oversubscribed card spills into system
               memory on Windows and runs five times slower with no error)
  knobs        GESTALT_GPU_DUTY, GESTALT_BENCH_VRAM_FRACTION, GESTALT_EVAL_QUERY_TIMEOUT, GESTALT_EVAL_EMBED_TIMEOUT, GESTALT_EMBED_DEVICE as set
  models       the embedding model and revision from gestalt_embed_config, the reranker alias and pinned revision from gestalt_rank

GESTALT_BENCH_ALLOW_CPU=1 or GESTALT_EMBED_DEVICE=cpu turns the CUDA check into a note, for a smoke run on a laptop.
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tools"))
sys.path.insert(0, str(HERE))

PINS_FILE = HERE / "requirements-bench.txt"
LOW_VRAM_GIB = 12.0


def pins(path: Path = PINS_FILE) -> dict[str, str]:
    """`name==version` lines of the requirements file, names lower-cased."""
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([^\s;]+)", line)
        if m:
            out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def installed(name: str) -> str | None:
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def cuda_facts() -> dict:
    """torch's view of CUDA without touching a model: available, device name, free and total VRAM in GiB."""
    facts = {"available": False, "name": None, "free_gib": None, "total_gib": None, "torch_error": None}
    try:
        torch = importlib.import_module("torch")
    except Exception as e:  # noqa: BLE001
        facts["torch_error"] = f"{type(e).__name__}: {e}"
        return facts
    try:
        facts["available"] = bool(torch.cuda.is_available())
        if facts["available"]:
            facts["name"] = torch.cuda.get_device_name(0)
            free, total = torch.cuda.mem_get_info(0)
            facts["free_gib"], facts["total_gib"] = free / 2**30, total / 2**30
    except Exception as e:  # noqa: BLE001
        facts["torch_error"] = f"{type(e).__name__}: {e}"
    return facts


def model_facts() -> dict:
    """Embedding model and revision, reranker alias and pinned revision, from the project's own config, no network."""
    facts = {}
    try:
        ec = importlib.import_module("gestalt_embed_config")
        facts["embedding_model"] = getattr(ec, "MODEL_NAME", None)
        facts["embedding_revision"] = getattr(ec, "MODEL_REVISION", None)
        facts["embed_profile"] = getattr(ec, "PROFILE", None)
    except Exception as e:  # noqa: BLE001
        facts["embedding_error"] = f"{type(e).__name__}: {e}"
    try:
        gr = importlib.import_module("gestalt_rank")
        alias = os.environ.get("GESTALT_RERANK_MODEL") or gr.rerank_alias()
        revs = getattr(gr, "RERANK_REVISIONS", {})
        facts["reranker_alias"] = alias
        facts["reranker_pinned"] = revs.get(alias)
    except Exception as e:  # noqa: BLE001
        facts["reranker_error"] = f"{type(e).__name__}: {e}"
    return facts


def evaluate(env: dict | None = None, cuda: dict | None = None, versions: dict | None = None, pinned: dict | None = None,
             models: dict | None = None) -> tuple[list[str], list[str]]:
    """Return (lines, problems). Problems make the verdict NO-GO. Pure, so tests can feed it facts."""
    env = os.environ if env is None else env
    cuda = cuda_facts() if cuda is None else cuda
    pinned = pins() if pinned is None else pinned
    versions = {n: installed(n) for n in ("torch", "transformers", "sentence-transformers")} if versions is None else versions
    models = model_facts() if models is None else models
    lines, problems = [], []
    lines.append(f"python {sys.version.split()[0]}")
    for name, have in versions.items():
        want = pinned.get(name)
        if have is None:
            lines.append(f"{name}: not installed")
            problems.append(f"{name} is not installed")
        elif want and have.split("+", 1)[0] != want:
            # The local build tag after "+" (torch 2.11.0+cu128, 2.11.0+cpu) names the wheel index, not the version.
            # The pin is the public version, so the tag is ignored. A clean CUDA install said NO-GO here on 2026-10-09.
            lines.append(f"{name}: {have} (pinned {want})")
            problems.append(f"{name} {have} differs from the pin {want} in requirements-bench.txt")
        else:
            lines.append(f"{name}: {have}" + (" (pinned)" if want else ""))
    cpu_ok = env.get("GESTALT_BENCH_ALLOW_CPU", "").strip() == "1" or env.get("GESTALT_EMBED_DEVICE", "").strip().lower().startswith("cpu")
    if cuda.get("torch_error"):
        lines.append(f"cuda: torch could not answer ({cuda['torch_error']})")
        problems.append("torch could not report CUDA")
    elif cuda.get("available"):
        lines.append(f"cuda: {cuda['name']}, {cuda['free_gib']:.1f} of {cuda['total_gib']:.1f} GiB free")
        if cuda["free_gib"] is not None and cuda["free_gib"] < LOW_VRAM_GIB and not env.get("GESTALT_BENCH_VRAM_FRACTION"):
            frac = max(0.3, round(cuda["free_gib"] / cuda["total_gib"] - 0.05, 2))
            lines.append(f"vram: under {LOW_VRAM_GIB:.0f} GiB free, set GESTALT_BENCH_VRAM_FRACTION={frac} so the run fails fast instead of spilling into system memory")
    else:
        if cpu_ok:
            lines.append("cuda: none, and a CPU run is allowed by GESTALT_BENCH_ALLOW_CPU or GESTALT_EMBED_DEVICE (smoke only, never a published number)")
        else:
            lines.append("cuda: none")
            problems.append("no CUDA device; set GESTALT_BENCH_ALLOW_CPU=1 for a CPU smoke run")
    for knob in ("GESTALT_GPU_DUTY", "GESTALT_BENCH_VRAM_FRACTION", "GESTALT_EVAL_QUERY_TIMEOUT", "GESTALT_EVAL_EMBED_TIMEOUT", "GESTALT_EMBED_DEVICE", "GESTALT_EMBED_PROFILE"):
        lines.append(f"{knob}={env.get(knob) or '(unset)'}")
    if models.get("embedding_error"):
        problems.append(f"embedding config: {models['embedding_error']}")
    else:
        lines.append(f"embedding: {models.get('embedding_model')} @ {models.get('embedding_revision')} (profile {models.get('embed_profile')})")
    if models.get("reranker_error"):
        problems.append(f"reranker config: {models['reranker_error']}")
    else:
        lines.append(f"reranker: {models.get('reranker_alias')} pinned {models.get('reranker_pinned')}")
        if not models.get("reranker_pinned"):
            problems.append(f"reranker alias {models.get('reranker_alias')} has no pinned revision")
    return lines, problems


def main() -> int:
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__.strip().splitlines()[0])
        print("usage: python3 evals/retrieval/check_env.py    (no arguments; prints the facts a run depends on and ends with GO or NO-GO, exit 0 or 2)")
        return 0
    lines, problems = evaluate()
    for line in lines:
        print(line)
    if problems:
        print("NO-GO: " + "; ".join(problems))
        return 2
    print("GO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
