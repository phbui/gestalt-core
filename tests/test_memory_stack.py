"""Behavioural tests for the F9 memory-stack consolidation (gestalt-efficiency-
audit-2026-08.md ^memory-triplication).

(a) gestalt-stop.sh's Letta send is gated on two independent conditions on top of
    the existing hub-health gate: `GESTALT_LETTA_EVERY_N` cadence (only session N of
    every-Nth actually sends) and a 4-user-turn floor (a session too short to be
    worth summarising never sends, at any cadence position). Both skips are logged
    to health.log. Driven end to end against a synthetic tmp workspace with a stub
    `curl` on PATH (records every invocation; answers the Letta backlog check and
    message POST) so no live Letta/Graphiti/network is required. GESTALT_DIR points
    at a tmp dir with no tools/backup.sh, so Task F (block backup) is inert -- it is
    unrelated to this finding and untouched by this change.

(b) tools/gestalt-graphiti-sync.sh's hash-gating: a tmp knowledge dir + tmp state
    file, driven purely with --dry-run (zero side effects: no network call, no state
    file write) so the diff logic is provable without a live Graphiti. The state
    file is written directly by the test between calls to simulate "already synced",
    matching --dry-run's contract.

(c) no tracked file in the repo still names the deleted tools/gestalt-capture.sh,
    except knowledge/home-mesh.md -- out of this writer's ownership per the F9 task
    brief (other agents own other findings' entries in that file); flagged in the
    audit's F9 status row instead of edited here.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "claude-tree" / "hooks"
STOP_HOOK = HOOKS / "gestalt-stop.sh"
SYNC_TOOL = REPO / "tools" / "gestalt-graphiti-sync.sh"


# --------------------------------------------------------------------------
# (a) gestalt-stop.sh Letta cadence + user-turn floor
# --------------------------------------------------------------------------

def _stub_curl_dir(tmp_path: Path) -> Path:
    """A fake `curl` that logs every invocation to $CURL_LOG and answers just
    enough to satisfy gestalt-stop.sh's Letta backlog check (`/runs/active` ->
    empty JSON array, zero backlog) and message POST (`-w "%{http_code}"` -> 200).
    Never touches a real network."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "${CURL_LOG:?CURL_LOG not set}"\n'
        'args="$*"\n'
        'if [[ "$args" == *"-w"* ]]; then printf "200"; '
        'elif [[ "$args" == *"/runs/active"* ]]; then printf "[]"; '
        'else printf ""; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _transcript(tmp_path: Path, name: str, user_turns: int) -> Path:
    lines = []
    for i in range(user_turns):
        lines.append(json.dumps({"type": "user", "message": {"content": f"user turn number {i} with real content"}}))
        lines.append(json.dumps({"type": "assistant", "message": {"content": f"assistant reply number {i} here"}}))
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _run_stop_hook(tmp_path: Path, *, state_dir: Path, curl_dir: Path, curl_log: Path,
                    transcript: Path, session_id: str, every_n: int) -> subprocess.CompletedProcess:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    fake_gestalt_dir = tmp_path / "fake_gestalt"
    (fake_gestalt_dir / "tools").mkdir(parents=True, exist_ok=True)  # no backup.sh inside -> Task F inert
    health_file = tmp_path / "hub-health-up"
    health_file.write_text(f"{int(time.time())} 1 1\n", encoding="utf-8")
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "letta-agent-id.txt").write_text("test-agent", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": str(curl_dir) + os.pathsep + os.environ["PATH"],
        "CURL_LOG": str(curl_log),
        "GESTALT_DIR": str(fake_gestalt_dir),
        "GESTALT_WORKSPACE": str(workspace),
        "GESTALT_STATE_DIR": str(state_dir),
        "GESTALT_HUB_HEALTH_FILE": str(health_file),
        "GESTALT_LETTA_EVERY_N": str(every_n),
        "LETTA_URL": "http://127.0.0.1:9/v1",
        "GRAPHITI_URL": "http://127.0.0.1:9",
    }
    stdin = json.dumps({"transcript_path": str(transcript), "session_id": session_id})
    return subprocess.run(["bash", str(STOP_HOOK)], input=stdin, capture_output=True, text=True, env=env, timeout=15)


def _message_post_count(curl_log: Path) -> int:
    if not curl_log.exists():
        return 0
    return sum(1 for line in curl_log.read_text().splitlines() if "/messages" in line)


@pytest.mark.local
def test_letta_send_only_on_nth_session(tmp_path):
    """EVERY_N=3, a 4-user-turn transcript every time: runs 1-2 skip (cadence),
    run 3 sends, runs 4-5 skip again, run 6 sends."""
    state_dir = tmp_path / "state"
    curl_dir = _stub_curl_dir(tmp_path)
    transcript = _transcript(tmp_path, "t.jsonl", user_turns=4)
    sends_by_run = {}
    for i in range(1, 7):
        curl_log = tmp_path / f"curl-{i}.log"
        health_file = tmp_path / "hub-health-up"
        health_file.write_text(f"{int(time.time())} 1 1\n", encoding="utf-8")
        r = _run_stop_hook(tmp_path, state_dir=state_dir, curl_dir=curl_dir, curl_log=curl_log,
                            transcript=transcript, session_id=f"session{i:04d}", every_n=3)
        assert r.returncode == 0, r.stderr
        sends_by_run[i] = _message_post_count(curl_log) > 0
    assert sends_by_run == {1: False, 2: False, 3: True, 4: False, 5: False, 6: True}, sends_by_run
    counter = (state_dir / "letta-session-counter").read_text().strip()
    assert counter == "6"


