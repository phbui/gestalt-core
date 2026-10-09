"""Embedding profiles (10): nomic stays the default, qwen3-4b reaches the builder through the same names (2026-10-08).

No model loads. The builder is driven with a stand-in for SentenceTransformer that records how it was constructed.
"""
from __future__ import annotations

import re
import sqlite3

import numpy as np
import pytest

from conftest import ConstEncoder, ConstSentenceTransformer, _load
from test_matryoshka import fresh_config


def test_default_profile_keeps_every_old_value(indexer):
    import gestalt_embed_config as ec

    assert ec.PROFILE == "nomic"
    assert ec.MODEL_NAME == "nomic-ai/nomic-embed-text-v1.5"
    assert ec.MODEL_REVISION == "e9b6763023c676ca8431644204f50c2b100d9aab"
    assert (ec.CODE_REPO, ec.CODE_REVISION) == ("nomic-ai/nomic-bert-2048", "7710840340a098cfb869c4f65e87cf2b1b70caca")
    assert (ec.EMBED_DIM, ec.DOC_PREFIX, ec.QUERY_PREFIX) == (768, "search_document: ", "search_query: ")
    native = ec._native_class_available("nomic_bert")
    assert ec.TRUST_REMOTE_CODE is (not native)
    assert ec.MODEL_KWARGS == ({} if native else {"code_revision": ec.CODE_REVISION}) and ec.TOKENIZER_KWARGS == {}
    assert ec.TEXT_FORMAT == "v2-title"


def test_contract_names_are_all_exported(indexer):
    import gestalt_embed_config as ec

    for name in ("PROFILE MODEL_NAME MODEL_REVISION CODE_REPO CODE_REVISION EMBED_DIM DOC_PREFIX QUERY_PREFIX TRUST_REMOTE_CODE "
                 "MODEL_KWARGS TOKENIZER_KWARGS EMBED_DEVICE TEXT_FORMAT SEARCH_DIR postprocess resolve_db_path").split():
        assert hasattr(ec, name), name


def test_qwen_profile_values(monkeypatch):
    q = fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b")
    assert q.PROFILE == "qwen3-4b" and q.MODEL_NAME == "Qwen/Qwen3-Embedding-4B"
    assert re.fullmatch(r"[0-9a-f]{40}", q.MODEL_REVISION)
    assert q.EMBED_DIM == 2560 and q.DOC_PREFIX == "" and q.TRUST_REMOTE_CODE is False
    assert q.QUERY_PREFIX.startswith("Instruct: ") and q.QUERY_PREFIX.endswith("\nQuery:")
    assert q.TOKENIZER_KWARGS == {"padding_side": "left"}
    assert q.MODEL_KWARGS.get("torch_dtype") == "bfloat16"


def test_unknown_profile_warns_and_uses_nomic(monkeypatch, capsys):
    c = fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="nope")
    assert c.PROFILE == "nomic" and "unknown GESTALT_EMBED_PROFILE" in capsys.readouterr().err


def test_device_and_search_dir(monkeypatch, tmp_path):
    from pathlib import Path

    c = fresh_config(monkeypatch)
    assert c.EMBED_DEVICE == "cpu" and c.SEARCH_DIR is None
    assert c.resolve_db_path(Path("/x/.search/gestalt.db")) == Path("/x/.search/gestalt.db")
    c = fresh_config(monkeypatch, GESTALT_EMBED_DEVICE="cuda", GESTALT_SEARCH_DIR=str(tmp_path / "side"))
    assert c.EMBED_DEVICE == "cuda" and c.SEARCH_DIR == tmp_path / "side"
    assert c.resolve_db_path(Path("/x/.search/gestalt.db")) == tmp_path / "side" / "gestalt.db"


def test_chunk_text_layouts(monkeypatch):
    c = fresh_config(monkeypatch)
    assert c.chunk_text("home-mesh", "", "Fleet", "body") == "search_document: home mesh — Fleet\n\nbody"
    assert c.chunk_text("home-mesh", "Home mesh", "Fleet", "body") == "search_document: Home mesh (home mesh) — Fleet\n\nbody"
    assert c.chunk_text("home-mesh", "Home mesh", "Fleet", "body", prefix="") == "Home mesh (home mesh) — Fleet\n\nbody"
    q = fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b")
    assert q.chunk_text("a-b", "T", "H", "c") == "T (a b) — H\n\nc"


