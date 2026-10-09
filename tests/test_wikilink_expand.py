"""One-hop wikilink expansion (GESTALT_LINK_EXPAND) on a tiny sqlite index built with the project's own schema.

The index has no links table, so the links are read from the section text. The dense vectors are one-hot, so the test controls which sections the two legs return."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import gestalt_rank as gr  # noqa: E402
from test_harness_fidelity import _fixture_schema  # noqa: E402

np = pytest.importorskip("numpy")
sqlite_vec = pytest.importorskip("sqlite_vec")

SECTIONS = [
    (0, "a-note", "Alpha", "The quokka facts. See [[b-note]] and [[filler-note#^x]] for more."),
    (1, "filler-note", "Filler", "Nothing here. See [[d-note]]."),
    (2, "b-note", "Beta one", "Beta body one, linked from alpha."),
    (3, "c-note", "Gamma", "Gamma points back at [[a-note#^x]]."),
    (4, "d-note", "Delta", "Delta links nowhere."),
    (5, "e-note", "Epsilon", "Epsilon mentions [[d-note]] only."),
    (6, "b-note", "Beta two", "Beta body two."),
] + [(i, f"g{i}-note", f"G{i}", f"Noise {i}.") for i in range(7, 12)]


def _onehot(i: int, extra: dict | None = None) -> bytes:
    v = np.zeros(768, dtype=np.float32)
    v[i] = 1.0
    for j, w in (extra or {}).items():
        v[j] = w
    return v.tobytes()


@pytest.fixture
def db(tmp_path):
    con = sqlite3.connect(str(tmp_path / "links.db"))
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.executescript(_fixture_schema())
    for i, slug, heading, content in SECTIONS:
        con.execute("INSERT INTO sections_meta (id, slug, heading, block_id, content, file_path, content_hash, anchors) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (i, slug, heading, None, content, f"knowledge/{slug}.md", f"h{i}", ""))
        con.execute("INSERT INTO sections_fts(rowid, slug, heading, block_id, content) VALUES (?, ?, ?, ?, ?)", (i, slug, heading, "", content))
        con.execute("INSERT INTO sections_vec (id, embedding) VALUES (?, ?)", (i, _onehot(i)))
    con.commit()
    con.row_factory = sqlite3.Row
    yield con
    con.close()


# The query vector sits next to section 0, then section 1, then the five noise sections. Sections 2 to 6 are the farthest.
QUERY_VEC = _onehot(0, {1: 0.1, **{i: 0.05 for i in range(7, 12)}})
QUERY = "quokka"


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    for k in ("GESTALT_LINK_EXPAND", "GESTALT_LINK_EXPAND_K", "GESTALT_ABSTAIN", "GESTALT_ABSTAIN_MIN", "GESTALT_CALIB", "GESTALT_FUSION", "GESTALT_SLUG_DECAY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GESTALT_RERANK", "off")
    monkeypatch.setenv("GESTALT_RERANK_DEPTH", "2")


def search(db, limit=1, rerank=False):
    return gr.hybrid_search(db, QUERY, limit, embed_query=lambda q: QUERY_VEC, rerank=rerank, full=True)


def run(db, monkeypatch, limit=1):
    """hybrid_search with the rerank stubbed to keep the pool order. Returns the pool the reranker saw, the fused scores, and the result."""
    seen = {}
    real_fuse = gr.fuse

    def fuse(*a, **k):
        seen["fused"] = real_fuse(*a, **k)
        return seen["fused"]

    def rerank_rows(query, rows, alias=None, maxchars=None):
        seen["pool"] = list(rows)
        n = len(rows)
        return list(rows), [float(n - i) for i in range(n)], {"model": "stub", "n": n, "ms": 0, "top1_changed": False, "fallback": "none"}

    monkeypatch.setattr(gr, "fuse", fuse)
    monkeypatch.setattr(gr, "rerank_rows", rerank_rows)
    result = search(db, limit, rerank=True)
    return [r["id"] for r in seen["pool"]], seen["fused"], result


class Boost:
    """Scores the Beta and Gamma sections highest, so the rerank can promote appended rows."""

    def predict(self, pairs, **_kw):
        return [5.0 if ("Beta" in doc or "Gamma" in doc) else 1.0 for _, doc in pairs]


def ids(result):
    return [r["id"] for r in result.rows]


def test_the_pool_is_the_two_legs_top_rows_when_the_knob_is_off(db, monkeypatch):
    assert run(db, monkeypatch)[0] == [0, 1]


def test_wikilink_slugs_reads_every_link_form():
    text = "[[a]] [[b#^blk]] [[c#Heading]] [[d|alias]] [[a]] [[ e ]]"
    assert gr.wikilink_slugs(text) == ["a", "b", "c", "d", "e"]


def test_knob_off_gives_the_old_pool_exactly(db, monkeypatch):
    base_pool, base_fused, base = run(db, monkeypatch)
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "off")
    pool, fused, off = run(db, monkeypatch)
    assert pool == base_pool and fused == base_fused and off.expanded == 0 and ids(off) == ids(base)


def test_outgoing_and_incoming_links_enter_the_pool(db, monkeypatch):
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    monkeypatch.setenv("GESTALT_LINK_EXPAND_K", "1")
    pool, _, got = run(db, monkeypatch)
    assert pool == [0, 1, 2, 3]  # b-note is linked from a-note, c-note links to a-note
    assert got.expanded == 2


def test_expanded_from_names_the_top_note_on_each_appended_row(db, monkeypatch):
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    monkeypatch.setenv("GESTALT_LINK_EXPAND_K", "1")
    rows = gr.expand_links(db, [db.execute("SELECT * FROM sections_meta WHERE id = ?", (i,)).fetchone() for i in (0, 1)], {0: 0.03, 1: 0.02}, 1)
    assert [(r["id"], r["expanded_from"]) for r in rows] == [(2, "a-note"), (3, "a-note")]


def test_an_unlinked_note_stays_out(db, monkeypatch):
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    pool, _, _ = run(db, monkeypatch)
    assert 5 not in pool  # e-note links to d-note but no seed links to e-note
    assert not set(pool) & {7, 8, 9, 10, 11}


def test_a_note_already_in_the_pool_is_not_duplicated(db, monkeypatch):
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    pool, _, _ = run(db, monkeypatch)
    assert pool.count(1) == 1 and len(pool) == len(set(pool))  # a-note links to filler-note, which was already there


def test_the_best_section_of_a_linked_note_is_one_row(db, monkeypatch):
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    pool, _, _ = run(db, monkeypatch)
    assert 2 in pool and 6 not in pool  # no fused score for either, so the lowest id wins


def test_the_original_pool_keeps_its_order_and_scores_and_the_new_rows_rank_below(db, monkeypatch):
    base_pool, base_fused, _ = run(db, monkeypatch)
    base_scores = {i: base_fused[i] for i in base_pool}
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    pool, fused, _ = run(db, monkeypatch)
    assert pool[:len(base_pool)] == base_pool
    assert {i: fused[i] for i in base_pool} == base_scores
    new = [fused[i] for i in pool[len(base_pool):]]
    assert new and all(x < min(base_scores.values()) for x in new) and new == sorted(new, reverse=True)


def test_k_limits_the_notes_the_expansion_starts_from(db, monkeypatch):
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    monkeypatch.setenv("GESTALT_LINK_EXPAND_K", "2")
    assert run(db, monkeypatch)[0] == [0, 1, 2, 4, 3]  # filler-note is the second seed and links to d-note
    monkeypatch.setenv("GESTALT_LINK_EXPAND_K", "1")
    assert run(db, monkeypatch)[0] == [0, 1, 2, 3]


def test_the_rerank_can_promote_an_appended_row(db, monkeypatch):
    monkeypatch.setattr(gr, "get_reranker", lambda alias=None: Boost())
    off = search(db, limit=3, rerank=True)
    assert not {2, 3} & set(ids(off))
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    monkeypatch.setenv("GESTALT_LINK_EXPAND_K", "1")
    got = search(db, limit=3, rerank=True)
    assert ids(got)[:2] == [2, 3] and {r["id"]: r["expanded_from"] for r in got.rows if "expanded_from" in r.keys()} == {2: "a-note", 3: "a-note"}


@pytest.mark.parametrize("name,raw", [("GESTALT_LINK_EXPAND", "maybe"), ("GESTALT_LINK_EXPAND_K", "zero"), ("GESTALT_LINK_EXPAND_K", "0")])
def test_bad_knob_values_raise(db, monkeypatch, name, raw):
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError, match=name):
        search(db)
