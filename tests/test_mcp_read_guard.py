"""X11: gestalt_read must not resolve outside knowledge/ (2026-10-06)."""
import pytest

from conftest import _load


@pytest.fixture
def srv(tmp_path, monkeypatch):
    mod = _load("gestalt_mcp_server_guard", "tools/gestalt-mcp-server.py")
    kd = tmp_path / "knowledge"
    kd.mkdir()
    (kd / "home-mesh.md").write_text("# mesh\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("SECRET", encoding="utf-8")
    monkeypatch.setattr(mod, "KNOWLEDGE_DIR", kd)
    return mod, tmp_path


def test_valid_slug(srv):
    mod, _ = srv
    assert mod._knowledge_path("home-mesh").name == "home-mesh.md"


@pytest.mark.parametrize("bad", ["../README", "/etc/passwd", "a/b", "..", "", "x\\y", "home-mesh\x00"])
def test_rejects_escapes(srv, bad):
    mod, _ = srv
    assert mod._knowledge_path(bad) is None


def test_symlink_escape_rejected(srv):
    mod, root = srv
    (root / "knowledge" / "evil.md").symlink_to(root / "README.md")
    assert mod._knowledge_path("evil") is None


def test_tool_reads_and_fuzzy(srv):
    mod, _ = srv
    try:
        mcp = mod.build_mcp()
    except Exception as e:  # the mcp package may be absent in a bare env
        pytest.skip(f"mcp unavailable: {e}")
    fn = mcp._tool_manager._tools["gestalt_read"].fn
    assert fn("home-mesh") == "# mesh\n"
    assert "Did you mean: home-mesh" in fn("mesh")
    assert "Invalid slug" in fn("../README")
    assert "SECRET" not in fn("/home/x/README")
