"""evals/retrieval/merge_summaries.py: two runs join only when they are the same experiment."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals" / "retrieval"))
import merge_summaries as ms  # noqa: E402

CODE = {"evals/retrieval/beir_bench.py": "a" * 64, "evals/retrieval/bench_engine.py": "b" * 64}


def _summary(name: str, instr: str, **over) -> dict:
    env = {"torch": "2.11.0", "embedding_revision": "e9b6763", "rerank": {"on": True, "model": "qwen3-0.6b", "depth": 40, "instruction": instr},
           "code_sha256": dict(CODE)}
    env.update(over.pop("env", {}))
    res = {"documents": 10, "queries": 3, "smoke_subset": False, "systems": {"hybrid": {"ndcg10": 0.5}}, "tests": {}, "rerank_fallback_queries": 0}
    res.update(over.pop("res", {}))
    s = {"environment": env, "generated_utc": "2026-10-08T22:00:00Z", "results": {name: res}, "smoke_subset": False}
    s.update(over)
    return s


def _write(tmp_path: Path, tag: str, s: dict) -> Path:
    p = tmp_path / tag / "summary.json"
    p.parent.mkdir()
    p.write_text(json.dumps(s))
    return p


def test_two_runs_merge_with_a_per_dataset_instruction(tmp_path):
    a = _write(tmp_path, "a", _summary("beir/scifact", "claims"))
    b = _write(tmp_path, "b", _summary("beir/nfcorpus", "questions"))
    out = tmp_path / "summary.json"
    assert ms.main([str(a), str(b), "--out", str(out)]) == 0
    m = json.loads(out.read_text())
    assert sorted(m["results"]) == ["beir/nfcorpus", "beir/scifact"]
    assert m["environment"]["rerank"]["instruction"] == {"beir/scifact": "claims", "beir/nfcorpus": "questions"}
    assert m["environment"]["code_sha256"] == CODE and m["smoke_subset"] is False
    assert len(m["merged_from"]) == 2


def test_different_code_hashes_are_refused(tmp_path):
    a = _write(tmp_path, "a", _summary("beir/scifact", "claims"))
    b = _write(tmp_path, "b", _summary("beir/nfcorpus", "questions", env={"code_sha256": {**CODE, "evals/retrieval/beir_bench.py": "c" * 64}}))
    with pytest.raises(SystemExit, match="code_sha256"):
        ms.main([str(a), str(b), "--out", str(tmp_path / "s.json")])


def test_a_different_embedding_revision_is_refused(tmp_path):
    a = _write(tmp_path, "a", _summary("beir/scifact", "claims"))
    b = _write(tmp_path, "b", _summary("beir/nfcorpus", "questions", env={"embedding_revision": "other"}))
    with pytest.raises(SystemExit, match="embedding_revision"):
        ms.main([str(a), str(b), "--out", str(tmp_path / "s.json")])


def test_a_fallback_or_a_subset_is_refused(tmp_path):
    a = _write(tmp_path, "a", _summary("beir/scifact", "claims", res={"rerank_fallback_queries": 3}))
    b = _write(tmp_path, "b", _summary("beir/nfcorpus", "questions"))
    with pytest.raises(SystemExit, match="fallbacks"):
        ms.main([str(a), str(b), "--out", str(tmp_path / "s.json")])
    c = _write(tmp_path, "c", _summary("beir/scifact", "claims", smoke_subset=True))
    with pytest.raises(SystemExit, match="smoke"):
        ms.main([str(c), str(b), "--out", str(tmp_path / "s2.json")])


def test_the_same_dataset_twice_is_refused(tmp_path):
    a = _write(tmp_path, "a", _summary("beir/scifact", "claims"))
    b = _write(tmp_path, "b", _summary("beir/scifact", "claims"))
    with pytest.raises(SystemExit, match="twice"):
        ms.main([str(a), str(b), "--out", str(tmp_path / "s.json")])


def test_cli_exit_code_on_refusal(tmp_path):
    a = _write(tmp_path, "a", _summary("beir/scifact", "claims"))
    b = _write(tmp_path, "b", _summary("beir/nfcorpus", "questions", env={"torch": "2.10"}))
    r = subprocess.run([sys.executable, str(Path(ms.__file__)), str(a), str(b), "--out", str(tmp_path / "s.json")], capture_output=True, text=True)
    assert r.returncode == 1 or r.returncode == 2
    assert "torch" in r.stderr
