"""The resident query embedder (tools/gestalt-embed-daemon.py). Hermetic: a stub encoder, never a model. Every daemon this file starts is stopped."""
from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import REPO, _load  # noqa: E402

daemon = _load("gestalt_embed_daemon", "tools/gestalt-embed-daemon.py")
SCRIPT = REPO / "tools" / "gestalt-embed-daemon.py"


def stub_embed(texts):
    return [[float(len(t)), 1.0, 0.5] for t in texts]


def start_thread(state_dir, idle_s=30.0, embed=stub_embed):
    stop = threading.Event()
    box = {}
    t = threading.Thread(target=lambda: box.update(reason=daemon.serve(state_dir, embed, idle_s, stop)), daemon=True)
    t.start()
    end = time.monotonic() + 5
    while time.monotonic() < end:  # the file exists at bind. Listening starts a moment later, so wait for a connect
        try:
            daemon.encode([], state_dir, 0.2)
            break
        except OSError:
            time.sleep(0.01)
    return stop, t, box


def cli(state_dir, *args, timeout=30):
    env = dict(os.environ, GESTALT_EMBED_DAEMON_STUB="1")
    return subprocess.run([sys.executable, str(SCRIPT), "--state-dir", str(state_dir), *args], capture_output=True, text=True, timeout=timeout, env=env)


def test_framing_round_trip():
    a, b = socket.socketpair()
    with a, b:
        daemon.send_frame(a, {"texts": ["café", "two"], "n": [1, 2.5]})
        assert daemon.recv_frame(b) == {"texts": ["café", "two"], "n": [1, 2.5]}
        a.sendall(struct.pack(">I", 20) + b"{\"a\":")  # a frame cut short must time out and not hang
        with pytest.raises(TimeoutError):
            daemon.recv_frame(b, timeout_s=0.2)


def test_oversize_frame_is_refused():
    a, b = socket.socketpair()
    with a, b:
        a.sendall(struct.pack(">I", daemon.MAX_FRAME + 1))
        with pytest.raises(ValueError):
            daemon.recv_frame(b)


def test_encode_over_the_socket(tmp_path):
    stop, t, _ = start_thread(tmp_path)
    try:
        assert daemon.encode(["ab", "abcd"], tmp_path, 2.0) == [[2.0, 1.0, 0.5], [4.0, 1.0, 0.5]]
        assert daemon.encode([], tmp_path, 2.0) == []
        assert oct(daemon.paths(tmp_path)[0].stat().st_mode & 0o777) == "0o600"
    finally:
        stop.set()
        t.join(5)
    assert not daemon.paths(tmp_path)[0].exists()


def test_bad_request_gets_an_error_and_the_daemon_lives_on(tmp_path):
    stop, t, _ = start_thread(tmp_path)
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(daemon.paths(tmp_path)[0]))
            daemon.send_frame(s, {"texts": "not a list"})
            assert "error" in daemon.recv_frame(s)
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(daemon.paths(tmp_path)[0]))
            daemon.send_frame(s, {"texts": ["x"] * (daemon.MAX_TEXTS + 1)})
            assert "error" in daemon.recv_frame(s)
        assert daemon.encode(["ok"], tmp_path, 2.0) == [[2.0, 1.0, 0.5]]
    finally:
        stop.set()
        t.join(5)


def test_encoder_failure_is_an_error_reply(tmp_path):
    def boom(texts):
        raise RuntimeError("no model")

    stop, t, _ = start_thread(tmp_path, embed=boom)
    try:
        with pytest.raises(RuntimeError, match="no model"):
            daemon.encode(["x"], tmp_path, 2.0)
    finally:
        stop.set()
        t.join(5)


def test_no_daemon_means_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        daemon.encode(["x"], tmp_path, 1.0)


def test_idle_exit(tmp_path):
    stop, t, box = start_thread(tmp_path, idle_s=0.4)
    t.join(5)
    assert not t.is_alive() and box["reason"] == "idle"
    assert not daemon.paths(tmp_path)[0].exists()


def test_a_request_resets_the_idle_clock(tmp_path):
    stop, t, box = start_thread(tmp_path, idle_s=0.6)
    try:
        for _ in range(3):
            time.sleep(0.3)
            daemon.encode(["x"], tmp_path, 1.0)
        assert t.is_alive()
    finally:
        stop.set()
        t.join(5)
    assert box["reason"] == "stop"


def test_status_and_stop_of_a_real_process(tmp_path):
    assert cli(tmp_path, "--status").returncode == 1
    assert "not running" in cli(tmp_path, "--stop").stdout
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "--state-dir", str(tmp_path), "--idle-s", "60"], env=dict(os.environ, GESTALT_EMBED_DAEMON_STUB="1"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sock_path, pid_path, _ = daemon.paths(tmp_path)
    try:
        end = time.monotonic() + 20
        while not sock_path.exists() and time.monotonic() < end:
            time.sleep(0.05)
        assert sock_path.exists()
        st = cli(tmp_path, "--status")
        assert st.returncode == 0 and f"pid {proc.pid}" in st.stdout and "running" in st.stdout
        vecs = daemon.encode(["hello world"], tmp_path, 2.0)
        assert len(vecs) == 1 and len(vecs[0]) > 100  # the stub fills the profile's embedding width
        second = subprocess.run([sys.executable, str(SCRIPT), "--state-dir", str(tmp_path)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, GESTALT_EMBED_DAEMON_STUB="1"))
        assert second.returncode == 1 and "already running" in second.stderr  # the pid file is claimed exclusively
        stopped = cli(tmp_path, "--stop")
        assert stopped.returncode == 0 and f"stopped pid {proc.pid}" in stopped.stdout
        assert proc.wait(10) is not None
        assert not sock_path.exists() and not pid_path.exists()
        assert cli(tmp_path, "--status").returncode == 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_stop_never_signals_a_pid_that_is_not_the_daemon(tmp_path):
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        daemon.paths(tmp_path)[1].write_text(str(bystander.pid))
        assert "not running" in cli(tmp_path, "--stop").stdout
        assert bystander.poll() is None
        assert not daemon.paths(tmp_path)[1].exists()
    finally:
        bystander.kill()
        bystander.wait()


def test_spawn_declines_without_a_venv(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTALT_VENV_PYTHON", str(tmp_path / "missing" / "python3"))
    assert daemon.spawn(tmp_path) is False
    assert not daemon.paths(tmp_path)[1].exists()


def test_a_deep_state_dir_still_gets_a_usable_socket_path(tmp_path):
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    sock = daemon.paths(deep)[0]
    assert len(str(sock).encode()) <= daemon.SOCK_PATH_MAX and sock != deep / "embed-daemon.sock"
    stop, th, _ = start_thread(deep)
    try:
        assert daemon.encode(["ab"], deep, 2.0) == [[2.0, 1.0, 0.5]]
    finally:
        stop.set()
        th.join(5)
    assert not sock.exists()
