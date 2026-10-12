#!/usr/bin/env python3
"""A resident query embedder for the per-prompt hook.

The hook cannot load the 137M nomic model on every prompt. A cold load costs about 5 seconds on CPU. A warm encode costs about 40 ms. This daemon holds the model and answers over a unix socket.

One request type. The client sends {"texts": [...]}. The daemon returns {"vectors": [[...], ...]}, one query vector per text, or {"error": "..."}. Each text is embedded the way the MCP server embeds a query: the profile prefix, then the profile post-processing. It uses the server's own functions, so the two cannot drift.

Framing is a 4 byte big-endian length, then that many bytes of UTF-8 JSON. A frame over 1 MB is refused.

The hook starts the daemon lazily with spawn(). The daemon exits after GESTALT_EMBED_DAEMON_IDLE_S seconds without a request (default 900). It loads the model before it binds the socket, so a socket that exists means a warm daemon.

Runtime files live in the hook state directory, $GESTALT_STATE_DIR or <workspace>/.claude/gestalt:
  embed-daemon.sock  the socket, mode 0600. A path over 100 bytes moves it to the temp directory under a short hashed name
  embed-daemon.pid   the pid, created exclusively so two starts cannot both win
  embed-daemon.log   stderr of a spawned daemon

Commands:
  gestalt-embed-daemon.py            run in the foreground
  gestalt-embed-daemon.py --status   print the state and exit 0 if running, else 1
  gestalt-embed-daemon.py --stop     stop it and remove its files

The daemon runs in the gestalt venv, since that holds sentence-transformers. GESTALT_VENV_PYTHON overrides the path. GESTALT_EMBED_DAEMON_STUB=1 swaps the model for a hash encoder, for tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

MAX_FRAME = 1 << 20
MAX_TEXTS = 64
DEFAULT_IDLE_S = 900
REQUEST_TIMEOUT_S = 5.0


def default_state_dir() -> Path:
    """The hook's STATE_DIR rule from hooks/lib.sh: $GESTALT_STATE_DIR, else <workspace>/.claude/gestalt."""
    if os.environ.get("GESTALT_STATE_DIR"):
        return Path(os.environ["GESTALT_STATE_DIR"]).expanduser()
    workspace = os.environ.get("GESTALT_WORKSPACE") or str(Path(__file__).resolve().parent.parent.parent)
    return Path(workspace) / ".claude" / "gestalt"


SOCK_PATH_MAX = 100  # AF_UNIX allows 107 bytes on Linux and 103 on macOS


def paths(state_dir) -> tuple[Path, Path, Path]:
    """(socket, pid file, log). A state directory too deep for a unix socket path gets a short socket name in the temp directory instead."""
    d = Path(state_dir)
    sock = d / "embed-daemon.sock"
    if len(str(sock).encode()) > SOCK_PATH_MAX:
        sock = Path(tempfile.gettempdir()) / f"gestalt-embed-{os.getuid()}-{hashlib.sha1(str(d).encode()).hexdigest()[:10]}.sock"
    return sock, d / "embed-daemon.pid", d / "embed-daemon.log"


# --- framing -------------------------------------------------------------------------------------

def send_frame(conn: socket.socket, obj) -> None:
    body = json.dumps(obj).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ValueError("frame too large")
    conn.sendall(struct.pack(">I", len(body)) + body)


def _recv_exact(conn: socket.socket, n: int, deadline: float) -> bytes:
    buf = b""
    while len(buf) < n:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("frame read past its deadline")
        conn.settimeout(left)
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed mid-frame")
        buf += chunk
    return buf


def recv_frame(conn: socket.socket, timeout_s: float = REQUEST_TIMEOUT_S):
    deadline = time.monotonic() + timeout_s
    (n,) = struct.unpack(">I", _recv_exact(conn, 4, deadline))
    if n > MAX_FRAME:
        raise ValueError("frame too large")
    return json.loads(_recv_exact(conn, n, deadline).decode("utf-8"))


# --- client --------------------------------------------------------------------------------------

class IdentityMismatch(RuntimeError):
    """The daemon encodes with another embedding setup than the caller expects (a profile swap while it was alive)."""