# --- the builder constructor and index_meta under each profile ----------------------------------


class StubST(ConstSentenceTransformer):
    dim = 768


def use_profile(monkeypatch, indexer, cfg):
    """Make the builder behave as if it had started with cfg's profile: swap the config and the constants derived from it at import."""
    monkeypatch.setattr(indexer, "_ec", cfg)
    for name in ("MODEL_NAME", "MODEL_REVISION", "DOC_PREFIX", "QUERY_PREFIX"):
        monkeypatch.setattr(indexer, name, getattr(cfg, name))


@pytest.fixture
def corpus(tmp_path, monkeypatch, indexer):
    pytest.importorskip("sqlite_vec")
    kd = tmp_path / "knowledge"
    kd.mkdir()
    (kd / "deploy-notes.md").write_text("---\ntitle: Deploy notes\n---\n## Steps ^steps\n\nrun the deploy script\n", encoding="utf-8")
    search = tmp_path / "side"
    for name, val in (("GESTALT_DIR", tmp_path), ("KNOWLEDGE_DIR", kd), ("SEARCH_DIR", search),
                      ("DB_PATH", search / "gestalt.db"), ("BUILD_PATH", search / "gestalt.db.building"),
                      ("RULES_DIR", tmp_path / ".claude" / "rules")):
        monkeypatch.setattr(indexer, name, val, raising=False)
    monkeypatch.delenv("GESTALT_EMBED_DEVICE", raising=False)
    monkeypatch.delenv("GESTALT_LATE_CHUNKING", raising=False)
    monkeypatch.setattr(StubST, "dim", 768)
    return search


def build(indexer):
    import sqlite_vec

    indexer._build_index_locked(sqlite_vec, StubST)
    return sqlite3.connect(indexer.DB_PATH)


def test_default_constructor_call_pins_the_weights(corpus, indexer):
    build(indexer)
    kw = StubST.last.kw
    assert StubST.last.name == "nomic-ai/nomic-embed-text-v1.5"
    assert kw["revision"] == "e9b6763023c676ca8431644204f50c2b100d9aab"
    assert kw["trust_remote_code"] is (indexer._ec.LOAD_PATH == "remote-code")


@pytest.mark.parametrize("mode,path,remote", [("1", "remote-code", True), ("0", "native", False)])
def test_load_path_follows_the_trust_setting(corpus, indexer, monkeypatch, mode, path, remote):
    """The builder passes the remote code pin only when the remote code runs, and LOAD_PATH names which way it went."""
    cfg = fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="nomic", GESTALT_TRUST_REMOTE_CODE=mode)
    assert cfg.LOAD_PATH == path
    use_profile(monkeypatch, indexer, cfg)
    build(indexer)
    kw = StubST.last.kw
    assert kw["trust_remote_code"] is remote
    assert ("code_revision" in kw.get("model_kwargs", {})) is remote


def test_the_qwen_profile_always_loads_natively(monkeypatch):
    cfg = fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b", GESTALT_TRUST_REMOTE_CODE="1")
    assert cfg.LOAD_PATH == "native" and cfg.TRUST_REMOTE_CODE is False


def test_qwen_profile_reaches_the_constructor(corpus, indexer, monkeypatch):
    q = fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b")
    use_profile(monkeypatch, indexer, q)
    monkeypatch.setattr(StubST, "dim", 2560)
    monkeypatch.setenv("GESTALT_EMBED_DEVICE", "cuda")
    db = build(indexer)
    kw = StubST.last.kw
    assert StubST.last.name == "Qwen/Qwen3-Embedding-4B"
    assert kw["revision"] == q.MODEL_REVISION and kw["trust_remote_code"] is False and kw["device"] == "cuda"
    assert kw["model_kwargs"] == {"torch_dtype": "bfloat16"} and kw["tokenizer_kwargs"] == {"padding_side": "left"}
    meta = dict(db.execute("SELECT key, value FROM index_meta"))
    assert meta["embed_profile"] == "qwen3-4b" and meta["model_name"] == "Qwen/Qwen3-Embedding-4B"
    assert meta["embed_dim"] == "2560" and meta["doc_prefix"] == "" and meta["query_prefix"] == q.QUERY_PREFIX


