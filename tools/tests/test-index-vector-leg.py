#!/usr/bin/env python3
"""needs_rebuild() must notice that the vector leg is gone.

A build that runs under a python without sqlite_vec produces an index that looks perfectly
healthy to an mtime-and-rowcount check: every file is older than the DB and sections_meta is
fully populated. It is simply missing every vector. Before this test, `gestalt-index-builder.py`
with no flags printed "Index is up to date -- skipping rebuild" over exactly such a database,
so the only way to repair it was --force, which requires somebody to already suspect the
problem. That is how the hub served FTS-only artifacts for two weeks.

Second case matters as much as the first: with expect_vectors False, a deliberate --fts-only
index must still read as up to date, or non-hub nodes would rebuild on every single run.
"""
import os, sqlite3, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "gib", Path(__file__).resolve().parents[1] / "gestalt-index-builder.py")
gib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gib)


def rebuild_needed(expect_vectors: bool) -> bool:
    """Call needs_rebuild across both signatures.

    The pre-fix function takes no arguments. Adapting here rather than letting the call raise
    TypeError is what makes this a behavioural test: against the old code it exercises the real
    decision path and fails with "called a vectorless index up to date", which is the actual
    defect, instead of failing merely because a keyword did not exist yet.
    """
    try:
        return gib.needs_rebuild(expect_vectors=expect_vectors)[0]
    except TypeError:
        return gib.needs_rebuild()[0]


def make_db(path: Path, with_vec: bool) -> None:
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE sections_meta (id INTEGER PRIMARY KEY, content_hash TEXT)")
    db.execute("INSERT INTO sections_meta (content_hash) VALUES ('deadbeef')")
    if with_vec:
        # A plain table standing in for the vec0 virtual table. needs_rebuild reads
        # sqlite_master by name and never queries it, precisely so that this check works
        # without the sqlite_vec extension loaded.
        db.execute("CREATE TABLE sections_vec (rowid INTEGER PRIMARY KEY)")
    db.commit(); db.close()
    future = time.time() + 3600          # newer than every knowledge file, so mtime is not the trigger
    os.utime(path, (future, future))


fails = 0
with tempfile.TemporaryDirectory() as td:
    db_path = Path(td) / "gestalt.db"
    gib.DB_PATH = db_path

    make_db(db_path, with_vec=False)
    needed = rebuild_needed(expect_vectors=True)
    if needed:
        print("  ok: a vectorless index with expect_vectors=True asks for a rebuild")
    else:
        print("  FAIL: needs_rebuild called a vectorless index up to date"); fails += 1

    needed = rebuild_needed(expect_vectors=False)
    if not needed:
        print("  ok: the same index reads as up to date when vectors are not expected")
    else:
        print("  FAIL: an --fts-only index would rebuild forever"); fails += 1

    db_path.unlink()
    make_db(db_path, with_vec=True)
    needed = rebuild_needed(expect_vectors=True)
    if not needed:
        print("  ok: an index that has its vector table is left alone")
    else:
        print("  FAIL: a complete index was marked stale"); fails += 1

print("PASS" if fails == 0 else "FAIL")
sys.exit(1 if fails else 0)
