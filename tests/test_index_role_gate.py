"""gestalt D1 role gate lives in the builder itself (2026-09-02): a bare `python3` build on a laptop stays
FTS-only, a bare build on the hub re-execs under the venv so the artifact carries vectors."""
from __future__ import annotations

import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
from conftest import _load  # noqa: E402


def test_role_hub_forces_fts_only_off_the_hub_and_not_on_it(monkeypatch):
    b = _load("gib_role", "tools/gestalt-index-builder.py")
    monkeypatch.setenv("GESTALT_INDEX_BUILD_ROLE", "hub")
    monkeypatch.setenv("FLEET_HUB_NAME", "not-this-host")
    assert b._role_forces_fts_only() is True
    monkeypatch.setenv("FLEET_HUB_NAME", socket.gethostname().split(".")[0])
    assert b._role_forces_fts_only() is False
    monkeypatch.setenv("GESTALT_INDEX_BUILD_ROLE", "any")
    monkeypatch.setenv("FLEET_HUB_NAME", "not-this-host")
    assert b._role_forces_fts_only() is False


def test_fts_only_and_stamp_only_never_reexec(monkeypatch):
    b = _load("gib_reexec", "tools/gestalt-index-builder.py")
    monkeypatch.delenv("GESTALT_NO_VENV_REEXEC", raising=False)

    def explode(*a, **k):
        raise AssertionError("execve must not be called")

    monkeypatch.setattr(b.os, "execve", explode)
    for argv in (["--fts-only"], ["--stamp-only"], ["--check"], ["--force", "--fts-only"]):
        assert b._reexec_in_venv_if_needed(argv) is None