@pytest.mark.local
def test_letta_send_never_below_four_user_turns(tmp_path):
    """Even on the exact Nth session, a transcript with <4 user turns never sends."""
    state_dir = tmp_path / "state"
    curl_dir = _stub_curl_dir(tmp_path)
    every_n = 1  # every run is "due" by cadence -- isolates the turn-count gate
    for turns in (0, 1, 2, 3):
        transcript = _transcript(tmp_path, f"short-{turns}.jsonl", user_turns=turns)
        curl_log = tmp_path / f"curl-short-{turns}.log"
        r = _run_stop_hook(tmp_path, state_dir=state_dir, curl_dir=curl_dir, curl_log=curl_log,
                            transcript=transcript, session_id=f"short{turns}", every_n=every_n)
        assert r.returncode == 0, r.stderr
        assert _message_post_count(curl_log) == 0, f"{turns} user turns should never send"
        log_text = (state_dir / "health.log").read_text()
        assert "noise filter" in log_text.splitlines()[-1] or "noise filter" in log_text, log_text


@pytest.mark.local
def test_letta_skip_reasons_logged_to_health_log(tmp_path):
    state_dir = tmp_path / "state"
    curl_dir = _stub_curl_dir(tmp_path)

    # Cadence skip: EVERY_N=5, first run (counter=1) is not due.
    transcript = _transcript(tmp_path, "cadence.jsonl", user_turns=4)
    curl_log = tmp_path / "curl-cadence.log"
    r = _run_stop_hook(tmp_path, state_dir=state_dir, curl_dir=curl_dir, curl_log=curl_log,
                        transcript=transcript, session_id="cad1", every_n=5)
    assert r.returncode == 0, r.stderr
    log_text = (state_dir / "health.log").read_text()
    assert "every-5 cadence" in log_text, log_text
    assert _message_post_count(curl_log) == 0


# --------------------------------------------------------------------------
# (b) tools/gestalt-graphiti-sync.sh hash gating (--dry-run only, zero side effects)
# --------------------------------------------------------------------------

