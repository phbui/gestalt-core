"""The per-prompt hook's optional semantic leg (GESTALT_HOOK_SEMANTIC). Hermetic: a synthetic index and a stub daemon, no model.

Knob off, the hook's output must stay byte-identical to the lexical-only hook. The frozen strings below were captured from the hook before the semantic leg existed.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import REPO, HashEncoder, _load  # noqa: E402

sqlite_vec = pytest.importorskip("sqlite_vec")
hook = _load("prompt_intelligence_semantic", "claude-tree/hooks/prompt-intelligence.py")
daemon = _load("gestalt_embed_daemon_for_hook_tests", "tools/gestalt-embed-daemon.py")
HOOK = REPO / "claude-tree" / "hooks" / "prompt-intelligence.py"

DOCS = [
    ("hub-restart", "Restart", "wsl gpu passthrough restart the hub after the gpu is lost", "unpublished"),
    ("embedding-model", "Model", "the retrieval index uses nomic embed text with a prefix for queries", "unpublished"),
    ("fellowship", "Deadline", "fellowship proposal draft deadline is october thirtieth", "unpublished"),
    ("private-notes", "Secret", "restricted entry about passthrough restart", "restricted"),
    ("cooking", "Soup", "simmer the onions slowly and add stock", "unpublished"),
]
PROMPTS = [
    "how do I restart the hub after the wsl gpu passthrough is lost",
    "which embedding model does the retrieval index use",
    "what is the plan for the fellowship proposal draft and the deadline",
]
# Captured from the hook at the commit before the semantic leg, against build_index() below.
GOLDEN = {
    PROMPTS[0]: '{"additionalContext": "## Relevant Context\\n\\n**Gestalt:** [[hub-restart#b1]] \\u2014 Restart, [[fellowship#b3]] \\u2014 Deadline\\n\\n---\\n\\nBEFORE RESPONDING: (1) Decompose this prompt into subtasks. If 2+ are independent, spawn parallel agents \\u2014 up to 6 per wave. Use Explore agents for read-only work, general-purpose for writes/research. Even 2 parallel agents = 50% faster. (2) If the task matches a gestalt skill, suggest it: /investigate (deep research), /discuss (structured critique), /build (parallel workstreams), /fix (batch fixes), /audit (quality gate). Suggest before auto-invoking expensive skills."}\n',
    PROMPTS[1]: '{"additionalContext": "## Relevant Context\\n\\n**Gestalt:** [[embedding-model#b2]] \\u2014 Model, [[hub-restart#b1]] \\u2014 Restart, [[cooking#b5]] \\u2014 Soup\\n\\n---\\n\\nBEFORE RESPONDING: (1) Decompose this prompt into subtasks. If 2+ are independent, spawn parallel agents \\u2014 up to 6 per wave. Use Explore agents for read-only work, general-purpose for writes/research. Even 2 parallel agents = 50% faster. (2) If the task matches a gestalt skill, suggest it: /investigate (deep research), /discuss (structured critique), /build (parallel workstreams), /fix (batch fixes), /audit (quality gate). Suggest before auto-invoking expensive skills."}\n',
    PROMPTS[2]: '{"additionalContext": "## Relevant Context\\n\\n**Gestalt:** [[fellowship#b3]] \\u2014 Deadline, [[cooking#b5]] \\u2014 Soup, [[embedding-model#b2]] \\u2014 Model\\n\\n---\\n\\nBEFORE RESPONDING: (1) Decompose this prompt into subtasks. If 2+ are independent, spawn parallel agents \\u2014 up to 6 per wave. Use Explore agents for read-only work, general-purpose for writes/research. Even 2 parallel agents = 50% faster. (2) If the task matches a gestalt skill, suggest it: /investigate (deep research), /discuss (structured critique), /build (parallel workstreams), /fix (batch fixes), /audit (quality gate). Suggest before auto-invoking expensive skills."}\n',
}


def build_index(directory: Path, with_vec: bool = True) -> Path:
    db = sqlite3.connect(directory / "gestalt.db")
    db.executescript(
        """
        CREATE VIRTUAL TABLE sections_fts USING fts5(slug, title, heading, block_id, content, tokenize='porter unicode61');
        CREATE TABLE sections_meta (id INTEGER PRIMARY KEY, slug TEXT NOT NULL, heading TEXT NOT NULL, block_id TEXT, content TEXT NOT NULL,
            file_path TEXT NOT NULL, content_hash TEXT, anchors TEXT, sensitivity TEXT NOT NULL DEFAULT 'unpublished', title TEXT NOT NULL DEFAULT '');
        """
    )
    if with_vec:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.execute("CREATE VIRTUAL TABLE sections_vec USING vec0(id INTEGER PRIMARY KEY, embedding FLOAT[768])")
    enc = HashEncoder(768)
    for i, (slug, heading, content, sens) in enumerate(DOCS, 1):
        db.execute("INSERT INTO sections_meta (id, slug, heading, block_id, content, file_path, sensitivity) VALUES (?,?,?,?,?,?,?)",
                   (i, slug, heading, f"b{i}", content, f"knowledge/{slug}.md", sens))
        db.execute("INSERT INTO sections_fts (rowid, slug, title, heading, block_id, content) VALUES (?,?,?,?,?,?)", (i, slug, "", heading, f"b{i}", content))
        if with_vec:
            db.execute("INSERT INTO sections_vec (id, embedding) VALUES (?, ?)", (i, enc.encode("search_document: " + content).astype("float32").tobytes()))
    db.commit()
    db.close()
    return directory


def run_hook(prompt: str, search_dir: Path, state_dir: Path, **env) -> str:
    e = dict(os.environ, GESTALT_SEARCH_DIR=str(search_dir), GESTALT_STATE_DIR=str(state_dir), GESTALT_HUB_MCP_URL="")
    e.pop("GESTALT_HOOK_SEMANTIC", None)
    e.pop("GESTALT_HOOK_SEMANTIC_MS", None)
    e.update(env)
    r = subprocess.run([sys.executable, str(HOOK), "--gestalt-dir", str(REPO), "--state-dir", str(state_dir), "--graphiti-url", "http://127.0.0.1:9",
                        "--letta-url", "http://127.0.0.1:9/v1"], input=json.dumps({"prompt": prompt}), capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture(autouse=True)
def _forget_harness_modules():
    """These tests point GESTALT_SEARCH_DIR at a temp index. run_retrieval_evals fixes DB_PATH at import, so a copy imported
    here would carry the temp path into later suites (test_harness_fidelity saw search_dir differ from the banked default)."""
    import sys

    yield
    for name in ("run_retrieval_evals", "bench_engine", "gestalt_embed_config"):  # the config module reads GESTALT_SEARCH_DIR once at import
        sys.modules.pop(name, None)


@pytest.fixture
def index(tmp_path):
    d = tmp_path / "idx"
    d.mkdir()
    return build_index(d)


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "st"
    d.mkdir()
    return d


class StubDaemon:
    """Serves daemon.serve() on a thread with a stub embedder. `delay` makes it slow."""

    def __init__(self, state_dir: Path, embed, delay: float = 0.0):
        self.seen: list[list[str]] = []

        def slow(texts):
            self.seen.append(list(texts))
            time.sleep(delay)
            return embed(texts)

        self.stop = threading.Event()
        self.t = threading.Thread(target=daemon.serve, args=(state_dir, slow, 30.0, self.stop), daemon=True)
        self.t.start()
        end = time.monotonic() + 5
        while time.monotonic() < end:  # the file exists at bind. Listening starts a moment later, so wait for a connect
            try:
                daemon.encode([], state_dir, 0.2)
                break
            except OSError:
                time.sleep(0.01)

    def close(self):
        self.stop.set()
        self.t.join(5)


def vec_of(text: str):
    return HashEncoder(768).encode(text).tolist()


# --- knob off ------------------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", PROMPTS)
@pytest.mark.parametrize("knob", [None, "off", "garbage", ""])
def test_knob_off_is_byte_identical(index, state, prompt, knob):
    env = {} if knob is None else {"GESTALT_HOOK_SEMANTIC": knob}
    out = run_hook(prompt, index, state, **env)
    assert out == GOLDEN[prompt]
    assert not daemon.paths(state)[0].exists() and not daemon.paths(state)[1].exists()  # off never starts a daemon


# --- knob on -------------------------------------------------------------------------------------

def sync(prompt, index, state):
    impl = sys.modules["_gestalt_mcp_server_impl"]
    impl.DB_PATH = index / "gestalt.db"  # the server reads GESTALT_SEARCH_DIR once at import. The `on` fixture restores this
    return hook.retrieve_gestalt_sync(prompt, str(REPO), 3, state_dir=str(state))


@pytest.fixture
def on(monkeypatch, index):
    monkeypatch.setenv("GESTALT_HOOK_SEMANTIC", "on")
    monkeypatch.setenv("GESTALT_SEARCH_DIR", str(index))
    monkeypatch.setenv("GESTALT_HUB_MCP_URL", "")
    monkeypatch.delenv("GESTALT_HOOK_SEMANTIC_MS", raising=False)
    started: list = []
    monkeypatch.setattr(daemon, "spawn", lambda sd: started.append(sd) or True)  # a test must never start a real daemon
    monkeypatch.setattr(hook, "_DAEMON_MOD", daemon, raising=False)
    sys.path.insert(0, str(REPO / "tools"))
    import gestalt_mcp_server  # noqa: F401

    monkeypatch.setattr(sys.modules["_gestalt_mcp_server_impl"], "DB_PATH", index / "gestalt.db")
    return started


def slugs(rows):
    return [r["slug"] for r in rows]


def test_dense_leg_adds_a_hit_the_lexical_leg_cannot_find(on, index, state):
    prompt = "zzz qqq"  # no lexical match for any document
    assert sync(prompt, index, state) == []
    target = DOCS[4][2]  # the cooking entry
    d = StubDaemon(state, lambda texts: [vec_of("search_document: " + target) for _ in texts])
    try:
        rows = sync(prompt, index, state)
        assert slugs(rows)[0] == "cooking"
        assert d.seen == [[prompt]]  # one request, the raw query text
    finally:
        d.close()


def test_restricted_entry_is_never_returned_by_the_semantic_leg(on, index, state):
    d = StubDaemon(state, lambda texts: [vec_of("search_document: " + DOCS[3][2]) for _ in texts])
    try:
        assert "private-notes" not in slugs(sync("zzz qqq", index, state))
    finally:
        d.close()


def test_budget_fallback_is_lexical_and_inside_the_budget(on, index, state, monkeypatch):
    monkeypatch.setenv("GESTALT_HOOK_SEMANTIC_MS", "150")
    lexical = hook_rows_with_knob_off(PROMPTS[0], index, state, monkeypatch)
    d = StubDaemon(state, lambda texts: [vec_of("x") for _ in texts], delay=1.0)
    try:
        t = time.monotonic()
        rows = sync(PROMPTS[0], index, state)
        took = time.monotonic() - t
    finally:
        d.close()
    assert slugs(rows) == slugs(lexical)
    assert took < 0.6, took  # 150 ms budget plus the lexical pass, well under the 1 s the stub sleeps


def hook_rows_with_knob_off(prompt, index, state, monkeypatch):
    monkeypatch.setenv("GESTALT_HOOK_SEMANTIC", "off")
    try:
        return sync(prompt, index, state)
    finally:
        monkeypatch.setenv("GESTALT_HOOK_SEMANTIC", "on")


def test_cold_daemon_falls_back_and_asks_for_a_start(on, index, state, monkeypatch):
    started = on
    lexical = hook_rows_with_knob_off(PROMPTS[1], index, state, monkeypatch)
    t = time.monotonic()
    rows = sync(PROMPTS[1], index, state)
    assert slugs(rows) == slugs(lexical)
    assert started == [str(state)]
    assert time.monotonic() - t < 0.6


def test_slow_daemon_is_not_respawned(on, index, state, monkeypatch):
    started = on
    monkeypatch.setenv("GESTALT_HOOK_SEMANTIC_MS", "100")
    d = StubDaemon(state, lambda texts: [vec_of("x") for _ in texts], delay=0.8)
    try:
        sync(PROMPTS[0], index, state)
    finally:
        d.close()
    assert started == []  # a timeout means the daemon is up. Only a missing socket asks for a start


def test_index_without_vectors_falls_back(on, tmp_path, state):
    plain = tmp_path / "plain"
    plain.mkdir()
    build_index(plain, with_vec=False)
    d = StubDaemon(state, lambda texts: [vec_of("x") for _ in texts])
    try:
        assert "hub-restart" in slugs(sync(PROMPTS[0], plain, state))
    finally:
        d.close()


def test_settings_parse(monkeypatch):
    for raw, want in (("on", True), (" ON ", True), ("off", False), ("1", False), ("", False)):
        monkeypatch.setenv("GESTALT_HOOK_SEMANTIC", raw)
        assert hook.semantic_settings()[0] is want
    monkeypatch.delenv("GESTALT_HOOK_SEMANTIC")
    assert hook.semantic_settings() == (False, 150)
    for raw, want in (("300", 300), ("abc", 150), ("0", 150), ("-5", 150)):
        monkeypatch.setenv("GESTALT_HOOK_SEMANTIC_MS", raw)
        assert hook.semantic_settings()[1] == want
