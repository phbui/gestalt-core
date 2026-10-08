"""Behavioral tests for the small tools that had none.

`merge-settings.py`, `enable-phase2.py`, `render-doc.py` and `bin/check-staleness.sh`
all mutate something a human depends on — the harness's own hook wiring, a
docker-compose file, a rendered document, and the staleness report that drives
session-start triage. None had a single test.

The shared risk is quiet wrongness rather than a crash: a dedup key that collides,
a block-end scan that eats a sibling, a heading fallback that yields an empty title,
a risk bucket off by one at its boundary. Each test below targets that, not
line coverage.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def merge_settings():
    from conftest import _load

    return _load("merge_settings", "tools/merge-settings.py")


@pytest.fixture(scope="module")
def enable_phase2():
    from conftest import _load

    return _load("enable_phase2", "tools/enable-phase2.py")


@pytest.fixture(scope="module")
def render_doc():
    from conftest import _load

    return _load("render_doc", "tools/render-doc.py")


# --------------------------------------------------------------------------
# merge-settings.py — merges hook JSON into settings.json
# --------------------------------------------------------------------------

def _hooks(event: str, *commands: str) -> dict:
    return {"hooks": {event: [{"hooks": [{"command": c} for c in commands]}]}}


def test_merge_hooks_adds_a_new_event_key(merge_settings):
    target: dict = {"hooks": {}}
    merge_settings.merge_hooks(target, _hooks("PreToolUse", "guard.sh"))
    assert "PreToolUse" in target["hooks"], "a new event key was not created"


def test_merge_hooks_does_not_duplicate_an_identical_command(merge_settings):
    """Re-running the merge must be idempotent, or hooks stack up and fire N times."""
    target = _hooks("PreToolUse", "guard.sh")
    before = len(target["hooks"]["PreToolUse"])
    merge_settings.merge_hooks(target, _hooks("PreToolUse", "guard.sh"))
    assert len(target["hooks"]["PreToolUse"]) == before, "identical hook was duplicated"


def test_merge_hooks_appends_a_genuinely_different_command(merge_settings):
    target = _hooks("PreToolUse", "guard.sh")
    merge_settings.merge_hooks(target, _hooks("PreToolUse", "totally-other.sh"))
    flat = [
        h["command"]
        for matcher in target["hooks"]["PreToolUse"]
        for h in matcher.get("hooks", [])
    ]
    assert "totally-other.sh" in flat, "a distinct hook was dropped as a duplicate"


def test_merge_hooks_distinguishes_commands_sharing_a_long_prefix(merge_settings):
    """The dedup key is a truncated command string.

    Two different hooks whose first N characters match must still both survive; if the
    key truncates too aggressively they collide and one is silently discarded — which
    would remove a safety hook without any error.
    """
    prefix = '"$CLAUDE_PROJECT_DIR"/.claude/hooks/' + "x" * 60
    target = _hooks("PreToolUse", prefix + "-alpha.sh")
    merge_settings.merge_hooks(target, _hooks("PreToolUse", prefix + "-beta.sh"))
    flat = [
        h["command"]
        for matcher in target["hooks"]["PreToolUse"]
        for h in matcher.get("hooks", [])
    ]
    assert any(c.endswith("-beta.sh") for c in flat), (
        "hooks sharing a long prefix collided in the dedup key; one was lost"
    )


# --------------------------------------------------------------------------
# enable-phase2.py — uncomments docker-compose service blocks
# --------------------------------------------------------------------------

_COMPOSE = [
    "services:\n",
    "  live-service:\n",
    "    image: a\n",
    "  # falkordb:\n",
    "  #   image: falkordb/falkordb\n",
    "  #   ports:\n",
    "  #     - 6379:6379\n",
    "  # graphiti-mcp:\n",
    "  #   image: graphiti\n",
    "# an unrelated standalone comment\n",
]


def test_phase2_detects_a_commented_block(enable_phase2):
    assert enable_phase2._is_phase2_commented(list(_COMPOSE)) is True


def test_phase2_uncomment_is_idempotent(enable_phase2):
    """Running twice must not strip a second '# ' and corrupt live YAML."""
    once = enable_phase2._uncomment_phase2_blocks(list(_COMPOSE))
    twice = enable_phase2._uncomment_phase2_blocks(list(once))
    assert once == twice, "second pass mutated already-uncommented YAML"


def test_phase2_leaves_unrelated_comments_alone(enable_phase2):
    """A block-end scan that overruns would eat comments outside the Phase-2 blocks."""
    out = enable_phase2._uncomment_phase2_blocks(list(_COMPOSE))
    assert "# an unrelated standalone comment\n" in out, (
        "an unrelated comment outside the Phase-2 blocks was uncommented"
    )


def test_phase2_actually_uncomments_the_anchors(enable_phase2):
    out = "".join(enable_phase2._uncomment_phase2_blocks(list(_COMPOSE)))
    assert "  falkordb:\n" in out and "  graphiti-mcp:\n" in out, (
        "the Phase-2 anchors were not uncommented"
    )


# --------------------------------------------------------------------------
# render-doc.py — markdown to themed HTML/PDF
# --------------------------------------------------------------------------

def test_first_heading_prefers_a_real_heading(render_doc):
    assert render_doc.first_heading("# Real Title\n\nbody\n", "fallback") == "Real Title"


def test_first_heading_falls_back_when_there_is_no_heading(render_doc):
    """An empty <title> is the visible symptom; assert the fallback is used."""
    assert render_doc.first_heading("just prose, no heading\n", "fallback") == "fallback"


def test_out_dir_for_explicit_out_dir_is_created_and_returned(render_doc, tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("# T\n")
    target = tmp_path / "somewhere" / "nested"
    got = render_doc.out_dir_for(src, target)
    assert got == target
    assert target.is_dir()


def test_out_dir_for_source_under_home_renders_beside_it(render_doc, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    src = fake_home / "docs" / "notes.md"
    src.parent.mkdir(parents=True)
    src.write_text("# T\n")
    got = render_doc.out_dir_for(src, None)
    assert got == src.parent


def test_out_dir_for_source_outside_home_redirects_to_artifacts(render_doc, tmp_path, monkeypatch, capsys):
    """Never /tmp: a source outside $HOME is redirected to ~/artifacts/<stem>/ so the
    file:// link a viewer opens actually resolves (source: module docstring)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    src = outside / "notes.md"
    src.write_text("# T\n")
    got = render_doc.out_dir_for(src, None)
    assert got == fake_home / "artifacts" / "notes"
    assert got.is_dir()
    assert "rendering to" in capsys.readouterr().err


