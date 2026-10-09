"""heldout.py and heldout_datasets.py on tiny synthetic fixtures. No network, no real corpus."""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq  # noqa: E402

import heldout as ho  # noqa: E402
import heldout_datasets as hd  # noqa: E402


def make_litsearch(root: Path) -> None:
    for sub, rows in {
        "corpus": [{"_id": "d1", "title": "T1", "text": "alpha"}, {"_id": "d2", "title": "T2", "text": "beta"}],
        "queries": [{"_id": "q1", "text": "find alpha"}, {"_id": "q2", "text": "find beta"}],
        "qrels": [{"query-id": "q1", "corpus-id": "d1", "score": 1}, {"query-id": "q2", "corpus-id": "d2", "score": 1}],
    }.items():
        (root / sub).mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows), root / sub / "test-00000-of-00001.parquet")
    (root / "flags").mkdir()
    pq.write_table(pa.Table.from_pylist([
        {"query_set": "manual_acl", "query": "find alpha", "specificity": 1, "quality": 2, "corpusids": [1]},
        {"query_set": "inline_nonacl", "query": "find beta", "specificity": 0, "quality": 2, "corpusids": [2]},
    ]), root / "flags" / "query.parquet")


def make_techqa(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / "corpus.zip", "w") as z:
        z.writestr("corpus/", "")
        z.writestr("corpus/swg1.txt", "Title: First note\n\nText:\n body one")
        z.writestr("corpus/swg2.txt", "Title: Second note\n\nText:\n body two")
    items = [
        {"id": "Q0", "question": "how one", "answer": "a", "is_impossible": False, "contexts": [{"filename": "swg1.txt", "text": "x"}]},
        {"id": "Q1", "question": "how none", "answer": "-", "is_impossible": True, "contexts": []},
    ]
    (root / "train.json").write_text(json.dumps(items))


