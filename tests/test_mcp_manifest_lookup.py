"""L2: gestalt_manifest takes an optional query. No argument gives a summary, a slug gives its block."""
import pytest

from conftest import _load

MAIN = (
    "# Gestalt Manifest\n\n## Rules\n\n- `alpha-rule` — Alpha Rule Title\n\n## Knowledge\n\n"
    "### home-mesh\nrepo | Home Mesh\nLinks: [[beta]]\nBlocks: ^seat-naming\n\n"
    "### beta\nnote | Beta entry\n\n## Papers\n\n2 literature notes are in MANIFEST-papers.md.\n"
)
PAPERS = (
    "# Gestalt Manifest: Papers\n\n## Knowledge\n\n"
    "### paper-attention\nreference | Attention Is All You Need\nLinks: [[beta]]\n\n"
    "### paper-bert\nreference | BERT Pretraining\n\n"
)


@pytest.fixture
def srv(tmp_path, monkeypatch):
    mod = _load("gestalt_mcp_server_manifest", "tools/gestalt-mcp-server.py")
    (tmp_path / "MANIFEST.md").write_text(MAIN, encoding="utf-8")
    (tmp_path / "MANIFEST-papers.md").write_text(PAPERS, encoding="utf-8")
    monkeypatch.setattr(mod, "GESTALT_DIR", tmp_path)
    return mod


def test_no_argument_is_a_small_summary(srv):
    out = srv.manifest_lookup()
    assert "1 rules, 2 knowledge entries" in out and "2 paper-* entries" in out
    assert "gestalt_manifest('<slug>')" in out
    assert "### " not in out and len(out) < 1500


def test_slug_returns_one_block_from_either_file(srv):
    assert srv.manifest_lookup("home-mesh") == "### home-mesh\nrepo | Home Mesh\nLinks: [[beta]]\nBlocks: ^seat-naming\n"
    paper = srv.manifest_lookup("paper-bert")
    assert paper.startswith("### paper-bert\n") and "paper-attention" not in paper


def test_query_matches_title_words(srv):
    out = srv.manifest_lookup("attention need")
    assert out.startswith("### paper-attention") and "home-mesh" not in out
    assert "No manifest entry matches" in srv.manifest_lookup("zzzzz")


def test_rule_slug(srv):
    assert srv.manifest_lookup("alpha-rule") == "- `alpha-rule` — Alpha Rule Title\n"


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/b", "..", "x\x00", "a" * 200])
def test_rejects_bad_queries(srv, bad):
    assert "Invalid query" in srv.manifest_lookup(bad)


def test_tool_is_registered_with_optional_argument(srv):
    try:
        mcp = srv.build_mcp()
    except Exception as e:
        pytest.skip(f"mcp unavailable: {e}")
    tool = mcp._tool_manager._tools["gestalt_manifest"]
    assert "paper-" in (tool.description or "") and "no argument" in (tool.description or "")
    assert tool.fn("beta").startswith("### beta")
    assert "Invalid query" in tool.fn("../x")
