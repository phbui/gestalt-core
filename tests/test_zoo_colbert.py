"""evals/zoo/colbert.py: the multi-vector Searcher, its token store, MaxSim and the three-leg fusion.

No model loads. A fake encoder returns small token matrices from a word table, and unknown words get a hashed unit vector.
"""
from __future__ import annotations

import json
import sys
import zlib
from collections import namedtuple
from pathlib import Path

import numpy as np
import pytest
from conftest import make_fake_ir_datasets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
pytest.importorskip("sqlite_vec")
pytest.importorskip("yaml")
pytest.importorskip("jsonschema")
import evals.zoo  # noqa: E402,F401
from resumable import FingerprintMismatch  # noqa: E402

from evals.zoo import colbert, schema, zoo_run  # noqa: E402
from evals.zoo.adapter import DESCRIBE_KEYS  # noqa: E402
from evals.zoo.fused import Fused  # noqa: E402

E, F, G = (1.0, 0.0), (0.0, 1.0), (0.6, 0.8)
TABLE = {"e": E, "f": F, "g": G}
Doc = namedtuple("Doc", "doc_id title text")
Query = namedtuple("Query", "query_id text")
Qrel = namedtuple("Qrel", "query_id doc_id relevance")


class FakeEncoder:
    """One unit vector per word. Words in `table` get their listed vector and any other word a hashed one. Records every text encoded."""

    def __init__(self, table=None, dim=2):
        self.table, self.dim, self.seen, self.calls = dict(table or {}), dim, [], 0

    def vec(self, word):
        if word in self.table:
            return np.array(self.table[word], dtype=np.float32)
        v = np.random.default_rng(zlib.crc32(word.encode())).normal(size=self.dim).astype(np.float32)
        return v / np.linalg.norm(v)

    def _encode(self, texts, **_kw):
        return [np.array([self.vec(w) for w in t.split()], dtype=np.float32).reshape(-1, self.dim) for t in texts]

    def encode_document(self, texts, **kw):
        self.calls += 1
        self.seen.extend(texts)
        return self._encode(texts, **kw)

    def encode_query(self, texts, **kw):
        return self._encode(texts, **kw)

    def parameters(self):
        return [type("P", (), {"numel": lambda self: 5})()]


class Flaky(FakeEncoder):
    """Raises on the document encode call after `ok` calls, as a kill would."""

    def __init__(self, ok, **kw):
        super().__init__(**kw)
        self.ok = ok

    def encode_document(self, texts, **kw):
        if self.calls >= self.ok:
            raise RuntimeError("killed")
        return super().encode_document(texts, **kw)


def make(tmp_path, monkeypatch, enc, name="mv", **kw):
    monkeypatch.setattr(colbert, "load_model", lambda *a, **k: enc)
    return colbert.MultiVector(name, model="fake", work_dir=tmp_path / "w", **({"block_size": 2} | kw))


def test_maxsim_matches_the_hand_computation():
    q = np.array([E, F, G], dtype=np.float32)  # query 1 has rows e f, query 2 has row g
    q_lens = np.array([2, 1])
    tokens = np.array([G, E, F], dtype=np.float32)  # doc 0: g e. doc 1: no rows. doc 2: f
    lens = np.array([2, 0, 1])
    got = colbert.maxsim_block(q, q_lens, tokens, lens)
    # q1 vs doc0: max(e.g, e.e) + max(f.g, f.e) = 1 + 0.8. q1 vs doc2: e.f + f.f = 0 + 1.
    # q2 vs doc0: max(g.g, g.e) = 1. q2 vs doc2: g.f = 0.8.
    want = np.array([[1.8, -np.inf, 1.0], [1.0, -np.inf, 0.8]], dtype=np.float32)
    assert np.allclose(got, want, atol=1e-6)


