"""evals/retrieval/reproduce.py: the command derived from summary.json carries every knob the stored run recorded."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals" / "retrieval"))
import reproduce as rp  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
_OVERLAY = REPO / "publish" / "overlay" / "evals" / "retrieval" / "results" / "summary.json"
SUMMARY = _OVERLAY if _OVERLAY.exists() else REPO / "evals" / "retrieval" / "results" / "summary.json"  # the export has no publish/


def _summary():
    return json.loads(SUMMARY.read_text())


def test_plan_carries_the_reranker_its_instruction_and_the_precision(tmp_path):
    rows = rp.plan(_summary(), tmp_path)
    assert [r["dataset"] for r in rows] == ["beir/nfcorpus", "beir/scifact"]
    sf = rows[1]
    assert "--rerank" in sf["argv"] and sf["argv"][sf["argv"].index("--rerank-model") + 1] == "qwen3-0.6b"
    assert sf["env"]["GESTALT_RERANK_INSTRUCTION"].startswith("Given a scientific claim")
    assert sf["env"]["GESTALT_EMBED_DTYPE"] == "float32" and sf["env"]["GESTALT_RERANK_DTYPE"] == "float16"
    assert sf["env"]["GESTALT_EMBED_NORMALIZE"] == "0" and sf["env"]["GESTALT_FUSION"] == "rrf"
    assert set(sf["stored"]) == set(rp.HEADLINE)


def test_fast_flips_only_the_embed_dtype(tmp_path):
    slow, fast = rp.plan(_summary(), tmp_path)[1], rp.plan(_summary(), tmp_path, fast=True)[1]
    assert fast["env"]["GESTALT_EMBED_DTYPE"] == "float16"
    assert {k: v for k, v in fast["env"].items() if k != "GESTALT_EMBED_DTYPE"} == {k: v for k, v in slow["env"].items() if k != "GESTALT_EMBED_DTYPE"}
    assert fast["argv"] == slow["argv"]


def test_smoke_limits_documents_and_shell_line_quotes_the_instruction(tmp_path):
    row = rp.plan(_summary(), tmp_path, smoke=True)[1]
    assert row["argv"][row["argv"].index("--limit-docs") + 1] == "200"
    line = rp.shell_line(row)
    assert line.startswith("env ") and "GESTALT_RERANK_INSTRUCTION='Given a scientific claim" in line


def test_compare_passes_within_tolerance_and_fails_a_missing_system():
    stored = {"bm25": 0.6824, "hybrid_rerank": 0.7748}
    rows = rp.compare(stored, {"bm25": 0.6827}, 0.0005)
    assert rows[0][3] is True and rows[1][2] is None and rows[1][3] is False
    assert rp.compare(stored, {"bm25": 0.6830, "hybrid_rerank": 0.7748}, 0.0005)[0][3] is False


def test_print_mode_runs_nothing(capsys, tmp_path):
    rc = rp.main(["--summary", str(SUMMARY), "--out", str(tmp_path), "--print"])
    out = capsys.readouterr().out
    assert rc == 0 and out.count("beir_bench.py") == 2 and "--rerank on" in out


def test_smoke_mode_runs_both_sets_and_never_claims_a_match(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(rp.subprocess, "call", lambda argv, env=None: (calls.append(argv), 0)[1])
    rc = rp.main(["--summary", str(SUMMARY), "--out", str(tmp_path), "--smoke", "--skip-check-env"])
    out = capsys.readouterr().out
    assert rc == 0 and len(calls) == 2 and all("--limit-docs" in c for c in calls)
    assert "never compared" in out and "matched" not in out
