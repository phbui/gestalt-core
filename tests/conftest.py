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
import types
import zlib
from collections import namedtuple
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The MCP server relays semantic searches to the fleet hub and keeps the embedding model off laptops
# (home-mesh ^hub-semantic-relay, 2026-09-02). Tests are synthetic and hermetic: no hub, local legs on.
# Individual tests opt back in with monkeypatch when they exercise the relay or the leaf policy.
os.environ.setdefault("GESTALT_HUB_MCP_URL", "")
os.environ.setdefault("GESTALT_BENCH_ALLOW_CPU", "1")  # the fake encoders live on the CPU; beir_bench.refuse_cpu is tested on its own
os.environ.setdefault("GESTALT_LEAF_LOCAL_MODEL", "1")
# The reranker defaults to auto, which turns on at the hub with CUDA. Tests stub the model and must never load a real one.
os.environ.setdefault("GESTALT_RERANK", "off")


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
    skip condition. The tiers are described in pytest.ini.
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


# --- shared stub encoders and a fake ir_datasets -------------------------------------------------
# Tests never load a real model. These stand in for SentenceTransformer. crc32 is unsalted, so vectors are the
# same in every process. The builtin hash() is salted per process and must not be used for this.

def _full_dim() -> int:
    tools = str(REPO / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import gestalt_embed_config

    return gestalt_embed_config.FULL_DIM


class HashEncoder:
    """Bag-of-words crc32 encoder. Shared words give near vectors. A bare string returns a 1D vector, a list a 2D stack.

    `dim` defaults to the embed config's FULL_DIM. `normalize` (default True, settable per subclass) makes rows unit length, and zero rows stay zero.
    `seen` collects every text encoded, in call order.
    """

    device = "cpu"
    max_seq_length = 512

    normalize = True

    def __init__(self, dim: int | None = None, *, normalize: bool | None = None):
        self.dim = _full_dim() if dim is None else dim
        if normalize is not None:
            self.normalize = normalize
        self.seen: list[str] = []

    def encode(self, texts, batch_size=64, convert_to_numpy=True, show_progress_bar=False, output_value=None, **_kw):
        import numpy as np

        if output_value not in (None, "sentence_embedding"):
            raise NotImplementedError("HashEncoder has no token-level output")
        one = isinstance(texts, str)
        items = [texts] if one else list(texts)
        self.seen.extend(items)
        out = np.zeros((len(items), self.dim), dtype="float32")
        for i, t in enumerate(items):
            for w in t.lower().split():
                out[i, zlib.crc32(w.encode()) % self.dim] += 1.0
        if self.normalize:
            norms = np.linalg.norm(out, axis=1, keepdims=True)
            out = np.divide(out, norms, out=out, where=norms > 0)
        return out[0] if one else out


class ConstEncoder:
    """Every text maps to the same unit vector 1/sqrt(dim). For tests that only care what was encoded, recorded in `seen`."""

    device = "cpu"
    max_seq_length = 512

    def __init__(self, dim: int = 768):
        self.dim = dim
        self.seen: list[str] = []

    def encode(self, inputs, **_kw):
        import numpy as np

        texts = [inputs] if isinstance(inputs, str) else list(inputs)
        self.seen.extend(texts)
        v = (np.ones((len(texts), self.dim)) / np.sqrt(self.dim)).astype(np.float32)
        return v[0] if isinstance(inputs, str) else v


class ConstSentenceTransformer:
    """Stands in for the SentenceTransformer class. Subclass it per test file so `dim` and `last` stay file-local.

    `last` is the most recent instance, and `name` and `kw` are the constructor arguments. Attribute access falls
    through to a ConstEncoder of width `dim`.
    """

    dim = 768
    last = None

    def __init__(self, name=None, **kw):
        self.name, self.kw = name, kw
        type(self).last = self
        self._m = ConstEncoder(type(self).dim)

    def __getattr__(self, attr):
        return getattr(self._m, attr)


@pytest.fixture
def hash_encoder():
    """Factory `hash_encoder(dim=None, *, normalize=None) -> HashEncoder`."""
    return HashEncoder


@pytest.fixture
def const_encoder():
    """Factory `const_encoder(dim=768) -> ConstEncoder`."""
    return ConstEncoder


FakeDoc = namedtuple("FakeDoc", "doc_id title text")
FakeQuery = namedtuple("FakeQuery", "query_id text")
FakeQrel = namedtuple("FakeQrel", "query_id doc_id relevance")


def make_fake_ir_datasets(docs, queries, qrels, loaded: list | None = None):
    """A module object shaped like `ir_datasets`: `load(name)` returns a dataset serving the given rows.

    Rows may be namedtuples or plain tuples of (doc_id, title, text), (query_id, text) and (query_id, doc_id, relevance).
    Each `load` appends the dataset name to `loaded` when given.
    """
    docs = [d if hasattr(d, "doc_id") else FakeDoc(*d) for d in docs]
    queries = [q if hasattr(q, "query_id") else FakeQuery(*q) for q in queries]
    qrels = [r if hasattr(r, "relevance") else FakeQrel(*r) for r in qrels]

    class Dataset:
        def __init__(self, name):
            if loaded is not None:
                loaded.append(name)

        def docs_iter(self):
            return iter(docs)

        def queries_iter(self):
            return iter(queries)

        def qrels_iter(self):
            return iter(qrels)

    mod = types.ModuleType("ir_datasets")
    mod.load = Dataset
    return mod


@pytest.fixture
def fake_ir_datasets():
    """Factory `fake_ir_datasets(docs, queries, qrels, loaded=None) -> module`. Install it with monkeypatch.setitem(sys.modules, "ir_datasets", mod)."""
    return make_fake_ir_datasets
