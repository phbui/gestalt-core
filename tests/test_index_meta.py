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


def test_builder_writes_text_format_profile_and_pooling(indexer):
    import gestalt_embed_config as ec
    db = sqlite3.connect(":memory:")
    indexer.write_index_meta(db, True, "late")
    meta = dict(db.execute("SELECT key, value FROM index_meta").fetchall())
    assert meta["text_format"] == ec.TEXT_FORMAT == "v2-title"
    assert meta["embed_profile"] == ec.PROFILE and meta["pooling"] == "late"


def test_fts_only_build_records_the_text_layout_but_no_model_identity(indexer):
    import gestalt_embed_config as ec
    db = sqlite3.connect(":memory:")
    indexer.write_index_meta(db, False)
    meta = dict(db.execute("SELECT key, value FROM index_meta").fetchall())
    assert meta == {"text_format": ec.TEXT_FORMAT, "embed_profile": ec.PROFILE, "embed_dim": str(ec.EMBED_DIM)}


def test_fts_only_meta_does_not_disable_the_vector_leg(srv, indexer):
    """The server reads only the keys it knows. An FTS-only index carries no model_name, so nothing can mismatch."""
    db = sqlite3.connect(":memory:")
    indexer.write_index_meta(db, False)
    db.row_factory = sqlite3.Row
    assert srv._vector_leg_ok(db) is True


@pytest.fixture
def db_file(tmp_path, monkeypatch, indexer):
    path = tmp_path / "gestalt.db"
    monkeypatch.setattr(indexer, "DB_PATH", path, raising=False)
    kd = tmp_path / "knowledge"
    kd.mkdir()
    (kd / "a.md").write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(indexer, "KNOWLEDGE_DIR", kd, raising=False)
    monkeypatch.setattr(indexer, "RULES_DIR", tmp_path / "rules", raising=False)
    import os, time
    future = time.time() + 3600

    def make(meta):
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE sections_meta (id INTEGER PRIMARY KEY, content_hash TEXT, title TEXT NOT NULL DEFAULT '')")
        db.execute("INSERT INTO sections_meta (content_hash) VALUES ('x')")
        db.execute("CREATE TABLE sections_vec (rowid INTEGER PRIMARY KEY)")  # stands in for the vec0 table, as in test-index-vector-leg.py
        if meta is not None:
            db.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.executemany("INSERT INTO index_meta VALUES (?, ?)", meta.items())
        db.commit()
        db.close()
        os.utime(path, (future, future))
        return path

    return make


def _current(indexer):
    db = sqlite3.connect(":memory:")
    indexer.write_index_meta(db, False)
    return dict(db.execute("SELECT key, value FROM index_meta").fetchall())


def test_needs_rebuild_notices_a_text_format_change(db_file, indexer):
    db_file(_current(indexer))
    assert indexer.needs_rebuild()[0] is False
    import os
    os.remove(indexer.DB_PATH)
    db_file(dict(_current(indexer), text_format="v1"))
    assert indexer.needs_rebuild()[0] is True
    os.remove(indexer.DB_PATH)
    stale = _current(indexer)
    del stale["text_format"]
    db_file(dict(stale, model_name="m"))  # an X14-era index: model keys, no text_format
    assert indexer.needs_rebuild()[0] is True


def test_needs_rebuild_leaves_an_index_without_meta_to_the_mtime_rules(db_file, indexer):
    db_file(None)
    assert indexer.needs_rebuild()[0] is False
    import os
    os.remove(indexer.DB_PATH)
    db_file({})
    assert indexer.needs_rebuild()[0] is False


def test_needs_rebuild_notices_a_profile_or_width_change_only_when_vectors_are_expected(db_file, indexer):
    meta = dict(_current(indexer), embed_profile="qwen3-4b", embed_dim="2560")
    db_file(meta)
    assert indexer.needs_rebuild(expect_vectors=True)[0] is True
    assert indexer.needs_rebuild(expect_vectors=False)[0] is False


def test_needs_rebuild_notices_a_pooling_change(db_file, indexer, monkeypatch):
    db_file(dict(_current(indexer), pooling="standard"))
    assert indexer.needs_rebuild(expect_vectors=True)[0] is False
    monkeypatch.setenv("GESTALT_LATE_CHUNKING", "1")
    assert indexer.needs_rebuild(expect_vectors=True)[0] is True


