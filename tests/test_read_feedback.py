"""Click feedback: read rows, the preceding query, the aggregator and the replay read rate (spec 6, 2026-10-08). Hermetic."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load  # noqa: E402
from test_harness_fidelity import _DUP_SECTIONS, _build_fixture_db  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def srv(tmp_path, monkeypatch):
    mod = _load("gms_read_feedback", "tools/gestalt-mcp-server.py")
    db = tmp_path / "gestalt.db"
    _build_fixture_db(db, _DUP_SECTIONS)
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "retrieval-fusion.md").write_text("# Fusion\nbody\n")
    (kdir / "fts-tokenisation.md").write_text("# Tokens\nbody\n")
    (kdir / "vault.md").write_text("---\nsensitivity: restricted\n---\n# Vault\nsecret\n")
    log = tmp_path / "hits.jsonl"
    monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(mod, "KNOWLEDGE_DIR", kdir)
    monkeypatch.setenv("GESTALT_HITS_LOG", str(log))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.delenv("GESTALT_HIT_SRC", raising=False)
    mod._RECENT_SEARCHES.clear()
    return mod, log


def _rows(log):
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def test_a_search_row_carries_sid_kind_src_and_rr(srv):
    mod, log = srv
    mod.gestalt_search_fts("rank fusion vector", limit=3)
    row = _rows(log)[0]
    assert (row["kind"], row["src"], row["sid"], row["rr"]) == ("search", "mcp", "sess-1", None)
    assert row["q"] == "rank fusion vector" and row["slugs"]


def test_the_hook_marks_its_searches(srv, monkeypatch):
    mod, log = srv
    monkeypatch.setenv("GESTALT_HIT_SRC", "hook")
    mod.gestalt_search_fts("rank fusion vector", limit=3)
    assert _rows(log)[0]["src"] == "hook"


def test_a_read_after_a_search_names_the_preceding_query(srv):
    mod, log = srv
    mod.gestalt_search_fts("rank fusion vector", limit=3)
    assert mod.read_entry("retrieval-fusion").startswith("# Fusion")
    read = _rows(log)[-1]
    assert read["kind"] == "read" and read["slug"] == "retrieval-fusion"
    assert read["preceding_q"] == "rank fusion vector" and read["sid"] == "sess-1"


def test_a_read_with_no_search_has_a_null_preceding_query(srv):
    mod, log = srv
    mod.read_entry("retrieval-fusion")
    assert _rows(log)[-1]["preceding_q"] is None


def test_the_window_is_600_seconds(srv, monkeypatch):
    mod, log = srv
    t = [1_000_000.0]
    monkeypatch.setattr(mod.time, "time", lambda: t[0])
    mod.gestalt_search_fts("rank fusion vector", limit=3)
    t[0] += 599
    mod.read_entry("retrieval-fusion")
    assert _rows(log)[-1]["preceding_q"] == "rank fusion vector"
    t[0] += 2
    mod.read_entry("retrieval-fusion")
    assert _rows(log)[-1]["preceding_q"] is None


def test_the_most_recent_search_that_held_the_entry_wins(srv):
    mod, log = srv
    mod.gestalt_search_fts("rank fusion vector", limit=3)
    mod.gestalt_search_fts("fusion constant damps", limit=3)
    mod.gestalt_search_fts("quoted terms tokenisation", limit=1)  # does not hold retrieval-fusion
    mod.read_entry("retrieval-fusion")
    assert _rows(log)[-1]["preceding_q"] == "fusion constant damps"


def test_the_recent_search_deque_keeps_twenty(srv):
    mod, _ = srv
    for i in range(25):
        mod._remember_search(f"q{i}", ["retrieval-fusion"])
    assert len(mod._RECENT_SEARCHES) == 20 and mod._RECENT_SEARCHES[0][1] == "q5"


def test_a_withheld_missing_or_invalid_read_writes_nothing(srv):
    mod, log = srv
    assert "withheld" in mod.read_entry("vault")
    assert "not found" in mod.read_entry("no-such-entry")
    assert "Invalid slug" in mod.read_entry("../etc/passwd")
    assert _rows(log) == []
    assert "secret" in mod.read_entry("vault", include_restricted=True)
    assert _rows(log)[-1]["slug"] == "vault"


def test_a_disabled_log_writes_no_file(srv, monkeypatch):
    mod, log = srv
    monkeypatch.setenv("GESTALT_HITS_LOG", "off")
    mod.gestalt_search_fts("rank fusion vector", limit=3)
    mod.read_entry("retrieval-fusion")
    assert not log.exists()


def test_the_mcp_tool_is_a_thin_wrapper_over_read_entry(srv):
    import inspect
    mod, _ = srv
    src = inspect.getsource(mod.build_mcp)
    assert "return read_entry(slug, include_restricted)" in src


# --- aggregator ----------------------------------------------------------------------------------------

def test_the_aggregator_keeps_read_rows(tmp_path):
    if not (REPO / "tools" / "gestalt-hit-aggregate.sh").exists():
        pytest.skip("the fleet aggregator is not in this tree (the public export leaves it out)")
    home = tmp_path / "home"
    (home / ".claude" / "gestalt").mkdir(parents=True)
    rows = [{"ts": 1, "q": "a b", "slugs": ["s"], "kind": "search", "sid": "x", "src": "mcp", "rr": None},
            {"ts": 2, "kind": "read", "slug": "s", "preceding_q": "a b", "sid": "x"}]
    (home / ".claude" / "gestalt" / "retrieval-hits.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    # A stub `hostname` first on PATH fixes the node name, so the test does not depend on the machine it runs on.
    me = "testnode"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "hostname"
    stub.write_text(f"#!/bin/sh\necho {me}\n")
    stub.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "FLEET_NODES": me, "TMPDIR": str(tmp_path), "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    out = subprocess.run(["sh", str(REPO / "tools" / "gestalt-hit-aggregate.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    merged = [json.loads(x) for x in (home / ".claude" / "gestalt" / "retrieval-hits-fleet.jsonl").read_text().splitlines()]
    assert [m.get("kind") for m in merged] == ["search", "read"]
    assert merged[1]["preceding_q"] == "a b" and merged[1]["node"] == me
    assert "(1 read rows)" in out.stdout


# --- replay ----------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def replay():
    return _load("replay_captured_t", "evals/retrieval/replay_captured.py")


def _s(ts, q, slugs, sid="x", src="mcp"):
    return {"ts": ts, "q": q, "slugs": slugs, "sid": sid, "src": src}


def _r(ts, slug, q=None, sid="x"):
    return {"ts": ts, "kind": "read", "slug": slug, "preceding_q": q, "sid": sid}


def test_read_rate_joins_by_sid_or_by_preceding_query_and_skips_hook_searches(replay):
    searches = [_s(100, "q1", ["a", "b", "c"]), _s(200, "q2", ["d", "e"], sid="other"), _s(300, "q3", ["f"], src="hook"), _s(400, "q4", ["g"])]
    reads = [_r(110, "c"), _r(210, "e", q="q2", sid=None)]
    rr = replay.read_rates(searches, reads)
    assert rr["n"] == 3, "the hook search is not an event"
    assert rr["hits"] == {1: 0, 3: 2, 5: 2}, "c is third in q1 and e is second in q2"
    assert rr["no_read"] == 1


def test_read_rate_ignores_a_read_from_another_session_with_another_query(replay):
    searches = [_s(100, "q1", ["a", "b"], sid="x")]
    assert replay.read_rates(searches, [_r(120, "a", q="something else", sid="y")])["hits"][5] == 0
    assert replay.read_rates(searches, [_r(120, "a", q="q1", sid="y")])["hits"][5] == 0, "two session ids that differ never join, whatever the query says"
    assert replay.read_rates(searches, [_r(120, "a", q="q1", sid=None)])["hits"][5] == 1, "the preceding query joins only when a session id is absent"
    assert replay.read_rates(searches, [_r(120, "a", q="something else", sid="x")])["hits"][5] == 1, "the session id wins over the query"
    assert replay.read_rates(searches, [_r(120, "a", q=None, sid="x")])["hits"][5] == 1, "the session id joins when the query is missing"


def test_read_rate_ignores_a_read_outside_the_window_or_of_another_entry(replay):
    searches = [_s(100, "q1", ["a", "b"])]
    assert replay.read_rates(searches, [_r(100 + 601, "a")])["hits"][5] == 0
    assert replay.read_rates(searches, [_r(50, "a")])["hits"][5] == 0
    assert replay.read_rates(searches, [_r(120, "zzz")])["hits"][5] == 0
    assert replay.read_rates(searches, [_r(120, "a")])["hits"][1] == 1


def test_load_log_skips_read_rows_and_load_reads_keeps_them(replay, tmp_path, monkeypatch):
    d = tmp_path / ".claude" / "gestalt"
    d.mkdir(parents=True)
    (d / "retrieval-hits.jsonl").write_text(json.dumps(_s(1, "q", ["a"])) + "\n" + json.dumps(_r(2, "a", "q")) + "\n")
    monkeypatch.setattr(replay, "HOME", tmp_path)
    assert [r["q"] for r in replay.load_log(False)] == ["q"]
    assert [r["slug"] for r in replay.load_reads(False)] == ["a"]


def test_the_report_refuses_below_min_n(replay, tmp_path, monkeypatch, capsys):
    d = tmp_path / ".claude" / "gestalt"
    d.mkdir(parents=True)
    (d / "retrieval-hits.jsonl").write_text(json.dumps(_s(1, "q", ["a"])) + "\n")
    monkeypatch.setattr(replay, "HOME", tmp_path)
    replay.report_read_rate(False, 10)
    assert "refusing to report a rate" in capsys.readouterr().out
    replay.report_read_rate(False, 1)
    assert "read-rate@1" in capsys.readouterr().out


# --- reviewer fixes (F1, 2026-10-08) -----------------------------------------------------------------------

def test_a_restricted_hit_reaches_neither_the_log_nor_the_recent_searches(srv, monkeypatch):
    mod, log = srv
    with sqlite3.connect(mod.DB_PATH) as c:
        c.execute("UPDATE sections_meta SET sensitivity = 'restricted' WHERE slug = 'fts-tokenisation'")
    rows = mod.gestalt_search_fts("tokenisation quoted terms", limit=3)
    assert any(r.get("withheld") for r in rows), "the caller still hears that one was withheld"
    logged = [s for r in _rows(log) for s in r.get("slugs", [])]
    assert "fts-tokenisation" not in logged
    assert all("fts-tokenisation" not in e[2] for e in mod._RECENT_SEARCHES)
    mod.gestalt_search_fts("tokenisation quoted terms", limit=3, include_restricted=True)
    assert "fts-tokenisation" in [s for r in _rows(log) for s in r.get("slugs", [])], "include_restricted is honoured"


def test_the_hub_relay_does_not_remember_a_withheld_notice(srv):
    mod, _ = srv
    mod._remember_search("q", mod._loggable_slugs([{"slug": "vault", "withheld": "restricted"}, {"slug": "ok"}]))
    assert mod._RECENT_SEARCHES[-1][2] == ["ok"]


def test_a_read_ignores_another_sessions_search(srv, monkeypatch):
    mod, log = srv
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    mod.gestalt_search_fts("rank fusion vector", limit=3)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-B")
    mod.read_entry("retrieval-fusion")
    assert _rows(log)[-1]["preceding_q"] is None
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    mod.read_entry("retrieval-fusion")
    assert _rows(log)[-1]["preceding_q"] == "rank fusion vector"


def test_on_the_shared_hub_the_slug_test_is_the_guard(srv, monkeypatch):
    mod, log = srv
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")  # every caller has the pid sid
    mod.gestalt_search_fts("rank fusion vector", limit=3)
    mod.read_entry("fts-tokenisation")
    assert _rows(log)[-1]["preceding_q"] is None, "that search did not hold this slug"


class _Hostile:
    """A deque stand-in that blows up when iterated, as a deque does when another thread appends mid-scan."""
    def __iter__(self):
        raise RuntimeError("deque mutated during iteration")

    def __reversed__(self):
        raise RuntimeError("deque mutated during iteration")


def test_a_read_survives_a_mutating_search_log(srv, monkeypatch):
    mod, log = srv
    monkeypatch.setattr(mod, "_RECENT_SEARCHES", _Hostile())
    assert mod.read_entry("retrieval-fusion").startswith("# Fusion")
    assert _rows(log)[-1]["preceding_q"] is None


@pytest.mark.parametrize("value", ["off", "OFF", "Off", "", "0", "disabled"])
def test_the_off_sentinel_disables_every_hit_log_write(srv, monkeypatch, value):
    mod, log = srv
    monkeypatch.setenv("GESTALT_HITS_LOG", value)
    monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: log.parent / "home"))  # an empty value must not fall back to the real home
    mod.gestalt_search_fts("rank fusion vector", limit=3)
    mod.read_entry("retrieval-fusion")
    assert not log.exists() and not (log.parent / "home").exists()
