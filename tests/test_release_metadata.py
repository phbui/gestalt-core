"""The release metadata agree with each other: VERSION, CITATION.cff and the newest CHANGELOG entry carry one version.

The files live at the repository root in the public export and under publish/overlay/ in the source tree, so the test looks in both."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = [ROOT / "publish" / "overlay", ROOT]
BASE = next((c for c in CANDIDATES if (c / "VERSION").exists()), None)

pytestmark = pytest.mark.skipif(BASE is None, reason="no VERSION file in the overlay or at the root")


def test_version_file_is_one_semantic_version():
    v = (BASE / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), v


def test_citation_cff_carries_the_same_version():
    v = (BASE / "VERSION").read_text().strip()
    cff = (BASE / "CITATION.cff").read_text()
    m = re.search(r"^version:\s*\"?([^\"\n]+)\"?\s*$", cff, re.M)
    assert m and m.group(1).strip() == v, (m and m.group(1), v)


def test_changelog_newest_entry_is_the_version():
    v = (BASE / "VERSION").read_text().strip()
    log = (BASE / "CHANGELOG.md").read_text()
    m = re.search(r"^## (\d+\.\d+\.\d+), (\d{4}-\d{2}-\d{2})$", log, re.M)
    assert m and m.group(1) == v, (m and m.group(0), v)


def test_security_contributing_and_conduct_exist():
    for name in ("SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "THIRD-PARTY-NOTICES.md"):
        assert (BASE / name).exists(), name