def identity(ec) -> dict:
    """What a query vector depends on: the profile, the model, its revision, the width, the query prefix, and since 2026-10-11 the
    normalisation and the dtype (GESTALT_EMBED_NORMALIZE and GESTALT_EMBED_DTYPE differ per process). Both ends build it from their own config."""
    return {"profile": getattr(ec, "PROFILE", None), "model_name": ec.MODEL_NAME, "model_revision": getattr(ec, "MODEL_REVISION", None) or "",
            "embed_dim": int(ec.EMBED_DIM), "query_prefix": getattr(ec, "QUERY_PREFIX", ""),
            "embed_normalize": bool(getattr(ec, "EMBED_NORMALIZE", False)), "embed_dtype": str(getattr(ec, "EMBED_DTYPE", "float32"))}


def encode(texts: list[str], state_dir, timeout_s: float, expect: dict | None = None) -> list[list[float]]:
    """Ask the daemon for query vectors. Raises on any failure, including FileNotFoundError or ConnectionRefusedError when no daemon is up.

    With expect (an identity() dict), a daemon that answers with another identity, or with none (an older daemon), raises
    IdentityMismatch: until 2026-10-10 a daemon started under the old profile kept encoding for up to its idle time after a swap,
    and the hook silently fell back to full text."""
    sock_path = paths(state_dir)[0]
    deadline = time.monotonic() + timeout_s  # one budget for connect, send and reply; before 2026-10-11 the reply got a fresh timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(max(timeout_s, 0.001))
        s.connect(str(sock_path))
        send_frame(s, {"texts": texts})
        resp = recv_frame(s, max(deadline - time.monotonic(), 0.001))
    if "vectors" not in resp:
        raise RuntimeError(str(resp.get("error", "bad response")))
    if expect is not None and resp.get("identity") != expect:
        raise IdentityMismatch(f"daemon encodes with {resp.get('identity')}, the caller expects {expect}")
    return resp["vectors"]


def _read_pid(state_dir) -> int | None:
    try:
        return int(paths(state_dir)[1].read_text().strip())
    except (OSError, ValueError):
        return None


def _is_daemon(pid: int) -> bool:
    """True when pid is alive and, where /proc exists, is this script. A reused pid is never ours to signal."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return b"gestalt-embed-daemon" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True


def venv_python() -> str:
    return os.environ.get("GESTALT_VENV_PYTHON") or str(Path.home() / ".claude" / "gestalt" / "venv" / "bin" / "python3")


def spawn(state_dir) -> bool:
    """Start a detached daemon unless one is running or loading. True when a start was issued. Never raises."""
    try:
        if (pid := _read_pid(state_dir)) is not None and _is_daemon(pid):
            return False
        py = venv_python()
        if not Path(py).exists():
            return False
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        with open(paths(state_dir)[2], "ab") as log:
            subprocess.Popen([py, str(Path(__file__).resolve()), "--state-dir", str(state_dir)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=log, start_new_session=True)
        return True
    except Exception:
        return False


# --- server --------------------------------------------------------------------------------------

CACHE_MAX = 512  # query vectors kept; a repeated prompt (hooks re-ask the same short queries often) costs a dict lookup


class QueryCache:
    """Most-recently-used query vectors, keyed by the exact text. One daemon has one identity, so the text is the whole key."""

    def __init__(self, embed, size: int = CACHE_MAX):
        from collections import OrderedDict

        self.embed, self.size, self.store, self.hits = embed, size, OrderedDict(), 0

    def __call__(self, texts: list[str]) -> list:
        missing = [t for t in dict.fromkeys(texts) if t not in self.store]
        if missing:
            for t, v in zip(missing, self.embed(missing)):
                self.store[t] = v.tolist() if hasattr(v, "tolist") else list(v)
        self.hits += len(texts) - len(missing)
        out = []
        for t in texts:
            self.store.move_to_end(t)
            out.append(self.store[t])
        while len(self.store) > self.size:
            self.store.popitem(last=False)
        return out


def _reply(texts, embed, ident: dict | None = None) -> dict:
    if not isinstance(texts, list) or len(texts) > MAX_TEXTS or not all(isinstance(t, str) for t in texts):
        return {"error": f"texts must be a list of at most {MAX_TEXTS} strings"}
    try:
        vecs = embed(texts) if texts else []
        out = {"vectors": [v.tolist() if hasattr(v, "tolist") else list(v) for v in vecs]}
        if ident is not None:
            out["identity"] = ident
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def serve(state_dir, embed, idle_s: float, stop: threading.Event | None = None, ident: dict | None = None) -> str:
    """Bind the socket and answer until idle_s seconds pass without a request, or stop is set. Returns "idle" or "stop"."""
    stop = stop or threading.Event()
    sock_path = paths(state_dir)[0]
    sock_path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        srv.listen(4)
        srv.settimeout(0.2)
        last = time.monotonic()
        while not stop.is_set():
            if time.monotonic() - last >= idle_s:
                return "idle"
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            with conn:
                try:
                    req = recv_frame(conn)
                    send_frame(conn, _reply(req.get("texts") if isinstance(req, dict) else None, embed, ident))
                except Exception:
                    pass  # a broken client costs this daemon nothing
            last = time.monotonic()
        return "stop"
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)


def load_embedder():
    """The server's own embedder: its get_model and its _embed_query, so prefix and post-processing match the index."""
    if os.environ.get("GESTALT_EMBED_DAEMON_STUB") == "1":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gestalt_embed_config as ec

        def stub(texts):
            out = []
            for t in texts:
                v = [0.0] * ec.EMBED_DIM
                for w in t.lower().split():
                    v[zlib.crc32(w.encode()) % ec.EMBED_DIM] += 1.0
                n = sum(x * x for x in v) ** 0.5 or 1.0
                out.append([x / n for x in v])
            return out
        return stub
    os.environ["GESTALT_MODEL_IDLE_S"] = "0"  # this daemon owns its idle exit. The server's unload timer must not drop the model
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gestalt_mcp_server  # noqa: F401  registers _gestalt_mcp_server_impl

    impl = sys.modules["_gestalt_mcp_server_impl"]
    model = impl.get_model()
    return lambda texts: [impl._embed_query(model, t) for t in texts]


