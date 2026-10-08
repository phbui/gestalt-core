"""Every graphiti/test-patch-*.py runs in the tier. Until 2026-09-06 none of them was wired anywhere, so a patch
whose upstream shape had drifted would only have been found at the next image rebuild, inside the maintenance
window."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((REPO / "graphiti").glob("test-patch-*.py"))


def test_there_are_patch_tests() -> None:
    assert SCRIPTS, "graphiti/test-patch-*.py missing"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_patch_script_discriminates(script: Path) -> None:
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("ok:"), r.stdout