def test_needs_rebuild_calls_an_old_index_without_title_columns_stale_even_with_empty_meta(db_file, indexer):
    """An FTS-only index from before the title columns has an empty index_meta. It must not be served until a file moves."""
    import os
    path = db_file({})
    assert indexer.needs_rebuild()[0] is False
    os.remove(path)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sections_meta (id INTEGER PRIMARY KEY, slug TEXT, content_hash TEXT)")
    db.execute("INSERT INTO sections_meta (slug, content_hash) VALUES ('a', 'x')")
    db.execute("CREATE VIRTUAL TABLE sections_fts USING fts5(slug, heading, block_id, content)")
    db.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.commit()
    db.close()
    future = os.stat(path).st_mtime + 3600
    os.utime(path, (future, future))
    assert indexer.needs_rebuild()[0] is True
    os.remove(path)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sections_meta (id INTEGER PRIMARY KEY, slug TEXT, content_hash TEXT, title TEXT NOT NULL DEFAULT '')")
    db.execute("INSERT INTO sections_meta (slug, content_hash) VALUES ('a', 'x')")
    db.execute("CREATE VIRTUAL TABLE sections_fts USING fts5(slug, heading, block_id, content)")
    db.commit()
    db.close()
    os.utime(path, (future, future))
    assert indexer.needs_rebuild()[0] is True, "a sections_fts without a title column is stale on its own"


def test_late_index_without_the_normalized_flag_is_stale(db_file, indexer, monkeypatch):
    monkeypatch.setenv("GESTALT_LATE_CHUNKING", "1")
    db_file(dict(_current(indexer), pooling="late"))
    assert indexer.needs_rebuild(expect_vectors=True)[0] is True
    import os
    os.remove(indexer.DB_PATH)
    db_file(dict(_current(indexer), pooling="late", normalized="1"))
    assert indexer.needs_rebuild(expect_vectors=True)[0] is False


def test_late_meta_records_normalized_and_default_meta_does_not(indexer):
    late, std = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    indexer.write_index_meta(late, True, "late")
    indexer.write_index_meta(std, True)
    assert dict(late.execute("SELECT key, value FROM index_meta"))["normalized"] == "1"
    assert "normalized" not in dict(std.execute("SELECT key, value FROM index_meta"))


def test_profile_switch_changes_the_meta_and_the_other_profile_disables_the_vector_leg(srv, indexer, monkeypatch, capsys):
    from test_matryoshka import fresh_config
    qwen = fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b")
    nomic_db = sqlite3.connect(":memory:")
    indexer.write_index_meta(nomic_db, True)
    nomic_meta = dict(nomic_db.execute("SELECT key, value FROM index_meta").fetchall())

    monkeypatch.setattr(indexer, "_ec", qwen)
    for name in ("MODEL_NAME", "MODEL_REVISION", "DOC_PREFIX", "QUERY_PREFIX"):
        monkeypatch.setattr(indexer, name, getattr(qwen, name))
    qwen_db = sqlite3.connect(":memory:")
    indexer.write_index_meta(qwen_db, True)
    qwen_meta = dict(qwen_db.execute("SELECT key, value FROM index_meta").fetchall())
    for key in ("embed_profile", "model_name", "model_revision", "embed_dim", "doc_prefix", "query_prefix"):
        assert nomic_meta[key] != qwen_meta[key], key

    # The server running on nomic reads the qwen index and refuses it. The reverse holds too.
    qwen_db.row_factory = sqlite3.Row
    srv._meta_logged.clear()
    assert srv._vector_leg_ok(qwen_db) is False
    assert "vector leg disabled" in capsys.readouterr().err
    monkeypatch.setattr(srv, "_ec", qwen)
    nomic_db.row_factory = sqlite3.Row
    srv._meta_logged.clear()
    assert srv._vector_leg_ok(nomic_db) is False


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


def test_load_path_is_recorded_when_the_config_exposes_it(indexer, monkeypatch):
    import gestalt_embed_config as ec
    monkeypatch.setattr(ec, "LOAD_PATH", "remote-code", raising=False)
    with_vectors, fts_only = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    indexer.write_index_meta(with_vectors, True)
    indexer.write_index_meta(fts_only, False)
    assert dict(with_vectors.execute("SELECT key, value FROM index_meta"))["load_path"] == "remote-code"
    assert "load_path" not in dict(fts_only.execute("SELECT key, value FROM index_meta"))


def test_load_path_is_left_out_when_the_config_has_none(indexer, monkeypatch):
    import gestalt_embed_config as ec
    monkeypatch.delattr(ec, "LOAD_PATH", raising=False)
    db = sqlite3.connect(":memory:")
    indexer.write_index_meta(db, True)
    assert "load_path" not in dict(db.execute("SELECT key, value FROM index_meta"))


def test_load_path_never_makes_an_index_stale(db_file, indexer, monkeypatch):
    import gestalt_embed_config as ec
    import os
    monkeypatch.setattr(ec, "LOAD_PATH", "native", raising=False)
    db_file(dict(_current(indexer), load_path="remote-code"))
    assert indexer.needs_rebuild(expect_vectors=True)[0] is False
    os.remove(indexer.DB_PATH)
    db_file(_current(indexer))  # an index built before the key existed
    assert indexer.needs_rebuild(expect_vectors=True)[0] is False
