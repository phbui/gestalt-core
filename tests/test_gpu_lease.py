"""gpu-lease: the hub GPU's flock lease. Runs the real script against a tmp state dir through GPU_LEASE_DIR."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "fleet" / "bin" / "gpu-lease"


@pytest.fixture
def lease(tmp_path):
    d = tmp_path / "lease"
    env = {**os.environ, "GPU_LEASE_DIR": str(d), "GPU_LEASE_POLL": "1"}

    def call(*args, **kw):
        return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True, **kw)

    def spawn(*args):
        return subprocess.Popen([str(SCRIPT), *args], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    call.dir = d
    call.spawn = spawn
    return call


def _wait_held(lease, want=True, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if (lease("held").returncode == 0) == want:
            return True
        time.sleep(0.05)
    return False


def test_run_holds_lock_and_held_sees_it(lease):
    p = lease.spawn("run", "--label", "bench-S1", "--", "sleep", "30")
    try:
        assert _wait_held(lease)
        r = lease("held")
        assert r.returncode == 0
        assert f"pid={p.pid} label=bench-S1 since=" in r.stdout
    finally:
        p.kill()
        p.wait()


def test_run_exits_with_command_code_and_frees_lock(lease):
    r = lease("run", "--label", "x", "--", "bash", "-c", "exit 7")
    assert r.returncode == 7
    assert lease("held").returncode == 1
    assert not (lease.dir / "holder").exists()


def test_second_run_exits_75_naming_holder(lease):
    p = lease.spawn("run", "--label", "bench-S2", "--", "sleep", "30")
    try:
        assert _wait_held(lease)
        r = lease("run", "--label", "other", "--wait", "0", "--", "true")
        assert r.returncode == 75
        assert "bench-S2" in r.stderr
    finally:
        p.kill()
        p.wait()


def test_lock_frees_when_holder_killed(lease):
    p = lease.spawn("run", "--label", "doomed", "--", "sleep", "60")
    assert _wait_held(lease)
    p.kill()
    p.wait()
    assert _wait_held(lease, want=False)
    assert lease("held").returncode == 1


def test_want_and_wanted_max_age(lease):
    assert lease("wanted").returncode == 1
    assert lease("want", "ingest").returncode == 0
    assert (lease.dir / "want").read_text().strip() == "ingest"
    assert lease("wanted").returncode == 0
    old = time.time() - 100
    os.utime(lease.dir / "want", (old, old))
    assert lease("wanted", "--max-age", "50").returncode == 1
    assert lease("wanted", "--max-age", "500").returncode == 0
    assert lease("clear-want").returncode == 0
    assert lease("wanted").returncode == 1


def test_yield_returns_at_once_when_not_wanted(lease):
    t0 = time.time()
    r = lease("yield", "--min-run", "60", "--max-wait", "30")
    assert r.returncode == 0
    assert time.time() - t0 < 2
    assert not (lease.dir / "last-yield").exists()


def test_yield_returns_at_once_inside_min_run(lease):
    lease("want", "ingest")
    (lease.dir / "last-yield").touch()
    t0 = time.time()
    r = lease("yield", "--min-run", "600", "--max-wait", "30")
    assert r.returncode == 0
    assert time.time() - t0 < 2
    assert "min-run" in r.stdout


def test_yield_waits_until_want_cleared(lease):
    lease("want", "ingest")
    threading.Timer(2.0, lambda: lease("clear-want")).start()
    t0 = time.time()
    r = lease("yield", "--min-run", "0", "--max-wait", "30")
    waited = time.time() - t0
    assert r.returncode == 0
    assert 2 <= waited <= 12, waited
    assert "yielded" in r.stdout
    assert (lease.dir / "last-yield").exists()


def test_yield_gives_up_at_max_wait(lease):
    lease("want", "ingest")
    t0 = time.time()
    r = lease("yield", "--min-run", "0", "--max-wait", "2")
    assert r.returncode == 0
    assert 2 <= time.time() - t0 <= 8
    assert "gave up" in r.stdout


def test_status_block(lease):
    r = lease("status")
    assert r.returncode == 0
    assert "none (lock free)" in r.stdout
    assert "last-yield: never" in r.stdout
    lease("want", "ingest")
    assert "ingest (age" in lease("status").stdout


def test_yield_starts_the_window_hook_and_ignores_want_age(lease, tmp_path):
    """Yield runs GPU_LEASE_ON_YIELD at once and keeps waiting while the want file exists, however old it is."""
    lease_dir = lease.dir
    lease_dir.mkdir(parents=True, exist_ok=True)
    want = lease_dir / "want"
    want.write_text("ingest-drain")
    old = time.time() - 3600
    os.utime(want, (old, old))
    marker = tmp_path / "hook-ran"
    hook = f"touch {marker} && sleep 2 && rm -f {want}"
    t0 = time.time()
    r = subprocess.run([str(SCRIPT), "yield", "--min-run", "0", "--max-wait", "30"],
                       env={**os.environ, "GPU_LEASE_DIR": str(lease_dir), "GPU_LEASE_ON_YIELD": hook, "GPU_LEASE_POLL": "1"}, capture_output=True, text=True)
    assert r.returncode == 0 and "yielded" in r.stdout, r.stdout + r.stderr
    assert marker.exists() and not want.exists()
    assert 1.5 <= time.time() - t0 <= 12


def test_yield_gives_up_when_nobody_takes_the_gpu(lease):
    """A want with no drain behind it must not cost the batch job the whole max-wait."""
    lease_dir = lease.dir
    lease_dir.mkdir(parents=True, exist_ok=True)
    (lease_dir / "want").write_text("ingest-drain")
    t0 = time.time()
    r = subprocess.run([str(SCRIPT), "yield", "--min-run", "0", "--max-wait", "60"],
                       env={**os.environ, "GPU_LEASE_DIR": str(lease_dir), "GPU_LEASE_ON_YIELD": "", "GPU_LEASE_NOHOLDER_GRACE": "2", "GPU_LEASE_POLL": "1"},
                       capture_output=True, text=True)
    assert r.returncode == 0 and "nobody took the GPU" in r.stdout, r.stdout
    assert time.time() - t0 < 10