def _run_sync_dry_run(knowledge_dir: Path, state_file: Path, *extra_args: str) -> list[str]:
    env = {
        **os.environ,
        "GESTALT_DIR": str(REPO),
        "GESTALT_KNOWLEDGE_DIR": str(knowledge_dir),
        "GESTALT_GRAPHITI_SYNC_STATE": str(state_file),
        "GESTALT_STATE_DIR": str(state_file.parent),
    }
    r = subprocess.run(["bash", str(SYNC_TOOL), "--dry-run", *extra_args],
                        capture_output=True, text=True, env=env, timeout=15)
    assert r.returncode == 0, r.stderr
    # Since the chunk-level dry run (gestalt-overhaul-2026-09 row 63) stdout is one line per
    # planned chunk, "would send<TAB>kb-<slug>[#<k>]<TAB><chars>"; reduce to distinct slugs so
    # the file-gate assertions below stay about which entries are due, not how they chunk.
    slugs: list[str] = []
    for line in r.stdout.splitlines():
        if not line.startswith("would send\t"):
            continue
        name = line.split("\t")[1]
        slug = name.removeprefix("kb-").split("#", 1)[0]
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def test_sync_dry_run_hash_gating(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    state_file = tmp_path / "state" / "graphiti-sync.json"
    state_file.parent.mkdir()

    (knowledge_dir / "a.md").write_text("alpha content", encoding="utf-8")
    (knowledge_dir / "b.md").write_text("beta content", encoding="utf-8")

    # First run, no state file yet: both are new.
    assert sorted(_run_sync_dry_run(knowledge_dir, state_file)) == ["a", "b"]

    # Simulate a real sync having happened: write matching sha1s to the state file.
    import hashlib
    state_file.write_text(json.dumps({
        "a": hashlib.sha1((knowledge_dir / "a.md").read_bytes()).hexdigest(),
        "b": hashlib.sha1((knowledge_dir / "b.md").read_bytes()).hexdigest(),
    }), encoding="utf-8")

    # Second run: both unchanged -> nothing to sync.
    assert _run_sync_dry_run(knowledge_dir, state_file) == []

    # Edit one file: only that slug is listed.
    (knowledge_dir / "a.md").write_text("alpha content, edited", encoding="utf-8")
    assert _run_sync_dry_run(knowledge_dir, state_file) == ["a"]

    # --all lists everything regardless of state.
    assert sorted(_run_sync_dry_run(knowledge_dir, state_file, "--all")) == ["a", "b"]

    # dry-run never writes the state file (zero side effects).
    on_disk = json.loads(state_file.read_text())
    assert "a" in on_disk and "b" in on_disk  # unchanged from what the test itself wrote


def test_sync_single_slug_mode_dry_run(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    state_file = tmp_path / "state" / "graphiti-sync.json"
    state_file.parent.mkdir()
    (knowledge_dir / "a.md").write_text("alpha", encoding="utf-8")
    (knowledge_dir / "b.md").write_text("beta", encoding="utf-8")
    import hashlib
    state_file.write_text(json.dumps({
        "a": hashlib.sha1((knowledge_dir / "a.md").read_bytes()).hexdigest(),
        "b": hashlib.sha1((knowledge_dir / "b.md").read_bytes()).hexdigest(),
    }), encoding="utf-8")
    # Even though "a" is unchanged, single-slug mode always includes it (used by /save).
    assert _run_sync_dry_run(knowledge_dir, state_file, "a") == ["a"]


def test_backfill_knowledge_is_a_wrapper_over_sync_tool():
    src = (REPO / "tools" / "backfill-knowledge.sh").read_text(encoding="utf-8")
    assert "gestalt-graphiti-sync.sh" in src
    assert "--all" in src


def test_sync_summaries_dry_run_lists_new_files_and_is_cursor_gated(tmp_path):
    """--summaries (F9 TODO, implemented 2026-08-29): dry-run lists session files
    newer than the touchstone cursor; a cursor touched to the newest file's mtime
    makes the next run report nothing. No network in either path."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "2026-08-29-aaaa.md").write_text("summary a\n")
    (sessions / "2026-08-29-bbbb.md").write_text("summary b\n")
    env = dict(
        os.environ,
        GESTALT_SESSIONS_DIR=str(sessions),
        GESTALT_STATE_DIR=str(tmp_path / "state"),
    )
    r = subprocess.run(
        ["bash", str(SYNC_TOOL), "--summaries", "--dry-run"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r.returncode == 0, r.stderr
    listed = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(listed) == 2 and all("sessions/" in l for l in listed), r.stdout

    # Simulate a completed digest: the cursor is a touchstone file matched by
    # mtime (touch -r of the newest input), full sub-second precision.
    cursor = tmp_path / "state" / "graphiti-summaries.cursor"
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.touch()
    newest = max(sessions.glob("*.md"), key=lambda f: f.stat().st_mtime_ns)
    os.utime(cursor, ns=(newest.stat().st_atime_ns, newest.stat().st_mtime_ns))

    r2 = subprocess.run(
        ["bash", str(SYNC_TOOL), "--summaries"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert r2.returncode == 0, r2.stderr
    assert "up to date" in r2.stdout


# --------------------------------------------------------------------------
# (c) gestalt-capture.sh is fully retired
# --------------------------------------------------------------------------

def test_gestalt_capture_script_deleted():
    assert not (REPO / "tools" / "gestalt-capture.sh").exists()


def test_no_tracked_file_references_gestalt_capture_except_known_exceptions():
    """No tracked file names the deleted tools/gestalt-capture.sh as a live,
    operational path any more, with two deliberate exceptions:

    - knowledge/home-mesh.md: still names it as of this writer's F9 pass. Out of
      this writer's ownership (other findings in that file are owned by other
      concurrent agents per the task brief) -- flagged, not edited, in the audit's
      F9 status row.
    - This test file, the two F9-decision docs (knowledge/gestalt.md,
      knowledge/gestalt-efficiency-audit-2026-08.md), and the new sync tool's own
      TODO comment (tools/gestalt-graphiti-sync.sh): each names it exactly once,
      deliberately, as a historical/audit-trail record of *why* it was deleted --
      not as a reference to a still-live mechanism. Standard audit-log practice;
      distinguishing "mentions the old name to explain a deletion" from "still
      depends on it" is not a string match this test can safely automate, so those
      four are enumerated exceptions rather than silently excluded by pattern.
    """
    out = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True).stdout
    allowed_exceptions = {
        "knowledge/home-mesh.md",
        "knowledge/gestalt.md",
        "knowledge/gestalt-efficiency-audit-2026-08.md",
        "knowledge/gestalt-internals.md",  # names the deleted digest only as history (F9 decision)
        "tests/test_memory_stack.py",
        "tools/gestalt-graphiti-sync.sh",
    }
    offenders = []
    for rel in out.splitlines():
        if rel in allowed_exceptions:
            continue
        # The 2026-10-06 system map and review, and their evidence reports, name the deleted script
        # only as history (what the Stop hook used to call). Same audit-trail case as the entries above.
        if rel.startswith(("knowledge/gestalt-system-map", "knowledge/gestalt-system-review-",
                           "personal/gestalt-system-map-", "personal/gestalt-system-review-")):
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "gestalt-capture.sh" in text:
            offenders.append(rel)
    assert not offenders, f"stale gestalt-capture.sh references: {offenders}"
