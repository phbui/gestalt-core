"""Behaviour tests for tools/fleet/bin/bench-resume (2026-10-27).

Runs the script for real under bash with a throwaway HOME. After a watchdog restart the marker
~/.fleet/gpu-unclamp/wsl-restart.pending exists; the script must write one maintenance-log line that
carries the marker text and the Windows restart log, move the marker, and run every registered job.
Without the marker it still runs the jobs and says so.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools/fleet/bin/bench-resume"


def _home(tmp_path):
    home = tmp_path / "home"
    (home / ".fleet/gpu-unclamp").mkdir(parents=True)
    jobs = home / ".fleet/bench-resume/jobs.d"
    jobs.mkdir(parents=True)
    job = jobs / "lme.sh"
    job.write_text("#!/usr/bin/env bash\necho relaunched > \"$HOME/ran\"\necho launched pid 1\n")
    job.chmod(job.stat().st_mode | stat.S_IEXEC)
    (jobs / "notes.txt").write_text("not a job\n")
    return home


def _run(home, winlog):
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "BENCH_RESUME_WINLOG": str(winlog), "BENCH_RESUME_DIRECT": "1"}
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True, timeout=30, capture_output=True)
    return (home / ".fleet/bench-resume/log").read_text()


def test_watchdog_restart_is_logged_and_jobs_relaunch(tmp_path):
    home = _home(tmp_path)
    (home / ".fleet/gpu-unclamp/wsl-restart.pending").write_text("2026-10-27T12:08:53-04:00 clamp held 238s (Current 10.00W < Default 80.00W)\n")
    winlog = tmp_path / "restart.log"
    winlog.write_text("start\npnputil rc=0: Device restarted successfully.\ncard healthy: enforced 80 W\ndone\n")
    log = _run(home, winlog)
    maint = (home / ".fleet/maintenance.log").read_text()
    assert "WSL restarted by gpu-unclamp-watchdog" in maint and "clamp held 238s" in maint and "card healthy" in maint
    assert not (home / ".fleet/gpu-unclamp/wsl-restart.pending").exists()
    assert (home / ".fleet/gpu-unclamp/wsl-restart.last").exists()
    assert (home / "ran").read_text().strip() == "relaunched"
    assert "watchdog restart" in log and "lme (direct) rc=0" in log


def test_plain_start_runs_jobs_without_a_maintenance_line(tmp_path):
    home = _home(tmp_path)
    log = _run(home, tmp_path / "missing.log")
    assert not (home / ".fleet/maintenance.log").exists()
    assert (home / "ran").exists()
    assert "without a watchdog marker" in log
