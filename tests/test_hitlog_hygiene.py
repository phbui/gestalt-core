"""Hit-log hygiene and the degraded marker (2026-11-09). Hermetic: synthetic index, no model, no hub, no live log."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load  # noqa: E402
from test_harness_fidelity import _DUP_SECTIONS, _build_fixture_db  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
Q = "rank fusion vector"


@pytest.fixture
def srv(tmp_path, monkeypatch):
    mod = _load("gms_hitlog_hygiene", "tools/gestalt-mcp-server.py")
    db = tmp_path / "gestalt.db"
    _build_fixture_db(db, _DUP_SECTIONS)
    monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(mod, "KNOWLEDGE_DIR", tmp_path)
    mod._RECENT_SEARCHES.clear()
    return mod


def _conn(path):
    import sqlite_vec
    c = sqlite3.connect(path)
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.row_factory = sqlite3.Row
    return c


# --- A: hit-log hygiene -----------------------------------------------------------------------------------

def test_the_suite_defaults_the_hit_log_off():
    """Guard: if conftest stops defaulting the log off, the live log takes fixture slugs again."""
    assert os.environ.get("GESTALT_HITS_LOG", "").strip().lower() in ("off", "0", "disabled", ""), "conftest must default GESTALT_HITS_LOG to off"
    assert "GESTALT_HITS_LOG" in (REPO / "tests" / "conftest.py").read_text()
    mod = _load("gms_hitlog_gate", "tools/gestalt-mcp-server.py")
    assert mod._hits_path() is None, "with the suite default no write path exists"


def test_replay_leaves_the_log_it_reads_unchanged(srv, tmp_path, monkeypatch):
    replay = _load("replay_captured_hyg", "evals/retrieval/replay_captured.py")
    log = tmp_path / ".claude" / "gestalt" / "retrieval-hits.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"ts": 1, "q": Q, "slugs": ["retrieval-fusion"], "kind": "search", "sid": "x", "src": "mcp"}) + "\n")
    monkeypatch.setattr(replay, "HOME", tmp_path)
    monkeypatch.setenv("GESTALT_HITS_LOG", str(log))  # the worst case: the server's own gate points at the log replay reads
    # replay loads the server itself; hand it the fixture-backed copy instead of the real index
    monkeypatch.setattr("importlib.util.spec_from_file_location", lambda *a, **k: types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda m: None)))
    monkeypatch.setattr("importlib.util.module_from_spec", lambda spec: srv)
    monkeypatch.setattr(sys, "argv", ["replay_captured.py", "--min-n", "1"])
    before = len(log.read_text().splitlines())
    replay.main()
    assert len(log.read_text().splitlines()) == before, "replay appended to the log it reads"


@pytest.mark.parametrize("src", ["hook", "eval", "test"])
def test_non_usage_sources_are_excluded_from_read_rates(src):
    replay = _load("replay_captured_hyg2", "evals/retrieval/replay_captured.py")
    s = [{"ts": 100, "q": "q", "slugs": ["a"], "sid": "x", "src": src}, {"ts": 100, "q": "q2", "slugs": ["a"], "sid": "x", "src": "mcp"}]
    assert replay.read_rates(s, [])["n"] == 1


def test_read_rows_carry_src(srv, tmp_path, monkeypatch):
    log = tmp_path / "hits.jsonl"
    (tmp_path / "note.md").write_text("# Note\nbody\n")
    monkeypatch.setenv("GESTALT_HITS_LOG", str(log))
    monkeypatch.setenv("GESTALT_HIT_SRC", "eval")
    srv.read_entry("note")
    assert json.loads(log.read_text().splitlines()[0])["src"] == "eval"


# --- B: degraded marker -----------------------------------------------------------------------------------

def _search(srv):
    return srv.gestalt_search(Q, limit=3, semantic=True)


def _marks(rows):
    assert rows and all("slug" in r for r in rows)
    return {r.get("degraded") for r in rows}


def test_no_vectors_table_marks_rows(srv, monkeypatch):
    c = _conn(srv.DB_PATH)
    c.execute("DROP TABLE sections_vec")
    c.commit()
    c.close()
    monkeypatch.setattr(srv, "get_db", lambda: _conn(srv.DB_PATH))
    assert _marks(_search(srv)) == {"fts-only:no-vectors-table"}


def test_index_model_mismatch_marks_rows_and_prints(srv, monkeypatch, capsys):
    def db():
        c = _conn(srv.DB_PATH)
        c.execute("CREATE TABLE IF NOT EXISTS sections_vec (id INTEGER)")
        return c
    monkeypatch.setattr(srv, "get_db", db)
    monkeypatch.setattr(srv, "_vector_leg_ok", lambda _db: False)
    assert _marks(_search(srv)) == {"fts-only:index-model-mismatch"}
    assert "mismatch" in capsys.readouterr().err


def test_import_error_marks_rows(srv, monkeypatch):
    def boom():
        raise ImportError("sqlite_vec")
    monkeypatch.setattr(srv, "get_db", boom)
    assert _marks(_search(srv)) == {"fts-only:import-error"}


def test_model_load_failure_marks_rows(srv, monkeypatch):
    def db():
        c = _conn(srv.DB_PATH)
        c.execute("CREATE TABLE IF NOT EXISTS sections_vec (id INTEGER)")
        return c
    def boom():
        raise RuntimeError("offline")
    monkeypatch.setattr(srv, "get_db", db)
    monkeypatch.setattr(srv, "_vector_leg_ok", lambda _db: True)
    monkeypatch.setattr(srv.DaemonModel, "connect", classmethod(lambda cls, timeout_s=0.5: None))
    monkeypatch.setattr(srv, "get_model", boom)
    assert _marks(_search(srv)) == {"fts-only:model-load-failed:RuntimeError"}


def test_hub_relay_failure_on_a_leaf_marks_rows(srv, monkeypatch):
    def boom(*a, **k):
        raise OSError("hub down")
    monkeypatch.setattr(srv, "_hub_mcp_url", lambda: "http://hub.invalid")
    monkeypatch.setattr(srv, "_hub_looks_down", lambda: False)
    monkeypatch.setattr(srv, "_hub_search", boom)
    monkeypatch.setattr(srv, "_leaf_may_load_model", lambda: False)
    assert _marks(_search(srv)) == {"fts-only:hub-relay-failed:OSError"}


def test_leaf_without_a_local_model_marks_rows(srv, monkeypatch):
    monkeypatch.setattr(srv, "_leaf_may_load_model", lambda: False)
    assert _marks(_search(srv)) == {"fts-only:leaf-no-local-model"}


def test_fts_leg_failure_marks_rows(srv, monkeypatch):
    found = types.SimpleNamespace(fusion="rrf", alpha=None, pool=1, rerank=None, decay=None, rows=[{"slug": "s"}], fts_failed=True)
    monkeypatch.setattr(srv, "_open_local_hybrid", lambda q, l: (_conn(srv.DB_PATH), object(), None))
    monkeypatch.setattr(srv.gestalt_rank, "hybrid_search", lambda *a, **k: found)
    monkeypatch.setattr(srv, "_search_row", lambda meta, f, q: {"slug": meta["slug"], "content": "x", "score": 1.0})
    assert _marks(_search(srv)) == {"dense-only:fts-failed"}


def test_mode_forced_fts_is_not_marked(srv, monkeypatch):
    assert _marks(srv.gestalt_search(Q, limit=3, semantic=False)) == {None}
    monkeypatch.setattr(srv, "search_mode", lambda: "fts")
    assert _marks(srv.gestalt_search(Q, limit=3)) == {None}
