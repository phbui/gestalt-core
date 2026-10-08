"""Shared fixtures.

The real corpus (`knowledge/**`) is git-crypt encrypted, so CI can never read
it — the decryption key is deliberately not a repository secret. Everything
here therefore runs against synthetic fixtures, which also makes the tests
deterministic and independent of whatever happens to be in the knowledge base
on a given day.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The MCP server relays semantic searches to the fleet hub and keeps the embedding model off laptops
# (home-mesh ^hub-semantic-relay, 2026-09-02). Tests are synthetic and hermetic: no hub, local legs on.
# Individual tests opt back in with monkeypatch when they exercise the relay or the leaf policy.
os.environ.setdefault("GESTALT_HUB_MCP_URL", "")
os.environ.setdefault("GESTALT_LEAF_LOCAL_MODEL", "1")


def _load(name: str, relpath: str):
    """Import a hyphenated script (not importable via normal import syntax)."""
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        pytest.skip(f"cannot load {relpath}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit as exc:
        # Scripts like render-doc.py sys.exit() at import when an optional package is
        # absent; that is an environment gap, not a failure of the code under test.
        sys.modules.pop(name, None)
        pytest.skip(f"{relpath} not importable here: {exc}")
    return mod


@pytest.fixture(scope="session")
def indexer():
    """The index builder, imported without executing its CLI."""
    return _load("gestalt_index_builder", "tools/gestalt-index-builder.py")


@pytest.fixture
def write_entry(tmp_path, monkeypatch, indexer):
    """Write a synthetic knowledge entry and parse it through the real chunker."""

    def _write(body: str, slug: str = "fixture"):
        path = tmp_path / f"{slug}.md"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(indexer, "GESTALT_DIR", tmp_path, raising=False)
        return indexer.parse_entry(path)

    return _write


def pytest_collection_modifyitems(config, items):
    """Tag capability-gated tests with the `local` marker (registered in pytest.ini).

    These are tests that already carry a `skipif` decorator conditioned on local
    capability (corpus decrypted, a binary present, hostname, an opt-in env flag) —
    they self-skip in CI and exercise real behavior only where the capability exists.
    This only labels them for selection (`pytest -m local`); it does not change any
    skip condition. See knowledge/gestalt.md ^ci-tiers.
    """
    for item in items:
        if any(marker.name == "skipif" for marker in item.own_markers):
            item.add_marker(pytest.mark.local)


# Live-index guard (2026-09-06, Phi's item 4): the tier must never touch the checkout's own
# MANIFEST.md / GRAPH.md. Every rebuild in tests/ targets a tmp GESTALT_DIR or a clone (swept
# 2026-09-06: test_auto_promote, test_graph_dataflow, test_manifest_*, test_tools), so a change
# during a test is either a new test that forgot the override or another process (a hook, a
# session) rebuilding the live index while the tier ran. Either way, say which test saw it.
_LIVE_INDEX = [Path(__file__).resolve().parent.parent / n for n in ("MANIFEST.md", "GRAPH.md")]


def _live_index_sig() -> tuple:
    return tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in _LIVE_INDEX)


@pytest.fixture(autouse=True)
def _live_index_untouched(request):
    before = _live_index_sig()
    yield
    after = _live_index_sig()
    assert after == before, (
        f"{request.node.nodeid}: the live MANIFEST.md/GRAPH.md changed during this test "
        f"({before} -> {after}); tests rebuild only under a tmp GESTALT_DIR, so either this test "
        f"is missing the override or another process rebuilt the live index while the tier ran"
    )
