"""Tests for tools/fix-ledger. The core guard: a guard built to fail must reopen its row."""
import json, os, subprocess, sys
import pytest

TOOL = os.path.join(os.path.dirname(__file__), "..", "tools", "fix-ledger")


@pytest.fixture
def led(tmp_path):
    return str(tmp_path / "ledger.jsonl")


def fl(led, *args):
    env = dict(os.environ, FLEET_ENV_FILE="/nonexistent", FLEET_NTFY_URL="", FLEET_NTFY_TOPIC="")
    return subprocess.run([sys.executable, TOOL, "--ledger", led, *args], capture_output=True, text=True, env=env)


def rows(led):
    return [json.loads(l) for l in open(led)]


def add(led, id, guard, *extra):
    return fl(led, "add", "--id", id, "--what", "defect " + id, "--guard", guard, *extra)


def test_add_and_duplicate(led):
    assert add(led, "a", "true").returncode == 0
    assert rows(led)[0]["status"] == "resolved"
    r = add(led, "a", "true")
    assert r.returncode != 0 and "already exists" in r.stderr


def test_add_refuses_failing_guard(led):
    r = add(led, "a", "false")
    assert r.returncode != 0 and not os.path.exists(led)
    assert add(led, "a", "false", "--allow-failing").returncode == 0
    assert rows(led)[0]["status"] == "reopened"


def test_check_holds(led):
    add(led, "a", "true")
    r = fl(led, "check")
    assert r.returncode == 0 and r.stdout.strip() == "FIX a HOLDS"


def test_check_reopens_on_failure(led):
    flag = os.path.join(os.path.dirname(led), "flag")
    open(flag, "w").close()
    add(led, "a", f"test -e {flag}")
    os.remove(flag)  # break the fix
    r = fl(led, "check")
    assert r.returncode != 0
    assert r.stdout.strip() == "FIX a REOPENED"
    row = rows(led)[0]
    assert row["status"] == "reopened"
    assert row["history"][-1]["rc"] == 1 and row["history"][-1]["ts"]


def test_timeout(led):
    flag = os.path.join(os.path.dirname(led), "slow")
    add(led, "a", f"test -e {flag} && sleep 5 || true", "--timeout", "1")
    open(flag, "w").close()
    r = fl(led, "check")
    assert r.returncode != 0 and r.stdout.strip() == "FIX a TIMEOUT"
    assert rows(led)[0]["status"] == "reopened"


def test_dismissed_never_reopens(led):
    add(led, "a", "true")
    assert fl(led, "dismiss", "--id", "a", "--expect", "resolved").returncode == 0
    row = rows(led)[0]; row["guard"] = "false"
    open(led, "w").write(json.dumps(row) + "\n")
    r = fl(led, "check")
    assert r.returncode == 0 and r.stdout.strip() == "FIX a DISMISSED-SKIP"
    assert rows(led)[0]["status"] == "dismissed"


def test_compare_and_set(led):
    add(led, "a", "true")
    r = fl(led, "resolve", "--id", "a", "--expect", "reopened")
    assert r.returncode != 0 and "expected reopened" in r.stderr
    assert fl(led, "dismiss", "--id", "a", "--expect", "reopened").returncode != 0
    assert rows(led)[0]["status"] == "resolved"


def test_resolve_after_reopen_keeps_history(led):
    add(led, "a", "false", "--allow-failing")
    assert fl(led, "resolve", "--id", "a", "--expect", "reopened").returncode == 0
    row = rows(led)[0]
    assert row["status"] == "resolved" and len(row["history"]) == 3


def test_dry_run_writes_nothing(led):
    flag = os.path.join(os.path.dirname(led), "flag")
    open(flag, "w").close()
    add(led, "a", f"test -e {flag}")
    os.remove(flag)
    before = open(led).read()
    r = fl(led, "check", "--dry-run")
    assert r.returncode != 0 and "REOPENED" in r.stdout
    assert open(led).read() == before


def test_atomic_write_valid_file(led):
    for i in range(5):
        add(led, f"r{i}", "true")
    assert [r["id"] for r in rows(led)] == [f"r{i}" for i in range(5)]
    assert not [f for f in os.listdir(os.path.dirname(led)) if f.startswith(".fix-ledger.")]
    assert "r0" in fl(led, "list").stdout
