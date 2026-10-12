"""The MCP server takes query vectors from a running embed daemon instead of loading a second model copy (2026-10-10)."""
from __future__ import annotations

import threading
import time

import numpy as np
from conftest import _load

server = _load("gestalt_mcp_server_daemon_model", "tools/gestalt-mcp-server.py")
daemon = _load("gestalt_embed_daemon_for_server", "tools/gestalt-embed-daemon.py")


def _serve(state_dir, ident):
    stop = threading.Event()
    t = threading.Thread(target=daemon.serve, args=(state_dir, lambda xs: [[0.6, 0.8] for _ in xs], 30.0, stop, ident), daemon=True)
    t.start()
    end = time.monotonic() + 5
    while time.monotonic() < end:
        try:
            daemon.encode([], state_dir, 0.2)
            break
        except OSError:
            time.sleep(0.01)
    return stop, t


def test_no_daemon_means_none_and_no_daemon_is_started(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTALT_STATE_DIR", str(tmp_path))
    assert server.DaemonModel.connect(timeout_s=0.2) is None
    assert not any(tmp_path.iterdir())  # nothing was spawned: no pid file, no socket


def test_a_matching_daemon_answers_and_one_from_another_setup_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTALT_STATE_DIR", str(tmp_path))
    ident = daemon.identity(server._ec)
    stop, t = _serve(tmp_path, ident)
    try:
        m = server.DaemonModel.connect(timeout_s=1.0)
        assert m is not None
        v = server._embed_query(m, "anything")
        assert isinstance(v, np.ndarray) and v.dtype == np.float32 and np.allclose(v, [0.6, 0.8])
        monkeypatch.setenv("GESTALT_SERVER_USE_DAEMON", "0")
        assert server.DaemonModel.connect(timeout_s=1.0) is None
    finally:
        stop.set()
        t.join(5)
    other = {**ident, "model_name": "NovaSearch/stella_en_400M_v5", "embed_dim": 1024}
    monkeypatch.delenv("GESTALT_SERVER_USE_DAEMON")
    stop, t = _serve(tmp_path, other)
    try:
        assert server.DaemonModel.connect(timeout_s=1.0) is None
    finally:
        stop.set()
        t.join(5)