def test_qwen_documents_carry_no_prefix_but_keep_the_title(corpus, indexer, monkeypatch):
    use_profile(monkeypatch, indexer, fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b"))
    monkeypatch.setattr(StubST, "dim", 2560)
    build(indexer)
    assert StubST.last._m.seen == ["Deploy notes (deploy notes) — Steps\n\nrun the deploy script"]


def test_profiles_never_share_cache_keys(indexer, monkeypatch):
    nomic = indexer.chunk_hash("same text")
    use_profile(monkeypatch, indexer, fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b"))
    assert indexer.chunk_hash("same text") != nomic


def test_query_side_is_prefix_plus_query(corpus, indexer, monkeypatch):
    """The server's one query-side rule is QUERY_PREFIX + query. A profile expresses prompt_name='query' only through the prefix."""
    import gestalt_embed_config as ec

    q_prefix = fresh_config(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b").QUERY_PREFIX
    monkeypatch.setattr(ec, "QUERY_PREFIX", q_prefix)
    monkeypatch.setattr(indexer, "QUERY_PREFIX", q_prefix)
    build(indexer)
    srv = _load("gestalt_mcp_server_profiles", "tools/gestalt-mcp-server.py")
    model = ConstEncoder(768)
    monkeypatch.setenv("GESTALT_SEARCH_MODE", "hybrid")
    monkeypatch.setattr(srv, "DB_PATH", indexer.DB_PATH, raising=False)
    monkeypatch.setattr(srv, "get_model", lambda: model, raising=False)
    rows = srv.gestalt_search("how do I deploy", limit=3, semantic=True)
    assert rows and "error" not in rows[0]
    assert model.seen == [q_prefix + "how do I deploy"]


# --- beir_bench flags ---------------------------------------------------------------------------


@pytest.fixture
def bench(indexer):
    return _load("beir_bench_flags", "evals/retrieval/beir_bench.py")


def test_bench_exports_profile_fusion_and_alpha_before_the_imports(bench, monkeypatch):
    import os

    for k in ("GESTALT_EMBED_PROFILE", "GESTALT_FUSION", "GESTALT_FUSION_ALPHA"):
        monkeypatch.setenv(k, "")  # so teardown restores the old state
    bench._export_flag_env(["--datasets", "beir/scifact", "--embed-profile", "qwen3-4b", "--fusion=convex", "--alpha", "0.4"])
    assert (os.environ["GESTALT_EMBED_PROFILE"], os.environ["GESTALT_FUSION"], os.environ["GESTALT_FUSION_ALPHA"]) == ("qwen3-4b", "convex", "0.4")


def test_bench_rerank_ids_hands_rows_to_gestalt_rank(bench, monkeypatch):
    import sys
    import types

    seen = {}

    def rerank_rows(query, rows, alias):
        seen.update(query=query, alias=alias, fields=sorted(rows[0]))
        return list(reversed(rows)), None, {"fallback": "none"}

    monkeypatch.setitem(sys.modules, "gestalt_rank", types.SimpleNamespace(rerank_rows=rerank_rows))
    rows = [{"slug": f"d{i}", "content": f"text {i}"} for i in range(3)]
    calls = {}

    def search(db, q, limit, mode, **kw):
        calls.update(kw)
        return rows

    monkeypatch.setattr(bench.harness, "search", search)
    ids = ["a", "b", "c"]
    got, info = bench.rerank_ids(None, "q", ids, depth=3, alias="bge")
    assert got == ["c", "b", "a"] and info["fallback"] == "none"
    assert calls.get("rerank") is False  # the pool is the base hybrid order; rerank_ids reranks it itself, never twice
    assert seen["alias"] == "bge" and {"title", "heading", "content", "slug"} <= set(seen["fields"])


def test_bench_environment_names_profile_rerank_and_fusion(bench):
    import bench_stats  # the environment dict moved there (QAL-001), beir_bench re-exports it

    src = open(bench_stats.__file__, encoding="utf-8").read()
    for token in ('"embed_profile": ec.PROFILE', '"fusion":', '"rerank":'):
        assert token in src


def test_builder_builds_into_gestalt_search_dir(monkeypatch, tmp_path):
    """GESTALT_SEARCH_DIR moves the builder's db, stamp and lock without touching <repo>/.search."""
    import sys

    monkeypatch.setenv("GESTALT_SEARCH_DIR", str(tmp_path / "side"))
    monkeypatch.delitem(sys.modules, "gestalt_embed_config", raising=False)  # config reads the variable at import
    mod = _load("gestalt_index_builder_side", "tools/gestalt-index-builder.py")
    assert mod.SEARCH_DIR == tmp_path / "side"
    assert mod.DB_PATH == tmp_path / "side" / "gestalt.db" and mod.STAMP_PATH.parent == tmp_path / "side"


def test_nomic_profile_pins_the_remote_modelling_code(monkeypatch):
    """The nomic weights are pinned by revision and the modelling code it executes lives in a second repo. The profile passes that repo's commit as code_revision, so a Hub push cannot change what runs."""
    import importlib, sys
    monkeypatch.setenv("GESTALT_EMBED_PROFILE", "nomic")
    sys.path.insert(0, "tools")
    monkeypatch.setenv("GESTALT_TRUST_REMOTE_CODE", "1")  # force the remote path, whatever the installed library can do natively
    ec = importlib.reload(importlib.import_module("gestalt_embed_config"))
    assert ec.TRUST_REMOTE_CODE is True and ec.MODEL_KWARGS.get("code_revision") == ec.CODE_REVISION
    assert len(ec.CODE_REVISION) == 40


def test_trust_remote_code_modes(monkeypatch):
    """auto follows the installed library, 1 forces the pinned remote code, 0 forces the native class, and the qwen profile never runs remote code."""
    import importlib, sys
    sys.path.insert(0, "tools")
    monkeypatch.setenv("GESTALT_EMBED_PROFILE", "nomic")
    ec = importlib.import_module("gestalt_embed_config")
    for mode, expect_native_lib, want in (("auto", True, False), ("auto", False, True), ("1", True, True), ("0", False, False)):
        monkeypatch.setenv("GESTALT_TRUST_REMOTE_CODE", mode)
        monkeypatch.setattr(ec, "_native_class_available", lambda t, v=expect_native_lib: v)
        assert ec._resolve_trust_remote_code(True) is want, (mode, expect_native_lib)
    monkeypatch.setenv("GESTALT_TRUST_REMOTE_CODE", "1")
    assert ec._resolve_trust_remote_code(False) is False


def test_importing_the_config_does_not_import_transformers():
    """The probe for the native nomic class reads package metadata. Importing transformers costs over a second and a search server that only answers FTS must not pay it."""
    import subprocess, sys, time
    from pathlib import Path
    code = ("import sys, time; sys.path.insert(0, %r); t = time.perf_counter(); import gestalt_embed_config; "
            "print('transformers' in sys.modules, time.perf_counter() - t)" % str(Path(__file__).resolve().parent.parent / "tools"))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out[0] == "False"
    assert float(out[1]) < 0.5, "the import took far longer than a metadata read"


@pytest.mark.parametrize("installed,native", [("5.4.9", False), ("5.5.0", True), ("5.16.1", True), ("6.0.0.dev0", True), ("4.57.1", False)])
def test_native_class_availability_reads_the_version(monkeypatch, installed, native):
    import importlib.metadata
    cfg = fresh_config(monkeypatch)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: installed)
    assert cfg._native_class_available("nomic_bert") is native


def test_a_missing_transformers_means_remote_code_is_needed(monkeypatch):
    import importlib.metadata
    cfg = fresh_config(monkeypatch)

    def gone(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", gone)
    assert cfg._native_class_available("nomic_bert") is False