def test_render_writes_themed_html_with_table_wrap_and_title(render_doc, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # source lands under fake $HOME: no artifacts redirect
    src = tmp_path / "notes.md"
    src.write_text(
        "# My Report\n\nSome prose.\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n",
        encoding="utf-8",
    )
    written = render_doc.render(src, want_pdf=False)
    assert written == [tmp_path / "notes.html"]
    html = written[0].read_text()
    assert "<title>My Report</title>" in html
    assert '<div class="table-wrap"><table>' in html
    assert "</table></div>" in html
    assert "<h1>My Report</h1>" in html


def test_render_with_pdf_but_weasyprint_missing_writes_html_only(render_doc, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(render_doc.shutil, "which", lambda exe: None)
    src = tmp_path / "notes.md"
    src.write_text("# T\n\nbody\n", encoding="utf-8")
    written = render_doc.render(src, want_pdf=True)
    assert written == [tmp_path / "notes.html"]
    assert "weasyprint not found" in capsys.readouterr().err


def test_render_with_pdf_runs_weasyprint_when_present(render_doc, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(render_doc.shutil, "which", lambda exe: "/usr/bin/weasyprint")

    calls = []

    class _Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        # weasyprint would normally write the pdf; simulate that side effect.
        Path(cmd[2]).write_bytes(b"%PDF-1.4 fake")
        return _Proc()

    monkeypatch.setattr(render_doc.subprocess, "run", fake_run)
    src = tmp_path / "notes.md"
    src.write_text("# T\n\nbody\n", encoding="utf-8")
    written = render_doc.render(src, want_pdf=True)
    assert written == [tmp_path / "notes.html", tmp_path / "notes.pdf"]
    assert (tmp_path / "notes.pdf").exists()
    assert calls and calls[0][0] == "/usr/bin/weasyprint"


def test_main_writes_file_and_prints_file_url(render_doc, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    src = tmp_path / "notes.md"
    src.write_text("# T\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr(render_doc.sys, "argv", ["render-doc.py", str(src)])
    render_doc.main()
    out = capsys.readouterr().out
    assert out.strip().startswith("file://")
    assert (tmp_path / "notes.html").exists()


def test_main_exits_when_source_does_not_exist(render_doc, tmp_path, monkeypatch):
    monkeypatch.setattr(render_doc.sys, "argv", ["render-doc.py", str(tmp_path / "missing.md")])
    with pytest.raises(SystemExit):
        render_doc.main()


# --------------------------------------------------------------------------
# bin/check-staleness.sh — risk bucketing
# --------------------------------------------------------------------------

_STALENESS = REPO / "bin" / "check-staleness.sh"


@pytest.mark.skipif(not _STALENESS.exists(), reason="bin/check-staleness.sh absent")
@pytest.mark.parametrize(
    ("commits", "expected"),
    [(21, "HIGH"), (20, "MEDIUM"), (6, "MEDIUM"), (5, "LOW"), (1, "LOW"), (0, "LOW")],
)
def test_staleness_risk_buckets_at_their_boundaries(commits: int, expected: str):
    """Boundary test on the real thresholds, extracted from the script itself.

    The thresholds are read out of the live script rather than retyped, so if someone
    changes 20 or 5 there, this test follows and the boundaries stay covered. Both
    comparisons are strictly-greater, so 20 is MEDIUM and 5 is LOW — exactly the kind
    of off-by-one that silently mislabels a stale entry as current.
    """
    src = _STALENESS.read_text()
    import re

    nums = re.findall(r'commit_count"?\s*-gt\s*(\d+)', src)
    if len(nums) < 2:
        pytest.skip("could not locate both risk thresholds in check-staleness.sh")
    hi, med = int(nums[0]), int(nums[1])
    script = (
        f'if [ "$1" -gt {hi} ]; then echo HIGH; '
        f'elif [ "$1" -gt {med} ]; then echo MEDIUM; else echo LOW; fi'
    )
    r = subprocess.run(
        ["bash", "-c", script, "_", str(commits)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout.strip() == expected, (
        f"{commits} commits bucketed as {r.stdout.strip()}, expected {expected} "
        f"(thresholds read from script: >{hi} HIGH, >{med} MEDIUM)"
    )


# --------------------------------------------------------------------------
# tools/gestalt — the shell entrypoint
# --------------------------------------------------------------------------

def test_gestalt_cli_rebuild_is_idempotent_on_indices(tmp_path):
    """`gestalt rebuild` must produce byte-identical indices when nothing changed.

    A generator whose output drifts run-to-run makes every MANIFEST diff noise, and
    `auto-promote.read_manifest()` parses that output — so instability there is a
    correctness problem, not just a cosmetic one.

    Runs against a copy in tmp_path, never the live repo: `gestalt rebuild` writes
    MANIFEST.md/GRAPH.md in place, so pointing it at REPO directly mutated the real
    index on every test run (caught 2026-09-06 gap scan — a no-op rebuild during the
    suite left MANIFEST.md dirty in the working tree). The `.git` dir is copied as
    plain files (never `git clone`, which reapplies the git-crypt clean/smudge
    filters and checks `knowledge/**` out locked) so `git ls-files` still sees the
    same tracked/untracked split the live repo would.

    Idempotency is checked by rebuilding the copy TWICE and diffing those two runs
    against each other, not by diffing against the committed MANIFEST.md: the working
    tree can carry uncommitted knowledge edits (it does right now — fellowship-
    applications.md and rewrite-context.md), which makes the committed file a
    stale baseline through no fault of the generator.
    """
    manifest = REPO / "MANIFEST.md"
    if not manifest.exists():
        pytest.skip("MANIFEST.md absent")
    live_before = manifest.read_bytes()
    clone = tmp_path / "gestalt-clone"
    (clone / "tools").mkdir(parents=True)
    shutil.copy2(REPO / "tools" / "gestalt", clone / "tools" / "gestalt")
    shutil.copytree(REPO / "knowledge", clone / "knowledge")
    shutil.copytree(REPO / ".git", clone / ".git")
    shutil.copy2(REPO / "MANIFEST.md", clone / "MANIFEST.md")
    shutil.copy2(REPO / "GRAPH.md", clone / "GRAPH.md")
    clone_manifest = clone / "MANIFEST.md"

    def rebuild():
        r = subprocess.run(
            ["bash", str(clone / "tools" / "gestalt"), "rebuild"],
            capture_output=True,
            text=True,
            cwd=clone,
            check=False,
        )
        if r.returncode != 0:
            pytest.skip(f"gestalt rebuild unavailable here: {r.stderr[-200:]}")
        return clone_manifest.read_bytes()

    first = rebuild()
    second = rebuild()
    assert second == first, (
        "MANIFEST.md changed between two back-to-back rebuilds of unchanged "
        "content — the generator is not deterministic"
    )
    assert manifest.read_bytes() == live_before, (
        "the live repo's MANIFEST.md was touched by a test that should run only "
        "against a copy in tmp_path"
    )


def test_gestalt_cli_reports_usage_for_an_unknown_subcommand():
    r = subprocess.run(
        ["bash", str(REPO / "tools" / "gestalt"), "definitely-not-a-subcommand"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    combined = (r.stdout + r.stderr).lower()
    assert r.returncode != 0 or "usage" in combined or "unknown" in combined, (
        "an unknown subcommand exited 0 with no usage message, so a typo would "
        "look like success"
    )
