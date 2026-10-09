"""Regression tests for the silent-failure class.

Five features in this repo have shipped broken and reported success by staying
quiet. They were invisible to tests, logs, AND code review, and they all failed
the same two ways:

  1. An import wrapped in try/except that could never resolve, so the feature
     degraded to a no-op instead of erroring.
  2. A relevance threshold set above the maximum score the scoring function can
     produce, so the guarded branch could never execute.

Instances found so far: `gestalt_search` unimportable (hyphenated module name AND
a closure inside `build_mcp()`), `auto-promote.read_manifest()` parsing a table
format the generator never emits, and thresholds of 0.05 / 0.1 / 0.05 against an
RRF ceiling of 2/(K+1) = 0.0328 in `gestalt-session-start.sh`, `auto-promote.py`,
and `post-compact-reinject.sh`.

Every test here fails loudly if one of those is reintroduced. They assert on real
imports and real source constants — never on a mock of the thing under test,
because a mock is exactly what would have hidden the original bugs.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
HOOKS = REPO / "claude-tree/hooks"  # F10: real repo tree, not the .claude compat symlink


# --------------------------------------------------------------------------
# Class 1: an import that can never resolve
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shim():
    """The importable alias for the hyphenated server module.

    Imported the way real callers do it — `sys.path` + a plain import statement —
    rather than through importlib, because the original bug was that this exact
    statement could not resolve. Loading it any other way would test a different
    thing and pass while production stayed broken.
    """
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    return pytest.importorskip(
        "gestalt_mcp_server",
        reason="tools/ not importable; the shim itself is what this file tests",
    )


def test_gestalt_search_importable(shim):
    assert callable(shim.gestalt_search)


def test_gestalt_search_fts_importable(shim):
    """The lexical-only entry point the per-prompt hook depends on."""
    assert callable(shim.gestalt_search_fts)


def test_shim_exports_the_full_public_surface(shim):
    """Every name a caller imports must be present.

    Listed explicitly rather than derived from __all__, so deleting a name from
    both the export list and the module cannot quietly pass.
    """
    for name in (
        "gestalt_search",
        "gestalt_search_fts",
        "build_mcp",
        "get_db",
        "get_fts_db",
        "get_model",
    ):
        assert hasattr(shim, name), f"shim no longer exports {name}"


def test_fts_path_does_not_import_torch():
    """The lexical path must stay torch-free.

    This is the whole reason gestalt_search_fts exists: importing the hybrid path
    loads a sentence-transformers model, which measured 14-18 s and blew the
    UserPromptSubmit hook's 5 s timeout on every single prompt. Asserting in a
    SUBPROCESS is deliberate — torch may already be resident in the test process
    via another import, which would make an in-process check vacuous.
    """
    code = (
        f"import sys; sys.path.insert(0, {str(TOOLS)!r})\n"
        "from gestalt_mcp_server import gestalt_search_fts\n"
        "gestalt_search_fts('retrieval index', limit=1)\n"
        "assert 'torch' not in sys.modules, 'FTS path pulled in torch'\n"
        "assert 'sentence_transformers' not in sys.modules, 'FTS path loaded the model'\n"
        "print('CLEAN')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr[-2000:]!r}"
    assert "CLEAN" in r.stdout


@pytest.mark.parametrize(
    "relpath",
    [
        "claude-tree/hooks/prompt-intelligence.py",
        "claude-tree/hooks/gestalt-session-start.sh",
        "claude-tree/hooks/post-compact-reinject.sh",
    ],
)
def test_call_sites_import_a_name_the_shim_actually_exports(relpath):
    """Guard the producer/consumer contract at every call site.

    The original failure was five call sites importing a name that did not exist,
    each inside a try/except that swallowed the ImportError. Here we read what each
    site actually imports and check it against the shim's real exports, so a typo
    or a rename fails CI instead of degrading to silence.
    """
    path = REPO / relpath
    if not path.exists():
        pytest.skip(f"{relpath} absent")
    text = path.read_text()
    # [^\S\n] = horizontal whitespace only. A plain \s crosses the newline and
    # swallows the following line into the captured name list.
    imported = set(
        re.findall(
            r"from[^\S\n]+gestalt_mcp_server[^\S\n]+import[^\S\n]+([A-Za-z_][A-Za-z0-9_,\x20\t]*)",
            text,
        )
    )
    if not imported:
        pytest.skip(f"{relpath} does not import from gestalt_mcp_server")

    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    mod = pytest.importorskip("gestalt_mcp_server")

    for clause in imported:
        for name in (n.strip() for n in clause.split(",")):
            if not name:
                continue
            assert hasattr(mod, name), (
                f"{relpath} imports `{name}` from gestalt_mcp_server, "
                f"which does not export it — this import is silently dead"
            )


# --------------------------------------------------------------------------
# F13 (gestalt-efficiency-audit-2026-08 ^silent-sqlite-vec): a node missing
# sqlite-vec/sentence-transformers used to make the hybrid leg degrade to a
# silent no-op — `post-compact-reinject.sh:100` wraps gestalt_search in a bare
# `except Exception: pass`, and `gestalt-index-builder.py` aborted the ENTIRE
# build (sys.exit(1)) so even the FTS tables never got written. Both call
# sites must now log ONE clear line naming the module + install command and
# continue FTS-only instead.
# --------------------------------------------------------------------------

def _load_mcp_impl():
    """The real hyphenated MCP server module, loaded directly (not through the
    shim) so get_model/gestalt_search_fts can be monkeypatched on the exact
    module object gestalt_search's own globals resolve against — patching the
    shim's re-exported copy would not affect the function's internal lookups.
    """
    spec = importlib.util.spec_from_file_location(
        "_test_mcp_impl_f13", TOOLS / "gestalt-mcp-server.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gestalt_search_logs_and_falls_back_to_fts_when_vec_deps_missing(monkeypatch, capsys):
    mod = _load_mcp_impl()

    def _raise_missing(*_a, **_k):
        raise ImportError("No module named 'sentence_transformers'", name="sentence_transformers")

    sentinel = [
        {
            "slug": "fts-fallback",
            "heading": "h",
            "block_id": "b",
            "content": "c",
            "file_path": "f",
            "bm25": -1.0,
        }
    ]
    monkeypatch.setenv("GESTALT_SEARCH_MODE", "hybrid")  # F18: default is fts off the hub, which never reaches the model
    monkeypatch.setattr(mod, "get_db", _raise_missing)     # F18: get_db is probed before get_model now
    monkeypatch.setattr(mod, "get_model", _raise_missing)
    monkeypatch.setattr(mod, "gestalt_search_fts", lambda query, limit=10: sentinel)
    # Give DB_PATH.exists() a real file so the search doesn't short-circuit on
    # the "index not built" branch before ever reaching get_model().
    monkeypatch.setattr(mod, "DB_PATH", Path(__file__), raising=False)

    result = mod.gestalt_search("git-crypt merge conflict")

    captured = capsys.readouterr()
    assert "Missing dependency" in captured.err, "must log to stderr, never stdout (the MCP stdio channel)"
    assert "sentence_transformers" in captured.err
    assert "pip install" in captured.err
    assert result == sentinel, "gestalt_search must fall back to the FTS leg, not crash or return nothing"


def test_builder_logs_and_continues_fts_only_when_sqlite_vec_missing(tmp_path, monkeypatch, indexer, capsys):
    """sys.modules[name] = None is the standard way to force `import name` to
    raise ImportError without actually uninstalling anything."""
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)

    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    rdir = tmp_path / ".claude" / "rules"
    rdir.mkdir(parents=True)
    sdir = tmp_path / ".search"
    sdir.mkdir()
    (kdir / "alpha.md").write_text("---\ntype: note\n---\n\n## One\n\nBody text.\n")

    monkeypatch.setattr(indexer, "GESTALT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(indexer, "KNOWLEDGE_DIR", kdir, raising=False)
    monkeypatch.setattr(indexer, "RULES_DIR", rdir, raising=False)
    monkeypatch.setattr(indexer, "SEARCH_DIR", sdir, raising=False)
    monkeypatch.setattr(indexer, "DB_PATH", sdir / "gestalt.db", raising=False)
    monkeypatch.setattr(indexer, "BUILD_PATH", sdir / "gestalt.db.build", raising=False)
    monkeypatch.setattr(indexer, "LOCK_PATH", sdir / "build.lock", raising=False)

    indexer.build_index()

    captured = capsys.readouterr()
    assert "Missing dependency" in captured.out
    assert "sqlite_vec" in captured.out
    assert "pip install" in captured.out

    db = sqlite3.connect(f"file:{sdir / 'gestalt.db'}?mode=ro", uri=True)
    try:
        n = db.execute("SELECT COUNT(*) FROM sections_meta").fetchone()[0]
        assert n > 0, "an FTS-only build must still populate sections_meta/sections_fts"
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "sections_vec" not in tables, "vec0 table must not be created without the extension loaded"
        assert "sections_fts" in tables
    finally:
        db.close()


# --------------------------------------------------------------------------
# Class 2: a threshold above the achievable score ceiling
# --------------------------------------------------------------------------

def _rrf_k() -> int:
    """The RRF constant the server fuses with. It is defined once, in gestalt_rank, and the server binds it."""
    sys.path.insert(0, str(TOOLS))
    import gestalt_rank

    src = (TOOLS / "gestalt-mcp-server.py").read_text()
    assert re.search(r"^\s*K\s*=\s*gestalt_rank\.RRF_K\s*$", src, re.MULTILINE), "the server must take K from gestalt_rank.RRF_K"
    return int(gestalt_rank.RRF_K)


def _n_legs() -> int:
    """Read the leg count from the server, never hardcode it.

    The ceiling is N_LEGS/(K+1). Hardcoding 2 here is precisely how a stale premise
    creeps in: a third leg (heading/block_id) was added and a test asserting the
    two-leg ceiling would have kept passing while describing the wrong system.
    """
    src = (TOOLS / "gestalt-mcp-server.py").read_text()
    m = re.search(r"^\s*N_LEGS\s*=\s*(\d+)\s*$", src, re.MULTILINE)
    assert m, "could not find N_LEGS in gestalt-mcp-server.py"
    return int(m.group(1))


def test_rrf_ceiling_is_derived_from_the_actual_leg_count():
    """Pin the arithmetic the threshold test depends on.

    gestalt_search fuses N_LEGS ranked legs, each contributing 1/(K+rank+1). Best
    case is rank 0 in every leg, so the maximum reachable score is N_LEGS/(K+1).
    """
    k, legs = _rrf_k(), _n_legs()
    ceiling = legs / (k + 1)
    assert k == 60, f"RRF K changed to {k}; re-check every threshold below"
    assert legs == 2, (
        f"leg count changed to {legs} — the ceiling is now {ceiling:.6f}; confirm no "
        f"relevance gate sits above it"
    )
    assert abs(ceiling - 0.032786885) < 1e-8, ceiling


def test_adding_a_leg_cannot_silently_raise_a_gate_above_the_ceiling():
    """A gate valid at 2 legs stays valid at 3 (the ceiling only rises), but a gate
    valid at 3 could become unreachable if a leg were REMOVED. Assert against the
    most pessimistic single-leg ceiling so the gates survive any leg count."""
    single_leg_ceiling = 1.0 / (_rrf_k() + 1)
    offenders = []
    for relpath, pattern in _SCORE_GATES:
        path = REPO / relpath
        if not path.exists():
            continue
        for m in re.finditer(pattern, path.read_text()):
            if float(m.group(1)) >= single_leg_ceiling:
                offenders.append(f"{relpath}: {m.group(1)} >= {single_leg_ceiling:.6f}")
    assert not offenders, (
        "gate(s) unreachable if any retrieval leg returns nothing:\n" + "\n".join(offenders)
    )


# Every place a gestalt_search score is compared against a constant. Listed
# explicitly, because a regex sweep over all source would silently stop covering
# a file that got renamed — and silence is the failure mode under test.
_SCORE_GATES = [
    ("claude-tree/hooks/gestalt-session-start.sh", r"score['\"]?,\s*0\)\s*>\s*([0-9.]+)"),
    ("claude-tree/hooks/post-compact-reinject.sh", r"score['\"]?,\s*0\)\s*>\s*([0-9.]+)"),
    ("claude-tree/hooks/prompt-intelligence.sh", r"GESTALT_RELEVANCE_THRESHOLD:-([0-9.]+)"),
    ("claude-tree/hooks/prompt-intelligence.py", r"--relevance-threshold[\s\S]{0,80}?default=([0-9.]+)"),
    ("tools/auto-promote.py", r"score['\"]?,\s*0\)\s*[>≥]=?\s*([0-9.]+)"),
]


def test_no_relevance_gate_sits_above_the_rrf_ceiling():
    """A gate above the ceiling can never fire — the bug that shipped five times.

    Any threshold compared against an RRF score must be strictly below 2/(K+1),
    or the branch it guards is unreachable and the feature is silently dead.
    """
    ceiling = 2.0 / (_rrf_k() + 1)
    offenders = []
    for relpath, pattern in _SCORE_GATES:
        path = REPO / relpath
        if not path.exists():
            continue
        for m in re.finditer(pattern, path.read_text()):
            value = float(m.group(1))
            line = path.read_text()[: m.start()].count("\n") + 1
            if value >= ceiling:
                offenders.append(
                    f"{relpath}:{line}: gate {value} >= RRF ceiling {ceiling:.6f} "
                    f"— this branch can never fire"
                )
    assert not offenders, "unreachable relevance gate(s):\n" + "\n".join(offenders)


def test_bm25_is_not_used_as_an_absolute_relevance_gate():
    """BM25 is a ranking score too, and thresholding it fails the same way.

    Measured 2026-08-10 on evals/retrieval/golden.yaml: real user queries score
    -7.04 to -19.64 while synthetic off-topic English scores -5.83 to -9.24, and
    dividing by query length overlaps them further. So no constant separates
    relevant from irrelevant, and a bm25 comparison against a literal is the same
    class of bug as an RRF gate above the ceiling.
    """
    offenders = []
    for path in list(HOOKS.glob("*.py")) + list(HOOKS.glob("*.sh")) + list(TOOLS.glob("*.py")):
        for m in re.finditer(r"bm25['\"]?\s*(?:,\s*0\s*)?\)?\s*[<>]=?\s*-?[0-9]+\.?[0-9]*", path.read_text()):
            offenders.append(f"{path.relative_to(REPO)}: {m.group(0)!r}")
    assert not offenders, (
        "bm25 compared against an absolute constant — ranking scores are not "
        "calibrated relevance measures:\n" + "\n".join(offenders)
    )


# --------------------------------------------------------------------------
# The per-prompt hook must stay fast enough to actually complete
# --------------------------------------------------------------------------

def test_prompt_hook_does_not_use_a_thread_pool_for_retrieval():
    """run_in_executor made the hook's own timeout unenforceable.

    Cancelling an asyncio task that wraps `loop.run_in_executor` does not stop the
    worker thread, and `asyncio.run()` joins the default executor before
    returning. So the hook paid the full 14-18 s model load, discarded the result
    at its 0.7 s internal timeout, and was then killed by the 5 s hook timeout.
    Checked against the parsed AST so a mention in a comment or docstring — this
    file's own explanation of the bug included — cannot satisfy the test.
    """
    import ast

    path = HOOKS / "prompt-intelligence.py"
    if not path.exists():
        pytest.skip("prompt-intelligence.py absent")
    tree = ast.parse(path.read_text())
    hits = [
        n.lineno
        for n in ast.walk(tree)
        if (isinstance(n, ast.Attribute) and n.attr == "run_in_executor")
        or (isinstance(n, ast.Name) and n.id == "run_in_executor")
    ]
    assert not hits, f"run_in_executor reintroduced at line(s) {hits}"


def test_prompt_hook_retrieval_is_reference_only():
    """Entries are injected as [[slug#block]] refs, never content.

    That cheapness is what lets the hook skip relevance gating entirely: a false
    positive costs ~20 tokens. If content ever starts being injected, the gating
    question comes back and this test should fail to force that conversation.
    """
    path = HOOKS / "prompt-intelligence.py"
    if not path.exists():
        pytest.skip("prompt-intelligence.py absent")
    src = path.read_text()
    m = re.search(r"def retrieve_gestalt_sync\b[\s\S]*?\n(?=def |\Z)", src)
    assert m, "retrieve_gestalt_sync not found — did the hook's retrieval change?"
    body = m.group(0)
    assert '"text": ""' in body, (
        "retrieve_gestalt_sync no longer injects reference-only entries; if it now "
        "injects content, revisit relevance gating (see ^rrf-thresholds)"
    )
