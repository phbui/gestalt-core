"""Static hygiene invariants over .claude/hooks/*.sh and *.py.

Covers the class behind two 2026-08-18 audit findings
(`knowledge/gestalt-efficiency-audit-2026-08.md ^findings`):

  - F13 (^silent-sqlite-vec): a hook's `gestalt_search` call wrapped in a bare
    `except Exception: pass` degrades to an invisible no-op. The fix (visible today
    in post-compact-reinject.sh) logs the failure instead of swallowing it; (b) pins
    that shape so it cannot regress, scoped to the hybrid `gestalt_search(` call the
    finding actually named (not the separate, lexical-only `gestalt_search_fts`
    call in gestalt-session-start.sh, which does not hit the sqlite-vec dependency
    this finding was about, and is out of this writer's edit ownership to fix if it
    is later judged to need the same treatment).
  - F3/F4 (^health-probe-cache): every hook that talks to Letta/Graphiti health
    endpoints must go through the single shared, cached `gestalt_hub_health` in
    lib.sh rather than probing directly -- (c) is the static line of defense, (d)
    pins the tight timeout budget on the probe itself.

(e) hook-event coverage (fleet-publish.sh handles exactly what register-fleet-hooks.py
registers) is already covered by tests/test_fleet.py::test_hook_events_registered_equal_events_handled
-- not duplicated here.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "claude-tree" / "hooks"
SH_FILES = sorted(HOOKS.glob("*.sh"))
PY_FILES = sorted(HOOKS.glob("*.py"))


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# (a) every .sh parses under `bash -n`, every .py compiles.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("script", SH_FILES, ids=lambda p: p.name)
def test_sh_hooks_parse(script: Path) -> None:
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f"{script.name}: {r.stderr}"


@pytest.mark.parametrize("script", PY_FILES, ids=lambda p: p.name)
def test_py_hooks_compile(script: Path) -> None:
    r = subprocess.run(["python3", "-m", "py_compile", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f"{script.name}: {r.stderr}"


# --------------------------------------------------------------------------
# (b) F13 class: no bare except/pass swallow, in .py hook files generally, and
#     specifically around the hybrid gestalt_search( call embedded in .sh heredocs.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("script", PY_FILES, ids=lambda p: p.name)
def test_py_hooks_have_no_bare_except_pass(script: Path) -> None:
    tree = ast.parse(_text(script), filename=str(script))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Pass)
    ]
    assert not offenders, f"{script.name}: bare except/pass (silent swallow, F13-class) at line(s) {offenders}"


def _heredoc_block(src: str, start: int) -> str:
    """From byte offset `start`, return text up to the next line that is a bare
    heredoc terminator (PYEOF, PYEOF2, EOF, ...) or end of file -- the span of the
    embedded python block containing that offset."""
    end_match = re.search(r"^[A-Z][A-Z0-9_]*\s*$", src[start:], re.M)
    return src[start:start + end_match.start()] if end_match else src[start:]


def test_hybrid_gestalt_search_calls_log_on_failure_not_swallow() -> None:
    """F13 (^silent-sqlite-vec): every embedded-python block that calls the hybrid
    `gestalt_search(` (as opposed to the lexical-only `gestalt_search_fts(`) must log
    on failure inside its except handler, not swallow silently. Grep + block-scoped,
    matching the audit's own description of the bug shape."""
    offenders = []
    for script in SH_FILES:
        src = _text(script)
        for m in re.finditer(r"\bgestalt_search\(", src):
            # exclude gestalt_search_fts( — different call, different finding
            if src[max(0, m.start() - 4):m.start()].endswith("_fts"):
                continue
            block = _heredoc_block(src, m.start())
            has_log = bool(re.search(r"\.write\(|logging\.|LOG\b", block))
            if not has_log:
                offenders.append(f"{script.name}: gestalt_search( call with no logging in its handler")
    assert not offenders, "\n".join(offenders)


# --------------------------------------------------------------------------
# (c) no raw health probes outside lib.sh's gestalt_hub_health; every hook that
#     curls LETTA_URL/GRAPHITI_URL sources lib.sh and calls gestalt_hub_health.
# --------------------------------------------------------------------------

def test_no_raw_health_probes_outside_lib_sh() -> None:
    offenders = []
    for script in SH_FILES:
        if script.name == "lib.sh":
            continue
        src = _text(script)
        for m in re.finditer(r"curl[^\n]*\$\{?(LETTA_URL|GRAPHITI_URL)\}?[^\n]*", src):
            if "/health" in m.group(0):
                offenders.append(f"{script.name}: {m.group(0).strip()[:100]}")
    assert not offenders, f"raw /health curl outside lib.sh (F3/F4-class): {offenders}"


def test_hooks_calling_letta_or_graphiti_curl_source_lib_and_call_hub_health() -> None:
    offenders = []
    for script in SH_FILES:
        if script.name == "lib.sh":
            continue
        src = _text(script)
        curls_hub_service = re.search(r"curl[^\n]*\$\{?(LETTA_URL|GRAPHITI_URL)\}?", src)
        if not curls_hub_service:
            continue
        sources_lib = re.search(r"source\s+\"?\$SCRIPT_DIR/lib\.sh\"?", src)
        calls_hub_health = "gestalt_hub_health" in src
        if not (sources_lib and calls_hub_health):
            offenders.append(script.name)
    assert not offenders, f"hooks curling Letta/Graphiti without going through gestalt_hub_health: {offenders}"


def test_four_consuming_hooks_call_gestalt_hub_health() -> None:
    """Static pin on the F3/F4 fix set -- the exact four hooks the audit named."""
    for name in ("gestalt-session-start.sh", "gestalt-stop.sh", "statusline.sh", "post-compact-reinject.sh"):
        src = _text(HOOKS / name)
        assert "gestalt_hub_health" in src, f"{name}: no call to gestalt_hub_health (F3/F4 regression)"


# --------------------------------------------------------------------------
# (d) health-probe timeout budget: the /health curls inside gestalt_hub_health
#     itself (the only place (c) permits them) must stay tight -- this is the
#     specific budget F3/F4 fixed the callers' cost by consolidating into.
# --------------------------------------------------------------------------

def test_health_probe_curls_have_max_time_le_3() -> None:
    src = _text(HOOKS / "lib.sh")
    health_curls = [
        line for line in src.splitlines()
        if "curl" in line and "/health" in line
    ]
    assert health_curls, "lib.sh has no /health curl to check -- gestalt_hub_health may have been refactored away"
    for line in health_curls:
        m = re.search(r"--max-time\s+(\d+(?:\.\d+)?)", line)
        assert m, f"lib.sh health curl has no --max-time: {line.strip()}"
        assert float(m.group(1)) <= 3, f"lib.sh health curl --max-time {m.group(1)} exceeds F3/F4 budget of 3s: {line.strip()}"
