"""tools/tests/ holds the discrimination tests written during the 2026-09 repair (each proves a fix RED against
the old behaviour and GREEN against the new). Until this file existed nothing ran them automatically (guard
audit 2026-09-05); CI and the local suite now execute each one as a subprocess. Scripts that need a live hub
or state the CI checkout lacks are skipped with the reason, never silently passed."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((REPO / "tools" / "tests").glob("test-*"))
# Scripts skipped because they are known-stale against a shipped change. Empty is the goal: an
# entry here is a test that is not testing anything, so it should carry the reason and be
# removed the moment the script is updated. The two chunk-gate entries were cleared on
# 2026-09-05 once both harnesses were re-aimed at the current chunker.
STALE: dict[str, str] = {}


# scripts that run the real chunker over knowledge/*.md without naming the directory in their own text
NEEDS_CORPUS = {"test-corpus-fire-skip.py", "test-sync-skip-names.sh"}
def _corpus_unreadable() -> bool:
    # A checkout without knowledge/home-mesh.md used to fail collection here (register A-71, 2026-10-06).
    try:
        return (REPO / "knowledge" / "home-mesh.md").read_bytes().startswith(b"\x00GITCRYPT")
    except OSError:
        return True


CORPUS_ENCRYPTED = _corpus_unreadable()


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_discrimination_script_is_green(script: Path) -> None:
    if script.name in STALE:
        pytest.skip(STALE[script.name])
    text = script.read_text(errors="ignore")
    if CORPUS_ENCRYPTED and (script.name in NEEDS_CORPUS or "knowledge/" in text):
        pytest.skip("script reads the knowledge corpus, which is git-crypt ciphertext here (CI has no key)")
    py = os.environ.get("GESTALT_PYTHON") or sys.executable
    cmd = [py, str(script)] if script.suffix == ".py" else ["bash", str(script)]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=300)
    tail = "\n".join((r.stdout + r.stderr).splitlines()[-8:])
    if r.returncode != 0 and ("No module named" in tail or "sqlite_vec" in tail or "docker" in tail.lower()):
        pytest.skip(f"environment lacks a dependency the script needs: {tail[-200:]}")
    assert r.returncode == 0, tail