def test_index_writes_the_store_and_search_order_follows_maxsim(monkeypatch, tmp_path):
    docs = [("d0", "e g"), ("d1", "f"), ("d2", "g g"), ("d3", ""), ("d4", "g")]
    s = make(tmp_path, monkeypatch, FakeEncoder(TABLE))
    s.index(docs)
    tok = tmp_path / "w" / "tok"
    assert sorted(p.name for p in tok.glob("emb-*")) == [f"emb-0000{i}.{x}" for i in range(3) for x in ("done", "npy")]
    receipt = json.loads((tok / "emb-00000.done").read_text())
    assert receipt["lens"] == [2, 1] and receipt["dim"] == 2 and np.load(tok / "emb-00000.npy").dtype == np.float16
    assert s.vectors_per_doc_mean == pytest.approx(6 / 5)
    # Query "e f": d0 = 1 + 0.8, d2 = 0.6 + 0.8, d4 = 0.6 + 0.8, d1 = 0 + 1. d3 has no rows and is left out.
    hits = s.search([("q", "e f")], 10)["q"]
    assert [d for d, _ in hits] == ["d0", "d2", "d4", "d1"]
    assert [v for _, v in hits] == pytest.approx([1.8, 1.4, 1.4, 1.0], abs=1e-3)
    assert [d for d, _ in s.search([("q", "e f")], 2)["q"]] == ["d0", "d2"]
    s.close()


def test_killed_run_resumes_and_encodes_only_the_missing_blocks(monkeypatch, tmp_path):
    docs = [(f"d{i}", f"e f w{i}") for i in range(5)]  # block size 2: three blocks
    a = make(tmp_path, monkeypatch, Flaky(ok=1, table=TABLE))
    with pytest.raises(RuntimeError, match="killed"):
        a.index(docs)
    a.close()
    second = FakeEncoder(TABLE)
    b = make(tmp_path, monkeypatch, second)
    b.index(docs)
    assert second.seen == [t for _, t in docs[2:]] and second.calls == 2  # block 0 kept, blocks 1 and 2 encoded
    assert b.search([("q", "w4")], 1)["q"][0][0] == "d4"
    b.close()
    third = FakeEncoder(TABLE)
    c = make(tmp_path, monkeypatch, third)
    c.index(docs)
    assert third.seen == []  # a finished store encodes nothing
    c.close()


def test_a_damaged_block_is_encoded_again(monkeypatch, tmp_path):
    docs = [(f"d{i}", f"e w{i}") for i in range(4)]
    a = make(tmp_path, monkeypatch, FakeEncoder(TABLE))
    a.index(docs)
    a.close()
    npy = tmp_path / "w" / "tok" / "emb-00001.npy"
    arr = np.load(npy)
    np.save(npy, arr + np.float16(1))
    enc = FakeEncoder(TABLE)
    b = make(tmp_path, monkeypatch, enc)
    b.index(docs)
    assert enc.seen == [t for _, t in docs[2:]]
    b.close()


def test_fingerprint_changes_with_max_doc_tokens(monkeypatch, tmp_path):
    docs = [("d0", "e f"), ("d1", "g")]
    a = make(tmp_path, monkeypatch, FakeEncoder(TABLE), max_doc_tokens=300)
    a.index(docs)
    fp300 = a.store.fingerprint
    a.close()
    with pytest.raises(FingerprintMismatch):
        make(tmp_path, monkeypatch, FakeEncoder(TABLE), max_doc_tokens=128).index(docs)
    other = colbert.MultiVector("mv2", model="fake", work_dir=tmp_path / "w2", block_size=2, max_doc_tokens=128)
    other.index(docs)
    assert other.store.fingerprint != fp300
    other.close()


def test_describe_has_every_cost_field(monkeypatch, tmp_path):
    s = make(tmp_path, monkeypatch, FakeEncoder(TABLE), revision="abc", licence="apache-2.0")
    assert s.describe()["vectors_per_doc_mean"] is None
    s.index([("d0", "e f"), ("d1", "g")])
    d = s.describe()
    assert set(DESCRIBE_KEYS) <= set(d) and d["vectors_per_doc_mean"] == 1.5 and d["index_bytes"] > 0
    assert d["parameters"] == 5 and d["revision"] == "abc" and d["licence"] == "apache-2.0"
    s.close()


