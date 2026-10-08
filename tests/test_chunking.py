"""Chunker edge cases.

Every case here corresponds to something that either did go wrong in this
corpus or would silently degrade retrieval if it did. Regression tests, not
speculative coverage.
"""

from __future__ import annotations

import pytest

FRONT = "---\ntype: note\ntitle: Fixture\n---\n\n"


# --- structural parsing ------------------------------------------------------


def test_frontmatter_is_stripped(write_entry):
    chunks = write_entry(FRONT + "## Alpha ^alpha\n\nbody text\n")
    assert all("type: note" not in c["content"] for c in chunks)


def test_empty_file_yields_no_chunks(write_entry):
    assert write_entry("") == []


def test_frontmatter_only_yields_no_chunks(write_entry):
    assert write_entry(FRONT) == []


def test_content_before_first_heading_is_kept(write_entry):
    """A preamble with no `##` above it must not be silently dropped."""
    chunks = write_entry(FRONT + "orphan preamble line\n\n## Alpha ^alpha\n\nbody\n")
    assert any("orphan preamble" in c["content"] for c in chunks)


def test_explicit_block_id_is_parsed(write_entry):
    chunks = write_entry(FRONT + "## Alpha ^my-anchor\n\nbody\n")
    assert chunks[0]["block_id"] == "my-anchor"
    assert chunks[0]["heading"] == "Alpha"


def test_block_id_is_derived_when_absent(write_entry):
    chunks = write_entry(FRONT + "## Some Long Heading\n\nbody\n")
    assert chunks[0]["block_id"] == "some-long-heading"


def test_standalone_anchor_line_sets_block_id(write_entry):
    chunks = write_entry(FRONT + "## Alpha\n\nbody\n^inline-anchor\n")
    assert chunks[0]["block_id"] == "inline-anchor"


def test_heading_inside_code_fence_does_not_split(write_entry):
    """`## ` inside a fenced block is a shell comment, not a heading."""
    body = FRONT + "## Real ^real\n\n```bash\n## not a heading\necho hi\n```\n\nafter\n"
    chunks = write_entry(body)
    assert len(chunks) == 1
    assert "not a heading" in chunks[0]["content"]


def test_crlf_line_endings_parse(write_entry):
    chunks = write_entry((FRONT + "## Alpha ^alpha\n\nbody\n").replace("\n", "\r\n"))
    assert chunks and "body" in chunks[0]["content"]


def test_unicode_survives_roundtrip(write_entry):
    chunks = write_entry(FRONT + "## Alpha ^alpha\n\nnaïve café — 日本語 ✅\n")
    assert "日本語" in chunks[0]["content"]


# --- size ceiling ------------------------------------------------------------


def test_ceiling_enforced_with_paragraphs(write_entry, indexer):
    body = FRONT + "## Big ^big\n\n" + "\n\n".join(["word " * 100] * 40)
    for c in write_entry(body):
        assert len(c["content"]) <= indexer.MAX_CHUNK_CHARS


def test_ceiling_enforced_without_paragraph_breaks(write_entry, indexer):
    """Regression: the real corpus had a 20,114-char section with ONE blank
    line, which a paragraph-only splitter left entirely unsplit."""
    body = FRONT + "## Big ^big\n\n" + ("sentence text " * 3000)
    chunks = write_entry(body)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c["content"]) <= indexer.MAX_CHUNK_CHARS


def test_ceiling_enforced_with_no_whitespace_at_all(write_entry, indexer):
    """Pathological input: one unbroken token far over the limit."""
    body = FRONT + "## Big ^big\n\n" + ("x" * (indexer.MAX_CHUNK_CHARS * 3))
    chunks = write_entry(body)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c["content"]) <= indexer.MAX_CHUNK_CHARS


def test_split_parts_get_unique_block_ids(write_entry):
    body = FRONT + "## Big ^big\n\n" + ("sentence text " * 3000)
    ids = [c["block_id"] for c in write_entry(body)]
    assert ids[0] == "big"
    assert len(ids) == len(set(ids)), "sub-chunk block_ids must stay unique"
    assert all(i.startswith("big") for i in ids)


