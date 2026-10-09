"""Supersession and freshness: the index columns, the in-place upgrade, the demotion and the labels (2026-10-09)."""
import datetime as dt
import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import gestalt_freshness as gf  # noqa: E402

NOW = dt.date(2026, 10, 9)
NEW_COLS = [n for n, _ in gf.COLUMNS]


def _builder():
    spec = importlib.util.spec_from_file_location("gib_fresh", str(REPO / "tools" / "gestalt-index-builder.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(k, name, front, body="## Part\nsome words here\n"):
    (k / f"{name}.md").write_text(f"---\n{front}\n---\n# {name}\n\n{body}")


@pytest.fixture
def kb(tmp_path, monkeypatch):
    b = _builder()
    k = tmp_path / "knowledge"
    r = tmp_path / ".claude" / "rules"
    k.mkdir()
    r.mkdir(parents=True)
    monkeypatch.setattr(b, "GESTALT_DIR", tmp_path)
    monkeypatch.setattr(b, "KNOWLEDGE_DIR", k)
    monkeypatch.setattr(b, "RULES_DIR", r)
    monkeypatch.setattr(b, "GIT_DATE_BUDGET_S", 0.0)  # tmp_path is no repo: mtime unless a test turns the lookup on
    _write(k, "old-note", "title: Old\nupdated: 2026-01-05")
    _write(k, "new-note", "title: New\nmodified: 2026-10-06\nsupersedes: [old-note, kept-note]\nvalid_until: 2026-09-01")
    _write(k, "kept-note", "title: Kept\nupdated: 2026-02-01\nsuperseded_by: other-note")
    _write(k, "plain-note", "title: Plain")
    return b, tmp_path


def _build(b, path):
    chunks, sens = b._collect_chunks()
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    b._create_schema(db, False)
    b._insert_chunks(db, chunks, [b"" for _ in chunks], ["h"] * len(chunks), sens, False, b.entry_freshness(b.all_source_entries()))
    db.commit()
    return db


def _row(db, slug):
    return db.execute("SELECT * FROM sections_meta WHERE slug=? LIMIT 1", (slug,)).fetchone()


OLD_SCHEMA = (
    "CREATE TABLE sections_meta (id INTEGER PRIMARY KEY, slug TEXT NOT NULL, heading TEXT NOT NULL, block_id TEXT, "
    "content TEXT NOT NULL, file_path TEXT NOT NULL, content_hash TEXT, anchors TEXT, "
    "sensitivity TEXT NOT NULL DEFAULT 'unpublished', title TEXT NOT NULL DEFAULT '')"
)


def test_columns_exist_after_a_build_and_supersedes_marks_the_other_note(kb):
    b, tmp = kb
    db = _build(b, tmp / "a.db")
    cols = {r[1] for r in db.execute("PRAGMA table_info(sections_meta)")}
    assert set(NEW_COLS) <= cols
    assert _row(db, "old-note")["superseded_by"] == "new-note"
    assert _row(db, "new-note")["supersedes"] == "old-note,kept-note"
    assert _row(db, "new-note")["valid_until"] == "2026-09-01"
    assert _row(db, "new-note")["superseded_by"] is None
    # kept-note declares its own superseded_by, which wins over the claim from new-note.
    assert _row(db, "kept-note")["superseded_by"] == "other-note"
    assert _row(db, "plain-note")["superseded_by"] is None and _row(db, "plain-note")["valid_until"] is None


def test_every_section_of_the_superseded_note_is_marked(kb):
    b, tmp = kb
    _write(tmp / "knowledge", "old-note", "title: Old\nupdated: 2026-01-05", "## One\nalpha\n\n## Two\nbeta\n")
    db = _build(b, tmp / "a.db")
    rows = db.execute("SELECT superseded_by FROM sections_meta WHERE slug='old-note'").fetchall()
    assert len(rows) >= 2 and all(r[0] == "new-note" for r in rows)


def test_note_modified_sources_are_recorded(kb, monkeypatch):
    b, tmp = kb
    db = _build(b, tmp / "a.db")
    old = _row(db, "old-note")
    assert (old["note_modified"], old["note_modified_source"]) == ("2026-01-05", "frontmatter")
    assert _row(db, "new-note")["note_modified"] == "2026-10-06"  # `modified` beats `updated`
    plain = _row(db, "plain-note")
    assert plain["note_modified_source"] == "mtime" and plain["note_modified"] == dt.date.today().isoformat()
    # With a repo and a budget, an undated note takes its last-commit date.
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-03-04T10:00:00+00:00", "GIT_COMMITTER_DATE": "2026-03-04T10:00:00+00:00",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["init", "-q"], ["add", "knowledge/plain-note.md"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *cmd], cwd=tmp, env=env, check=True, capture_output=True)
    monkeypatch.setattr(b, "GIT_DATE_BUDGET_S", 20.0)
    db2 = _build(b, tmp / "b.db")
    plain2 = _row(db2, "plain-note")
    assert (plain2["note_modified"][:10], plain2["note_modified_source"]) == ("2026-03-04", "git")


def test_an_old_index_upgrades_in_place_and_is_filled(kb):
    b, tmp = kb
    db = _build(b, tmp / "a.db")
    full = [tuple(r) for r in db.execute("SELECT * FROM sections_meta ORDER BY id")]
    db.close()
    old = sqlite3.connect(str(tmp / "old.db"))
    old.execute(OLD_SCHEMA)
    for row in full:
        old.execute("INSERT INTO sections_meta VALUES (?,?,?,?,?,?,?,?,?,?)", row[:10])
    old.commit()
    old.close()
    assert b.upgrade_freshness(tmp / "old.db") == NEW_COLS
    assert b.upgrade_freshness(tmp / "old.db") == []  # a second run changes nothing
    up = sqlite3.connect(str(tmp / "old.db"))
    got = [tuple(r) for r in up.execute("SELECT * FROM sections_meta ORDER BY id")]
    assert [r[:10] for r in got] == [r[:10] for r in full]
    assert [r[10:] for r in got] == [r[10:] for r in full]


def test_notes_without_the_new_fields_leave_the_old_columns_as_they_were(kb):
    b, tmp = kb
    db = _build(b, tmp / "a.db")
    chunks, sens = b._collect_chunks()
    legacy = sqlite3.connect(":memory:")
    b._create_schema(legacy, False)
    b._insert_chunks(legacy, chunks, [b""] * len(chunks), ["h"] * len(chunks), sens, False)  # no freshness map
    a = [tuple(r)[:10] for r in db.execute("SELECT * FROM sections_meta ORDER BY id")]
    c = [tuple(r)[:10] for r in legacy.execute("SELECT * FROM sections_meta ORDER BY id")]
    assert a == c
    assert all(tuple(r)[10:] == (None,) * 5 for r in legacy.execute("SELECT * FROM sections_meta"))


# --- demote -----------------------------------------------------------------------------------------------

def _rows():
    return [{"id": i, "score": s} for i, s in ((1, 1.0), (2, 0.9), (3, 0.8), (4, 0.7))]


META = {
    1: {"superseded_by": "z", "note_modified": "2026-10-06"},
    2: {"valid_until": "2026-09-01"},
    3: {"valid_until": "2026-12-01", "note_modified": "2026-01-09"},
}


def test_demote_halves_and_quarters_the_right_rows(monkeypatch):
    monkeypatch.setenv("GESTALT_FRESHNESS", "on")
    out = gf.demote(_rows(), META, NOW)
    by = {r["id"]: r for r in out}
    assert by[1]["score"] == 0.5 and by[2]["score"] == pytest.approx(0.225)
    assert by[3]["score"] == 0.8 and by[4]["score"] == 0.7
    assert [r["id"] for r in out] == [3, 4, 1, 2]
    assert by[1]["freshness"]["demoted"] and by[1]["freshness"]["superseded_by"] == "z" and by[1]["freshness"]["age_days"] == 3
    assert by[2]["freshness"]["demoted"] and by[2]["freshness"]["valid_until"] == "2026-09-01"
    assert not by[3]["freshness"]["demoted"] and by[3]["freshness"]["age_days"] == 273


def test_demote_keeps_order_without_demotions_and_drops_nothing(monkeypatch):
    monkeypatch.setenv("GESTALT_FRESHNESS", "on")
    rows = [{"id": i, "score": 0.5} for i in (7, 3, 9)]
    assert [r["id"] for r in gf.demote(rows, {}, NOW)] == [7, 3, 9]
    out = gf.demote(_rows(), lambda i: {"superseded_by": "z", "valid_until": "2000-01-01"}, NOW)
    assert sorted(r["id"] for r in out) == [1, 2, 3, 4]
    # both flags: the smaller multiplier (0.25) applies once
    assert [r["score"] for r in out] == pytest.approx([0.25, 0.225, 0.2, 0.175])


def test_demote_off_returns_rows_unchanged(monkeypatch):
    monkeypatch.delenv("GESTALT_FRESHNESS", raising=False)
    rows = _rows()
    assert gf.demote(rows, META, NOW) == rows
    assert "freshness" not in gf.demote(rows, META, NOW)[0]
    monkeypatch.setenv("GESTALT_FRESHNESS", "off")
    assert gf.demote(rows, META, NOW) == rows


def test_negative_scores_also_drop(monkeypatch):
    monkeypatch.setenv("GESTALT_FRESHNESS", "on")
    out = gf.demote([{"id": 1, "score": -1.0}, {"id": 2, "score": -1.5}], {1: {"superseded_by": "z"}}, NOW)
    assert [r["id"] for r in out] == [2, 1]


@pytest.mark.parametrize("name,val,call", [
    ("GESTALT_FRESHNESS", "maybe", gf.enabled),
    ("GESTALT_FRESHNESS_FACTOR", "half", gf.factor),
    ("GESTALT_FRESHNESS_FACTOR", "1.5", gf.factor),
    ("GESTALT_FRESHNESS_EXPIRED_FACTOR", "-0.1", gf.expired_factor),
])
def test_bad_env_values_raise_naming_the_variable(monkeypatch, name, val, call):
    monkeypatch.setenv(name, val)
    with pytest.raises(ValueError, match=name):
        call()


def test_knob_defaults(monkeypatch):
    for n in ("GESTALT_FRESHNESS", "GESTALT_FRESHNESS_FACTOR", "GESTALT_FRESHNESS_EXPIRED_FACTOR"):
        monkeypatch.delenv(n, raising=False)
    assert (gf.enabled(), gf.factor(), gf.expired_factor()) == (False, 0.5, 0.25)
    monkeypatch.setenv("GESTALT_FRESHNESS_FACTOR", "0.8")
    assert gf.factor() == 0.8


# --- labels and prefix ------------------------------------------------------------------------------------

@pytest.mark.parametrize("mod,label", [
    ("2026-10-09", "today"), ("2026-10-08", "1 day"), ("2026-10-06", "3 days"), ("2026-08-09", "2 months"),
    ("2025-10-09", "1 year"), ("2023-10-09", "3 years"), ("2026-10-20", "today"), ("", ""), (None, ""), ("junk", ""),
    ("2026-10-06T12:00:00+00:00", "3 days"),
])
def test_age_label(mod, label):
    assert gf.age_label(mod, NOW) == label


def test_snippet_prefix(monkeypatch):
    row = {"freshness": {"age": "3 days", "supersedes": ["x"], "superseded_by": None, "expired": False}}
    monkeypatch.delenv("GESTALT_FRESHNESS", raising=False)
    assert gf.snippet_prefix(row) == ""
    monkeypatch.setenv("GESTALT_FRESHNESS", "on")
    assert gf.snippet_prefix(row) == "[updated 3 days ago, supersedes x] "
    expired = {"freshness": {"age": "", "superseded_by": "y", "expired": True, "valid_until": "2026-09-01"}}
    assert gf.snippet_prefix(expired) == "[superseded by y, expired 2026-09-01] "
    assert gf.snippet_prefix({"freshness": {}}) == ""
    assert gf.snippet_prefix({"superseded_by": "y", "note_modified": "2026-10-06"}, NOW) == "[updated 3 days ago, superseded by y] "


def test_apply_to_result_reorders_and_off_is_a_noop(monkeypatch):
    from types import SimpleNamespace
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE sections_meta (id INTEGER PRIMARY KEY, slug TEXT)")
    gf.ensure_columns(db)
    db.executemany("INSERT INTO sections_meta (id, slug, superseded_by) VALUES (?,?,?)", [(1, "a", "b"), (2, "b", None)])

    def res():
        return SimpleNamespace(rows=[{"id": 1}, {"id": 2}], scores={1: 1.0, 2: 0.8}, fused_scores={1: 1.0, 2: 0.8})

    r = res()
    monkeypatch.delenv("GESTALT_FRESHNESS", raising=False)
    gf.apply_to_result(db, r)
    assert [x["id"] for x in r.rows] == [1, 2] and not hasattr(r, "freshness")
    monkeypatch.setenv("GESTALT_FRESHNESS", "on")
    gf.apply_to_result(db, r, NOW)
    assert [x["id"] for x in r.rows] == [2, 1] and r.scores[1] == 0.5 and r.fused_scores[1] == 0.5 and r.freshness[1]["demoted"]


def test_parse_front_forms():
    p = gf.parse_front("---\nsupersedes:\n  - a\n  - \"[[b]]\"\nvalid_until: 2026-13-45\nsuperseded_by: knowledge/c.md\n---\nbody")
    assert p["supersedes"] == ["a", "b"] and p["valid_until"] is None and p["superseded_by"] == "c"
    assert gf.parse_front("no frontmatter")["supersedes"] == []