YAML = """
systems:
  bm25: {adapter: bm25}
  mv: {adapter: colbert, args: {model: fake, block_size: 10, max_doc_tokens: 300}}
  "mv+bm25:rrf": {adapter: fused, args: {a: bm25, b: mv, fusion: rrf}}
  "mv3": {adapter: fused, args: {a: bm25, b: mv, c: mv, fusion: rrf}}
"""
COST_FIELDS = {"index_seconds", "docs_per_second", "query_latency_ms_p50", "query_latency_ms_p95", "latency_queries", "queries_per_second",
               "peak_vram_mib", "index_bytes", "parameters", "dtype", "gpu"}


def test_zoo_run_with_the_multivector_system_validates(monkeypatch, tmp_path):
    docs = [Doc(f"d{i}", f"title{i}", f"alpha{i} beta{i} gamma") for i in range(30)]
    queries = [Query("q1", "alpha25 beta25"), Query("q2", "alpha27 beta27")]
    monkeypatch.setitem(sys.modules, "ir_datasets", make_fake_ir_datasets(docs, queries, [Qrel("q1", "d25", 1), Qrel("q2", "d27", 1)]))
    enc = FakeEncoder(dim=16)
    monkeypatch.setattr(colbert, "load_model", lambda *a, **k: enc)
    cfg = tmp_path / "zoo.yaml"
    cfg.write_text(YAML)
    out = tmp_path / "out"
    assert zoo_run.main(["--dataset", "beir/scifact", "--systems", "mv", "bm25", "mv+bm25:rrf", "mv3", "--out", str(out), "--config", str(cfg),
                         "--work-dir", str(tmp_path / "work"), "--latency-queries", "2"]) == 0
    assert schema.validate(out / "beir-scifact.json") == []
    r = json.loads((out / "beir-scifact.json").read_text())
    assert set(r["systems"]["mv"]["cost"]) == COST_FIELDS and r["systems"]["mv"]["cost"]["index_bytes"] > 0
    assert r["model"]["systems"]["mv"]["vectors_per_doc_mean"] == 4.0  # title, alpha, beta, gamma
    assert r["systems"]["mv"]["ndcg10"] > 0.5 and r["systems"]["mv3"]["ndcg10"] > 0.5
    assert "mv3_vs_mv" in r["tests"]


class Stub:
    """A leaf Searcher with fixed hits. Score is larger-is-better."""
    parts, indexed = (), True

    def __init__(self, name, ids):
        self.name, self.ids = name, ids

    def search(self, queries, k):
        return {q: [(d, float(len(self.ids) - i)) for i, d in enumerate(self.ids)][:k] for q, _ in queries}

    def describe(self):
        return {"model_id": self.name, "revision": None, "dtype": None, "parameters": None, "licence": None, "index_bytes": 1}


def test_three_leg_rrf_equals_the_hand_computation():
    a, b, c = Stub("a", ["d1", "d2", "d3"]), Stub("b", ["d2", "d4", "d1"]), Stub("c", ["d3", "d2", "d5"])
    # Reciprocal rank fusion at K=60 with 1-based ranks: sum of 1 / (60 + rank) over the legs that hold the document.
    want = {"d1": 1 / 61 + 1 / 63, "d2": 1 / 62 + 1 / 61 + 1 / 62, "d3": 1 / 63 + 1 / 61, "d4": 1 / 62, "d5": 1 / 63}
    got = Fused("f", a, b, "rrf", c=c).search([("q", "x")], 10)["q"]
    assert [d for d, _ in got] == ["d2", "d1", "d3", "d4", "d5"]  # d1 and d3 tie at 1/61 + 1/63, and d1 appears first
    assert dict(got) == pytest.approx(want, abs=1e-12)
    assert len(Fused("f", a, b, "rrf", c=c).parts) == 3 and len(Fused("f", a, b).parts) == 2
    with pytest.raises(ValueError):
        Fused("f", a, b, "convex", c=c)