def make_lme(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    sess = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    items = [
        {"question_id": "a1", "question": "what", "haystack_session_ids": ["s1", "s2"], "haystack_sessions": [sess, sess], "answer_session_ids": ["s2"]},
        {"question_id": "a2_abs", "question": "never said", "haystack_session_ids": ["s2", "s3"], "haystack_sessions": [sess, sess], "answer_session_ids": []},
    ]
    (root / "longmemeval_s_cleaned.json").write_text(json.dumps(items))


MAKERS = {"litsearch": make_litsearch, "techqa": make_techqa, "longmemeval-s": make_lme}


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTALT_HELDOUT_DIR", str(tmp_path))
    monkeypatch.delenv("GESTALT_HELDOUT_OPEN", raising=False)
    return tmp_path


def fetched(base: Path, name: str) -> None:
    """Lay the fixture files down and write the fetch record, as `fetch` would."""
    MAKERS[name](base / name)
    spec = hd.SPECS[name]
    ho.write_json(ho.fetch_record_path(base, name), {"name": name, "sources": spec["sources"], "license": spec["license"], "fetched_at": ho.now(), "files": []})


def test_litsearch_fields_counts_and_flags(base):
    MAKERS["litsearch"](base / "litsearch")
    ds = hd.lookup("litsearch", gated=False)
    assert ds.docs_count() == 2
    assert [tuple(d) for d in ds.docs_iter()][0] == ("d1", "T1", "alpha")
    assert [tuple(q) for q in ds.queries_iter()] == [("q1", "find alpha"), ("q2", "find beta")]
    assert [tuple(r) for r in ds.qrels_iter()][0] == ("q1", "d1", 1)
    flags = ds.query_flags()
    assert flags["q1"]["origin"] == "expert" and flags["q2"]["origin"] == "llm"


def test_techqa_gold_and_unanswerable(base):
    make_techqa(base / "techqa")
    ds = hd.lookup("techqa/test", gated=False)
    assert ds.docs_count() == 2
    docs = list(ds.docs_iter())
    assert tuple(docs[0]) == ("swg1", "First note", " body one")
    assert [tuple(q) for q in ds.queries_iter()] == [("Q0", "how one")]
    assert [tuple(r) for r in ds.qrels_iter()] == [("Q0", "swg1", 1)]
    assert ds.unanswerable_ids() == ["Q1"]
    assert ds.all_query_ids() == ["Q0", "Q1"]


def test_longmemeval_per_question_corpus_and_abstention(base):
    make_lme(base / "longmemeval-s")
    ds = hd.lookup("heldout/longmemeval-s", gated=False)
    assert ds.question_ids() == ["a1"]
    assert [d.doc_id for d in ds.corpus_for("a1")] == ["s1", "s2"]
    assert next(iter(ds.corpus_for("a1"))).text == "user: hi\nassistant: hello"
    assert ds.qrels_for("a1") == {"s2": 1}
    assert ds.unanswerable_ids() == ["a2_abs"]
    assert [r.doc_id for r in ds.qrels_iter()] == ["s2"]
    assert ds.docs_count() == 3  # distinct sessions across haystacks


def test_reads_are_gated_until_opened(base, monkeypatch):
    make_techqa(base / "techqa")
    ds = hd.lookup("techqa")
    with pytest.raises(PermissionError):
        list(ds.queries_iter())
    monkeypatch.setenv("GESTALT_HELDOUT_OPEN", "techqa")
    assert len(list(ds.queries_iter())) == 1


def test_register_exposes_names_to_ir_datasets(base, monkeypatch):
    ir_datasets = pytest.importorskip("ir_datasets")
    make_techqa(base / "techqa")
    monkeypatch.setenv("GESTALT_HELDOUT_OPEN", "techqa")
    hd.register()
    hd.register()  # twice is harmless
    assert ir_datasets.load("techqa/test").docs_count() == 2


@pytest.mark.parametrize("name", sorted(MAKERS))
def test_seal_then_verify_then_one_byte_change(base, name, capsys):
    fetched(base, name)
    assert ho.main(["seal", name]) == 0
    m = json.loads(ho.manifest_path(base, name).read_text())
    assert m["opened"] == [] and m["commit_sha"] == hd.SPECS[name]["sources"][0]["commit"] and m["license"]
    assert m["query_ids"] == sorted(m["query_ids"]) and len(m["query_ids_sha256"]) == 64
    assert m["counts"]["docs"] >= 2 and m["files"]
    assert ho.main(["verify", name]) == 0
    victim = base / name / sorted(m["files"])[0]
    victim.write_bytes(victim.read_bytes() + b"x")
    assert ho.main(["verify", name]) == 1
    assert "changed:" in capsys.readouterr().err
    assert victim.exists()  # the tool never deletes data
    assert ho.main(["seal", name]) == 1  # a sealed set cannot be sealed again


def test_open_refuses_second_open_without_reopen(base, capsys):
    fetched(base, "techqa")
    assert ho.main(["seal", "techqa"]) == 0
    assert ho.main(["open", "techqa", "--reason", "first look"]) == 0
    assert "GESTALT_HELDOUT_OPEN=techqa" in capsys.readouterr().out
    assert ho.main(["open", "techqa", "--reason", "again"]) == 1
    assert ho.main(["open", "techqa", "--reason", "crash recovery", "--reopen"]) == 0
    opened = json.loads(ho.manifest_path(base, "techqa").read_text())["opened"]
    assert [o["reason"] for o in opened] == ["first look", "crash recovery"]
    assert opened[0]["at"] and opened[0]["git_sha"] and opened[1]["reopen"] is True


def test_open_refuses_a_tampered_set(base):
    fetched(base, "techqa")
    ho.main(["seal", "techqa"])
    (base / "techqa" / "train.json").write_text("[]")
    assert ho.main(["open", "techqa", "--reason", "x"]) == 1
    assert json.loads(ho.manifest_path(base, "techqa").read_text())["opened"] == []


def test_fetch_records_commit_and_refuses_sealed(base, tmp_path_factory):
    src = tmp_path_factory.mktemp("src")
    MAKERS["techqa"](src)
    asked = []

    def fake(url, repo, commit, path, out):
        asked.append((url, repo, commit, path))
        shutil.copyfile(src / path, out)

    dest = base / "dest"
    assert ho.main(["fetch", "techqa", "--dest", str(dest)], downloader=fake) == 0
    sha = hd.SPECS["techqa"]["sources"][0]["commit"]
    assert all(c == sha and sha in u for u, _, c, _ in asked) and len(asked) == 2
    rec = json.loads(ho.fetch_record_path(dest, "techqa").read_text())
    assert rec["sources"][0]["commit"] == sha and rec["fetched_at"]
    assert ho.main(["seal", "techqa", "--dir", str(dest)]) == 0
    asked.clear()
    assert ho.main(["fetch", "techqa", "--dest", str(dest)], downloader=fake) == 1
    assert asked == []


def test_dry_run_downloads_nothing(base, capsys):
    def boom(*a):
        raise AssertionError("network")

    assert ho.main(["fetch", "litsearch", "--dest", str(base / "d"), "--dry-run"], downloader=boom) == 0
    out = capsys.readouterr().out
    assert "f9c8810dcec439c17a4a6ed18baec1922a49b0c4" in out and not (base / "d").exists()
