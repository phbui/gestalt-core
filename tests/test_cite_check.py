"""Hermetic tests for tools/cite-check.py: key extraction, access tiers, Crossref judging from fixture JSON."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load


@pytest.fixture(scope="module")
def cc():
    return _load("cite_check", "tools/cite-check.py")


NOTE = """---
tags: [paper]
type: paper
title: "Deep Learning for Widgets"
created: 2026-10-07
updated: 2026-10-07
doi: 10.1/ok
summary_basis: abstract
read_by: none
---

# Deep Learning for Widgets ^overview

**Year:** 2019 · [DOI](https://doi.org/10.1/ok)

## Abstract

{abstract}

## Notes
"""
LONG = "Widgets are studied at length in this paper using a large and varied corpus of examples."


def _kdir(tmp_path):
    k = tmp_path / "knowledge"
    k.mkdir(exist_ok=True)
    (k / "paper-readKey2019.md").write_text(NOTE.format(abstract=LONG))
    (k / "paper-absKey2019.md").write_text(NOTE.format(abstract=LONG))
    (k / "paper-bareKey2019.md").write_text(NOTE.format(abstract="(no abstract in source metadata)"))
    pdf = tmp_path / "pdf"
    pdf.mkdir(exist_ok=True)
    (pdf / "readKey2019.pdf").write_bytes(b"%PDF")
    return k, pdf


def test_extract_keys_tex_and_bib(cc, tmp_path):
    tex = tmp_path / "p.tex"
    tex.write_text("a \\cite{a1,b2} b \\citep[see][p.3]{c3} \\textcite*{d4}\n% \\cite{commented}\n")
    bib = tmp_path / "r.bib"
    bib.write_text("@article{e5,\n title={x}}\n@comment{skip}\n@book{ f6 ,\n}\n")
    assert cc.extract_keys(tex) == ["a1", "b2", "c3", "d4"]
    assert cc.extract_keys(bib) == ["e5", "f6"]
    assert cc.collect([tmp_path]) == ["a1", "b2", "c3", "d4", "e5", "f6"]


def test_tiers_from_pdf_and_abstract(cc, tmp_path):
    k, pdf = _kdir(tmp_path)
    assert cc.note_info("readKey2019", k, pdf)["tier"] == "READ"
    assert cc.note_info("absKey2019", k, pdf)["tier"] == "ABSTRACT-ONLY"
    assert cc.note_info("bareKey2019", k, pdf)["tier"] == "UNREAD"
    assert cc.note_info("nope", k, pdf)["tier"] == "NO-NOTE"
    assert cc.note_info("readkey2019", k, pdf)["note"] is not None   # case-insensitive fallback


def test_doi_in_body_keeps_parentheses(cc):
    m = cc.DOI_LINK.search("[DOI](https://doi.org/10.1061/(ASCE)CP.1943-5487.0000051) · <x>")
    assert m.group(1) == "10.1061/(ASCE)CP.1943-5487.0000051"


RETRACTED = {"status": 200, "message": {
    "title": ["RETRACTED: Downregulation of <i>x</i>\n via y"], "issued": {"date-parts": [[2019, 9]]},
    "updated-by": [{"type": "retraction", "DOI": "10.1/notice", "source": "retraction-watch"}]}}
CLEAN = {"status": 200, "message": {"title": ["Deep learning for widgets"], "issued": {"date-parts": [[2020]]}}}
CORRECTED = {"status": 200, "message": {"title": ["T"], "updated-by": [{"type": "correction", "DOI": "10.1/c"}]}}


def test_judge_retracted_clean_corrected_missing(cc):
    v = cc.judge(RETRACTED, "Downregulation of x via y", "2019")
    assert v["retracted"] and v["title_ratio"] >= 0.9 and v["year_delta"] == 0
    v = cc.judge(CLEAN, "Deep Learning for Widgets", "2019")
    assert not v["retracted"] and v["title_ratio"] == 1.0 and v["year_delta"] == 1
    v = cc.judge(CORRECTED)
    assert not v["retracted"] and v["notices"][0]["type"] == "correction"
    assert cc.judge({"status": 404, "message": {}})["resolved"] is False


def test_crossref_caches_and_trims(cc, tmp_path, monkeypatch):
    calls = []
    import json
    body = json.dumps({"message": {**RETRACTED["message"], "reference": ["huge"] * 5}})
    monkeypatch.setattr(cc, "http_get", lambda url: (calls.append(url) or (200, body)))
    r1 = cc.crossref("10.1/x", cache=tmp_path, sleep=lambda s: None)
    r2 = cc.crossref("10.1/x", cache=tmp_path, sleep=lambda s: None)
    assert len(calls) == 1 and r1 == r2 and "reference" not in r1["message"]
    assert "select" not in calls[0]


def test_crossref_backs_off_on_429(cc, tmp_path, monkeypatch):
    seq = iter([(429, ""), (200, '{"message": {"title": ["T"]}}')])
    monkeypatch.setattr(cc, "http_get", lambda url: next(seq))
    waits = []
    r = cc.crossref("10.1/y", cache=tmp_path, sleep=waits.append)
    assert r["status"] == 200 and 1 in waits


def _run(cc, tmp_path, monkeypatch, capsys, tex, fake=None, online=True):
    k, pdf = _kdir(tmp_path)
    f = tmp_path / "m.tex"
    f.write_text(tex)
    if fake is not None:
        monkeypatch.setattr(cc, "crossref", lambda doi, **kw: fake)
    args = [str(f), "--knowledge-dir", str(k), "--pdf-dir", str(pdf)] + (["--online"] if online else [])
    code = cc.main(args)
    return code, capsys.readouterr().out


def test_exit_codes(cc, tmp_path, monkeypatch, capsys):
    code, out = _run(cc, tmp_path, monkeypatch, capsys, "\\cite{readKey2019,absKey2019}", online=False)
    assert code == 0 and "READ" in out and "ABSTRACT-ONLY" in out


def test_missing_note_fails_offline(cc, tmp_path, monkeypatch, capsys):
    code, out = _run(cc, tmp_path, monkeypatch, capsys, "\\cite{ghost}", online=False)
    assert code == 1 and "no note" in out


def test_retracted_fails_online(cc, tmp_path, monkeypatch, capsys):
    code, out = _run(cc, tmp_path, monkeypatch, capsys, "\\cite{readKey2019}", fake=RETRACTED)
    assert code == 1 and "RETRACTED 10.1/notice" in out


def test_unresolved_doi_fails_and_network_error_only_warns(cc, tmp_path, monkeypatch, capsys):
    code, out = _run(cc, tmp_path, monkeypatch, capsys, "\\cite{readKey2019}", fake={"status": 404, "message": {}})
    assert code == 1 and "does not resolve" in out
    code, out = _run(cc, tmp_path, monkeypatch, capsys, "\\cite{absKey2019}", fake={"status": 0, "message": {}})
    assert code == 0 and "unavailable" in out


def test_title_mismatch_warns_but_strict_fails(cc, tmp_path, monkeypatch, capsys):
    other = {"status": 200, "message": {"title": ["Completely different subject"], "issued": {"date-parts": [[2019]]}}}
    code, out = _run(cc, tmp_path, monkeypatch, capsys, "\\cite{readKey2019}", fake=other)
    assert code == 0 and "title ratio" in out
    k, pdf = tmp_path / "knowledge", tmp_path / "pdf"
    code = cc.main([str(tmp_path / "m.tex"), "--online", "--strict", "--knowledge-dir", str(k), "--pdf-dir", str(pdf)])
    assert code == 1
