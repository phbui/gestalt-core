"""GPU lease admission and --drain-wait for tools/gestalt-graphiti-sync.sh (2026-10-08).

Hermetic: stub gpu-lease, ssh, docker and curl on a private PATH, plus the stub Graphiti endpoint and tmp state dir
from the ledger suite. Nothing here touches the real lease, the hub or docker.
"""
import os
import subprocess
from pathlib import Path

import pytest

from test_graphiti_sync_ledger import SYNC, rig  # noqa: F401  (rig is a fixture)

GPU_LEASE = '''#!/bin/bash
echo "gpu-lease $*" >> "$STUB_CALLS"
case "$1" in
    held) [ -n "${STUB_HOLDER:-}" ] && { echo "$STUB_HOLDER"; exit 0; }; exit 1 ;;
    *) exit 0 ;;
esac
'''
# ssh <opts> host cmd...: log it, then answer as the remote gpu-lease would. STUB_SSH_FAIL=1 acts like a dead host.
SSH = '''#!/bin/bash
echo "ssh $*" >> "$STUB_CALLS"
[ "${STUB_SSH_FAIL:-0}" = "1" ] && exit 255
while [ $# -gt 0 ] && [ "$1" != gpu-lease ]; do shift; done
exec gpu-lease "${@:2}"
'''
# docker: the Episodic count starts at STUB_START and rises by STUB_STEP per call, capped at STUB_CAP.
DOCKER = '''#!/bin/bash
f="$STUB_DIR/docker-count"; n=$(cat "$f" 2>/dev/null || echo "$STUB_START")
echo "docker" >> "$STUB_CALLS"
echo "$n"
n=$((n + STUB_STEP)); [ "$n" -gt "$STUB_CAP" ] && n=$STUB_CAP
echo "$n" > "$f"
'''
# curl: the remote-host case uses 127.0.0.2, which the stub server does not bind. Point it back at 127.0.0.1.
CURL = '''#!/bin/bash
args=(); for a in "$@"; do args+=("${a//127.0.0.2/127.0.0.1}"); done
exec "$REAL_CURL" "${args[@]}"
'''


@pytest.fixture
def lease(rig):  # noqa: F811
    bind = rig.tmp / "bin"; bind.mkdir()
    for name, body in (("gpu-lease", GPU_LEASE), ("ssh", SSH), ("docker", DOCKER), ("curl", CURL)):
        (bind / name).write_text(body); (bind / name).chmod(0o755)
    real_curl = subprocess.run(["bash", "-c", "command -v curl"], capture_output=True, text=True).stdout.strip()
    calls = rig.tmp / "calls"; calls.write_text("")
    keep = [d for d in os.environ["PATH"].split(":") if not (Path(d) / "gpu-lease").exists()]
    rig.env.update(PATH=":".join([str(bind)] + keep), STUB_CALLS=str(calls), STUB_DIR=str(rig.tmp),
                   REAL_CURL=real_curl, STUB_START="100", STUB_STEP="1", STUB_CAP="1000")
    rig.bin, rig.calls = bind, calls
    rig.read_calls = lambda: calls.read_text().splitlines()
    return rig


def go(rig, *args, **extra):
    return subprocess.run(["bash", str(SYNC), *args], env=dict(rig.env, **extra), capture_output=True, text=True, timeout=120)


def test_foreign_holder_skips_and_records_want(lease):
    r = go(lease, STUB_HOLDER="pid=7 label=bench-S3 since=2026-10-08T01:00:00")
    assert r.returncode == 0
    assert lease.stub.received == []
    assert not lease.state.exists()
    assert "reason=gpu-leased holder=pid=7 label=bench-S3" in lease.log.read_text()
    assert "gpu-lease want sync" in lease.read_calls()
    assert go(lease, STUB_HOLDER="pid=7 label=bench-S3 since=x", GESTALT_SYNC_EXIT75="1").returncode == 75


def test_ingest_holder_proceeds(lease):
    r = go(lease, STUB_HOLDER="pid=8 label=ingest-drain since=x")
    assert r.returncode == 0, r.stderr
    assert len(lease.stub.received) >= 2
    assert "gpu-lease want sync" not in lease.read_calls()


