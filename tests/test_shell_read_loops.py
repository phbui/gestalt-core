"""A bash `while IFS= read -r x; do` loop silently drops a last line that has no trailing newline, which is
exactly what Python's "\\n".join() feeds it; on 2026-09-04 that left one of eight absent paper episodes
unsent with no error (gestalt ^audit-surface-traps, seventh instance). Every read loop in the shell tools
carries the `|| [ -n "$x" ]` guard, and this test keeps it that way."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FILES = sorted(p for p in list(REPO.glob("tools/*.sh")) + list(REPO.glob("tools/fleet/bin/*")) + list(REPO.glob("tools/fleet/*.sh"))
               if p.is_file() and "bash" in p.read_text(errors="ignore")[:200])
LOOP = re.compile(r"while IFS= read -r (\w+)(.*?); do")


@pytest.mark.parametrize("path", FILES, ids=[str(p.relative_to(REPO)) for p in FILES])
def test_every_read_loop_survives_a_missing_trailing_newline(path: Path) -> None:
    bad = []
    for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
        m = LOOP.search(line)
        if m and f'|| [ -n "${m.group(1)}" ]' not in line:
            bad.append(f"{path.relative_to(REPO)}:{n}: {line.strip()[:80]}")
    assert not bad, "unguarded read loops (add `|| [ -n \"$var\" ]`): " + "; ".join(bad)
