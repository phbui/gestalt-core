"""L7: `sensitivity` is stored by the index builder and enforced by the MCP server and the prompt hook (2026-10-06)."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import _load

ENTRIES = {
    "open-note": "---\ntype: note\nsensitivity: public\n---\n# Open\n\n## Zebra facts\n\nzebrafish colony notes for everyone.\n",
    "plain-note": "---\ntype: note\n---\n# Plain\n\n## Zebra habits\n\nzebrafish feeding schedule.\n",
    "secret-note": "---\ntype: note\nsensitivity: restricted\n---\n# Secret\n\n## Zebra vault\n\nzebrafish vault passphrase lives elsewhere.\n",
}


@pytest.fixture
def corpus(tmp_path, monkeypatch, indexer):
    kd = tmp_path / "knowledge"
    kd.mkdir()
    for slug, body in ENTRIES.items():
        (kd / f"{slug}.md").write_text(body, encoding="utf-8")
    search = tmp_path / ".search"
    for name, val in (("GESTALT_DIR", tmp_path), ("KNOWLEDGE_DIR", kd), ("SEARCH_DIR", search),
                      ("DB_PATH", search / "gestalt.db"), ("LOCK_PATH", search / "build.lock"),
                      ("BUILD_PATH", search / "gestalt.db.building"), ("STAMP_PATH", search / "gestalt.db.stamp"),
                      ("RULES_DIR", tmp_path / ".claude" / "rules")):
        monkeypatch.setattr(indexer, name, val, raising=False)
    return tmp_path


@pytest.fixture
def built(corpus, indexer):
    indexer.build_index(force_fts_only=True)
    return corpus


@pytest.fixture
def srv(built, monkeypatch):
    mod = _load("gestalt_mcp_server_sens", "tools/gestalt-mcp-server.py")
    monkeypatch.setattr(mod, "GESTALT_DIR", built)
    monkeypatch.setattr(mod, "KNOWLEDGE_DIR", built / "knowledge")
    monkeypatch.setattr(mod, "DB_PATH", built / ".search" / "gestalt.db")
    return mod


def test_entry_sensitivity_parse_and_default(corpus, indexer) -> None:
    kd = corpus / "knowledge"
    assert indexer.entry_sensitivity(kd / "open-note.md") == "public"
    assert indexer.entry_sensitivity(kd / "plain-note.md") == "unpublished"
    assert indexer.entry_sensitivity(kd / "secret-note.md") == "restricted"
    (kd / "typo.md").write_text("---\nsensitivity: secrt\n---\nbody\n", encoding="utf-8")
    assert indexer.entry_sensitivity(kd / "typo.md") == "unpublished"
    (kd / "nofm.md").write_text("no frontmatter\n", encoding="utf-8")
    assert indexer.entry_sensitivity(kd / "nofm.md") == "unpublished"


def test_builder_stores_sensitivity_per_section(built) -> None:
    db = sqlite3.connect(built / ".search" / "gestalt.db")
    rows = dict(db.execute("SELECT slug, sensitivity FROM sections_meta").fetchall())
    assert rows == {"open-note": "public", "plain-note": "unpublished", "secret-note": "restricted"}


def test_embedding_cache_key_unchanged(indexer) -> None:
    """The cache key is sha256(model identity, NUL, prefixed text). L7 must not touch it. Checked by calling the function, not by
    reading the source (2026-10-11); the identity carries the revision, the normalisation and the dtype since that day."""
    import hashlib
    ec = indexer._ec  # the builder's own config object, which the key reads
    ident = f"{indexer.MODEL_NAME}\x00rev={indexer.MODEL_REVISION or ''}\x00norm={int(bool(ec.EMBED_NORMALIZE))}\x00dtype={ec.EMBED_DTYPE}"
    assert indexer.chunk_hash("search_document: x") == hashlib.sha256(f"{ident}\x00search_document: x".encode("utf-8")).hexdigest()


def test_fts_withholds_restricted_and_says_so(srv) -> None:
    rows = srv.gestalt_search_fts("zebrafish", limit=10)
    slugs = {r["slug"] for r in rows if not r.get("withheld")}
    assert slugs == {"open-note", "plain-note"}
    notice = [r for r in rows if r.get("withheld")]
    assert len(notice) == 1 and notice[0]["slug"] == "secret-note"
    assert "withheld" in notice[0]["content"] and "include_restricted=true" in notice[0]["content"]
    assert "passphrase" not in " ".join(str(v) for r in rows for v in r.values())


def test_fts_include_restricted_returns_content(srv) -> None:
    rows = srv.gestalt_search_fts("zebrafish", limit=10, include_restricted=True)
    assert {r["slug"] for r in rows} == {"open-note", "plain-note", "secret-note"}
    assert not any(r.get("withheld") for r in rows)
    assert not any("sensitivity" in r for r in rows)


def test_hybrid_search_wrapper_withholds(srv) -> None:
    rows = srv.gestalt_search("zebrafish", limit=10, semantic=False)
    assert [r["slug"] for r in rows if r.get("withheld")] == ["secret-note"]
    assert "secret-note" not in {r["slug"] for r in rows if not r.get("withheld")}
    assert "secret-note" in {r["slug"] for r in srv.gestalt_search("zebrafish", 10, False, include_restricted=True)}


def test_old_index_without_column_falls_back_to_frontmatter(srv, built) -> None:
    """An index built before the column still opens, and a restricted entry stays withheld."""
    db = sqlite3.connect(built / ".search" / "gestalt.db")
    db.execute("ALTER TABLE sections_meta DROP COLUMN sensitivity")
    db.commit()
    db.close()
    rows = srv.gestalt_search_fts("zebrafish", limit=10)
    assert [r["slug"] for r in rows if r.get("withheld")] == ["secret-note"]
    assert {r["slug"] for r in rows if not r.get("withheld")} == {"open-note", "plain-note"}


def test_gestalt_read_gate(srv) -> None:
    try:
        fn = srv.build_mcp()._tool_manager._tools["gestalt_read"].fn
    except Exception as e:  # the mcp package may be absent in a bare env
        pytest.skip(f"mcp unavailable: {e}")
    assert "passphrase" not in fn("secret-note")
    assert "restricted entry matched" in fn("secret-note") and "include_restricted=true" in fn("secret-note")
    assert "passphrase" in fn("secret-note", include_restricted=True)
    assert "zebrafish" in fn("plain-note") and "zebrafish" in fn("open-note")


def test_hook_never_injects_restricted(built, monkeypatch) -> None:
    hook = _load("prompt_intelligence_sens", "claude-tree/hooks/prompt-intelligence.py")
    import sys
    sys.modules.pop("gestalt_mcp_server", None)
    srvmod = _load("gestalt_mcp_server", "tools/gestalt-mcp-server.py")
    monkeypatch.setattr(srvmod, "GESTALT_DIR", built)
    monkeypatch.setattr(srvmod, "KNOWLEDGE_DIR", built / "knowledge")
    monkeypatch.setattr(srvmod, "DB_PATH", built / ".search" / "gestalt.db")
    out = hook.retrieve_gestalt_sync("zebrafish", str(built), 10)
    assert {r["slug"] for r in out} == {"open-note", "plain-note"}
    # belt: even a server that returned a restricted row would not reach the injection
    monkeypatch.setattr(srvmod, "gestalt_search_fts", lambda q, limit=10: [
        {"slug": "secret-note", "heading": "h", "block_id": "b", "sensitivity": "restricted"},
        {"slug": "withheld-note", "withheld": "restricted"},
        {"slug": "open-note", "heading": "h", "block_id": "b"}])
    assert [r["slug"] for r in hook.retrieve_gestalt_sync("zebrafish", str(built), 10)] == ["open-note"]


# --- 2026-10-11 line review -----------------------------------------------------------------------


def test_an_unreadable_or_escaping_file_path_is_restricted_not_unpublished(srv, built) -> None:
    """The fallback for an index without the column and for relay rows failed open: a git-crypt blob read as "unpublished" and
    passed the filter, and an absolute or dotted path left the knowledge directory."""
    (built / "knowledge" / "locked.md").write_bytes(b"\x00GITCRYPT\x00\xa7\xe7c+")
    assert srv._file_sensitivity("knowledge/locked.md") == "restricted"
    assert srv._file_sensitivity("knowledge/does-not-exist.md") == "unpublished"  # a moved note keeps the old reading
    assert srv._file_sensitivity("/etc/hostname") == "restricted" and srv._file_sensitivity("../outside.md") == "restricted"
    assert srv._file_sensitivity("knowledge/plain-note.md") in ("public", "unpublished")


def test_a_search_limit_is_clamped_to_one_through_one_hundred(srv) -> None:
    """SQLite reads LIMIT -1 as no limit, so a negative limit returned the whole corpus."""
    assert srv._clamp_limit(-1) == 1 and srv._clamp_limit(0) == 1 and srv._clamp_limit(10**6) == 100 and srv._clamp_limit(7) == 7
    assert srv._clamp_limit("12") == 12 and srv._clamp_limit("junk") == 10
