"""bench_stats.py: the statistics, code hashes and environment record that beir_bench and the memory benchmarks share (QAL-001)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RETRIEVAL = REPO / "evals" / "retrieval"
sys.path.insert(0, str(RETRIEVAL))
sys.path.insert(0, str(REPO / "tools"))
import bench_stats as bs  # noqa: E402

PUBLIC_NAMES = ("SEED", "BOOTSTRAP", "PERMUTATIONS", "ndcg_at_10", "permutation_p", "code_hashes", "environment", "split_text")


def test_importing_bench_stats_reads_no_argv_and_changes_no_environment():
    code = (
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(RETRIEVAL)!r})\n"
        "sys.argv = ['poisoned', '--embed-profile', 'qwen3-4b', '--fusion', 'convex', '--alpha', '0.9', '--rerank', 'on']\n"
        "before = dict(os.environ)\n"
        "path_before = list(sys.path)\n"
        "import bench_stats\n"
        "after = dict(os.environ)\n"
        "print(json.dumps({'env_same': before == after, 'argv_same': sys.argv[1] == '--embed-profile',\n"
        "  'path_same': sys.path == path_before,\n"
        "  'heavy': sorted(m for m in ('beir_bench', 'gestalt_embed_config', 'bench_engine', 'run_retrieval_evals', 'torch', 'gestalt_rank') if m in sys.modules)}))\n"
    )
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("GESTALT_")}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got == {"env_same": True, "argv_same": True, "path_same": True, "heavy": []}


def test_every_public_name_exists_and_beir_bench_re_exports_the_same_objects():
    pytest.importorskip("sqlite_vec")
    import beir_bench as bb

    for name in PUBLIC_NAMES:
        assert hasattr(bs, name), name
        assert getattr(bb, name) is getattr(bs, name), name
    for name in ("bootstrap_ci", "TOPK", "_split_text"):
        assert getattr(bb, name) is getattr(bs, name), name


def test_the_constants_keep_their_published_values():
    assert (bs.SEED, bs.BOOTSTRAP, bs.PERMUTATIONS) == (12345, 10_000, 20_000)


def test_the_block_size_default_equals_the_one_in_resumable():
    import resumable

    assert bs.DEFAULT_BLOCK_SIZE == resumable.DEFAULT_BLOCK_SIZE


def test_ndcg_and_permutation_p_are_the_published_functions():
    assert bs.ndcg_at_10(["a", "b"], {"a": 1}) == 1.0
    assert bs.ndcg_at_10(["x"], {"a": 1}) == 0.0
    p = bs.permutation_p([0.9, 0.8, 0.7, 0.9], [0.1, 0.2, 0.3, 0.2])
    assert set(p) == {"mean_difference", "p_value", "permutations"} and p["permutations"] == 20_000
    assert bs.permutation_p([0.9, 0.8, 0.7, 0.9], [0.1, 0.2, 0.3, 0.2]) == p  # seeded, so repeatable


def test_code_hashes_name_eight_files_with_the_sha256_of_their_bytes():
    got = bs.code_hashes()
    assert set(got) == {"evals/retrieval/beir_bench.py", "evals/retrieval/bench_stats.py", "evals/retrieval/bench_engine.py", "evals/retrieval/fusion.py",
                        "evals/retrieval/resumable.py", "evals/retrieval/run_retrieval_evals.py", "tools/gestalt_embed_config.py",
                        "tools/gestalt_rank.py"}
    for name, digest in got.items():
        assert digest == hashlib.sha256((REPO / name).read_bytes()).hexdigest()


def test_split_text_is_public_with_the_private_name_kept_as_an_alias():
    assert bs._split_text is bs.split_text
    parts = bs.split_text("a " * 3000)
    assert len(parts) > 1 and all(len(x) <= 2000 for x in parts)
    assert bs.split_text("short") == ["short"]
    assert bs.split_text("a" * 5000, 1000, 0)[0] == "a" * 1000  # explicit limits are honoured


def test_environment_records_the_rerank_instruction_and_maxchars_and_the_true_engine_notes(monkeypatch):
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("torch")
    pytest.importorskip("sentence_transformers")
    from conftest import HashEncoder

    monkeypatch.delenv("GESTALT_RERANK_INSTRUCTION", raising=False)
    model = HashEncoder()
    model.device = "cpu"
    env = bs.environment(model, {"model": "qwen3-4b", "depth": 40})
    import gestalt_rank

    assert env["rerank"]["instruction"] == gestalt_rank.qwen_instruction()
    assert env["rerank"]["maxchars"] == gestalt_rank.rerank_maxchars()
    assert "dataset-specific" in env["rerank"]["instruction_note"]
    assert "fuse_pools" in env["fusion"]["call_site"]
    assert "contentless" in env["engine"]["fts"] and "exact" in env["engine"]["dense_note"]
    assert len(env["code_sha256"]) == 8 and env["embedding_load_path"]
    assert set(env["fusion"]) == {"mode", "alpha", "call_site", "resolved"}  # the old keys stay, the resolved knobs are added
    assert env["fusion"]["resolved"] == gestalt_rank.fusion_settings()
    assert set(env["fusion"]["resolved"]) == {"method", "alpha", "w_bm25", "norm", "missing", "rescue", "rrf_k"}
    off = bs.environment(model, None)
    assert off["rerank"]["instruction"] is None and off["rerank"]["maxchars"] is None


def test_importing_beir_bench_reads_no_argv():
    """QAL-001. The flag export runs only when beir_bench is the script, so an importer with a poisoned argv sees no environment change."""
    import os
    import subprocess
    import sys

    code = ("import sys, os; sys.argv = ['x', '--rerank', 'poison']; before = dict(os.environ); "
            "sys.path.insert(0, 'evals/retrieval'); import beir_bench; "
            "print('clean' if dict(os.environ) == before else 'dirty')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=os.getcwd())
    assert out.stdout.strip() == "clean", out.stderr[-500:]