def test_free_lease_proceeds(lease):
    r = go(lease)
    assert r.returncode == 0, r.stderr
    assert len(lease.stub.received) >= 2
    assert "gpu-lease held" in lease.read_calls()


def test_missing_gpu_lease_fails_open_and_logs_once(lease):
    (lease.bin / "gpu-lease").unlink()
    r = go(lease)
    assert r.returncode == 0, r.stderr
    assert len(lease.stub.received) >= 2
    assert lease.log.read_text().count("gpu-lease unavailable") == 1


def test_remote_host_goes_through_ssh(lease):
    url = lease.env["GRAPHITI_URL"].replace("127.0.0.1", "127.0.0.2")
    r = go(lease, GRAPHITI_URL=url, STUB_HOLDER="pid=7 label=bench-S3 since=x")
    assert r.returncode == 0
    assert lease.stub.received == []
    ssh = [c for c in lease.read_calls() if c.startswith("ssh ")]
    assert ssh and "-o BatchMode=yes" in ssh[0] and "-o ConnectTimeout=5" in ssh[0] and "127.0.0.2 gpu-lease held" in ssh[0]
    assert any(c.startswith("ssh ") and c.endswith("gpu-lease want sync") for c in lease.read_calls())


def test_remote_ssh_failure_fails_open(lease):
    url = lease.env["GRAPHITI_URL"].replace("127.0.0.1", "127.0.0.2")
    r = go(lease, GRAPHITI_URL=url, STUB_SSH_FAIL="1")
    assert r.returncode == 0, r.stderr
    assert len(lease.stub.received) >= 2
    assert "gpu-lease unavailable (rc=255" in lease.log.read_text()


def test_ignore_lease_bypasses_the_check(lease):
    r = go(lease, STUB_HOLDER="pid=7 label=bench-S3 since=x", GESTALT_SYNC_IGNORE_LEASE="1")
    assert r.returncode == 0, r.stderr
    assert len(lease.stub.received) >= 2
    assert lease.read_calls() == []


def test_dry_run_never_checks_the_lease(lease):
    r = go(lease, "--dry-run", STUB_HOLDER="pid=7 label=bench-S3 since=x")
    assert r.returncode == 0
    assert "gpu-lease" not in "\n".join(lease.read_calls())


def test_drain_wait_stops_when_the_count_reaches_the_target(lease):
    r = go(lease, "--drain-wait", GESTALT_SYNC_DRAIN_POLL="0.1", GESTALT_SYNC_DRAIN_STALL="30", GESTALT_SYNC_DRAIN_REPORT="1")
    assert r.returncode == 0, r.stderr
    n = len(lease.stub.received)
    assert n >= 2
    log = lease.log.read_text()
    assert "waiting for %d episodes (count 100 -> %d)" % (n, 100 + n) in log
    assert "DRAIN: landed %d of %d" % (n, n) in log
    assert "giving up" not in log
    # one base read plus one poll per step: it stopped at the target and did not keep polling.
    assert lease.read_calls().count("docker") == 1 + n


def test_drain_wait_gives_up_after_no_change(lease):
    r = go(lease, "--drain-wait", STUB_STEP="0", GESTALT_SYNC_DRAIN_POLL="0.2", GESTALT_SYNC_DRAIN_STALL="1")
    assert r.returncode == 0, r.stderr
    n = len(lease.stub.received)
    log = lease.log.read_text()
    assert "no change for 1s, giving up" in log
    assert "DRAIN: landed 0 of %d" % n in log


def test_drain_wait_total_cap(lease):
    r = go(lease, "--drain-wait", STUB_STEP="1", STUB_CAP="101", GESTALT_SYNC_DRAIN_POLL="0.3",
           GESTALT_SYNC_DRAIN_STALL="100", GESTALT_SYNC_DRAIN_MAX="1")
    assert r.returncode == 0, r.stderr
    log = lease.log.read_text()
    assert "reached the 1s cap" in log
    assert "DRAIN: landed 1 of" in log


def test_drain_wait_without_docker_is_skipped(lease):
    (lease.bin / "docker").unlink()
    keep = [d for d in lease.env["PATH"].split(":") if not (Path(d) / "docker").exists()]
    r = go(lease, "--drain-wait", PATH=":".join(keep))
    assert r.returncode == 0, r.stderr
    assert "DRAIN: skipped" in lease.log.read_text()