def run(state_dir, idle_s: float) -> int:
    sock_path, pid_path, _ = paths(state_dir)
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if (pid := _read_pid(state_dir)) is not None and _is_daemon(pid):
                print(f"already running, pid {pid}", file=sys.stderr)
                return 1
            try:
                young = time.time() - pid_path.stat().st_mtime < 5
            except OSError:
                young = False
            if pid is None and young:  # another starter holds the file and has not written its pid yet (two spawns within microseconds)
                print("another daemon is starting", file=sys.stderr)
                return 1
            pid_path.unlink(missing_ok=True)  # a stale file from a dead daemon
    else:
        return 1
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        embed = QueryCache(load_embedder())
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gestalt_embed_config as ec

        ident = identity(ec)
        print(f"gestalt-embed-daemon: ready, pid {os.getpid()}, idle exit {idle_s:g}s, {ident['model_name']} at {ident['embed_dim']}", file=sys.stderr, flush=True)
        print(f"gestalt-embed-daemon: exit on {serve(state_dir, embed, idle_s, stop, ident)}", file=sys.stderr, flush=True)
        return 0
    finally:
        pid_path.unlink(missing_ok=True)
        sock_path.unlink(missing_ok=True)


def status(state_dir) -> int:
    sock_path = paths(state_dir)[0]
    pid = _read_pid(state_dir)
    if pid is None or not _is_daemon(pid):
        print("not running")
        return 1
    try:
        encode([], state_dir, 2.0)
    except Exception:
        print(f"loading, pid {pid}, no socket answer yet")
        return 0
    print(f"running, pid {pid}, socket {sock_path}")
    return 0


def stop(state_dir, wait_s: float = 10.0) -> int:
    sock_path, pid_path, _ = paths(state_dir)
    pid = _read_pid(state_dir)
    if pid is not None and _is_daemon(pid):
        os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + wait_s
        while time.monotonic() < end and _is_daemon(pid):
            time.sleep(0.05)
        if _is_daemon(pid):
            os.kill(pid, signal.SIGKILL)
        print(f"stopped pid {pid}")
    else:
        print("not running")
    pid_path.unlink(missing_ok=True)
    sock_path.unlink(missing_ok=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Resident query embedder for the per-prompt hook.")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--idle-s", type=float, default=float(os.environ.get("GESTALT_EMBED_DAEMON_IDLE_S", DEFAULT_IDLE_S)))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--stop", action="store_true")
    g.add_argument("--spawn", action="store_true", help="start a detached daemon unless one is running, then return at once")
    a = ap.parse_args(argv)
    state_dir = Path(a.state_dir).expanduser() if a.state_dir else default_state_dir()
    if a.status:
        return status(state_dir)
    if a.stop:
        return stop(state_dir)
    if a.spawn:
        spawn(state_dir)
        return 0
    return run(state_dir, a.idle_s)


if __name__ == "__main__":
    sys.exit(main())
