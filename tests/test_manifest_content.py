"""`tools/gestalt rebuild` correctness: MANIFEST.md lists exactly the entries
under `knowledge/`, each with its real outgoing wikilinks and `^block-id`s, and
GRAPH.md's per-slug sections correspond to entries that actually carry a
`## Data Flow` section.

CI-safe: runs entirely against a synthetic 3-entry fixture repo built under
`tmp_path`, using `tools/gestalt`'s `GESTALT_DIR` env-var root override (the
script derives every path — `KNOWLEDGE_DIR`, `MANIFEST`, `GRAPH`, `RULES_DIR` —
from `$GESTALT_DIR` at the top of the file rather than from its own location,
so pointing it at a tmp_path fixture is sufficient; no copying of `tools/` is
needed here because `cmd_rebuild` is pure bash with no Python import to resolve,
unlike `tests/test_cli_search.py`'s `cmd_search`, which does need real copies).

Idempotency (a second no-op rebuild is byte-identical) is already covered by
`tests/test_tools.py::test_gestalt_cli_rebuild_is_idempotent_on_indices` against
the real repo — not duplicated here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GESTALT_BIN = REPO / "tools" / "gestalt"

FIXTURE_ENTRIES = {
    "alpha": (
        "---\n"
        "type: note\n"
        'title: "Alpha entry"\n'
        "tags: [test]\n"
        "created: 2026-08-18\n"
        "updated: 2026-08-18\n"
        "---\n\n"
        "## Overview ^overview\n\n"
        "Links to [[beta]] and [[gamma#^gamma-anchor]]. ^alpha-landmark\n"
    ),
    "beta": (
        "---\n"
        "type: note\n"
        'title: "Beta entry"\n'
        "tags: [test]\n"
        "created: 2026-08-18\n"
        "updated: 2026-08-18\n"
        "---\n\n"
        "## Overview ^overview\n\n"
        "Refers back to [[alpha]]. ^beta-landmark\n\n"
        "## Data Flow ^data-flow\n\n"
        "alpha -> beta -> gamma\n"
    ),
    "gamma": (
        "---\n"
        "type: reference\n"
        'title: "Gamma entry"\n'
        "tags: [test]\n"
        "created: 2026-08-18\n"
        "updated: 2026-08-18\n"
        "---\n\n"
        "## Something ^gamma-anchor\n\n"
        "The gamma content.\n"
    ),
}


@pytest.fixture
def fixture_repo(tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    for slug, body in FIXTURE_ENTRIES.items():
        (kdir / f"{slug}.md").write_text(body, encoding="utf-8")
    return tmp_path


def _rebuild(root: Path) -> subprocess.CompletedProcess:
    env = {"GESTALT_DIR": str(root), "HOME": str(root / ".home"), "PATH": __import__("os").environ.get("PATH", "")}
    return subprocess.run(
        ["bash", str(GESTALT_BIN), "rebuild"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(root),
        timeout=30,
        check=False,
    )


def test_rebuild_succeeds_on_a_synthetic_repo(fixture_repo):
    r = _rebuild(fixture_repo)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert (fixture_repo / "MANIFEST.md").exists()
    assert (fixture_repo / "GRAPH.md").exists()


def test_manifest_lists_exactly_the_knowledge_entries(fixture_repo):
    _rebuild(fixture_repo)
    manifest = (fixture_repo / "MANIFEST.md").read_text(encoding="utf-8")
    listed_slugs = set(re.findall(r"^### (\S+)$", manifest, re.M))
    assert listed_slugs == set(FIXTURE_ENTRIES), f"MANIFEST slugs {listed_slugs} != fixture entries {set(FIXTURE_ENTRIES)}"


def test_manifest_carries_each_entrys_real_outgoing_links(fixture_repo):
    _rebuild(fixture_repo)
    manifest = (fixture_repo / "MANIFEST.md").read_text(encoding="utf-8")
    # alpha links to beta and gamma; beta links back to alpha; gamma links nowhere.
    alpha_block = manifest.split("### alpha", 1)[1].split("### ", 1)[0]
    assert "[[beta]]" in alpha_block
    assert "[[gamma]]" in alpha_block
    beta_block = manifest.split("### beta", 1)[1].split("### ", 1)[0]
    assert "[[alpha]]" in beta_block
    gamma_block = manifest.split("### gamma", 1)[1]
    assert "Links:" not in gamma_block.split("\n\n")[0]


def test_manifest_carries_each_entrys_own_block_ids(fixture_repo):
    _rebuild(fixture_repo)
    manifest = (fixture_repo / "MANIFEST.md").read_text(encoding="utf-8")
    alpha_block = manifest.split("### alpha", 1)[1].split("### ", 1)[0]
    assert "^overview" in alpha_block
    assert "^alpha-landmark" in alpha_block
    gamma_block = manifest.split("### gamma", 1)[1]
    assert "^gamma-anchor" in gamma_block


def test_graph_edges_correspond_to_entries_with_a_data_flow_section(fixture_repo):
    _rebuild(fixture_repo)
    graph = (fixture_repo / "GRAPH.md").read_text(encoding="utf-8")
    # Only beta declares a `## Data Flow` section; alpha and gamma must not appear.
    assert "### beta" in graph
    assert "### alpha" not in graph
    assert "### gamma" not in graph


def test_rebuild_is_idempotent_on_the_fixture(fixture_repo):
    _rebuild(fixture_repo)
    before = (fixture_repo / "MANIFEST.md").read_bytes()
    r = _rebuild(fixture_repo)
    assert r.returncode == 0
    assert (fixture_repo / "MANIFEST.md").read_bytes() == before, (
        "MANIFEST.md changed on a no-op rebuild of the fixture — see also "
        "tests/test_tools.py::test_gestalt_cli_rebuild_is_idempotent_on_indices for the real-repo variant"
    )
