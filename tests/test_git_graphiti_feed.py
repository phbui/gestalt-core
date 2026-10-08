"""Behavioural tests for tools/gestalt-git-graphiti-feed.sh (Phase-2 git-change-
detection feed, docs/pitches/gestalt-memory-expansion/sdd/hooks.md ^git-change-
detection): iterate repos under $WORKSPACE and call add_memory per repo with commits
since the last time each repo was fed.

Driven end to end against a synthetic tmp $WORKSPACE containing a real (git init'd)
repo, with a stub `curl` on PATH answering the MCP handshake (initialize -> a
`mcp-session-id` response header, tools/call -> `{"isError":false}`). No live
Letta/Graphiti/network is required, matching tests/test_memory_stack.py's style.

(a) Ledger idempotency: first run feeds the bootstrap-then-incremental sequence and
    records each repo's HEAD sha; a rerun with no new commits makes zero further
    add_memory POSTs.
(b) Hub-down gating: with HUB_GRAPHITI_OK=0, the script never invokes curl at all and
    completes in well under the F3/F4-style latency budget -- including a bootstrap-
    only repo, whose ledger baseline is local-only and must still get persisted even
    though the hub is down (the bug this pins: an earlier draft gated the whole
    bootstrap-persist step behind the same health check as the network path, so a
    bootstrap repo's baseline silently never landed while the hub was dark).
(c) Summary size cap: a repo with a very large diff still produces an episode body
    within GESTALT_FEED_SUMMARY_CHARS, inspected via --dry-run (zero network calls,
    zero state writes -- the same zero-side-effect contract as gestalt-graphiti-
    sync.sh's --dry-run).
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
TOOLS = REPO / "tools"
FEED_SCRIPT = TOOLS / "gestalt-git-graphiti-feed.sh"


def _stub_curl_dir(tmp_path: Path) -> Path:
    """A fake `curl` that logs every invocation to $CURL_LOG and answers the MCP
    handshake the script performs: `initialize` gets an `mcp-session-id` response
    header (the script's own -D target is the literal string "/dev/stderr", so the
    header goes straight to fd 2 regardless of args); `notifications/initialized`
    gets an empty body; anything else (the actual add_memory tools/call) gets a
    success body. Never touches a real network."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "${CURL_LOG:?CURL_LOG not set}"\n'
        'args="$*"\n'
        'if [[ "$args" == *"initialize"* ]]; then\n'
        '    echo "mcp-session-id: fake-session-123" >&2\n'
        '    printf ""\n'
        'elif [[ "$args" == *"notifications/initialized"* ]]; then\n'
        '    printf ""\n'
        "else\n"
        '    printf \'{"result":{"isError":false},"jsonrpc":"2.0"}\'\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.example"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True,
                           text=True, check=True).stdout.strip()


def _env(tmp_path: Path, *, workspace: Path, state_dir: Path, health_file: Path,
          curl_dir: Path | None, curl_log: Path, graphiti_ok: int, letta_ok: int = 1) -> dict:
    health_file.write_text(f"{int(time.time())} {letta_ok} {graphiti_ok}\n", encoding="utf-8")
    env = {
        **os.environ,
        "GESTALT_DIR": str(REPO),
        "GESTALT_WORKSPACE": str(workspace),
        "GESTALT_STATE_DIR": str(state_dir),
        "GESTALT_HUB_HEALTH_FILE": str(health_file),
        "CURL_LOG": str(curl_log),
        "GRAPHITI_URL": "http://127.0.0.1:9",
        "LETTA_URL": "http://127.0.0.1:9/v1",
    }
    if curl_dir is not None:
        env["PATH"] = str(curl_dir) + os.pathsep + os.environ["PATH"]
    return env


def _run(env: dict, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(FEED_SCRIPT), *extra_args], capture_output=True,
                           text=True, env=env, timeout=15)


def _post_count(curl_log: Path, needle: str = "add_memory") -> int:
    if not curl_log.exists():
        return 0
    # The stub logs full argv; the actual add_memory payload goes via `-d @-` (stdin,
    # not argv -- the whole point of the ARG_MAX fix this pattern was ported from), so
    # count POSTs to /mcp with `-d @-` in them rather than grepping for the tool name.
    return sum(1 for line in curl_log.read_text().splitlines() if "-d @-" in line)


# --------------------------------------------------------------------------
# (a) Ledger idempotency
# --------------------------------------------------------------------------

