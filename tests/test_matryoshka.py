"""Matryoshka widths (9b): layer-norm, slice, L2 normalise, applied at every encode site (2026-10-08)."""
from __future__ import annotations

import hashlib
import sqlite3

import numpy as np
import pytest

from conftest import ConstSentenceTransformer, _load

ENV_KEYS = ("GESTALT_EMBED_PROFILE", "GESTALT_EMBED_DIM", "GESTALT_EMBED_DEVICE", "GESTALT_SEARCH_DIR")


def fresh_config(monkeypatch, **env):
    """A new copy of gestalt_embed_config, evaluated under the given environment."""
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return _load("gestalt_embed_config_fresh", "tools/gestalt_embed_config.py")


@pytest.fixture
def ec(indexer):
    """The shared config module. Needs `indexer` because loading the builder puts tools/ on sys.path."""
    import gestalt_embed_config

    return gestalt_embed_config


def _batch(n=3, d=768, seed=7):
    v = np.random.default_rng(seed).standard_normal((n, d)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_full_width_is_identity(ec, monkeypatch):
    monkeypatch.setattr(ec, "EMBED_DIM", ec.FULL_DIM)
    x = _batch()
    out = ec.postprocess(x)
    assert out.dtype == x.dtype and out.shape == x.shape and out.tobytes() == x.tobytes()


@pytest.mark.parametrize("dim", [512, 256, 128, 64])
def test_narrow_width_has_unit_norm_and_right_length(ec, monkeypatch, dim):
    monkeypatch.setattr(ec, "EMBED_DIM", dim)
    out = ec.postprocess(_batch())
    assert out.shape == (3, dim) and out.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)
    one = ec.postprocess(_batch()[0])
    assert one.shape == (dim,)
    np.testing.assert_allclose(one, out[0], atol=1e-6)


def test_narrow_width_matches_the_model_card_recipe(ec, monkeypatch):
    torch = pytest.importorskip("torch")
    F = torch.nn.functional
    monkeypatch.setattr(ec, "EMBED_DIM", 256)
    x = _batch()
    t = torch.from_numpy(x)
    expect = F.normalize(F.layer_norm(t, normalized_shape=(768,))[:, :256], p=2, dim=1).numpy()
    np.testing.assert_allclose(ec.postprocess(x), expect, atol=1e-5)


def test_a_vector_narrower_than_the_width_is_an_error(ec, monkeypatch):
    monkeypatch.setattr(ec, "EMBED_DIM", 256)
    with pytest.raises(ValueError):
        ec.postprocess(np.ones(128, np.float32))


def test_env_width_is_validated(monkeypatch, capsys):
    assert fresh_config(monkeypatch, GESTALT_EMBED_DIM="256").EMBED_DIM == 256
    assert fresh_config(monkeypatch).EMBED_DIM == 768
    bad = fresh_config(monkeypatch, GESTALT_EMBED_DIM="100")
    assert bad.EMBED_DIM == 768 and "not a Matryoshka width" in capsys.readouterr().err
    assert fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b", GESTALT_EMBED_DIM="1024").EMBED_DIM == 1024


def test_qwen_width_skips_layer_norm(monkeypatch):
    q = fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b", GESTALT_EMBED_DIM="1024")
    x = _batch(2, 2560)
    expect = x[:, :1024] / np.linalg.norm(x[:, :1024], axis=1, keepdims=True)
    np.testing.assert_allclose(q.postprocess(x), expect, atol=1e-5)


def test_cache_key_is_salted_with_the_width(indexer, ec, monkeypatch):
    monkeypatch.setattr(ec, "EMBED_DIM", ec.FULL_DIM)
    assert indexer.cache_salt(False, None) == ""
    assert indexer.chunk_hash("t") == hashlib.sha256(f"{indexer.MODEL_NAME}\x00t".encode()).hexdigest()
    monkeypatch.setattr(ec, "EMBED_DIM", 256)
    assert indexer.cache_salt(False, None) == "dim=256"
    assert indexer.chunk_hash("t", indexer.cache_salt(False, None)) != indexer.chunk_hash("t")


# --- every encode site in the builder ------------------------------------------------------------


class StubST(ConstSentenceTransformer):
    dim = 768


@pytest.fixture
def built_at(tmp_path, monkeypatch, indexer, ec):
    pytest.importorskip("sqlite_vec")
    import sqlite_vec

    kd = tmp_path / "knowledge"
    kd.mkdir()
    (kd / "note.md").write_text("---\ntitle: Note\n---\n## A ^a\n\nbody one\n\n## B ^b\n\nbody two\n", encoding="utf-8")
    skill = tmp_path / ".claude" / "skills" / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Does a demo thing\n---\nbody\n", encoding="utf-8")
    search = tmp_path / ".search"
    for name, val in (("GESTALT_DIR", tmp_path), ("KNOWLEDGE_DIR", kd), ("SEARCH_DIR", search),
                      ("DB_PATH", search / "gestalt.db"), ("BUILD_PATH", search / "gestalt.db.building"),
                      ("RULES_DIR", tmp_path / ".claude" / "rules")):
        monkeypatch.setattr(indexer, name, val, raising=False)

    def _build(dim: int):
        monkeypatch.setattr(ec, "EMBED_DIM", dim)
        indexer._build_index_locked(sqlite_vec, StubST)
        db = sqlite3.connect(search / "gestalt.db")
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        return db

    return _build


@pytest.mark.parametrize("dim", [768, 256])
def test_builder_writes_vectors_at_the_configured_width(built_at, dim):
    db = built_at(dim)
    for table in ("sections_vec", "skills_vec"):
        rows = db.execute(f"SELECT embedding FROM {table}").fetchall()
        assert rows, table
        assert all(len(r[0]) == dim * 4 for r in rows), f"{table} rows are not {dim} floats wide"
        assert f"FLOAT[{dim}]" in db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    assert dict(db.execute("SELECT key, value FROM index_meta"))["embed_dim"] == str(dim)


def test_narrow_vectors_are_unit_length(built_at):
    db = built_at(256)
    for (blob,) in db.execute("SELECT embedding FROM sections_vec"):
        assert abs(float(np.linalg.norm(np.frombuffer(blob, np.float32))) - 1.0) < 1e-5


def test_changing_the_width_changes_every_content_hash(built_at):
    wide = {r[0] for r in built_at(768).execute("SELECT content_hash FROM sections_meta")}
    narrow = {r[0] for r in built_at(256).execute("SELECT content_hash FROM sections_meta")}
    assert not (wide & narrow)
