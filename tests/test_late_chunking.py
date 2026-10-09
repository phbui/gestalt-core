"""Late chunking (9a): one forward pass per document, token vectors pooled per chunk span (2026-10-08).

Everything runs on a stub model whose token vectors are deterministic and mix in the whole window's mean, so a
token's vector depends on its context. That is the property late chunking exists to use. No real model loads.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import sys

import numpy as np
import pytest

DIM = 768


class StubTok:
    """Whitespace tokenizer with character offsets. [CLS] and [SEP] get offset (0, 0) and a special-token mask of 1."""

    def __call__(self, text, add_special_tokens=True, return_offsets_mapping=False, return_special_tokens_mask=False,
                 truncation=False, max_length=None):
        offs = [m.span() for m in re.finditer(r"\S+", text)]
        mask = [0] * len(offs)
        if add_special_tokens:
            offs, mask = [(0, 0)] + offs + [(0, 0)], [1] + mask + [1]
        if truncation and max_length:
            offs, mask = offs[:max_length], mask[:max_length]
        out = {"offset_mapping": offs}
        if return_special_tokens_mask:
            out["special_tokens_mask"] = mask
        return out


def _word_vec(word: str) -> np.ndarray:
    seed = int(hashlib.md5(word.encode()).hexdigest()[:8], 16)
    return np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)


class StubModel:
    def __init__(self, max_seq_length=16, break_on: str | None = None):
        self.tokenizer = StubTok()
        self.max_seq_length = max_seq_length
        self.break_on = break_on  # a word that makes token vectors misalign, to force the per-document fallback
        self.calls: list = []
        self.texts: list = []  # (output_value, text) per encoded string

    def get_sentence_embedding_dimension(self):
        return DIM

    def encode(self, inputs, output_value="sentence_embedding", **_kw):
        single = isinstance(inputs, str)
        rows = []
        for text in [inputs] if single else inputs:
            self.texts.append((output_value, text))
            words = text.split()[: self.max_seq_length - 2]
            self.calls.append((output_value, len(words)))
            tv = np.array([_word_vec(w) for w in words]) if words else np.zeros((0, DIM), np.float32)
            ctx = tv.mean(axis=0) if words else np.zeros(DIM, np.float32)
            mixed = tv + 0.5 * ctx  # every token sees the whole window
            if output_value == "token_embeddings":
                full = np.vstack([ctx[None], mixed, ctx[None]]).astype(np.float32)
                if self.break_on and self.break_on in text:
                    full = full[:-1]
                rows.append(full)
            else:
                v = mixed.mean(axis=0)
                rows.append((v / np.linalg.norm(v)).astype(np.float32))
        return rows[0] if single else (rows if output_value == "token_embeddings" else np.stack(rows))


@pytest.fixture
def ib(indexer):
    return indexer


def _spans(parts, prefix=""):
    pos, out = len(prefix), []
    for p in parts:
        out.append((pos, pos + len(p)))
        pos += len(p) + 2
    return out


def _unit(v):
    return v / np.linalg.norm(v)


def test_windows_one_when_it_fits_and_overlap_half_when_not(ib):
    assert ib.late_windows(10, 10) == [(0, 10)]
    wins = ib.late_windows(30, 10)
    assert wins == [(0, 10), (5, 15), (10, 20), (15, 25), (20, 30)]
    assert ib.late_windows(33, 10)[-1] == (25, 33), "the last window ends at the last token and may be short"


def test_single_window_pools_the_span_tokens(ib):
    model = StubModel(max_seq_length=64)
    parts = ["alpha beta gamma", "delta epsilon zeta"]
    doc = "\n\n".join(parts)
    vecs, n = ib.embed_late(model, doc, _spans(parts))
    assert n == 1
    toks = model.encode(doc, output_value="token_embeddings")  # row 0 is [CLS], words follow
    expect0 = _unit(toks[1:4].mean(axis=0))
    expect1 = _unit(toks[4:7].mean(axis=0))
    np.testing.assert_allclose(vecs[0], expect0, rtol=1e-5, atol=1e-6)
    np.testing.assert_allclose(vecs[1], expect1, rtol=1e-5, atol=1e-6)
    assert abs(np.linalg.norm(vecs[0]) - 1.0) < 1e-5


def test_late_vector_sees_the_rest_of_the_document(ib):
    """The point of late chunking: the same chunk text pools to a different vector in another document."""
    model = StubModel(max_seq_length=64)
    a = ["alpha beta gamma", "delta epsilon zeta"]
    b = ["alpha beta gamma", "completely different neighbour"]
    va, _ = ib.embed_late(model, "\n\n".join(a), _spans(a))
    vb, _ = ib.embed_late(model, "\n\n".join(b), _spans(b))
    assert not np.allclose(va[0], vb[0], atol=1e-4)
    ordinary = model.encode("alpha beta gamma")
    assert not np.allclose(va[0], ordinary, atol=1e-4)


def test_sliding_window_picks_the_most_central_window(ib):
    words = [f"w{i:02d}" for i in range(30)]
    doc = " ".join(words)
    model = StubModel(max_seq_length=12)  # cap 10 tokens, windows (0,10) (5,15) (10,20) (15,25) (20,30)
    start = doc.index("w12")
    span = (start, doc.index("w14") + 3)  # words 12..14
    vecs, n = ib.embed_late(model, doc, [span])
    assert n == 5
    # windows (5,15) and (10,20) hold the span. (10,20) keeps two words on its left and five on its right. (5,15) keeps none on its right.
    win_text = " ".join(words[10:20])
    toks = model.encode(win_text, output_value="token_embeddings")
    expect = _unit(toks[1 + 2 : 1 + 5].mean(axis=0))  # words 12..14 are window words 2..4
    np.testing.assert_allclose(vecs[0], expect, rtol=1e-5, atol=1e-6)


def test_a_chunk_at_the_document_start_pools_in_the_first_window(ib):
    words = [f"w{i:02d}" for i in range(30)]
    doc = " ".join(words)
    model = StubModel(max_seq_length=12)
    vecs, _ = ib.embed_late(model, doc, [(0, 11)])  # words 0..2
    toks = model.encode(" ".join(words[0:10]), output_value="token_embeddings")
    np.testing.assert_allclose(vecs[0], _unit(toks[1:4].mean(axis=0)), rtol=1e-5, atol=1e-6)


def test_chunk_wider_than_a_window_is_left_to_ordinary_encoding(ib):
    words = [f"w{i:02d}" for i in range(30)]
    doc = " ".join(words)
    model = StubModel(max_seq_length=12)
    vecs, _ = ib.embed_late(model, doc, [(0, 13 * 4), (0, 11)])
    assert vecs[0] is None and vecs[1] is not None


def test_misaligned_token_vectors_raise(ib):
    model = StubModel(max_seq_length=64, break_on="bad")
    with pytest.raises(ValueError, match="do not line up"):
        ib.embed_late(model, "good bad", [(0, 8)])


def test_cache_key_is_salted_with_pooling_and_document_sha(ib):
    plain = ib.chunk_hash("t")
    assert plain == hashlib.sha256(f"{ib.MODEL_NAME}\x00t".encode()).hexdigest(), "an unsalted key must stay the old key"
    s1, s2 = ib.cache_salt(True, "aaaa"), ib.cache_salt(True, "bbbb")
    assert s1 != s2 and "pooling=late" in s1 and "doc=aaaa" in s1
    assert ib.chunk_hash("t", s1) != plain and ib.chunk_hash("t", s1) != ib.chunk_hash("t", s2)
    assert ib.cache_salt(False, None) == ""


# --- end to end through _build_index_locked with the stub model ---------------------------------


class StubST:
    """Stands in for SentenceTransformer in _build_index_locked. Records the constructor call."""
    last: "StubST | None" = None
    model_cls = StubModel

    def __init__(self, name, **kw):
        self.name, self.kw = name, kw
        StubST.last = self
        self._m = StubST.model_cls(max_seq_length=24)

    def __getattr__(self, attr):
        return getattr(self._m, attr)


@pytest.fixture
def build(tmp_path, monkeypatch, indexer):
    pytest.importorskip("sqlite_vec")
    import sqlite_vec

    kd = tmp_path / "knowledge"
    kd.mkdir()
    search = tmp_path / ".search"
    for name, val in (("GESTALT_DIR", tmp_path), ("KNOWLEDGE_DIR", kd), ("SEARCH_DIR", search),
                      ("DB_PATH", search / "gestalt.db"), ("BUILD_PATH", search / "gestalt.db.building"),
                      ("RULES_DIR", tmp_path / ".claude" / "rules")):
        monkeypatch.setattr(indexer, name, val, raising=False)
    monkeypatch.delenv("GESTALT_LATE_CHUNKING", raising=False)
    monkeypatch.setattr(StubST, "model_cls", StubModel)

    def _run(entries: dict[str, str]):
        for slug, body in entries.items():
            (kd / f"{slug}.md").write_text(body, encoding="utf-8")
        indexer._build_index_locked(sqlite_vec, StubST)
        db = sqlite3.connect(search / "gestalt.db")
        return db

    _run.kd = kd
    return _run


ENTRIES = {
    "alpha-doc": "---\ntitle: Alpha doc\n---\n## One ^one\n\nfirst chunk words here\n\n## Two ^two\n\nsecond chunk words there\n",
    "beta-doc": "---\ntitle: Beta doc\n---\n## Only ^only\n\nthe lone chunk of beta\n",
}


def test_default_build_is_standard_pooling(build, capsys):
    db = build(ENTRIES)
    assert dict(db.execute("SELECT key, value FROM index_meta"))["pooling"] == "standard"
    assert "late-chunking" not in capsys.readouterr().err


def test_late_build_records_pooling_and_logs_once(build, monkeypatch, capsys):
    monkeypatch.setenv("GESTALT_LATE_CHUNKING", "1")
    db = build(ENTRIES)
    meta = dict(db.execute("SELECT key, value FROM index_meta"))
    assert meta["pooling"] == "late"
    err = capsys.readouterr().err
    line = [l for l in err.splitlines() if l.startswith("late-chunking: docs=")]
    assert len(line) == 1 and "docs=2 " in line[0] and "fallback=0" in line[0], err


def test_late_vectors_differ_from_ordinary_vectors(build, monkeypatch, indexer):
    import sqlite_vec

    db0 = build(ENTRIES)
    ordinary = {r[0]: r[1] for r in db0.execute("SELECT content_hash, 1 FROM sections_meta")}
    monkeypatch.setenv("GESTALT_LATE_CHUNKING", "1")
    db1 = build(ENTRIES)
    late_hashes = {r[0] for r in db1.execute("SELECT content_hash FROM sections_meta")}
    assert not (late_hashes & set(ordinary)), "late chunks must not reuse ordinary cache keys"
    db1.enable_load_extension(True)
    sqlite_vec.load(db1)
    db0.enable_load_extension(True)
    sqlite_vec.load(db0)
    v_late = np.frombuffer(db1.execute("SELECT embedding FROM sections_vec WHERE id = 0").fetchone()[0], np.float32)
    v_ord = np.frombuffer(db0.execute("SELECT embedding FROM sections_vec WHERE id = 0").fetchone()[0], np.float32)
    assert v_late.shape == v_ord.shape == (DIM,) and not np.allclose(v_late, v_ord, atol=1e-4)


def test_editing_one_chunk_changes_every_hash_of_its_document_only(build, monkeypatch):
    monkeypatch.setenv("GESTALT_LATE_CHUNKING", "1")
    before = dict(build(ENTRIES).execute("SELECT slug || block_id, content_hash FROM sections_meta"))
    edited = dict(ENTRIES, **{"alpha-doc": ENTRIES["alpha-doc"].replace("second chunk words there", "second chunk EDITED")})
    after = dict(build(edited).execute("SELECT slug || block_id, content_hash FROM sections_meta"))
    assert before["alpha-docone"] != after["alpha-docone"], "the untouched chunk shares a document with the edit"
    assert before["beta-doconly"] == after["beta-doconly"]


def test_misaligned_document_falls_back_and_still_embeds(build, monkeypatch, capsys):
    class Breaking(StubModel):
        def __init__(self, **kw):
            super().__init__(break_on="lone", **kw)

    monkeypatch.setattr(StubST, "model_cls", Breaking)
    monkeypatch.setenv("GESTALT_LATE_CHUNKING", "1")
    db = build(ENTRIES)
    err = capsys.readouterr().err
    assert "late-chunking: fallback doc=knowledge/beta-doc.md" in err
    assert "late-chunking: docs=2 " in err and "fallback=1" in err
    assert db.execute("SELECT count(*) FROM sections_meta").fetchone()[0] == 3


# --- review fixes (F2, 2026-10-08) --------------------------------------------------------------


PREFIX = "search_document: "


def test_every_window_after_the_first_carries_the_document_prefix(ib):
    words = [f"w{i:02d}" for i in range(30)]
    doc = PREFIX + " ".join(words)
    model = StubModel(max_seq_length=12)
    start = doc.index("w25")
    span = (start, doc.index("w27") + 3)
    vecs, n = ib.embed_late(model, doc, [span], PREFIX)
    assert n > 1
    windows = [t for kind, t in model.texts if kind == "token_embeddings"]
    assert windows[0].startswith(PREFIX) and all(t.startswith(PREFIX) for t in windows), windows
    assert all(len(t.split()) <= 12 - 2 for t in windows), "the cap leaves room for the prefix tokens"
    # The vector is the pool of exactly w25..w27 in some window. A wrong span shift would pool a neighbour.
    candidates = []
    for t in windows:
        ws = t.split()
        if "w25" in ws and "w27" in ws:
            toks = model.encode(t, output_value="token_embeddings")
            i = ws.index("w25") + 1  # row 0 is [CLS]
            candidates.append(_unit(toks[i : i + 3].mean(axis=0)))
    assert candidates and any(np.allclose(vecs[0], c, rtol=1e-5, atol=1e-6) for c in candidates)


class GrowTok(StubTok):
    """Retokenising a cut window yields three more tokens than the first pass counted. Real subword tokenizers do this mid-word."""

    full = ""

    def __call__(self, text, add_special_tokens=True, return_offsets_mapping=False, return_special_tokens_mask=False,
                 truncation=False, max_length=None):
        out = super().__call__(text, add_special_tokens, return_offsets_mapping, return_special_tokens_mask, False, None)
        if text != self.full and add_special_tokens:
            out["offset_mapping"] = out["offset_mapping"][:-1] + [(0, 1)] * 3 + out["offset_mapping"][-1:]
            if return_special_tokens_mask:
                out["special_tokens_mask"] = out["special_tokens_mask"][:-1] + [0] * 3 + out["special_tokens_mask"][-1:]
        if truncation and max_length:
            out = {k: v[:max_length] for k, v in out.items()}
        return out


def test_a_window_that_retokenises_past_max_len_raises_instead_of_losing_tail_tokens(ib):
    words = [f"w{i:02d}" for i in range(30)]
    doc = " ".join(words)
    model = StubModel(max_seq_length=12)
    model.tokenizer = GrowTok()
    model.tokenizer.full = doc
    with pytest.raises(ValueError, match="exceeds max_seq_length"):
        ib.embed_late(model, doc, [(0, 11)])


def test_fallback_chunks_are_not_cached_under_the_late_key(build, monkeypatch, capsys):
    class Breaking(StubModel):
        def __init__(self, **kw):
            super().__init__(break_on="lone", **kw)

    monkeypatch.setenv("GESTALT_LATE_CHUNKING", "1")
    monkeypatch.setattr(StubST, "model_cls", Breaking)
    db = build(ENTRIES)
    err = capsys.readouterr().err
    assert "fallback=1 fallback_chunks=1 " in err, err
    first = dict(db.execute("SELECT slug, content_hash FROM sections_meta WHERE slug = 'beta-doc'"))
    db.close()
    # The tokenizer is fixed. The next build must retry beta-doc late, not reuse the ordinary vector.
    monkeypatch.setattr(StubST, "model_cls", StubModel)
    db = build(ENTRIES)
    err = capsys.readouterr().err
    assert "fallback=0 " in err and "docs=1 " in err, err
    second = dict(db.execute("SELECT slug, content_hash FROM sections_meta WHERE slug = 'beta-doc'"))
    assert first != second, "the fallback vector was served from the late cache key"


def _vectors(db):
    import sqlite_vec

    db.enable_load_extension(True)
    sqlite_vec.load(db)
    return [np.frombuffer(r[0], np.float32) for r in db.execute("SELECT embedding FROM sections_vec ORDER BY id")]


class Loud(StubModel):
    """Sentence embeddings come back with norm 3, like the nomic pipeline that has no Normalize module. One document breaks."""

    def __init__(self, **kw):
        super().__init__(break_on="lone", **kw)

    def encode(self, inputs, output_value="sentence_embedding", **kw):
        out = super().encode(inputs, output_value=output_value, **kw)
        return out * 3 if output_value == "sentence_embedding" else out


def test_late_build_stores_unit_norm_vectors_including_fallbacks_and_skills(build, monkeypatch, indexer, tmp_path):
    skill = tmp_path / ".claude" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: A demo skill for tests\n---\n\nBody.\n", encoding="utf-8")
    monkeypatch.setattr(StubST, "model_cls", Loud)
    monkeypatch.setenv("GESTALT_LATE_CHUNKING", "1")
    db = build(ENTRIES)
    norms = [float(np.linalg.norm(v)) for v in _vectors(db)]
    assert len(norms) == 3 and all(abs(x - 1.0) < 1e-4 for x in norms), norms
    skill_norm = float(np.linalg.norm(np.frombuffer(db.execute("SELECT embedding FROM skills_vec").fetchone()[0], np.float32)))
    assert abs(skill_norm - 1.0) < 1e-4, skill_norm
    assert dict(db.execute("SELECT key, value FROM index_meta"))["normalized"] == "1"


def test_default_build_keeps_raw_norms_and_no_normalized_flag(build, monkeypatch):
    monkeypatch.setattr(StubST, "model_cls", Loud)
    db = build(ENTRIES)
    norms = [float(np.linalg.norm(v)) for v in _vectors(db)]
    assert all(abs(x - 3.0) < 1e-3 for x in norms), norms
    assert "normalized" not in dict(db.execute("SELECT key, value FROM index_meta"))


# --- GPU check follows the explicit device (BUILD-007) ------------------------------------------


def _pretend_gpu_is_wasted(ib, monkeypatch):
    import shutil
    import subprocess
    import types

    monkeypatch.delenv("GESTALT_ALLOW_CPU_EMBED", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    fake = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False), __version__="x")
    monkeypatch.setitem(sys.modules, "torch", fake)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="550.0"))


def test_unset_device_still_refuses_a_wasted_gpu(ib, monkeypatch):
    _pretend_gpu_is_wasted(ib, monkeypatch)
    monkeypatch.delenv("GESTALT_EMBED_DEVICE", raising=False)
    with pytest.raises(SystemExit):
        ib._warn_if_gpu_wasted(5000)


def test_an_explicit_device_skips_the_wasted_gpu_check(ib, monkeypatch):
    _pretend_gpu_is_wasted(ib, monkeypatch)
    monkeypatch.setenv("GESTALT_EMBED_DEVICE", "cpu")
    ib._warn_if_gpu_wasted(5000)


# --- batched windows and fallback caching (BUILD-005, BUILD-006) ---------------------------------


class OneAtATime:
    """Wraps a model and encodes a list of texts one string per call, the way embed_late used to."""

    def __init__(self, inner):
        self.inner, self.tokenizer, self.max_seq_length = inner, inner.tokenizer, inner.max_seq_length
        self.encode_calls = 0

    def encode(self, inputs, **kw):
        kw.pop("batch_size", None)
        texts = [inputs] if isinstance(inputs, str) else inputs
        self.encode_calls += 1
        out = [self.inner.encode(t, **kw) for t in texts]
        return out[0] if isinstance(inputs, str) else out


def test_all_windows_go_to_the_encoder_in_one_call_with_the_same_vectors(ib):
    words = [f"w{i:02d}" for i in range(40)]
    doc = " ".join(words)
    spans = [(doc.index(f"w{a:02d}"), doc.index(f"w{b:02d}") + 3) for a, b in ((0, 3), (9, 13), (18, 22), (36, 39))]

    class Counting(StubModel):
        def encode(self, inputs, **kw):
            self.n_calls = getattr(self, "n_calls", 0) + 1
            return super().encode(inputs, **kw)

    batched = Counting(max_seq_length=12)
    reference = OneAtATime(StubModel(max_seq_length=12))
    got, n_win = ib.embed_late(batched, doc, spans, "", batch_size=4)
    # The reference path encodes each window as its own string, which is what one window per call meant.
    expect_vecs, n_ref = ib.embed_late(reference, doc, spans)
    assert n_win == n_ref and n_win > 2
    assert batched.n_calls == 1
    for g, e in zip(got, expect_vecs):
        np.testing.assert_array_equal(g, e)


def test_a_fallback_chunk_is_not_embedded_again_on_the_next_build(build, monkeypatch, capsys):
    class Breaking(StubModel):
        def __init__(self, **kw):
            super().__init__(break_on="lone", **kw)

    monkeypatch.setenv("GESTALT_LATE_CHUNKING", "1")
    monkeypatch.setattr(StubST, "model_cls", Breaking)
    build(ENTRIES).close()
    first = [t for kind, t in StubST.last._m.texts if kind == "sentence_embedding" and "lone" in t]
    assert len(first) == 1
    capsys.readouterr()
    db = build(ENTRIES)
    again = [t for kind, t in StubST.last._m.texts if kind == "sentence_embedding" and "lone" in t]
    assert again == [], "the fallback chunk was encoded the ordinary way a second time"
    assert db.execute("SELECT count(*) FROM sections_meta").fetchone()[0] == 3
