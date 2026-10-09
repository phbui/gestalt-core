"""The stop hook queues a repeated fact once and records every session that said it.

auto-promote needs two sessions on one fact. The hook used to drop a repeat, so no fact ever held two.
The test drives the real hook twice with two session ids. Task D runs in the background, so the test polls the queue file for a short, bounded time.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STOP_HOOK = REPO / "claude-tree" / "hooks" / "gestalt-stop.sh"
FACT = "Actually the billing database schema migration runs through the staging pipeline first"


def _transcript(tmp_path: Path) -> Path:
    rows = []
    for i in range(3):
        rows.append({"type": "user", "message": {"content": f"{FACT}, turn {i}."}})
        rows.append({"type": "assistant", "message": {"content": f"Noted the schema detail number {i} here."}})
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _run(tmp_path: Path, session_id: str, queue: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    fake = tmp_path / "fake_gestalt"
    (fake / "tools").mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    health = tmp_path / "hub-health-up"
    health.write_text(f"{int(time.time())} 1 1\n", encoding="utf-8")
    env = {**os.environ, "GESTALT_DIR": str(fake), "GESTALT_WORKSPACE": str(workspace),
           "GESTALT_STATE_DIR": str(state), "GESTALT_HUB_HEALTH_FILE": str(health),
           "GESTALT_PROMOTE_QUEUE_PATH": str(queue), "LETTA_URL": "http://127.0.0.1:9/v1",
           "GRAPHITI_URL": "http://127.0.0.1:9"}
    stdin = json.dumps({"transcript_path": str(_transcript(tmp_path)), "session_id": session_id})
    r = subprocess.run(["bash", str(STOP_HOOK)], input=stdin, capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr


def _wait_for(queue: Path, ready) -> list[dict]:
    deadline = time.time() + 5
    rows: list[dict] = []
    while time.time() < deadline:
        if queue.exists():
            try:
                rows = json.loads(queue.read_text())
            except ValueError:
                rows = []
            if ready(rows):
                return rows
        time.sleep(0.02)
    return rows


def test_same_fact_in_two_sessions_holds_two_session_ids(tmp_path):
    queue = tmp_path / "queue.json"
    _run(tmp_path, "sess-aaaa", queue)
    first = _wait_for(queue, lambda rows: bool(rows))
    assert first and all(r["sessions"] == ["sess-aaaa"] for r in first), first
    _run(tmp_path, "sess-bbbb", queue)
    rows = _wait_for(queue, lambda rs: bool(rs) and all(len(r.get("sessions", [])) == 2 for r in rs))
    assert rows and all(r["sessions"] == ["sess-aaaa", "sess-bbbb"] for r in rows), rows
    assert all("last_seen" in r for r in rows)
    assert len({r["fact"][:80].lower() for r in rows}) == len(rows)


def test_same_session_twice_is_not_double_counted(tmp_path):
    queue = tmp_path / "queue.json"
    _run(tmp_path, "sess-aaaa", queue)
    _wait_for(queue, lambda rows: bool(rows))
    before = queue.read_text()
    _run(tmp_path, "sess-aaaa", queue)
    # The hook waits for its background jobs, so the second run has finished writing when _run returns.
    rows = json.loads(queue.read_text())
    assert before and rows
    assert rows and all(r["sessions"] == ["sess-aaaa"] for r in rows), rows