def test_split_parts_preserve_heading_and_slug(write_entry):
    body = FRONT + "## Big ^big\n\n" + ("sentence text " * 3000)
    chunks = write_entry(body)
    assert {c["heading"] for c in chunks} == {"Big"}
    assert {c["slug"] for c in chunks} == {"fixture"}


def test_no_content_lost_across_split(write_entry):
    """Overlap may duplicate, but nothing may vanish."""
    marker_a, marker_b = "UNIQUEALPHA", "UNIQUEOMEGA"
    body = FRONT + "## Big ^big\n\n" + marker_a + " filler " * 2000 + marker_b
    joined = " ".join(c["content"] for c in write_entry(body))
    assert marker_a in joined
    assert marker_b in joined


def test_chunk_at_exact_limit_is_not_split(write_entry, indexer):
    body = FRONT + "## Exact ^exact\n\n" + ("y" * indexer.MAX_CHUNK_CHARS)
    assert len(write_entry(body)) == 1


# --- splitter unit level -----------------------------------------------------


@pytest.mark.parametrize("size", [1, 50, 999, 5000, 50_000])
def test_split_text_always_respects_limit(indexer, size):
    text = "lorem ipsum dolor sit amet " * size
    for part in indexer._split_text(text, indexer.MAX_CHUNK_CHARS, indexer.SUBCHUNK_OVERLAP):
        assert len(part) <= indexer.MAX_CHUNK_CHARS


def test_split_text_short_input_is_identity(indexer):
    assert indexer._split_text("short", 100, 10) == ["short"]


def test_split_text_terminates_on_zero_overlap(indexer):
    parts = indexer._split_text("a" * 5000, 1000, 0)
    assert parts and all(len(p) <= 1000 for p in parts)


# --- anchor-first splitting (F8, ^chunk-anchor-loss) -------------------------


def test_anchored_paragraph_keeps_id_inside_oversized_heading(write_entry, indexer):
    """A short paragraph carrying its own trailing `^anchor` inside a `##`
    section that overflows MAX_CHUNK_CHARS overall must become its own
    dedicated chunk keyed by that anchor — not get diluted into a synthetic
    `-pN` fragment mixed with unrelated padding either side of it. Regression
    for gestalt-efficiency-audit-2026-08 ^chunk-anchor-loss (the real-corpus
    case: `^git-crypt-merge` ranked #3 behind a chunk that never mentions
    git-crypt, because the old splitter ignored anchors entirely)."""
    filler_one = ("padding text " * 200) + "done. ^filler-anchor-one"
    filler_two = ("padding text " * 200) + "done. ^filler-anchor-two"
    body = (
        FRONT
        + "## Big ^big\n\n"
        + filler_one
        + "\n\n"
        + "short target text. ^target-anchor\n\n"
        + filler_two
    )
    assert len(filler_one) > indexer.MAX_CHUNK_CHARS  # each filler is oversized alone
    chunks = write_entry(body)
    assert len(chunks) > 1

    target = [c for c in chunks if c["block_id"] == "target-anchor"]
    assert len(target) == 1, [c["block_id"] for c in chunks]
    assert target[0]["content"].strip() == "short target text. ^target-anchor"
    assert len(target[0]["content"]) < indexer.MAX_CHUNK_CHARS

    # The oversized neighbours still get split, but keep THEIR anchor as the
    # base id for part 0 rather than a synthetic "big-pN".
    filler_ids = {c["block_id"] for c in chunks if c["block_id"] != "target-anchor"}
    assert "filler-anchor-one" in filler_ids
    assert "filler-anchor-two" in filler_ids
    assert not any(i.startswith("big-p") for i in filler_ids)


def test_no_internal_anchors_falls_back_to_size_only_split(write_entry, indexer):
    """A heading with no internal ^anchors keeps the pre-F8 behaviour exactly:
    size-only split, first part keeps the heading's own block_id."""
    body = FRONT + "## Big ^big\n\n" + ("sentence text " * 3000)
    chunks = write_entry(body)
    ids = [c["block_id"] for c in chunks]
    assert ids[0] == "big"
    assert len(ids) == len(set(ids))
    assert all(i.startswith("big") for i in ids)
