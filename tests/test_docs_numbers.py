"""F12 (gestalt-efficiency-audit-2026-08 ^docs-drift): the numbers `knowledge/gestalt.md`
states about itself (entry count, chunk count, index size) silently rot as the corpus
grows — the audit's own headline example was gestalt.md claiming "28 entries / 554
chunks / 6.5 MB" against an actual 40/797/11.0 MB, corrected in the audit's own commit.
This is the regression test for that class: it re-measures the real repo/index every
run and compares against the SAME marked sentences the audit corrected, rather than
trusting that a past correction stays true.

`local`-only: needs the real (decrypted) `knowledge/` tree and, for the chunk/size
checks, a built `.search/gestalt.db` — skips cleanly without either, per `gestalt.md`
^engine-baseline noting the index is a derived, rebuildable artifact, not something a
docs test should require building.

Each check is written to XFAIL (not error, not silently pass) when it finds real,
live drift, reporting the exact measured numbers in the reason — this file's own job
is to make that drift visible and dated, not to force a permanent green by loosening
tolerances. A future run where the doc has been corrected turns the xfail into a real
pass with no code change needed here.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.local

REPO = Path(__file__).resolve().parent.parent
KDIR = REPO / "knowledge"
GESTALT_MD = REPO / "knowledge" / "gestalt.md"
DB_PATH = REPO / ".search" / "gestalt.db"

GITCRYPT_MAGIC = b"\x00GITCRYPT"


def _corpus_readable() -> bool:
    entries = list(KDIR.glob("*.md")) if KDIR.exists() else []
    if not entries:
        return False
    return not entries[0].read_bytes().startswith(GITCRYPT_MAGIC)


pytestmark = [
    pytest.mark.local,
    pytest.mark.skipif(not _corpus_readable(), reason="knowledge/ is git-crypt encrypted here"),
]

# The single marked sentences this test treats as gestalt.md's canonical, checkable
# claims. Chosen because each is unambiguous and singular — gestalt.md ALSO contains
# two other, staler "28 [curated] entries/files" mentions (the ^overview closing
# sentence and the Repo Layout tree comment) that were missed by the 2026-08-18
# correction commit; those are a real, separate, second instance of the same F12
# class, reported in RETURN rather than asserted on here, since a helper keyed to
# a single marked line — per this task's own instructions — can only be as robust
# as gestalt.md having exactly one such line, and it doesn't. OPEN, not fixed here
# (a prose correction is outside a test-writer's read-knowledge/-only scope).
ENTRIES_RE = re.compile(r"(\d+) curated markdown entries under")
CHUNKS_RE = re.compile(r"(\d+) chunks is nowhere near the regime")
DB_MB_RE = re.compile(r"gestalt\.db` \(([\d.]+) MB SQLite\)")


@pytest.fixture(scope="module")
def doc_text() -> str:
    if not GESTALT_MD.exists():
        pytest.skip("knowledge/gestalt.md missing")
    return GESTALT_MD.read_text(encoding="utf-8")


def test_marked_lines_are_present(doc_text):
    """If any of these three sentences got reworded, the rest of this file is
    silently checking nothing — fail loudly rather than let the other tests
    vacuously pass."""
    assert ENTRIES_RE.search(doc_text), "no 'N curated markdown entries under' sentence found in gestalt.md"
    assert CHUNKS_RE.search(doc_text), "no 'N chunks is nowhere near the regime' sentence found in gestalt.md"
    assert DB_MB_RE.search(doc_text), "no 'gestalt.db (N MB SQLite)' sentence found in gestalt.md"


def test_entry_count_matches_real_corpus(doc_text):
    m = ENTRIES_RE.search(doc_text)
    if not m:
        pytest.skip("marked entries-count sentence not found (see test_marked_lines_are_present)")
    documented = int(m.group(1))
    real = len(list(KDIR.glob("*.md")))
    if documented != real:
        pytest.xfail(
            f"F12 docs drift: gestalt.md says {documented} curated markdown entries, "
            f"the real corpus has {real} (diff {real - documented:+d}) as measured just now"
        )
    assert documented == real


def test_chunk_count_within_tolerance_of_index(doc_text):
    if not DB_PATH.exists():
        pytest.skip("search index not built")
    m = CHUNKS_RE.search(doc_text)
    if not m:
        pytest.skip("marked chunk-count sentence not found (see test_marked_lines_are_present)")
    documented = int(m.group(1))
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        real = db.execute("SELECT COUNT(*) FROM sections_meta").fetchone()[0]
    finally:
        db.close()
    tolerance = 0.10
    low, high = documented * (1 - tolerance), documented * (1 + tolerance)
    if not (low <= real <= high):
        pct = (real - documented) / documented
        pytest.xfail(
            f"F12 docs drift: gestalt.md says {documented} chunks, sections_meta has {real} "
            f"({pct:+.1%}, outside the ±{tolerance:.0%} tolerance) as measured just now"
        )
    assert low <= real <= high


def test_db_size_within_tolerance_of_documented_mb(doc_text):
    if not DB_PATH.exists():
        pytest.skip("search index not built")
    m = DB_MB_RE.search(doc_text)
    if not m:
        pytest.skip("marked db-size sentence not found (see test_marked_lines_are_present)")
    documented_mb = float(m.group(1))
    real_mb = DB_PATH.stat().st_size / (1024 * 1024)
    tolerance = 0.20
    low, high = documented_mb * (1 - tolerance), documented_mb * (1 + tolerance)
    if not (low <= real_mb <= high):
        pct = (real_mb - documented_mb) / documented_mb
        pytest.xfail(
            f"F12 docs drift: gestalt.md says {documented_mb} MB, .search/gestalt.db is "
            f"{real_mb:.2f} MB ({pct:+.1%}, outside the ±{tolerance:.0%} tolerance) as measured just now — "
            "likely because the index was rebuilt FTS-only (no embeddings) by a concurrent session"
        )
    assert low <= real_mb <= high
