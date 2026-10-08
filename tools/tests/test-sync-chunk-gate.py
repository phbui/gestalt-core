#!/usr/bin/env python3
"""The sync gate must be per chunk, and an explicit slug must never imply --force.

Before 2026-09-03 the gate was whole-file and naming a slug bypassed it, so every
`gestalt-graphiti-sync.sh <slug>` re-sent every chunk. /save calls exactly that after each
write, so seven routine /save runs in one evening queued 54 chunks and left 50 duplicate
episodes. The duplicate count equalled the number of syncs, not the amount changed.

Four cases, run end to end against a stub MCP server that counts add_memory calls:
  1. first sync of a 2-chunk entry queues both chunks
  2. three further syncs of the UNCHANGED entry queue zero        <- the regression
  3. editing one chunk queues exactly that chunk, not the entry
  4. --force re-sends every chunk of the named slug
"""
import http.server, json, os, pathlib, re, subprocess, sys, tempfile, threading

REPO = pathlib.Path(__file__).resolve().parents[2]
SYNC = pathlib.Path(os.environ.get("SYNC_SCRIPT") or (REPO / "tools" / "gestalt-graphiti-sync.sh"))
queued: list[str] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        try:
            msg = json.loads(raw or b"{}")
        except Exception:
            msg = {}
        if msg.get("method") == "initialize":
            self.send_response(200)
            self.send_header("Mcp-Session-Id", "test-session")
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()); return
        if msg.get("method") == "tools/call":
            queued.append(msg["params"]["arguments"]["name"])
        self.send_response(200)
        self.send_header("Content-Type", "application/json"); self.end_headers()
        # Compact separators matter: the script greps for the literal '"isError":false', so a
        # default json.dumps (which emits '"isError": false') reads as a FAILED send, the chunk
        # hashes are never committed to state, and every later run re-sends. The stub must
        # answer in the shape the real server does or the test measures the stub, not the gate.
        self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                                     "result": {"isError": False}},
                                    separators=(",", ":")).encode())


srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"

tmp = tempfile.mkdtemp()
kd = pathlib.Path(tmp, "knowledge"); kd.mkdir()
state = pathlib.Path(tmp, "state.json")
entry = kd / "fixture.md"
CHUNK_A = "## Alpha\n" + ("alpha body line\n" * 40)
CHUNK_B = "## Beta\n" + ("beta body line\n" * 40)
entry.write_text("---\ntitle: Fixture\n---\n\n" + CHUNK_A + CHUNK_B)

env = dict(os.environ, GESTALT_KNOWLEDGE_DIR=str(kd), GESTALT_GRAPHITI_SYNC_STATE=str(state),
           GRAPHITI_URL=URL, GESTALT_SYNC_CHUNK_CHARS="700")


def run(*args):
    queued.clear()
    r = subprocess.run([str(SYNC), *args], env=env, capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or os.environ.get("SYNC_TEST_DEBUG"):
        print(f"    [rc={r.returncode}] stdout={r.stdout.strip()[:200]!r} stderr={r.stderr.strip()[:300]!r}")
    return list(queued)


fails = 0


def check(label, got, want):
    global fails
    ok = sorted(got) == sorted(want)
    print(f"  {'ok  ' if ok else 'FAIL'}: {label}: queued {sorted(got) or '[]'}"
          + ("" if ok else f"  wanted {sorted(want)}"))
    if not ok:
        fails += 1


print("--- 1. first sync queues both chunks ---")
check("first sync", run("fixture"), ["kb-fixture#1", "kb-fixture#2"])

print("--- 2. three more syncs of the UNCHANGED entry queue nothing ---")
for n in (1, 2, 3):
    check(f"unchanged run {n}", run("fixture"), [])

print("--- 3. editing one chunk queues only that chunk ---")
entry.write_text(entry.read_text().replace("beta body line", "beta body EDITED", 1))
check("one chunk edited", run("fixture"), ["kb-fixture#2"])

print("--- 4. --force re-sends every chunk of the named slug ---")
check("--force", run("--force", "fixture"), ["kb-fixture#1", "kb-fixture#2"])

srv.shutdown()
print("PASS" if fails == 0 else "FAIL")
sys.exit(1 if fails else 0)
