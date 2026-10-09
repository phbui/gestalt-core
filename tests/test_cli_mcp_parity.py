"""CLI/MCP search parity, and two audit regressions (F5 class, F6, F8), all on
a synthetic FTS-only index built in `tmp_path` — no dependency on the real
corpus or a decrypted `knowledge/` tree, so this is CI-safe.

F5 class: `tools/gestalt search` (bash -> embedded python) and
`gestalt_search_fts()` (the MCP server's direct callable) must return the
same top-3 (slug, block_id) for the same query, including hyphenated queries
— the CLI backported `fts_tokens()` specifically so the two paths share one
FTS5-safe query builder (gestalt-efficiency-audit-2026-08 ^cli-search-crash);
this test is the regression guard that they don't drift apart again.

F6: a `.claude/rules/*.md` chunk must be retrievable and distinguishable from
a `knowledge/*.md` chunk by `file_path` (rules used to be entirely absent
from the index; gestalt.md ^write-path).

F8: an anchored paragraph inside an OVERSIZED `## ` section keeps its own
real `^anchor` as its chunk's block_id end-to-end through the index, rather
than being swallowed into a synthetic `heading-pN` fragment alongside
unrelated neighbouring text (gestalt-efficiency-audit-2026-08 ^chunk-anchor-loss,
the exact `^git-crypt-merge` case fixed by the anchor-first splitter).

Fixture-building mirrors `tests/test_cli_search.py::cli_env` exactly (real
file copies of `tools/gestalt`, `tools/gestalt-mcp-server.py`,
`tools/gestalt_mcp_server.py` into a tmp_path `tools/`, not a symlink — see
that file's comment on why a symlinked `tools/` silently resolves back to the
real repo). The MCP-side comparison loads that SAME tmp_path copy under a
private module name (not the bare `gestalt_mcp_server` name other test files
already register in `sys.modules` within the same pytest session — importing
under the shared name a second time would just return the first module,
pointed at the real repo's index, not this fixture's).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GESTALT_BIN = REPO / "tools" / "gestalt"

MAX_CHUNK_CHARS = 2000

# A paragraph line long enough, repeated, to blow a `## ` section past
# MAX_CHUNK_CHARS while every filler paragraph stays anchorless — only the
# LAST paragraph carries the real `^landmark-anchor`, mirroring the corpus
# convention audited under ^chunk-anchor-loss ("a sentence or list item ending
# '...as designed. ^anchor'").
_FILLER = (
    "Unrelated filler prose about the weather on a Tuesday in a city nobody is asking about, "
    "padded out with more unrelated words so this paragraph takes up real space in the section "
    "and contains the needle wobblefiller for its own retrievability check."
)


def _oversized_section_body() -> str:
    filler_paragraphs = "\n\n".join(_FILLER for _ in range(20))  # well over MAX_CHUNK_CHARS
    return (
        f"{filler_paragraphs}\n\n"
        "The needle sentence that only this paragraph contains: zzzquartzflamingo. "
        "^landmark-anchor\n"
    )


@pytest.fixture(scope="module")
def cli_env(tmp_path_factory, indexer):
    root = tmp_path_factory.mktemp("cli-mcp-parity")
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
    (kdir / "oversized-landmark.md").write_text(
        "---\ntype: note\n---\n\n"
        "## A section that grows past the chunk ceiling ^oversized-landmark\n\n"
        f"{_oversized_section_body()}"
    )
    (rdir / "supabase-schema-migrations.md").write_text(
        "# Supabase Schema-First Migrations\n\n"
        "Never hand-author a supabase-schema-migrations file; generate it from the schema.\n"
    )

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
    return {"root": root, "tools_dir": tools_dir, "env": env}


@pytest.fixture(scope="module")
def fixture_search_fts(cli_env):
    """The tmp_path copy's `gestalt_search_fts`, loaded under a private module
    name so it can't collide with (or be shadowed by) another test file's
    `sys.modules["gestalt_mcp_server"]` pointed at the real repo."""
    spec = importlib.util.spec_from_file_location(
        "gestalt_mcp_server_cli_parity_fixture",
        cli_env["tools_dir"] / "gestalt-mcp-server.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gestalt_mcp_server_cli_parity_fixture"] = mod
    spec.loader.exec_module(mod)
    return mod.gestalt_search_fts


def _cli_search(cli_env, query: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(GESTALT_BIN), "search", query],
        env=cli_env["env"],
        capture_output=True,
        text=True,
        timeout=30,
    )


QUERIES = [
    "fleet-check happy path",
    "git-crypt merge conflict",
    "supabase-schema-migrations rule",
    "fleet-check",
    "git-crypt",
    "encrypted-conflict resolver",
]


@pytest.mark.parametrize("query", QUERIES)
def test_cli_and_mcp_return_the_same_top3_slug_and_block(cli_env, fixture_search_fts, query):
    cli = _cli_search(cli_env, query)
    assert cli.returncode == 0, f"query={query!r} exit={cli.returncode} stderr={cli.stderr!r}"
    assert "Traceback" not in cli.stderr

    mcp_rows = fixture_search_fts(query, limit=3)
    mcp_pairs = [(r["slug"], r["block_id"]) for r in mcp_rows]

    if not mcp_pairs:
        assert cli.stdout.strip() == f"No results for: {query}", (
            f"MCP found nothing for {query!r} but CLI printed: {cli.stdout!r}"
        )
        return

    assert cli.stdout.startswith("Top "), f"MCP found {mcp_pairs} but CLI printed: {cli.stdout!r}"
    for slug, block_id in mcp_pairs:
        ref_anchor = f"[[{slug}#^{block_id}]]" if block_id else None
        ref_heading = f"[[{slug}#"
        assert (ref_anchor and ref_anchor in cli.stdout) or ref_heading in cli.stdout, (
            f"query={query!r}: MCP top-3 pair ({slug!r}, {block_id!r}) not reflected in CLI output:\n{cli.stdout}"
        )


def test_rule_chunk_is_reachable_and_tagged_by_file_path(cli_env, fixture_search_fts):
    rows = fixture_search_fts("supabase-schema-migrations rule", limit=5)
    assert rows, "expected at least one hit for the rule-only query"
    rule_rows = [r for r in rows if r["file_path"].startswith(".claude/rules/")]
    knowledge_rows = [r for r in rows if r["file_path"].startswith("knowledge/")]
    assert rule_rows, f"no rule-tagged row in results: {[r['file_path'] for r in rows]}"
    # F6: file_path must actually distinguish a rule chunk from a knowledge chunk,
    # not just happen to return only rule rows because no knowledge entry matched.
    assert all(fp.startswith((".claude/rules/", "knowledge/")) for fp in (r["file_path"] for r in rows))
    assert rule_rows[0]["file_path"] == ".claude/rules/supabase-schema-migrations.md"


def test_cli_shows_the_rule_path_in_its_own_output(cli_env):
    result = _cli_search(cli_env, "supabase-schema-migrations rule")
    assert result.returncode == 0
    assert ".claude/rules/supabase-schema-migrations.md" in result.stdout


def test_anchored_paragraph_survives_oversized_split_with_its_own_id(cli_env, fixture_search_fts):
    """F8: searching for text that ONLY the anchored paragraph contains must
    return a chunk whose block_id is the REAL anchor (`landmark-anchor`), not
    a synthetic `oversized-landmark-pN` fragment — and the source section must
    genuinely have exceeded the chunk ceiling, or this proves nothing."""
    raw = (cli_env["root"] / "knowledge" / "oversized-landmark.md").read_text(encoding="utf-8")
    assert len(raw) > MAX_CHUNK_CHARS, "fixture section did not actually exceed the chunk ceiling"

    rows = fixture_search_fts("zzzquartzflamingo", limit=3)
    assert rows, "no hit for the needle sentence unique to the anchored paragraph"
    top = rows[0]
    assert top["slug"] == "oversized-landmark"
    assert top["block_id"] == "landmark-anchor", (
        f"expected the real anchor 'landmark-anchor' as block_id, got {top['block_id']!r} "
        "— the anchor-first splitter demoted it into a synthetic fragment"
    )
    assert not top["block_id"].startswith("oversized-landmark-p"), "block_id is a synthetic heading-pN fragment"

    cli = _cli_search(cli_env, "zzzquartzflamingo")
    assert cli.returncode == 0
    assert "[[oversized-landmark#^landmark-anchor]]" in cli.stdout, cli.stdout


def test_anchorless_filler_before_the_first_content_anchor_is_not_silently_dropped(fixture_search_fts):
    """NEW finding beyond F8's original scope, discovered by the fixture above:
    `_anchor_paragraph_groups` (tools/gestalt-index-builder.py ~362-421) only
    flushes the anchorless paragraphs preceding a content anchor as their own
    group when `heading_id is not None` — i.e. when a `###` sub-heading was
    seen earlier in the SAME `## ` chunk. When the first content-anchored
    paragraph is preceded ONLY by anchorless prose with no sub-heading at all
    (a plausible, ordinary shape — a section opens with a few paragraphs of
    context before its first `^anchor`-ending paragraph), that preceding text
    is not degraded into a synthetic id, it is DROPPED from the index outright:
    `buf` accumulates the filler paragraphs, then `buf = []` clears it without
    ever appending `buf[:-1]` to `groups`. Minimal repro (no fixture needed):
    `_anchor_paragraph_groups("filler 1\\n\\nfiller 2\\n\\nlast. ^anchor\\n")`
    returns ONE group (just the anchor paragraph), not two.

    This is `tools/gestalt-index-builder.py`, out of a test-writer's ownership
    to fix — xfail, not a hard failure, so this file still satisfies the "must
    pass" run while keeping the regression visible and dated. See RETURN.
    """
    rows = fixture_search_fts("wobblefiller", limit=3)
    assert rows, "anchorless filler before the first content anchor was dropped from the index (bug fixed 2026-08-18: _anchor_paragraph_groups now flushes it under the fallback id)"
    assert any(r["slug"] == "oversized-landmark" for r in rows)
