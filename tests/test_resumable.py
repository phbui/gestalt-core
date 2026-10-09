"""resumable.py: atomic writes, the embedding block store and the query log, each under a simulated kill. CPU only, no model."""
from __future__ import annotations

import json
import os
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
import resumable as R  # noqa: E402

DIM = 8


class Killed(Exception):
    pass


class CountingEncoder:
    """crc32 bag-of-words vectors. `encoded` counts every text whose vector was returned. `die_on_call` raises on that call."""

    def __init__(self, die_on_call: int | None = None):
        self.encoded: list[str] = []
        self.calls = 0
        self.die_on_call = die_on_call

    def __call__(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        if self.die_on_call == self.calls:
            raise Killed
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in t.split():
                out[i, zlib.crc32(w.encode()) % DIM] += 1.0
        self.encoded.extend(texts)
        return out


TEXTS = [f"doc{i} word{i % 7} common" for i in range(45)]


def expected() -> np.ndarray:
    return CountingEncoder()(TEXTS)


def stacked(store: R.BlockStore, n: int) -> np.ndarray:
    return np.concatenate([np.asarray(b) for _, b in store.iter_blocks(n)])


def test_atomic_write_replaces_whole_and_leaves_no_temp(tmp_path):
    p = tmp_path / "sub" / "f.json"
    R.atomic_write(p, "one\n")
    R.atomic_write(p, b"two\n")
    assert p.read_bytes() == b"two\n"
    assert [x.name for x in p.parent.iterdir()] == ["f.json"]


def test_atomic_write_keeps_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    p = tmp_path / "f.txt"
    R.atomic_write(p, "old")

    def boom(*a):
        raise Killed

    monkeypatch.setattr(R.os, "replace", boom)
    with pytest.raises(Killed):
        R.atomic_write(p, "new")
    assert p.read_text() == "old" and [x.name for x in tmp_path.iterdir()] == ["f.txt"]


def test_blocks_resume_after_a_kill_between_blocks(tmp_path):
    fp = R.make_fingerprint(model="m", dataset="d")
    enc = CountingEncoder(die_on_call=3)
    with pytest.raises(Killed):
        R.BlockStore(tmp_path, fp, block_size=10).encode_missing(TEXTS, enc)
    assert len(enc.encoded) == 20  # blocks 0 and 1 finished before the kill
    again = CountingEncoder()
    store = R.BlockStore(tmp_path, fp, block_size=10)
    assert store.encode_missing(TEXTS, again) == 25
    assert again.encoded == TEXTS[20:]  # nothing already embedded is embedded twice
    np.testing.assert_array_equal(stacked(store, len(TEXTS)), expected())


def test_a_truncated_array_and_a_wrong_hash_are_each_reencoded_alone(tmp_path):
    fp = R.make_fingerprint(model="m")
    store = R.BlockStore(tmp_path, fp, block_size=10)
    store.encode_missing(TEXTS, CountingEncoder())
    npy1 = tmp_path / "emb-00001.npy"
    npy1.write_bytes(npy1.read_bytes()[:-17])  # a kill mid-write without the atomic rename
    done3 = tmp_path / "emb-00003.done"
    receipt = json.loads(done3.read_text())
    receipt["sha256"] = "0" * 64
    done3.write_text(json.dumps(receipt))
    (tmp_path / "emb-00004.done").unlink()  # a kill after the array, before the receipt
    enc = CountingEncoder()
    assert store.encode_missing(TEXTS, enc) == 25
    assert enc.encoded == TEXTS[10:20] + TEXTS[30:45]
    np.testing.assert_array_equal(stacked(store, len(TEXTS)), expected())


def test_a_foreign_fingerprint_is_refused_and_rebuild_clears_only_stale_state(tmp_path):
    store = R.BlockStore(tmp_path, "a" * 64, block_size=10)
    store.encode_missing(TEXTS, CountingEncoder())
    store.close()  # one process holds one lock per directory
    with pytest.raises(R.FingerprintMismatch, match="--rebuild"):
        R.BlockStore(tmp_path, "b" * 64, block_size=10)
    with pytest.raises(R.FingerprintMismatch):
        R.BlockStore(tmp_path, "a" * 64, block_size=20)  # another block layout is another store
    # rebuild on a matching store keeps everything, so repeating a --rebuild command after a crash loses nothing
    enc = CountingEncoder()
    with R.BlockStore(tmp_path, "a" * 64, block_size=10, rebuild=True) as again:
        again.encode_missing(TEXTS, enc)
    assert enc.encoded == []
    fresh = R.BlockStore(tmp_path, "b" * 64, block_size=10, rebuild=True)
    assert not list(tmp_path.glob("emb-*"))
    enc = CountingEncoder()
    fresh.encode_missing(TEXTS, enc)
    assert enc.encoded == TEXTS


def test_progress_reports_every_block(tmp_path):
    seen = []
    R.BlockStore(tmp_path, "f" * 64, block_size=10).encode_missing(TEXTS, CountingEncoder(), progress=lambda d, t, r: seen.append((d, t)))
    assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


def test_reuse_supplies_rows_and_only_the_rest_is_encoded(tmp_path):
    ref = expected()

    def reuse(start, end):
        mask = np.array([i % 2 == 0 for i in range(start, end)])
        vecs = np.where(mask[:, None], ref[start:end], 0).astype(np.float32)
        return mask, vecs

    enc = CountingEncoder()
    store = R.BlockStore(tmp_path, "r" * 64, block_size=10)
    store.encode_missing(TEXTS, enc, reuse=reuse)
    assert enc.encoded == [t for i, t in enumerate(TEXTS) if i % 2]
    np.testing.assert_array_equal(stacked(store, len(TEXTS)), ref)


HEADER = {"systems": ["bm25", "hybrid"], "rerank": None}


def test_querylog_resumes_and_drops_a_truncated_last_line(tmp_path):
    p = tmp_path / "q.jsonl"
    with R.QueryLog(p, HEADER) as log:
        for i in range(5):
            log.append({"system": "bm25", "qid": f"q{i}", "ids": ["a", "b"]})
    with open(p, "ab") as fh:
        fh.write(b'{"system": "bm25", "qid": "q5", "ids": ["a"')  # killed mid-line
    log = R.QueryLog(p, HEADER)
    assert log.done() == {("bm25", f"q{i}") for i in range(5)}
    log.append({"system": "hybrid", "qid": "q0", "ids": ["c"]})
    log.close()
    lines = p.read_text().splitlines()
    assert all(json.loads(x) for x in lines) and len(lines) == 7  # header, five entries, the new one: the stub is gone
    assert R.QueryLog(p, HEADER).entries[("hybrid", "q0")]["ids"] == ["c"]


def test_querylog_refuses_another_configuration_unless_rebuilt(tmp_path):
    p = tmp_path / "q.jsonl"
    with R.QueryLog(p, HEADER) as log:
        log.append({"system": "bm25", "qid": "q1", "ids": []})
    other = {**HEADER, "rerank": {"model": "bge", "depth": 40}}
    with pytest.raises(R.FingerprintMismatch, match="rerank"):
        R.QueryLog(p, other)
    with R.QueryLog(p, other, rebuild=True) as log:
        assert log.done() == set()
    with R.QueryLog(p, other, rebuild=True) as log:  # a matching log survives --rebuild
        log.append({"system": "bm25", "qid": "q2", "ids": []})
    assert R.QueryLog(p, other, rebuild=True).done() == {("bm25", "q2")}


def test_querylog_batches_fsync(tmp_path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(R.os, "fsync", lambda fd: (calls.append(fd), real(fd)))
    log = R.QueryLog(tmp_path / "q.jsonl", HEADER, sync_every=20, sync_seconds=3600)
    calls.clear()
    for i in range(45):
        log.append({"system": "bm25", "qid": f"q{i}", "ids": []})
    assert len(calls) == 2  # after lines 20 and 40
    log.close()
    assert len(calls) == 3  # the last five on close


# --- SEC-008: one run per work directory ------------------------------------------------------------------------


def test_a_second_blockstore_on_a_held_work_dir_is_refused_with_the_holder_pid(tmp_path):
    first = R.BlockStore(tmp_path, "a" * 64, block_size=10)
    with pytest.raises(R.WorkDirLocked, match=f"pid {os.getpid()}"):
        R.BlockStore(tmp_path, "a" * 64, block_size=10)  # a separate fd, so flock treats it as another process
    first.close()
    R.BlockStore(tmp_path, "a" * 64, block_size=10).close()  # released on close


def test_a_second_process_is_refused_while_the_first_holds_the_lock(tmp_path):
    import subprocess

    code = (f"import sys; sys.path.insert(0, {str(REPO / 'evals' / 'retrieval')!r}); import resumable as R\n"
            f"try:\n    R.BlockStore({str(tmp_path)!r}, 'a'*64, block_size=10)\n"
            "except R.WorkDirLocked as e:\n    print(e); sys.exit(7)\n")
    with R.BlockStore(tmp_path, "a" * 64, block_size=10):
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 7 and f"pid {os.getpid()}" in r.stdout
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).returncode == 0  # free once released


def test_a_refused_blockstore_does_not_keep_the_lock(tmp_path):
    R.BlockStore(tmp_path, "a" * 64, block_size=10).close()
    with pytest.raises(R.FingerprintMismatch):
        R.BlockStore(tmp_path, "b" * 64, block_size=10)
    R.BlockStore(tmp_path, "a" * 64, block_size=10).close()


# --- SEC-009: damage is quarantined or reported, never silent ------------------------------------------------------------------------


def test_an_unparseable_manifest_is_quarantined_and_the_embeddings_survive(tmp_path, capsys):
    fp = "a" * 64
    with R.BlockStore(tmp_path, fp, block_size=10) as store:
        store.encode_missing(TEXTS, CountingEncoder())
    (tmp_path / "manifest.json").write_text("{not json")
    capsys.readouterr()
    enc = CountingEncoder()
    with R.BlockStore(tmp_path, fp, block_size=10) as store:
        assert store.encode_missing(TEXTS, enc) == 0  # every block still proves itself by its receipt
    assert enc.encoded == []
    aside = list(tmp_path.glob("manifest.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == "{not json"
    assert json.loads((tmp_path / "manifest.json").read_text())["fingerprint"] == fp
    err = capsys.readouterr().err
    assert "WARNING" in err and str(tmp_path / "manifest.json") in err


def test_a_block_laid_out_for_another_block_size_is_not_trusted_after_the_manifest_is_lost(tmp_path):
    fp = "a" * 64
    with R.BlockStore(tmp_path, fp, block_size=10) as store:
        store.encode_missing(TEXTS[:30], CountingEncoder())
    (tmp_path / "manifest.json").unlink()
    with R.BlockStore(tmp_path, fp, block_size=15) as other:
        assert not other.block_valid(1, 15)  # block 1 of the old layout starts at row 10, this one would start at 15


def test_a_bad_line_in_the_middle_is_reported_with_the_size_dropped(tmp_path, capsys):
    p = tmp_path / "q.jsonl"
    with R.QueryLog(p, HEADER) as log:
        for i in range(5):
            log.append({"system": "bm25", "qid": f"q{i}", "ids": []})
    lines = p.read_bytes().split(b"\n")
    lines[3] = b"{garbage"  # the header, q0 and q1 are good. q2 is bad. q3 and q4 follow it
    p.write_bytes(b"\n".join(lines))
    capsys.readouterr()
    log = R.QueryLog(p, HEADER)
    assert log.done() == {("bm25", "q0"), ("bm25", "q1")}
    err = capsys.readouterr().err
    assert "WARNING" in err and str(p) in err and "Dropped 3 lines" in err and "bytes" in err
    log.close()


def test_a_cut_short_last_line_is_dropped_without_a_warning(tmp_path, capsys):
    p = tmp_path / "q.jsonl"
    with R.QueryLog(p, HEADER) as log:
        log.append({"system": "bm25", "qid": "q0", "ids": []})
    with open(p, "ab") as fh:
        fh.write(b'{"system": "bm25", "qid": "q1"')
    capsys.readouterr()
    R.QueryLog(p, HEADER).close()
    assert capsys.readouterr().err == ""
