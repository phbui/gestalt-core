"""Ledger hardening for tools/gestalt-graphiti-sync.sh (roadmap N4, N5, X5, L6, defect B-04, 2026-10-06).

Hermetic: a temp STATE_DIR, a fake HOME, a stub Graphiti MCP endpoint on a random local port and a stub FalkorDB
CLI. Nothing here reads or writes the real ledger under ~/Documents/GitHub/.claude/gestalt/.
"""
import fcntl
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SYNC = REPO / "tools" / "gestalt-graphiti-sync.sh"
sys.path.insert(0, str(REPO / "tools"))
from graphiti_sync import ledger  # noqa: E402


class Stub:
    """Graphiti stand-in. Records the chunk name of every add_memory. `fail` names are answered with an error."""

    def __init__(self):
        self.received, self.fail = [], set()
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                req = json.loads(body or b"{}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if req.get("method") == "initialize":
                    self.send_header("mcp-session-id", "stub-session")
                    out = {"jsonrpc": "2.0", "id": 1, "result": {}}
                elif req.get("method") == "tools/call":
                    name = req["params"]["arguments"]["name"]
                    if name in stub.fail:
                        out = {"jsonrpc": "2.0", "id": req["id"], "result": {"isError": True}}
                    else:
                        stub.received.append(name)
                        out = {"jsonrpc": "2.0", "id": req["id"], "result": {"isError": False}}
                else:
                    out = {}
                data = json.dumps(out, separators=(",", ":")).encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


FAKE_CLI = '''#!/usr/bin/env python3
"""redis-cli stand-in. LANDED lists the names the graph holds. FAKE_CLI_DOWN=1 makes it fail like a missing hub."""
import json, os, re, sys
if os.environ.get("FAKE_CLI_DOWN") == "1":
    sys.stderr.write("Error: No such container: gestalt-falkordb\\n"); sys.exit(1)
landed = set(os.environ.get("LANDED", "").split())
names = json.loads(re.search(r"IN (\\[.*\\]) RETURN", sys.argv[-1]).group(1))
print(json.dumps([["e.name"], [[n] for n in names if n in landed], ["stats"]]))
'''


@pytest.fixture
def rig(tmp_path):
    stub = Stub()
    kd = tmp_path / "knowledge"; kd.mkdir()
    (kd / "fixture.md").write_text("---\ntitle: Fixture\n---\n\n## Alpha\n" + "alpha body line\n" * 40 + "## Beta\n" + "beta body line\n" * 40)
    home = tmp_path / "home"; home.mkdir()
    cli = tmp_path / "fake-redis-cli"; cli.write_text(FAKE_CLI); cli.chmod(0o755)
    state = tmp_path / "state" / "graphiti-sync.json"
    env = {k: v for k, v in os.environ.items() if k not in ("GESTALT_SYNC_EXIT75", "GESTALT_SYNC_SKIP_NAMES")}
    env.update(HOME=str(home), GESTALT_KNOWLEDGE_DIR=str(kd), GESTALT_GRAPHITI_SYNC_STATE=str(state),
               GRAPHITI_URL=stub.url, GESTALT_SYNC_CHUNK_CHARS="700", GESTALT_FALKORDB_CLI=str(cli))
    yield type("Rig", (), {"env": env, "state": state, "stub": stub, "tmp": tmp_path, "log": state.parent / "backfill.log"})
    stub.close()


def run(rig, *args, **extra):
    env = dict(rig.env, **extra)
    return subprocess.run(["bash", str(SYNC), *args], env=env, capture_output=True, text=True, timeout=120)


def ledger_of(rig):
    return json.loads(rig.state.read_text())


def test_missing_ledger_is_a_normal_first_run(rig):
    assert not rig.state.exists()
    r = run(rig)
    assert r.returncode == 0, r.stderr
    assert len(rig.stub.received) >= 2
    chunks = ledger_of(rig)["#chunks"]
    assert set(chunks) == set(rig.stub.received)


@pytest.mark.parametrize("junk", ["{not json", "", "[1, 2]", '{"#chunks": ["x"]}'])
def test_corrupt_ledger_stops_the_run_before_any_send(rig, junk):
    rig.state.parent.mkdir(parents=True)
    rig.state.write_text(junk)
    r = run(rig)
    assert r.returncode == 4
    assert rig.stub.received == []
    assert rig.state.read_text() == junk
    assert "reason=ledger-corrupt" in rig.log.read_text()
    assert run(rig, "--dry-run").returncode == 4


def test_second_concurrent_run_does_not_send(rig):
    rig.state.parent.mkdir(parents=True)
    with open(rig.state.parent / "ledger.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = run(rig)
        assert r.returncode == 0
        assert rig.stub.received == []
        assert "reason=locked" in rig.log.read_text()
        assert run(rig, GESTALT_SYNC_EXIT75="1").returncode == 75


def test_partial_failure_does_not_resend_acknowledged_chunks(rig):
    assert run(rig, "--dry-run").returncode == 0
    first = run(rig)  # learn the chunk names
    names = list(rig.stub.received)
    assert len(names) >= 2
    rig.state.unlink()
    rig.stub.received.clear()
    rig.stub.fail = {names[1]}
    r = run(rig, "--force", "fixture")
    assert r.returncode == 0, r.stderr
    assert names[0] in ledger_of(rig)["#chunks"], "acknowledged chunk must be recorded at its ack"
    assert names[1] not in ledger_of(rig)["#chunks"]
    rig.stub.fail = set()
    rig.stub.received.clear()
    run(rig, "fixture")
    assert names[0] not in rig.stub.received
    assert names[1] in rig.stub.received


def test_atomic_write_leaves_old_file_intact_when_interrupted(tmp_path, monkeypatch):
    p = tmp_path / "graphiti-sync.json"
    ledger.save(str(p), {"a": "1"})
    before = p.read_bytes()

    def boom(*a, **k):
        raise OSError("disk went away")
    monkeypatch.setattr(ledger.os, "replace", boom)
    with pytest.raises(OSError):
        ledger.save(str(p), {"a": "2", "b": "3"})
    assert p.read_bytes() == before
    assert [x.name for x in tmp_path.iterdir()] == ["graphiti-sync.json"]


def _seed(rig, chunks, landed=None):
    rig.state.parent.mkdir(parents=True, exist_ok=True)
    state = {"#chunks": chunks}
    if landed is not None:
        state["#landed"] = landed
    rig.state.write_text(json.dumps(state))


def test_reconcile_marks_landed_and_lists_missing(rig):
    _seed(rig, {"kb-fixture#1": "h1", "kb-fixture#2": "h2", "kb-other#1": "h3"})
    r = run(rig, "--reconcile", "fixture", LANDED="kb-fixture#1 kb-other#1")
    assert r.returncode == 0, r.stderr
    assert "landed\tkb-fixture#1" in r.stdout and "missing\tkb-fixture#2" in r.stdout
    assert "kb-other" not in r.stdout
    st = ledger_of(rig)
    assert st["#landed"] == {"kb-fixture#1": "h1"}
    assert st["#chunks"]["kb-fixture#2"] == "h2"


def test_reconcile_dry_run_changes_nothing(rig):
    _seed(rig, {"kb-fixture#1": "h1", "kb-fixture#2": "h2"})
    before = rig.state.read_bytes()
    r = run(rig, "--reconcile", "--dry-run", LANDED="kb-fixture#1")
    assert r.returncode == 0
    assert "missing\tkb-fixture#2" in r.stdout
    assert rig.state.read_bytes() == before


def test_reconcile_with_unreachable_graph_changes_nothing(rig):
    _seed(rig, {"kb-fixture#1": "h1"})
    before = rig.state.read_bytes()
    r = run(rig, "--reconcile", FAKE_CLI_DOWN="1")
    assert r.returncode == 5
    assert "graph" in r.stderr.lower()
    assert rig.state.read_bytes() == before
    assert rig.stub.received == []


def test_reconcile_resend_sends_only_the_missing_chunks(rig):
    run(rig)  # real chunk hashes in the ledger
    names = list(rig.stub.received)
    assert len(names) >= 2
    rig.stub.received.clear()
    lost = names[-1]
    r = run(rig, "--reconcile", "--resend", LANDED=" ".join(n for n in names if n != lost))
    assert r.returncode == 0, r.stderr
    assert rig.stub.received == [lost]
    r = run(rig, "--reconcile", "--resend", LANDED=" ".join(names))
    assert rig.stub.received == [lost], "a chunk the graph already has is never re-sent"


def test_old_format_ledger_still_loads(rig):
    rig.state.parent.mkdir(parents=True)
    rig.state.write_text(json.dumps({"tiny": "abc", "#chunks": {"kb-tiny": "def"}}))
    r = run(rig, "--dry-run")
    assert r.returncode == 0, r.stderr
    st = ledger.load(str(rig.state))
    assert ledger.pending(st) == {"kb-tiny": "def"}


def test_unhealthy_graphiti_skips_with_a_reason_and_opt_in_exit_75(rig):
    rig.env["GRAPHITI_URL"] = "http://127.0.0.1:9"
    r = run(rig)
    assert r.returncode == 0
    assert "reason=graphiti-unhealthy" in r.stderr
    assert run(rig, GESTALT_SYNC_EXIT75="1").returncode == 75


def test_reconcile_quotes_the_query_for_an_ssh_cli(tmp_path, monkeypatch):
    """Over ssh the remote shell re-parses the command, so each graph argument must arrive shell-quoted."""
    import importlib.util, json as _json, shlex as _shlex, subprocess as _sp
    spec = importlib.util.spec_from_file_location("ledger_ssh", str(Path(__file__).resolve().parent.parent / "tools" / "graphiti_sync" / "ledger.py"))
    led = importlib.util.module_from_spec(spec); spec.loader.exec_module(led)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return _sp.CompletedProcess(argv, 0, stdout=_json.dumps([["e.name"], [["kb-a#1"]], []]), stderr="")

    monkeypatch.setattr(led.subprocess, "run", fake_run)
    assert led.graph_names({"kb-a#1", "kb-b#1"}, cli="ssh hub docker exec gestalt-falkordb redis-cli") == {"kb-a#1"}
    remote = " ".join(seen["argv"][2:])          # what the remote shell will parse
    parts = _shlex.split(remote)                  # must survive that parse intact
    assert parts[-3] == "GRAPH.RO_QUERY" and parts[-1].startswith("MATCH (e:Episodic)") and '"kb-a#1"' in parts[-1]
