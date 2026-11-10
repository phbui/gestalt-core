"""The live-index guard compares file content, so an identical rewrite by another process (the post-merge hook) is not a change."""
from __future__ import annotations

import os

import conftest


def _point(monkeypatch, tmp_path):
    a, b = tmp_path / "MANIFEST.md", tmp_path / "GRAPH.md"
    a.write_bytes(b"manifest")
    b.write_bytes(b"graph")
    monkeypatch.setattr(conftest, "_LIVE_INDEX", [a, b])
    return a, b


def test_different_bytes_change_the_signature(monkeypatch, tmp_path):
    a, _ = _point(monkeypatch, tmp_path)
    before = conftest._live_index_sig()
    a.write_bytes(b"manifest2")
    assert conftest._live_index_sig() != before


def test_new_mtime_or_identical_rewrite_keeps_the_signature(monkeypatch, tmp_path):
    a, b = _point(monkeypatch, tmp_path)
    before = conftest._live_index_sig()
    os.utime(a, ns=(1_000_000_000, 1_000_000_000))
    b.write_bytes(b"graph")
    assert conftest._live_index_sig() == before


def test_missing_file_is_none(monkeypatch, tmp_path):
    a, b = _point(monkeypatch, tmp_path)
    b.unlink()
    sig = conftest._live_index_sig()
    assert sig[1] is None and sig[0] is not None
