"""The per-prompt hook's semantic leg asks the hub first (2026-10-11, plan ^laptop-path-decision-2026-10-11): the hub's GPU encodes and
reranks in tens of milliseconds, where a laptop CPU needs 190 ms for the encode alone. Hermetic: a fake hub over HTTP, no model."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_prompt_intelligence_semantic import PROMPTS, hook, index, on, state, sync  # noqa: E402,F401

HUB_ROWS = [{"slug": "hub-row", "heading": "From the hub", "block_id": "h1", "content": "x", "file_path": "knowledge/hub-row.md", "sensitivity": None, "via": "hub"}]


class _Hub:
    """A stand-in for the hub's MCP server: initialize, initialized, tools/call. `delay` makes every answer slow."""

    def __init__(self, delay: float = 0.0):
        self.calls: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")) or "{}"))
                outer.calls.append(body.get("method", "?"))
                time.sleep(delay)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("mcp-session-id", "s1")
                self.end_headers()
                if body.get("method") == "tools/call":
                    self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"result": HUB_ROWS}}}).encode())
                else:
                    self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode())

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/mcp"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def slugs(rows):
    return [r["slug"] for r in rows]


def test_the_hook_takes_the_hubs_rows_and_never_touches_the_daemon(on, index, state, monkeypatch):
    started = on
    hub = _Hub()
    monkeypatch.setenv("GESTALT_HUB_MCP_URL", hub.url)
    monkeypatch.setattr(sys.modules["_gestalt_mcp_server_impl"], "_hub_looks_down", lambda: False)
    try:
        rows = sync(PROMPTS[0], index, state)
    finally:
        hub.close()
    assert "hub-row" in slugs(rows) and started == []  # answered by the hub, so no daemon was asked for
    assert hub.calls == ["initialize", "notifications/initialized", "tools/call"]


def test_a_slow_hub_falls_through_to_the_local_leg_inside_the_budget(on, index, state, monkeypatch):
    started = on
    monkeypatch.setenv("GESTALT_HOOK_SEMANTIC_MS", "150")
    hub = _Hub(delay=1.0)
    monkeypatch.setenv("GESTALT_HUB_MCP_URL", hub.url)
    monkeypatch.setattr(sys.modules["_gestalt_mcp_server_impl"], "_hub_looks_down", lambda: False)
    try:
        t = time.monotonic()
        rows = sync(PROMPTS[1], index, state)
        took = time.monotonic() - t
    finally:
        hub.close()
    assert "hub-row" not in slugs(rows) and took < 0.7, took
    assert started == [str(state)]  # the local leg ran next: no daemon was up, so one was asked for


def test_the_relay_is_skipped_when_the_knob_is_off_or_the_hub_looks_down(on, index, state, monkeypatch):
    hub = _Hub()
    monkeypatch.setenv("GESTALT_HUB_MCP_URL", hub.url)
    impl = sys.modules["_gestalt_mcp_server_impl"]
    try:
        monkeypatch.setenv("GESTALT_HOOK_HUB", "off")
        monkeypatch.setattr(impl, "_hub_looks_down", lambda: False)
        sync(PROMPTS[0], index, state)
        monkeypatch.delenv("GESTALT_HOOK_HUB")
        monkeypatch.setattr(impl, "_hub_looks_down", lambda: True)
        sync(PROMPTS[0], index, state)
    finally:
        hub.close()
    assert hub.calls == []
