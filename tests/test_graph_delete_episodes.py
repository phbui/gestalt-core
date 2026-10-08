"""gestalt-graph-delete-episodes.py: identity-list deletion under a human hand.

Fakes both ends in-process: a redis-cli stand-in that answers GRAPH.QUERY from a small dict, and an MCP
server that records delete_episode calls. Asserts the gates (dry run deletes nothing, a missing UUID
aborts, unarmed --execute refuses) and that an armed run deletes exactly the list and verifies after.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "gestalt-graph-delete-episodes.py"

FAKE_CLI = r'''#!/usr/bin/env python3
import json, re, sys
state = json.load(open(sys.argv[1]))
q = sys.argv[-1]
if "count(e)" in q:
    rows = [[len(state), len({v["name"] for v in state.values()})]]
else:
    ids = re.findall(r'"([0-9a-f-]{36})"', q)
    rows = [[u, state[u]["name"], state[u]["created"], state[u]["size"]] for u in ids if u in state]
print(json.dumps([["h"], rows, ["stats"]]))
'''


class FakeMcp(BaseHTTPRequestHandler):
    deleted: list[str] = []
    state_path = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        out: dict = {}
        if body.get("method") == "initialize":
            out = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}
        elif body.get("method") == "tools/call":
            assert self.headers.get("Mcp-Session-Id") == "sess-1"
            u = body["params"]["arguments"]["uuid"]
            FakeMcp.deleted.append(u)
            st = json.load(open(FakeMcp.state_path))
            st.pop(u, None)
            json.dump(st, open(FakeMcp.state_path, "w"))
            out = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": f"Episode {u} deleted"}]}}
        data = ("data: " + json.dumps(out) + "\n\n").encode() if out else b""
        self.send_response(200 if out else 202)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", "sess-1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def rig(tmp_path):
    state = tmp_path / "graph.json"
    graph = {
        "11111111-1111-4111-8111-111111111111": {"name": "kb-a", "created": "2026-09-01", "size": 10},
        "22222222-2222-4222-8222-222222222222": {"name": "kb-a", "created": "2026-09-03", "size": 12},
        "33333333-3333-4333-8333-333333333333": {"name": "kb-b", "created": "2026-09-02", "size": 8},
    }
    json.dump(graph, open(state, "w"))
    cli = tmp_path / "fake-cli.py"
    cli.write_text(FAKE_CLI)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    FakeMcp.deleted = []
    FakeMcp.state_path = str(state)
    srv = ThreadingHTTPServer(("127.0.0.1", port), FakeMcp)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield {"state": state, "env": {"GRAPHITI_URL": f"http://127.0.0.1:{port}", "GESTALT_FALKORDB_CLI": f"python3 {cli} {state}"}}
    finally:
        srv.shutdown()


def _run(rig, listfile: Path, *args: str, armed: bool = False):
    env = dict(os.environ, **rig["env"])
    env.pop("GESTALT_DEDUPE_ARMED", None)
    if armed:
        env["GESTALT_DEDUPE_ARMED"] = "1"
    return subprocess.run(["python3", str(SCRIPT), *args, str(listfile)], env=env, capture_output=True, text=True, timeout=30)


def _list(tmp_path: Path, uuids: list[str]) -> Path:
    p = tmp_path / "list.json"
    p.write_text(json.dumps(uuids))
    return p


def test_dry_run_deletes_nothing_and_names_each_uuid(rig, tmp_path) -> None:
    r = _run(rig, _list(tmp_path, ["11111111-1111-4111-8111-111111111111"]))
    assert r.returncode == 0, r.stderr
    assert "would delete\t11111111-1111-4111-8111-111111111111\tkb-a\t2026-09-01" in r.stdout
    assert FakeMcp.deleted == [] and len(json.load(open(rig["state"]))) == 3


def test_missing_uuid_aborts_the_whole_run(rig, tmp_path) -> None:
    r = _run(rig, _list(tmp_path, ["11111111-1111-4111-8111-111111111111", "99999999-9999-4999-8999-999999999999"]), "--execute", armed=True)
    assert r.returncode == 3 and "MISSING" in r.stdout
    assert FakeMcp.deleted == []


def test_execute_without_arming_refuses(rig, tmp_path) -> None:
    r = _run(rig, _list(tmp_path, ["11111111-1111-4111-8111-111111111111"]), "--execute")
    assert r.returncode == 4 and FakeMcp.deleted == []


def test_armed_execute_deletes_exactly_the_list_and_verifies(rig, tmp_path) -> None:
    r = _run(rig, _list(tmp_path, ["11111111-1111-4111-8111-111111111111"]), "--execute", armed=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert FakeMcp.deleted == ["11111111-1111-4111-8111-111111111111"]
    assert set(json.load(open(rig["state"]))) == {"22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"}
    assert "graph after: 2 episodes, 2 distinct names; deleted 1; still present: 0; excess names: 0" in r.stdout
