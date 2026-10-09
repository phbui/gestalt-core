"""gpu-drain: runs the ingest drain under the lease unless a non-ingest holder owns the GPU. Real gpu-lease, stub sync."""

from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "tools" / "fleet" / "bin"


@pytest.fixture
def rig(tmp_path):
    home = tmp_path / "home"
    stubs = home / ".local" / "bin"
    stubs.mkdir(parents=True)
    calls = home / "calls.log"
    sync = stubs / "gestalt-graphiti-sync.sh"
    sync.write_text(f'#!/usr/bin/env bash\necho "sync $*" >> "{calls}"\n[ "$1" = --dry-run ] && [ -n "${{SYNC_PENDING:-}}" ] && echo "would send\tkb-x#1\t100"\nexit "${{SYNC_RC:-0}}"\n')
    sync.chmod(sync.stat().st_mode | stat.S_IEXEC)
    for name in ("gpu-lease", "gpu-drain"):
        (stubs / name).symlink_to(BIN / name)
    lease_dir = home / "lease"
    env = {**os.environ, "HOME": str(home), "GPU_LEASE_DIR": str(lease_dir)}

    def drain(**extra):
        return subprocess.run([str(BIN / "gpu-drain")], env={**env, **extra}, capture_output=True, text=True)

    drain.env = env
    drain.calls = calls
    drain.log = lease_dir / "drain.log"
    return drain


def _hold(rig, label):
    p = subprocess.Popen([str(BIN / "gpu-lease"), "run", "--label", label, "--", "sleep", "30"], env=rig.env)
    end = time.time() + 5
    while time.time() < end:
        if subprocess.run([str(BIN / "gpu-lease"), "held"], env=rig.env, capture_output=True).returncode == 0:
            return p
        time.sleep(0.05)
    p.kill()
    raise AssertionError("holder never took the lock")


def test_does_nothing_when_batch_job_holds_lock(rig):
    p = _hold(rig, "bench-S3")
    try:
        r = rig()
        assert r.returncode == 0
        assert rig.calls.read_text().strip() == "sync --dry-run", "only the no-network pending check runs under a batch holder"
        assert not rig.log.exists()
    finally:
        p.kill()
        p.wait()


def test_runs_sync_under_lease_when_free_and_logs_exit(rig):
    r = rig()
    assert r.returncode == 0
    assert rig.calls.read_text().split("\n")[:2] == ["sync --reconcile", "sync --drain-wait"], "reconcile runs before the drain, both under one lease"
    assert "ingest-drain exit=0" in rig.log.read_text()


def test_logs_nonzero_exit_of_sync(rig):
    r = rig(SYNC_RC="3")
    assert r.returncode == 0
    assert "ingest-drain exit=3" in rig.log.read_text()


def test_ingest_holder_does_not_skip_but_lease_is_busy(rig):
    p = _hold(rig, "ingest-drain")
    try:
        rig()
        assert not rig.calls.exists()
        assert "ingest-drain exit=75" in rig.log.read_text()
    finally:
        p.kill()
        p.wait()


def test_batch_holder_with_pending_entries_records_want(rig):
    """A benchmark holds the lease and entries are pending: the drain records want so the job yields at its next step.
    With nothing pending it records nothing, and in both cases it never takes the lease."""
    p = _hold(rig, "bench-S3")
    try:
        r = rig(SYNC_PENDING="1")
        assert r.returncode == 0
        assert (rig.env["GPU_LEASE_DIR"] and (pathlib.Path(rig.env["GPU_LEASE_DIR"]) / "want").read_text().strip() == "ingest-drain")
        assert "--reconcile" not in rig.calls.read_text()
        (pathlib.Path(rig.env["GPU_LEASE_DIR"]) / "want").unlink()
        r = rig()
        assert not (pathlib.Path(rig.env["GPU_LEASE_DIR"]) / "want").exists()
    finally:
        p.kill()
        p.wait()


def test_drain_passes_the_batch_cap_and_window_cap_to_the_sync(rig, tmp_path):
    """The sync under the lease sees GESTALT_SYNC_MAX_ENTRIES and GESTALT_SYNC_DRAIN_MAX from the drain's knobs."""
    stubs = pathlib.Path(rig.env["HOME"]) / ".local" / "bin"
    sync = stubs / "gestalt-graphiti-sync.sh"
    sync.write_text('#!/usr/bin/env bash\necho "cap=$GESTALT_SYNC_MAX_ENTRIES max=$GESTALT_SYNC_DRAIN_MAX" >> "$HOME/env.log"\nexit 0\n')
    lease_dir = pathlib.Path(rig.env["GPU_LEASE_DIR"])
    lease_dir.mkdir(parents=True, exist_ok=True)
    (lease_dir / "last-yield").write_text("")  # a batch job is around, so the contended batch applies
    r = rig(GPU_DRAIN_BATCH="3")
    assert r.returncode == 0
    assert "cap=3 max=1800" in (pathlib.Path(rig.env["HOME"]) / "env.log").read_text()


def test_a_served_window_clears_want(rig):
    """After the drain runs under the lease it clears want, so a yielding batch job resumes at once."""
    want = pathlib.Path(rig.env["GPU_LEASE_DIR"]) / "want"
    want.parent.mkdir(parents=True, exist_ok=True)
    want.write_text("ingest-drain")
    r = rig()
    assert r.returncode == 0 and not want.exists()


def test_batch_size_follows_whether_a_batch_job_is_around(rig):
    """10 entries per window while a batch job yielded within 2 h, 40 when the GPU has nobody else."""
    stubs = pathlib.Path(rig.env["HOME"]) / ".local" / "bin"
    (stubs / "gestalt-graphiti-sync.sh").write_text('#!/usr/bin/env bash\necho "cap=$GESTALT_SYNC_MAX_ENTRIES" >> "$HOME/env.log"\nexit 0\n')
    lease_dir = pathlib.Path(rig.env["GPU_LEASE_DIR"])
    lease_dir.mkdir(parents=True, exist_ok=True)
    r = rig()
    assert r.returncode == 0
    (lease_dir / "last-yield").write_text("")
    r = rig()
    assert r.returncode == 0
    # the stub is called twice per window, once for --reconcile and once for --drain-wait
    assert (pathlib.Path(rig.env["HOME"]) / "env.log").read_text().split() == ["cap=40", "cap=40", "cap=10", "cap=10"]
