"""Text cleanup and chunking for tools/read-aloud.py (no audio, no models)."""
import importlib.util
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "tools" / "read-aloud.py"
spec = importlib.util.spec_from_file_location("read_aloud", SRC)
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)

pytest.importorskip("pysbd")


def test_markdown_becomes_speakable():
    md = (
        "---\ntitle: x\n---\n# Why it closed, and what it cost\n\n"
        "The **bridge** closed on *Tuesday*. See [the notice](https://e.com/n) and https://e.com [12].\n"
        "Settlement was 3.5 cm — above the threshold.\n\n"
        "- First point\n- Second $L = 0.8$ point\n\n"
        "| Year | N |\n|---|---|\n| 2024 | 3 |\n\n```py\nprint(1)\n```\n\n> quoted\n"
    )
    out = ra.to_speakable(md)
    assert out.startswith("Why it closed, and what it cost.")
    assert "title: x" not in out
    assert "**" not in out and "*Tuesday*" not in out
    assert "the notice" in out and "https://" not in out and "[12]" not in out
    assert "settlement was 3.5 cm, above the threshold." in out.lower()
    assert "First point\nSecond L = 0.8 point" in out
    assert "Year, N.\n2024, 3." in out
    assert "print(1)" not in out and "(code block omitted)" in out
    assert "quoted" in out and ">" not in out


def test_display_math_is_announced_not_read():
    out = ra.to_speakable("Before $$\\int_0^1 f(x) dx$$ after.")
    assert "\\int" not in out and "equation omitted" in out


def test_sentences_and_paragraph_markers():
    s = ra.split_sentences("Dr. Smith et al. found it. It worked!\n\nNew paragraph here.")
    assert s == ["Dr. Smith et al. found it.", "It worked!", "", "New paragraph here."]


def test_chunks_respect_limit_and_gaps():
    long = "word " * 120  # 600 chars, one "sentence" with no punctuation
    chunks = ra.make_chunks(["Short one.", "", long.strip(), "Tail."], limit=100)
    assert all(len(t) <= 100 for t, _ in chunks)
    assert chunks[0] == ("Short one.", ra.PARAGRAPH_GAP_S)
    assert chunks[-1][0].endswith("Tail.")
    assert chunks[-1][1] == ra.SENTENCE_GAP_S


def test_short_sentences_pack_together():
    chunks = ra.make_chunks(["A b.", "C d.", "E f."], limit=100)
    assert chunks == [("A b. C d. E f.", ra.SENTENCE_GAP_S)]
