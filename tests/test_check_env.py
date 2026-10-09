"""evals/retrieval/check_env.py: the preflight a public user runs first. Pure evaluate() fed with facts, plus one real run."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals" / "retrieval"))
import check_env as ce  # noqa: E402

PINNED = {"torch": "2.9.0", "transformers": "5.15.0", "sentence-transformers": "6.0.0"}
OK_VERSIONS = dict(PINNED)
CUDA = {"available": True, "name": "RTX", "free_gib": 14.0, "total_gib": 16.0, "torch_error": None}
MODELS = {"embedding_model": "nomic-ai/nomic-embed-text-v1.5", "embedding_revision": "e9b6763", "embed_profile": "nomic",
          "reranker_alias": "qwen3-0.6b", "reranker_pinned": "e61197ed"}


def test_go_when_pins_cuda_and_models_line_up():
    lines, problems = ce.evaluate(env={}, cuda=CUDA, versions=OK_VERSIONS, pinned=PINNED, models=MODELS)
    assert problems == [] and any(l.startswith("cuda: RTX") for l in lines)


def test_a_version_off_the_pin_is_no_go():
    v = dict(OK_VERSIONS, transformers="5.19.0")
    _, problems = ce.evaluate(env={}, cuda=CUDA, versions=v, pinned=PINNED, models=MODELS)
    assert any("transformers 5.19.0 differs from the pin 5.15.0" in p for p in problems)


def test_no_cuda_is_no_go_unless_a_cpu_run_is_allowed():
    nocuda = dict(CUDA, available=False, name=None, free_gib=None, total_gib=None)
    _, problems = ce.evaluate(env={}, cuda=nocuda, versions=OK_VERSIONS, pinned=PINNED, models=MODELS)
    assert any("no CUDA device" in p for p in problems)
    lines, problems = ce.evaluate(env={"GESTALT_BENCH_ALLOW_CPU": "1"}, cuda=nocuda, versions=OK_VERSIONS, pinned=PINNED, models=MODELS)
    assert problems == [] and any("smoke only" in l for l in lines)


def test_low_vram_suggests_the_fraction_knob():
    low = dict(CUDA, free_gib=9.0)
    lines, _ = ce.evaluate(env={}, cuda=low, versions=OK_VERSIONS, pinned=PINNED, models=MODELS)
    assert any("GESTALT_BENCH_VRAM_FRACTION=" in l for l in lines)
    lines, _ = ce.evaluate(env={"GESTALT_BENCH_VRAM_FRACTION": "0.5"}, cuda=low, versions=OK_VERSIONS, pinned=PINNED, models=MODELS)
    assert not any("set GESTALT_BENCH_VRAM_FRACTION" in l for l in lines)


def test_an_unpinned_reranker_is_no_go():
    m = dict(MODELS, reranker_pinned=None)
    _, problems = ce.evaluate(env={}, cuda=CUDA, versions=OK_VERSIONS, pinned=PINNED, models=MODELS | m)
    assert any("no pinned revision" in p for p in problems)


def test_the_script_runs_and_ends_with_a_verdict():
    r = subprocess.run([sys.executable, str(Path(ce.__file__))], capture_output=True, text=True, timeout=120)
    assert r.returncode in (0, 2), r.stderr[-500:]
    assert r.stdout.strip().splitlines()[-1].startswith(("GO", "NO-GO")), r.stdout


def test_a_local_build_tag_after_plus_is_not_a_version_difference():
    """torch from the CUDA index reports 2.11.0+cu128. The pin is 2.11.0. A clean CUDA install must say GO (2026-10-09)."""
    v = dict(OK_VERSIONS, torch="2.11.0+cu128")
    lines, problems = ce.evaluate(env={}, cuda=CUDA, versions=v, pinned=dict(PINNED, torch="2.11.0"), models=MODELS)
    assert problems == [] and any(l.startswith("torch: 2.11.0+cu128") for l in lines)
    v = dict(OK_VERSIONS, torch="2.11.1+cu128")
    _, problems = ce.evaluate(env={}, cuda=CUDA, versions=v, pinned=dict(PINNED, torch="2.11.0"), models=MODELS)
    assert any("torch 2.11.1+cu128 differs from the pin 2.11.0" in p for p in problems)
