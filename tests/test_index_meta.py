"""X14: index_meta is written by the builder and checked by the server (2026-10-06)."""
import sqlite3

import pytest

from conftest import _load


@pytest.fixture(scope="module")
def srv():
    return _load("gestalt_mcp_server_meta", "tools/gestalt-mcp-server.py")


def _db(rows=None):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    if rows is not None:
        db.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.executemany("INSERT INTO index_meta VALUES (?, ?)", rows.items())
    return db


def test_config_shared(srv, indexer):
    import gestalt_embed_config as ec
    assert indexer.MODEL_NAME == srv.MODEL_NAME == ec.MODEL_NAME
    assert indexer.MODEL_REVISION == ec.MODEL_REVISION
    assert indexer.DOC_PREFIX == ec.DOC_PREFIX and indexer.QUERY_PREFIX == ec.QUERY_PREFIX


def test_builder_writes_meta(indexer):
    import gestalt_embed_config as ec
    db = sqlite3.connect(":memory:")
    indexer.write_index_meta(db, True)
    meta = dict(db.execute("SELECT key, value FROM index_meta").fetchall())
    assert meta["model_name"] == ec.MODEL_NAME and meta["model_revision"] == (ec.MODEL_REVISION or "")
    assert meta["embed_dim"] == str(ec.EMBED_DIM) and meta["built_at"].isdigit()
    assert meta["doc_prefix"] == ec.DOC_PREFIX and meta["query_prefix"] == ec.QUERY_PREFIX


def test_fts_only_build_writes_empty_table(indexer):
    db = sqlite3.connect(":memory:")
    indexer.write_index_meta(db, False)
    assert db.execute("SELECT count(*) FROM index_meta").fetchone()[0] == 0


def test_missing_table_proceeds_and_logs_once(srv, capsys):
    srv._meta_logged.clear()
    assert srv._vector_leg_ok(_db()) is True
    assert srv._vector_leg_ok(_db()) is True
    assert capsys.readouterr().err.count("no index_meta") == 1


def test_matching_table_ok(srv, indexer):
    db = sqlite3.connect(":memory:")
    indexer.write_index_meta(db, True)
    db.row_factory = sqlite3.Row
    assert srv._vector_leg_ok(db) is True


def test_mismatch_disables_vector_leg(srv, capsys):
    srv._meta_logged.clear()
    db = _db({"model_name": "other/model", "embed_dim": "768"})
    assert srv._vector_leg_ok(db) is False
    assert "vector leg disabled" in capsys.readouterr().err
