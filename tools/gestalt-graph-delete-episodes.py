#!/usr/bin/env python3
"""Delete a named list of Graphiti episodes by UUID, through graphiti's own cascade, under a human hand.

Why this exists: the MCP safety hook denies `delete_episode` from any agent session, by design (the
2026-09-01 dedupe wiped the graph). A human runs this instead, with the identity list in a file, so the
deletion is exactly the list that was reviewed and nothing else.

    gestalt-graph-delete-episodes.py LIST.json              dry run: shows name, created_at and size per UUID
    GESTALT_DEDUPE_ARMED=1 gestalt-graph-delete-episodes.py --execute LIST.json

LIST.json is a JSON array of episode UUIDs. Every UUID must exist before execution (a missing one aborts the
whole run), each deletion goes through the MCP tool `delete_episode` (Graphiti.remove_episode: entities and
facts sourced only by that episode go with it), and afterwards every UUID is re-checked as absent. Env:
GRAPHITI_URL (default http://127.0.0.1:8200), GESTALT_FALKORDB_CLI (default: docker exec gestalt-falkordb redis-cli).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import urllib.request

GRAPHITI_URL = os.environ.get("GRAPHITI_URL", "http://127.0.0.1:8200").rstrip("/")
REDIS_CLI = shlex.split(os.environ.get("GESTALT_FALKORDB_CLI", "docker exec gestalt-falkordb redis-cli"))
GRAPH = os.environ.get("GESTALT_GRAPH", "gestalt")
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def graph_query(cypher: str) -> list[list]:
    out = subprocess.run([*REDIS_CLI, "--json", "GRAPH.QUERY", GRAPH, cypher], capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return data[1] if isinstance(data, list) and len(data) >= 2 else []


def episodes_by_uuid(uuids: list[str]) -> dict[str, tuple[str, str, int]]:
    rows = graph_query("MATCH (e:Episodic) WHERE e.uuid IN [" + ",".join(json.dumps(u) for u in uuids) + "] RETURN e.uuid, e.name, e.created_at, size(e.content)")
    return {r[0]: (r[1], r[2], int(r[3] or 0)) for r in rows}


def counts() -> tuple[int, int]:
    rows = graph_query("MATCH (e:Episodic) RETURN count(e), count(DISTINCT e.name)")
    return int(rows[0][0]), int(rows[0][1])


def _post(path: str, body: dict, session: str | None = None) -> tuple[dict | None, str | None]:
    req = urllib.request.Request(GRAPHITI_URL + path, data=json.dumps(body).encode(), method="POST")
    for k, v in MCP_HEADERS.items():
        req.add_header(k, v)
    if session:
        req.add_header("Mcp-Session-Id", session)
    with urllib.request.urlopen(req, timeout=120) as r:
        sid = r.headers.get("Mcp-Session-Id")
        raw = r.read().decode()
    payload = None
    for line in raw.splitlines():  # streamable-http answers either plain JSON or SSE "data:" lines
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
    return payload, sid


class Mcp:
    def __init__(self) -> None:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "gestalt-graph-delete-episodes", "version": "1"}}}
        _, self.session = _post("/mcp", init)
        if not self.session:
            raise SystemExit("MCP session init failed (no Mcp-Session-Id)")
        _post("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.session)

    def delete_episode(self, uuid: str) -> str:
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delete_episode", "arguments": {"uuid": uuid}}}
        payload, _ = _post("/mcp", body, self.session)
        if not payload or "error" in payload:
            raise RuntimeError(f"delete_episode {uuid}: {payload}")
        content = payload.get("result", {}).get("content", [])
        text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        if payload.get("result", {}).get("isError") or "error" in text.lower():
            raise RuntimeError(f"delete_episode {uuid}: {text}")
        return text


def main(argv: list[str]) -> int:
    execute = "--execute" in argv
    files = [a for a in argv if not a.startswith("--")]
    if len(files) != 1:
        print(__doc__)
        return 2
    uuids = json.load(open(files[0]))
    if not isinstance(uuids, list) or not all(isinstance(u, str) for u in uuids) or len(set(uuids)) != len(uuids):
        raise SystemExit("LIST.json must be a JSON array of unique UUID strings")
    before = counts()
    found = episodes_by_uuid(uuids)
    missing = [u for u in uuids if u not in found]
    print(f"graph before: {before[0]} episodes, {before[1]} distinct names; list: {len(uuids)} uuids, {len(found)} present, {len(missing)} missing")
    for u in uuids:
        if u in found:
            name, created, size = found[u]
            print(f"  {'DELETE' if execute else 'would delete'}\t{u}\t{name}\t{created}\t{size} chars")
        else:
            print(f"  MISSING\t{u}")
    if missing:
        print("refusing: every listed UUID must exist before execution", file=sys.stderr)
        return 3
    if not execute:
        print("dry run only; add --execute with GESTALT_DEDUPE_ARMED=1 to delete")
        return 0
    if os.environ.get("GESTALT_DEDUPE_ARMED") != "1":
        print("refusing: --execute needs GESTALT_DEDUPE_ARMED=1 in the environment", file=sys.stderr)
        return 4
    mcp = Mcp()
    done = 0
    for u in uuids:
        text = mcp.delete_episode(u)
        done += 1
        print(f"  deleted {u}: {text[:80]}")
    still = episodes_by_uuid(uuids)
    after = counts()
    print(f"graph after: {after[0]} episodes, {after[1]} distinct names; deleted {done}; still present: {len(still)}; excess names: {after[0] - after[1]}")
    return 0 if not still and after[0] == before[0] - len(uuids) else 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
