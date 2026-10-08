"""Injection log of the per-prompt hook (X7, 2026-10-06). Hermetic."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load  # noqa: E402

hook = _load("prompt_intelligence_log", "claude-tree/hooks/prompt-intelligence.py")

RESULTS = {
    "gestalt": [{"source": "gestalt", "key": "gestalt:home-mesh#seats", "text": "SECRET TEXT"}],
    "graphiti": [{"source": "graphiti", "key": "graphiti:a private fact about someone", "text": "x"}],
    "letta": [],
}


def test_record_shape_and_no_prompt_text():
    rec = hook.build_injection_record("my private prompt", RESULTS, [RESULTS["gestalt"][0], RESULTS["graphiti"][0]],
                                      {"gestalt": True, "graphiti": False, "letta": True}, now=100)
    assert set(rec) == {"ts", "prompt_sha", "answered", "returned", "items"}
    assert rec["ts"] == 100 and len(rec["prompt_sha"]) == 16
    assert rec["answered"] == {"gestalt": True, "graphiti": False, "letta": True}
    assert rec["items"][0] == {"source": "gestalt", "id": "home-mesh#seats", "rank": 1, "merged_rank": 1}
    blob = json.dumps(rec)
    assert "private prompt" not in blob and "private fact" not in blob and "SECRET" not in blob


def test_log_appends_one_line_per_prompt(tmp_path):
    for i in range(3):
        hook.log_injection(str(tmp_path), {"ts": i})
    lines = (tmp_path / "injections.jsonl").read_text().splitlines()
    assert [json.loads(x)["ts"] for x in lines] == [0, 1, 2]


def test_log_fails_open(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    hook.log_injection(str(blocker / "sub"), {"ts": 1})  # parent is a file: must not raise
    assert capsys.readouterr().out == ""  # stdout is the hook protocol: nothing may leak into it


def test_log_is_capped_by_size(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "INJECTION_LOG_MAX_BYTES", 2000)
    for i in range(200):
        hook.log_injection(str(tmp_path), {"ts": i, "pad": "x" * 40})
    p = tmp_path / "injections.jsonl"
    assert p.stat().st_size < 4000
    assert all(json.loads(x) for x in p.read_text().splitlines())  # still whole lines
    assert json.loads(p.read_text().splitlines()[-1])["ts"] == 199
