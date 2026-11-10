"""A test that ships in gestalt-core must not depend on a tool the export leaves out.

Twice the Publish core gate failed on a new test that drove an excluded file: tests/test_egress_inventory.py loaded
tools/publish-public.py (2026-11-09) and tests/test_pre_push_live_index_retry.py ran tools/ci/pre-push.sh (2026-11-10).
Both passed every local gate, because the file exists in the full repo. This test names the excluded tools and fails when
a shipped test file mentions one by basename without the module-level skip the other export-aware tests use."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if not (REPO / "tools" / "publish-public.py").exists():
    pytest.skip("the exporter is not part of the public tree", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("publish_public_shipcheck", REPO / "tools" / "publish-public.py")
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

# Shipped test files that name an excluded tool and are known to be safe, with the reason.
KNOWN_SAFE: dict[str, str] = {
    "tests/test_read_feedback.py": "the one test that runs gestalt-hit-aggregate.sh skips inside the function when the script is absent",
    "tests/test_tools_discrimination_scripts.py": "it names test-corpus-fire-skip.py only in a set of scripts to leave out",
}


def _tracked() -> list[str]:
    return subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split("\n")


def _strings(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    doc = {id(n.body[0].value) for n in ast.walk(tree)
           if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
           and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc}


def _guards_itself(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    return "allow_module_level=True" in text or "pytest.importorskip" in text


def violations(tracked: list[str]) -> list[str]:
    shipped = {f for f in tracked if f and pp.wanted(f)}
    excluded_tools = {Path(f).name for f in tracked if f.startswith("tools/") and f not in shipped and Path(f).suffix in (".py", ".sh")}
    shipped_names = {Path(f).name for f in shipped}
    excluded_tools -= shipped_names  # a basename that also ships somewhere is not evidence
    out = []
    for f in sorted(shipped):
        if not (f.startswith("tests/") and f.endswith(".py")) or f in KNOWN_SAFE:
            continue
        p = REPO / f
        if not p.exists() or _guards_itself(p):
            continue
        hit = sorted(s for s in _strings(p) if s in excluded_tools or Path(s).name in excluded_tools and "/" in s)
        if hit:
            out.append(f"{f}: names excluded tool(s) {hit}")
    return out


def test_no_shipped_test_names_an_excluded_tool():
    bad = violations(_tracked())
    assert not bad, ("shipped tests depend on tools the export excludes. Add the test to the exporter's exclusion list in "
                     "tools/publish-public.py, or give it a module-level skip when the tool is absent:\n  " + "\n  ".join(bad))
