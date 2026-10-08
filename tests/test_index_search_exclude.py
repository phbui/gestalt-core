"""`search: exclude` in frontmatter keeps an entry out of the index sources (2026-10-06)."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _builder():
    spec = importlib.util.spec_from_file_location("gib_exclude", str(REPO / "tools" / "gestalt-index-builder.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_search_exclude_key_drops_the_entry_from_the_sources(tmp_path, monkeypatch):
    b = _builder()
    k = tmp_path / "knowledge"; r = tmp_path / "rules"; k.mkdir(); r.mkdir()
    (k / "kept.md").write_text("---\ntype: note\ntitle: Kept\n---\nbody\n")
    (k / "snapshot.md").write_text("---\ntype: reference\nsearch: exclude\n---\nbody that mentions everything\n")
    (k / "body-mention.md").write_text("---\ntype: note\n---\nsearch: exclude appears in the body only\n")
    (k / "no-frontmatter.md").write_text("search: exclude\n")
    # A locked clone holds git-crypt ciphertext here. CI runs on one, and a decode error there
    # failed the build on 2026-10-06. A binary file is never "excluded": it is simply not text.
    (k / "zz-ciphertext.md").write_bytes(b"\x00GITCRYPT\x00\xc1\xff\xfe\x80binary\n---\nsearch: exclude\n")
    (r / "a-rule.md").write_text("# Rule\n")
    monkeypatch.setattr(b, "KNOWLEDGE_DIR", k)
    monkeypatch.setattr(b, "RULES_DIR", r)
    names = [p.name for p in b.all_source_entries()]
    assert names == ["body-mention.md", "kept.md", "no-frontmatter.md", "zz-ciphertext.md", "a-rule.md"]
    assert b.entry_search_excluded(k / "snapshot.md") and not b.entry_search_excluded(k / "kept.md")


def test_the_live_snapshot_is_the_only_excluded_entry_today():
    b = _builder()
    k = REPO / "knowledge"
    try:
        head = (k / "gestalt-system-map.md").read_text(encoding="utf-8")[:3]
    except (OSError, UnicodeDecodeError):
        head = ""
    if head != "---":
        import pytest
        pytest.skip("corpus is encrypted or absent")
    excluded = sorted(p.name for p in k.glob("*.md") if b.entry_search_excluded(p))
    assert excluded == ["gestalt-system-map-live-2026-10-06.md"]