@pytest.mark.local
def test_ledger_idempotent_across_bootstrap_then_incremental_then_norepeat(tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "repoA"
    _init_repo(repo)
    _commit(repo, "f.txt", "hello\n", "first commit")

    state_dir = tmp_path / "state"
    curl_dir = _stub_curl_dir(tmp_path)
    curl_log = tmp_path / "curl.log"
    health_file = tmp_path / "health"

    # Run 1: bootstrap only -- no episode fed, no add_memory POST, baseline recorded.
    env = _env(tmp_path, workspace=workspace, state_dir=state_dir, health_file=health_file,
               curl_dir=curl_dir, curl_log=curl_log, graphiti_ok=1)
    r1 = _run(env)
    assert r1.returncode == 0, r1.stderr
    assert _post_count(curl_log) == 0, "bootstrap must not feed an episode"
    ledger = json.loads((state_dir / "git-graphiti-feed.json").read_text())
    bootstrap_sha = ledger["repoA"]

    # New commit, run 2: incremental -- exactly one add_memory POST, ledger advances.
    curl_log.write_text("", encoding="utf-8")
    head2 = _commit(repo, "f.txt", "hello\nworld\n", "second commit")
    r2 = _run(env)
    assert r2.returncode == 0, r2.stderr
    assert _post_count(curl_log) == 1, r2.stdout + r2.stderr
    ledger = json.loads((state_dir / "git-graphiti-feed.json").read_text())
    assert ledger["repoA"] == head2

    # No new commits, run 3: idempotent -- zero further POSTs, ledger unchanged.
    curl_log.write_text("", encoding="utf-8")
    r3 = _run(env)
    assert r3.returncode == 0, r3.stderr
    assert _post_count(curl_log) == 0, "rerun with no new commits must not re-feed"
    ledger = json.loads((state_dir / "git-graphiti-feed.json").read_text())
    assert ledger["repoA"] == head2
    assert bootstrap_sha != head2


# --------------------------------------------------------------------------
# (b) Hub-down gating: fast, network-free, but bootstrap still persists
# --------------------------------------------------------------------------

@pytest.mark.local
def test_hub_down_makes_zero_curl_calls_and_completes_fast(tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "repoA"
    _init_repo(repo)
    _commit(repo, "f.txt", "hello\n", "first commit")

    state_dir = tmp_path / "state"
    curl_dir = _stub_curl_dir(tmp_path)
    curl_log = tmp_path / "curl.log"
    health_file = tmp_path / "health"

    # Bootstrap first (hub up, but bootstrap never calls curl anyway).
    env_up = _env(tmp_path, workspace=workspace, state_dir=state_dir, health_file=health_file,
                  curl_dir=curl_dir, curl_log=curl_log, graphiti_ok=1)
    assert _run(env_up).returncode == 0

    # New commit, hub down: incremental work is pending but must be deferred.
    _commit(repo, "f.txt", "hello\nworld\n", "second commit")
    curl_log.write_text("", encoding="utf-8")
    env_down = _env(tmp_path, workspace=workspace, state_dir=state_dir, health_file=health_file,
                     curl_dir=curl_dir, curl_log=curl_log, graphiti_ok=0)
    t0 = time.monotonic()
    r = _run(env_down)
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    assert elapsed < 0.5, f"hub-down path took {elapsed:.2f}s (F3/F4-style budget: <0.5s)"
    assert _post_count(curl_log) == 0, "hub-down must make zero add_memory POSTs"
    assert not curl_log.exists() or curl_log.read_text().strip() == "", (
        "hub-down must make zero curl invocations of any kind (not just add_memory)"
    )


@pytest.mark.local
def test_bootstrap_still_persists_when_hub_is_down(tmp_path):
    """Regression pin: bootstrap needs no network (it only records a local HEAD sha),
    so it must land in the ledger even when graphiti is unreachable -- a dark hub must
    not silently drop bootstrap baselines. (An earlier draft of the script gated the
    whole bootstrap-persist step behind the same gestalt_hub_health check as the
    network path, so this failed until the persist was moved ahead of the gate.)"""
    workspace = tmp_path / "workspace"
    repo = workspace / "repoA"
    _init_repo(repo)
    _commit(repo, "f.txt", "hello\n", "first commit")

    state_dir = tmp_path / "state"
    curl_dir = _stub_curl_dir(tmp_path)
    curl_log = tmp_path / "curl.log"
    health_file = tmp_path / "health"
    env_down = _env(tmp_path, workspace=workspace, state_dir=state_dir, health_file=health_file,
                     curl_dir=curl_dir, curl_log=curl_log, graphiti_ok=0)

    r = _run(env_down)
    assert r.returncode == 0, r.stderr
    ledger_path = state_dir / "git-graphiti-feed.json"
    assert ledger_path.exists(), "bootstrap baseline must be written even with the hub down"
    ledger = json.loads(ledger_path.read_text())
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                           text=True, check=True).stdout.strip()
    assert ledger["repoA"] == head
    assert _post_count(curl_log) == 0


# --------------------------------------------------------------------------
# (c) Summary size cap
# --------------------------------------------------------------------------

def _commit_many_files(repo: Path, batch: int, n: int, message: str) -> None:
    """`git diff --stat` emits one line per changed file (a single huge file is still
    one short line), and `git log --oneline` emits one line per commit -- so to make
    both halves of the summary genuinely large, change many distinctly-named files in
    one commit, across several commits. Filenames are unique per batch so each commit
    actually has something new to record (a repeated batch index would otherwise
    reproduce identical content and git would see "nothing to commit")."""
    for k in range(n):
        (repo / f"file_{batch:04d}_{k:04d}.txt").write_text(f"content for file {batch}-{k}\n" * 5, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def test_dry_run_summary_stays_within_the_char_cap(tmp_path):
    workspace = tmp_path / "workspace"
    repo = workspace / "repoA"
    _init_repo(repo)
    _commit(repo, "f.txt", "hello\n", "first commit")

    state_dir = tmp_path / "state"
    health_file = tmp_path / "health"
    cap = 2000  # deliberately small, to prove the cap is enforced not just "usually small"
    env = {
        **os.environ,
        "GESTALT_DIR": str(REPO),
        "GESTALT_WORKSPACE": str(workspace),
        "GESTALT_STATE_DIR": str(state_dir),
        "GESTALT_HUB_HEALTH_FILE": str(health_file),
        "GESTALT_FEED_SUMMARY_CHARS": str(cap),
    }
    health_file.write_text(f"{int(time.time())} 1 0\n", encoding="utf-8")
    # Feed once for real so the ledger has a baseline (bootstrap), so the *next*
    # commits are classified incremental.
    assert _run(env).returncode == 0

    # Many commits touching many files: both the log and the diffstat scale with this,
    # so the uncapped body is provably far larger than `cap`.
    for i in range(20):
        _commit_many_files(repo, batch=i, n=10, message=f"batch {i}: add 10 files")

    r = _run(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, lines
    repo_name, from_sha, to_sha, kind, nbytes = lines[0].split("\t")
    assert kind == "incremental", lines[0]
    assert int(nbytes) <= cap, f"episode body {nbytes} bytes exceeds cap {cap}"
    # And the cap must actually be binding here, not just coincidentally under budget:
    raw_log = subprocess.run(["git", "-C", str(repo), "log", "--oneline", f"{from_sha}..{to_sha}"],
                              capture_output=True, text=True, check=True).stdout
    raw_stat = subprocess.run(["git", "-C", str(repo), "diff", "--stat", f"{from_sha}..{to_sha}"],
                               capture_output=True, text=True, check=True).stdout
    assert len(raw_log) + len(raw_stat) > cap * 3, (
        "test fixture too small to prove the cap actually binds: "
        f"raw log+stat is only {len(raw_log) + len(raw_stat)} chars vs cap {cap}"
    )


def test_summary_chars_env_var_is_honored_with_a_larger_cap(tmp_path):
    """Same fixture, a much larger cap: proves GESTALT_FEED_SUMMARY_CHARS actually
    controls the bound rather than the script silently using a hardcoded budget."""
    workspace = tmp_path / "workspace"
    repo = workspace / "repoA"
    _init_repo(repo)
    _commit(repo, "f.txt", "hello\n", "first commit")

    state_dir = tmp_path / "state"
    health_file = tmp_path / "health"
    small_cap = 500
    env = {
        **os.environ,
        "GESTALT_DIR": str(REPO),
        "GESTALT_WORKSPACE": str(workspace),
        "GESTALT_STATE_DIR": str(state_dir),
        "GESTALT_HUB_HEALTH_FILE": str(health_file),
        "GESTALT_FEED_SUMMARY_CHARS": str(small_cap),
    }
    health_file.write_text(f"{int(time.time())} 1 0\n", encoding="utf-8")
    assert _run(env).returncode == 0

    for i in range(20):
        _commit_many_files(repo, batch=i, n=10, message=f"batch {i}: add 10 files")

    r_small = _run(env, "--dry-run")
    nbytes_small = int(r_small.stdout.strip().split("\t")[-1])
    # SUMMARY_CHARS bounds the two `head -c` budgets (log half + stat half); the
    # "## Commits (<range>)\n...\n\n## Changes\n...\n" wrapper and the two full 40-char
    # shas in <range> add a small, roughly-constant amount on top -- not part of the
    # capped content itself, so the assertion allows that fixed overhead rather than
    # pretending the cap is exact-byte.
    assert nbytes_small <= small_cap + 200, f"{nbytes_small} exceeds cap {small_cap} by more than fixed overhead"

    env_large = {**env, "GESTALT_FEED_SUMMARY_CHARS": "20000"}
    r_large = _run(env_large, "--dry-run")
    nbytes_large = int(r_large.stdout.strip().split("\t")[-1])
    assert nbytes_large > nbytes_small, (
        "raising GESTALT_FEED_SUMMARY_CHARS must actually raise the observed body size"
    )
