"""Fixes from the 2026-10-11 line review of the MCP server's search path, the embed daemon and the builder's index_meta. Each test failed first."""
from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time

import pytest
from conftest import _load

server = _load("gestalt_mcp_server_audit", "tools/gestalt-mcp-server.py")
daemon = _load("gestalt_embed_daemon_audit", "tools/gestalt-embed-daemon.py")
indexer = _load("gestalt_index_builder_audit", "tools/gestalt-index-builder.py")


class _Db:
    def __init__(self):
        self.closed = False

    def execute(self, *_a):
        class R:
            def fetchone(self_):
                return (1,)
        return R()

    def close(self):
        self.closed = True


def test_a_local_hybrid_search_falls_back_to_full_text_on_any_model_error(monkeypatch, capsys):
    """Only ImportError was caught. An OSError from an offline model fetch, or a RuntimeError, left the database open and escaped
    gestalt_search instead of answering from full text."""
    db = _Db()
    monkeypatch.setattr(server, "get_db", lambda: db)
    monkeypatch.setattr(server, "_vector_leg_ok", lambda _db: True)
    monkeypatch.setattr(server.DaemonModel, "connect", classmethod(lambda cls, timeout_s=0.5: None))

    def boom():
        raise RuntimeError("model cache offline")
    monkeypatch.setattr(server, "get_model", boom)
    monkeypatch.setattr(server, "gestalt_search_fts", lambda q, limit: [{"slug": "fts-row"}])
    got = server._open_local_hybrid("quokka", 3)
    assert got == (None, None, [{"slug": "fts-row"}]) and db.closed
    assert "model cache offline" in capsys.readouterr().err


def test_the_daemon_client_spends_one_deadline_across_connect_send_and_reply(tmp_path):
    """The socket timeout covered connect and send, and recv_frame then started a fresh one, so a 150 ms call could take 300 ms."""
    sock_path = daemon.paths(tmp_path)[0]
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)

    def silent():
        conn, _ = srv.accept()
        time.sleep(2.0)
        conn.close()
    threading.Thread(target=silent, daemon=True).start()
    t0 = time.monotonic()
    with pytest.raises(OSError):
        daemon.encode(["a"], tmp_path, 0.3)
    assert time.monotonic() - t0 < 0.7
    srv.close()


def test_an_empty_pid_file_younger_than_five_seconds_means_a_daemon_is_starting(tmp_path, monkeypatch):
    """Two spawns a few microseconds apart: the loser read the winner's not-yet-written pid file as stale, unlinked it, and a second
    daemon came up and later removed the first one's socket."""
    pid_path = daemon.paths(tmp_path)[1]
    pid_path.write_text("")
    monkeypatch.setattr(daemon, "load_embedder", lambda: pytest.fail("the loser must not load a model"))
    assert daemon.run(tmp_path, 1.0) == 1
    assert pid_path.exists()


class _EC:
    PROFILE = "demo"
    MODEL_NAME = "m"
    MODEL_REVISION = "r"
    EMBED_DIM = 4
    QUERY_PREFIX = "q: "
    EMBED_NORMALIZE = True
    EMBED_DTYPE = "float16"


def test_the_daemon_identity_carries_normalisation_and_dtype():
    """A daemon spawned under GESTALT_EMBED_NORMALIZE=1 answered a server running with 0: same profile, same model, different vectors."""
    a = daemon.identity(_EC)
    assert a["embed_normalize"] is True and a["embed_dtype"] == "float16"

    class Other(_EC):
        EMBED_NORMALIZE = False
    assert daemon.identity(Other) != a


def _meta_db(rows):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.executemany("INSERT INTO index_meta VALUES (?, ?)", rows.items())
    return db


def test_the_vector_leg_check_reads_normalisation_and_dtype_when_the_index_stores_them(monkeypatch):
    ec = server._ec
    base = {"model_name": ec.MODEL_NAME, "model_revision": ec.MODEL_REVISION or "", "embed_dim": str(ec.EMBED_DIM),
            "doc_prefix": ec.DOC_PREFIX, "query_prefix": ec.QUERY_PREFIX}
    monkeypatch.setattr(server, "_meta_logged", set())
    assert server._vector_leg_ok(_meta_db({**base, "embed_normalize": "1" if ec.EMBED_NORMALIZE else "0"})) is True
    assert server._vector_leg_ok(_meta_db({**base, "embed_normalize": "0" if ec.EMBED_NORMALIZE else "1"})) is False
    monkeypatch.setattr(server, "_meta_logged", set())
    assert server._vector_leg_ok(_meta_db({**base, "embed_dtype": "not-the-dtype"})) is False
    assert server._vector_leg_ok(_meta_db(base)) is True  # an older index without the keys is still accepted


def test_the_builder_records_normalisation_and_dtype():
    ec = indexer._ec  # the builder's own config object, whatever profile an earlier test left under the shared name
    db = sqlite3.connect(":memory:")
    indexer.write_index_meta(db, True)
    meta = dict(db.execute("SELECT key, value FROM index_meta").fetchall())
    assert meta["embed_normalize"] == ("1" if ec.EMBED_NORMALIZE else "0") and meta["embed_dtype"] == str(ec.EMBED_DTYPE)


def test_a_refused_daemon_is_logged_once(tmp_path, monkeypatch, capsys):
    """connect() returned None in silence when the daemon's identity differed, and the server then loaded its own copy of the model
    inside a live search with no line in the log."""
    monkeypatch.setenv("GESTALT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_daemon_logged", set())
    stop = threading.Event()
    t = threading.Thread(target=daemon.serve, args=(tmp_path, lambda xs: [[0.6, 0.8] for _ in xs], 30.0, stop, {"embed_dim": 999}), daemon=True)
    t.start()
    end = time.monotonic() + 5
    while time.monotonic() < end:
        try:
            daemon.encode([], tmp_path, 0.2)
            break
        except OSError:
            time.sleep(0.01)
    assert server.DaemonModel.connect(timeout_s=0.5) is None
    assert server.DaemonModel.connect(timeout_s=0.5) is None
    err = capsys.readouterr().err
    assert err.count("embed daemon") == 1 and "identity" in err
    stop.set()
    t.join(timeout=3)
