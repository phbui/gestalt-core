"""CLI search integration: F5 backport of fts_tokens() into `gestalt search`.

Regression for gestalt-efficiency-audit-2026-08 ^cli-search-crash: `cmd_search`
in `tools/gestalt` used to hand the raw query straight to
`sections_fts MATCH ?`, which crashed on any hyphenated query —
`./tools/gestalt search "fleet-check happy path"` raised
`sqlite3.OperationalError: no such column: check`, because FTS5's query parser
reads a leading "-token" as a column filter. The MCP server already had the
fix (`fts_tokens()` in tools/gestalt-mcp-server.py, per-token quote + OR); the
CLI now shares it by importing `gestalt_search_fts` through the importable
shim (tools/gestalt_mcp_server.py) instead of re-deriving the SQL. These tests
run the REAL `tools/gestalt` binary as a subprocess against a throwaway
FTS-only index (no sqlite-vec/sentence-transformers needed — cmd_search only
ever calls the lexical leg), so a regression here means a real terminal user
hit a traceback, not just a broken import.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GESTALT_BIN = REPO / "tools" / "gestalt"


@pytest.fixture(scope="module")
def cli_env(tmp_path_factory, indexer):
    """A throwaway GESTALT_DIR: a real FTS-only sections_fts/sections_meta
    index built from a tiny synthetic corpus with hyphenated headings, plus a
    `tools/` symlink back to the real repo so the CLI's internal
    `from gestalt_mcp_server import gestalt_search_fts` import resolves.
    """
    root = tmp_path_factory.mktemp("cli-search")
    kdir = root / "knowledge"
    kdir.mkdir()
    rdir = root / ".claude" / "rules"
    rdir.mkdir(parents=True)
    sdir = root / ".search"
    sdir.mkdir()

    (kdir / "fleet-check.md").write_text(
        "---\ntype: note\n---\n\n"
        "## Fleet check happy path ^fleet-check\n\n"
        "Running fleet-check end to end is the happy path for verifying the fleet.\n"
    )
    (kdir / "git-crypt.md").write_text(
        "---\ntype: note\n---\n\n"
        "## Merge conflicts ^git-crypt-merge\n\n"
        "Resolving a git-crypt merge conflict needs the encrypted-conflict resolver.\n"
    )
    (rdir / "supabase-schema-migrations.md").write_text(
        "# Supabase Schema-First Migrations\n\n"
        "Never hand-author a supabase-schema-migrations file; generate it from the schema.\n"
    )

    # Real copies, not a symlink: gestalt_mcp_server.py's shim resolves
    # __file__ with Path.resolve(), which chases a symlinked tools/ straight
    # back to the REAL repo root and computes DB_PATH there — silently
    # querying the actual corpus instead of this fixture's throwaway one.
    tools_dir = root / "tools"
    tools_dir.mkdir()
    for name in ("gestalt", "gestalt-mcp-server.py", "gestalt_mcp_server.py", "gestalt_rank.py"):
        shutil.copy2(REPO / "tools" / name, tools_dir / name)
        (tools_dir / name).chmod(0o755)
    (root / "evals" / "retrieval").mkdir(parents=True)  # gestalt_rank loads its fusion arithmetic from here
    shutil.copy2(REPO / "evals" / "retrieval" / "fusion.py", root / "evals" / "retrieval" / "fusion.py")

    mp = pytest.MonkeyPatch()
    mp.setattr(indexer, "GESTALT_DIR", root, raising=False)
    mp.setattr(indexer, "KNOWLEDGE_DIR", kdir, raising=False)
    mp.setattr(indexer, "RULES_DIR", rdir, raising=False)
    mp.setattr(indexer, "SEARCH_DIR", sdir, raising=False)
    mp.setattr(indexer, "DB_PATH", sdir / "gestalt.db", raising=False)
    mp.setattr(indexer, "BUILD_PATH", sdir / "gestalt.db.build", raising=False)
    try:
        indexer._build_index_locked(None, None)  # FTS-only — no vec deps needed
    finally:
        mp.undo()

    env = dict(os.environ)
    env["GESTALT_DIR"] = str(root)
    env["GESTALT_PYTHON"] = sys.executable
    return env


@pytest.mark.parametrize(
    "query",
    [
        "fleet-check happy path",
        "git-crypt merge conflict",
        "supabase-schema-migrations rule",
    ],
)
def test_hyphenated_query_exits_cleanly_with_a_hit_or_an_explicit_no_results_line(cli_env, query):
    result = subprocess.run(
        [str(GESTALT_BIN), "search", query],
        env=cli_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"query={query!r} exit={result.returncode} stderr={result.stderr!r}"
    assert "Traceback" not in result.stderr, f"query={query!r} crashed: {result.stderr}"
    combined = result.stdout + result.stderr
    assert "OperationalError" not in combined, f"query={query!r}: {combined}"
    assert result.stdout.startswith("Top ") or result.stdout.startswith("No results for:"), (
        f"query={query!r} produced neither a hit nor an explicit no-results line: {result.stdout!r}"
    )
    # The three fixture queries above are each built to match their own entry.
    assert result.stdout.startswith("Top "), f"query={query!r} unexpectedly found nothing: {result.stdout!r}"


def test_hyphenated_query_with_no_matches_says_so_explicitly(cli_env):
    result = subprocess.run(
        [str(GESTALT_BIN), "search", "zzz-totally-unmatched-token-xyz"],
        env=cli_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "No results for: zzz-totally-unmatched-token-xyz"


def test_search_result_carries_the_source_file_path(cli_env):
    """F6: a rule chunk must be tellable from a knowledge chunk in the CLI's
    own output, not just in the underlying row."""
    result = subprocess.run(
        [str(GESTALT_BIN), "search", "supabase-schema-migrations rule"],
        env=cli_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert ".claude/rules/supabase-schema-migrations.md" in result.stdout
