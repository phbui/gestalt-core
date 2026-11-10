"""SubagentStop hook: VERDICT line check for verifier and reviewer returns."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "claude-tree" / "hooks" / "subagent-output-check.sh"


def run(tmp_path, stdin: str):
    env = {**os.environ, "GESTALT_STATE_DIR": str(tmp_path)}
    r = subprocess.run(["bash", str(HOOK)], input=stdin, text=True, capture_output=True, env=env, timeout=30)
    log = tmp_path / "health.log"
    jl = tmp_path / "subagent-verdicts.jsonl"
    return r, log.read_text() if log.exists() else "", jl.read_text() if jl.exists() else ""


def payload(agent_type, msg):
    return json.dumps({"session_id": "s1", "agent_type": agent_type, "last_assistant_message": msg})


def test_good_verdict_writes_record(tmp_path):
    r, log, jl = run(tmp_path, payload("verifier", "1. ok -- Source: a:1\nVERDICT: PASS — all verified.\n"))
    assert r.returncode == 0
    assert "VERDICT-MISSING" not in log
    rec = json.loads(jl.strip())
    assert (rec["agent_type"], rec["verdict"], rec["sentence"], rec["session_id"]) == ("verifier", "PASS", "all verified.", "s1")


def test_missing_verdict_flagged(tmp_path):
    r, log, jl = run(tmp_path, payload("verifier", "1. ok -- Source: a:1\nAll good."))
    assert r.returncode == 0
    assert "VERDICT-MISSING: subagent (verifier)" in log
    assert jl == ""


def test_malformed_verdict_word_flagged(tmp_path):
    r, log, jl = run(tmp_path, payload("reviewer", "text\nVERDICT: GREAT — fine."))
    assert r.returncode == 0
    assert "VERDICT-MISSING: subagent (reviewer)" in log
    assert jl == ""


def test_general_purpose_not_flagged(tmp_path):
    r, log, jl = run(tmp_path, payload("general-purpose", "no verdict here"))
    assert r.returncode == 0
    assert "VERDICT-MISSING" not in log
    assert jl == ""


def test_garbage_stdin_exits_zero(tmp_path):
    r, log, jl = run(tmp_path, "\x00{not json")
    assert r.returncode == 0
    assert jl == ""
