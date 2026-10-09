"""Abstention (GESTALT_ABSTAIN): the reranker's top score through a Platt fit gives a probability that the query is answerable."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
import gestalt_rank as gr  # noqa: E402
from conftest import _load  # noqa: E402
from test_wikilink_expand import QUERY, Boost, db, ids, search  # noqa: E402,F401  the tiny index and its helpers


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    for k in ("GESTALT_LINK_EXPAND", "GESTALT_ABSTAIN", "GESTALT_ABSTAIN_MIN", "GESTALT_CALIB", "GESTALT_SLUG_DECAY", "GESTALT_FUSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GESTALT_RERANK", "off")
    monkeypatch.setenv("GESTALT_RERANK_DEPTH", "2")
    monkeypatch.setattr(gr, "_warned", set())


class Fixed:
    """A reranker that returns one score for the first pair and a lower one for the rest."""

    def __init__(self, top):
        self.top = top

    def predict(self, pairs, **_kw):
        return [self.top] + [self.top - 1.0] * (len(pairs) - 1)


def write_calib(path, scale, offset, status="ok"):
    cal = {"status": status, "scale": scale, "offset": offset} if status == "ok" else {"status": status}
    path.write_text(json.dumps({"signals": {"top1": {"calibration": cal}}}))
    return path


def test_identity_map_returns_the_raw_score():
    assert gr.confidence(2.5, None) == 2.5
    assert gr.confidence(-1.0, None) == -1.0


def test_platt_fit_is_a_sigmoid_of_scale_times_score_plus_offset():
    assert gr.confidence(0.0, (1.0, 0.0)) == 0.5
    assert gr.confidence(2.0, (2.0, -1.0)) == pytest.approx(1 / (1 + math.exp(-3.0)))
    assert gr.confidence(-800.0, (1.0, 0.0)) == pytest.approx(0.0, abs=1e-12)  # no overflow
    assert gr.confidence(800.0, (1.0, 0.0)) == 1.0


def test_none_stays_none_and_never_abstains():
    assert gr.confidence(None, (1.0, 0.0)) is None
    assert gr.abstain_decision(None, (1.0, 0.0)) == (None, False)
    assert gr.abstain_decision(None, None, 0.9) == (None, False)


def test_threshold_edge_a_score_equal_to_the_minimum_does_not_abstain():
    assert gr.abstain_decision(0.5, None, 0.5) == (0.5, False)
    assert gr.abstain_decision(0.4999, None, 0.5) == (0.4999, True)
    assert gr.abstain_decision(0.0, (1.0, 0.0), 0.5) == (0.5, False)
    assert gr.abstain_decision(-0.1, (1.0, 0.0), 0.5)[1] is True


def test_env_parsing(monkeypatch):
    assert gr.abstain_on() is False
    monkeypatch.setenv("GESTALT_ABSTAIN", "on")
    assert gr.abstain_on() is True
    monkeypatch.setenv("GESTALT_ABSTAIN", "yes")
    with pytest.raises(ValueError, match="GESTALT_ABSTAIN"):
        gr.abstain_on()
    assert gr.abstain_min(True) == 0.5
    monkeypatch.setenv("GESTALT_ABSTAIN_MIN", "7")
    assert gr.abstain_min(False) == 7.0
    with pytest.raises(ValueError, match="GESTALT_ABSTAIN_MIN"):
        gr.abstain_min(True)  # a probability must lie in 0..1
    monkeypatch.setenv("GESTALT_ABSTAIN_MIN", "high")
    with pytest.raises(ValueError, match="GESTALT_ABSTAIN_MIN"):
        gr.abstain_min(False)


def test_calib_file_parsing(tmp_path, monkeypatch):
    assert gr.load_calib() is None
    good = write_calib(tmp_path / "c.json", 1.5, -0.25)
    monkeypatch.setenv("GESTALT_CALIB", str(good))
    assert gr.load_calib() == (1.5, -0.25)
    for bad in (tmp_path / "missing.json", write_calib(tmp_path / "u.json", 0, 0, status="undefined")):
        monkeypatch.setenv("GESTALT_CALIB", str(bad))
        with pytest.raises(ValueError):
            gr.load_calib()
    junk = tmp_path / "junk.json"
    junk.write_text("not json")
    with pytest.raises(ValueError):
        gr.calib_fit(junk)


def test_calibration_fit_for_is_the_same_reader(tmp_path):
    cal = pytest.importorskip("calibration")
    path = write_calib(tmp_path / "c.json", 2.0, -3.0)
    assert cal.fit_for(path) == gr.calib_fit(path) == (2.0, -3.0)
    with pytest.raises(ValueError):
        cal.fit_for(path, signal="margin")


def test_on_with_a_rerank_carries_confidence_and_abstain(db, monkeypatch, tmp_path):
    monkeypatch.setenv("GESTALT_ABSTAIN", "on")
    monkeypatch.setattr(gr, "get_reranker", lambda alias=None: Fixed(0.3))
    got = search(db, limit=5, rerank=True)
    assert got.top_score == 0.3 and got.confidence == 0.3 and got.abstain is True  # identity map, 0.3 < 0.5
    monkeypatch.setenv("GESTALT_CALIB", str(write_calib(tmp_path / "c.json", 4.0, 0.0)))
    got = search(db, limit=5, rerank=True)
    assert got.confidence == pytest.approx(1 / (1 + math.exp(-1.2))) and got.abstain is False


def test_on_never_reorders_or_drops_rows(db, monkeypatch):
    monkeypatch.setattr(gr, "get_reranker", lambda alias=None: Fixed(-5.0))
    off = search(db, limit=5, rerank=True)
    monkeypatch.setenv("GESTALT_ABSTAIN", "on")
    on = search(db, limit=5, rerank=True)
    assert on.abstain is True and ids(on) == ids(off) and on.scores == off.scores


def test_no_rerank_gives_none_and_false(db, monkeypatch):
    monkeypatch.setenv("GESTALT_ABSTAIN", "on")
    got = search(db, limit=5, rerank=False)
    assert got.rows and got.confidence is None and got.abstain is False


def test_a_rerank_fallback_gives_none_and_false(db, monkeypatch):
    monkeypatch.setenv("GESTALT_ABSTAIN", "on")

    def boom(alias=None):
        raise RuntimeError("no model")

    monkeypatch.setattr(gr, "get_reranker", boom)
    got = search(db, limit=5, rerank=True)
    assert got.rerank_fallback is True and got.confidence is None and got.abstain is False


def test_knobs_unset_leave_rows_and_order_unchanged(db, monkeypatch):
    monkeypatch.setattr(gr, "get_reranker", lambda alias=None: Fixed(0.3))
    base = search(db, limit=5, rerank=True)
    assert base.confidence is None and base.abstain is False and base.expanded == 0
    for k, v in (("GESTALT_ABSTAIN", "off"), ("GESTALT_LINK_EXPAND", "off")):
        monkeypatch.setenv(k, v)
    again = search(db, limit=5, rerank=True)
    assert ids(again) == ids(base) and again.scores == base.scores and again.top_score == base.top_score


def test_the_mcp_row_carries_confidence_and_abstain_only_when_on(db, monkeypatch):
    server = _load("gestalt_mcp_server_abstain", "tools/gestalt-mcp-server.py")
    monkeypatch.setattr(gr, "get_reranker", lambda alias=None: Fixed(0.3))
    off = search(db, limit=5, rerank=True)
    row = server._search_row(off.rows[0], off, QUERY)
    assert "confidence" not in row and "abstain" not in row and "rerank_score" in row
    monkeypatch.setenv("GESTALT_ABSTAIN", "on")
    on = search(db, limit=5, rerank=True)
    row_on = server._search_row(on.rows[0], on, QUERY)
    assert row_on["confidence"] == 0.3 and row_on["abstain"] is True
    assert {k: v for k, v in row_on.items() if k not in ("confidence", "abstain")} == row
    monkeypatch.setenv("GESTALT_LINK_EXPAND", "on")
    monkeypatch.setenv("GESTALT_LINK_EXPAND_K", "1")
    monkeypatch.setattr(gr, "get_reranker", lambda alias=None: Boost())
    exp = search(db, limit=3, rerank=True)
    rows = [server._search_row(m, exp, QUERY) for m in exp.rows]
    assert [r["expanded_from"] for r in rows if "expanded_from" in r] == ["a-note", "a-note"]
