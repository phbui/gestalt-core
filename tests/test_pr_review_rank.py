"""PR review fixes in tools/gestalt_rank.py and tools/gestalt-mcp-server.py: QAL-002, QAL-003, SEC-006, SEC-011, QAL-008, SEC-012."""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gestalt_rank as gr  # noqa: E402
from conftest import _load  # noqa: E402


@pytest.fixture(scope="module")
def server():
    return _load("gestalt_mcp_server_prreview", "tools/gestalt-mcp-server.py")


# --- QAL-002 ---------------------------------------------------------------------------------------

def test_a_missing_fts_table_is_logged_and_flagged(capsys):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    found = gr.hybrid_search(db, "alpha beta", 3, embed_query=lambda q: b"", mode="fts", rerank=False)
    assert found.fts_failed is True and found.rows == []
    assert "FTS leg failed" in capsys.readouterr().err


def test_a_healthy_fts_table_leaves_the_flag_false():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE VIRTUAL TABLE sections_fts USING fts5(body)")
    found = gr.hybrid_search(db, "alpha", 3, embed_query=lambda q: b"", mode="fts", rerank=False)
    assert found.fts_failed is False


# --- QAL-003 ---------------------------------------------------------------------------------------

def test_log_prefix_follows_the_module_variable(monkeypatch, capsys):
    monkeypatch.setattr(gr, "LOG_PREFIX", "gestalt")
    gr._log("one")
    monkeypatch.setattr(gr, "LOG_PREFIX", "gestalt-eval")
    gr._log("two")
    err = capsys.readouterr().err
    assert "gestalt: one" in err and "gestalt-eval: two" in err


def test_the_server_and_the_runner_set_their_prefix():
    assert 'gestalt_rank.LOG_PREFIX = "gestalt-eval"' in (REPO / "evals" / "retrieval" / "run_retrieval_evals.py").read_text()
    assert 'gestalt_rank.LOG_PREFIX = "gestalt-mcp"' in (REPO / "tools" / "gestalt-mcp-server.py").read_text()


# --- SEC-006 ---------------------------------------------------------------------------------------

def test_every_known_alias_has_a_pinned_forty_hex_revision():
    assert set(gr.RERANK_REVISIONS) == set(gr.RERANK_MODELS)
    for alias, (hub_id, rev) in gr.RERANK_REVISIONS.items():
        assert hub_id == gr.RERANK_MODELS[alias] and len(rev) == 40 and int(rev, 16) >= 0


def test_unknown_alias_is_rejected_unless_allow_any(monkeypatch):
    monkeypatch.delenv("GESTALT_RERANK_ALLOW_ANY", raising=False)
    with pytest.raises(ValueError, match="GESTALT_RERANK_ALLOW_ANY"):
        gr.pinned_model("someone/else-reranker")
    monkeypatch.setenv("GESTALT_RERANK_ALLOW_ANY", "1")
    assert gr.pinned_model("someone/else-reranker") == ("someone/else-reranker", None)
    assert gr.pinned_model("bge") == gr.RERANK_REVISIONS["bge"], "known aliases stay pinned under allow-any"


def test_get_reranker_passes_the_revision_and_rejects_unknown(monkeypatch):
    seen = {}

    class FakeCE:
        def __init__(self, name, **kw):
            seen["name"], seen["kw"] = name, kw

    fake = type(sys)("sentence_transformers")
    fake.CrossEncoder = FakeCE
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setattr(gr, "_rerankers", {})
    monkeypatch.setattr(gr, "_load_failed", {})
    monkeypatch.setattr(gr, "throttle_calls", lambda *a, **k: None)
    monkeypatch.delenv("GESTALT_RERANK_ALLOW_ANY", raising=False)
    monkeypatch.setenv("GESTALT_RERANK_DEVICE", "cpu")
    gr.get_reranker("bge")
    assert seen["name"] == "BAAI/bge-reranker-v2-m3" and seen["kw"]["revision"] == gr.RERANK_REVISIONS["bge"][1]
    with pytest.raises(ValueError):
        gr.get_reranker("nope/unknown")
    monkeypatch.setenv("GESTALT_RERANK_ALLOW_ANY", "1")
    gr.get_reranker("nope/unknown")
    assert seen["name"] == "nope/unknown" and seen["kw"]["revision"] is None


# --- SEC-011 ---------------------------------------------------------------------------------------

def test_hit_log_is_created_owner_only(server, tmp_path, monkeypatch):
    path = tmp_path / "sub" / "hits.jsonl"
    monkeypatch.setenv("GESTALT_HITS_LOG", str(path))
    server._write_hit_row({"q": "x"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text().count("\n") == 1


def test_hit_log_rotates_at_the_cap(server, tmp_path, monkeypatch):
    path = tmp_path / "hits.jsonl"
    monkeypatch.setenv("GESTALT_HITS_LOG", str(path))
    monkeypatch.setattr(server, "HITS_MAX_BYTES", 100)
    path.write_text("x" * 150)
    server._write_hit_row({"q": "new"})
    assert (tmp_path / "hits.jsonl.1").read_text() == "x" * 150
    assert path.read_text().strip() == '{"q": "new"}' and path.stat().st_mode & 0o777 == 0o600


# --- QAL-008 ---------------------------------------------------------------------------------------

def test_the_dedup_log_line_is_written_in_one_place():
    src = (REPO / "tools" / "gestalt-mcp-server.py").read_text()
    assert src.count("dedup decay=") == 1


# --- SEC-012 ---------------------------------------------------------------------------------------

def test_load_watchdog_logs_when_a_load_runs_long_and_stays_quiet_otherwise(monkeypatch, capsys):
    monkeypatch.setattr(gr, "LOAD_SLOW_S", 0.05)
    t = gr.load_watchdog("slow model")
    t.join(1)
    assert "slow model load still holds its lock" in capsys.readouterr().err
    quick = gr.load_watchdog("quick model")
    quick.cancel()
    assert isinstance(quick, threading.Timer) and quick.daemon
    assert "quick model" not in capsys.readouterr().err
