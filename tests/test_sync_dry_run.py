"""`gestalt-graphiti-sync.sh --dry-run` plans the exact chunks a fire would send.

Until 2026-09-06 the dry run exited before the chunker and printed whole-file candidates, so it proved
nothing about what would be queued (gestalt-overhaul-2026-09 row 63; the 2026-09-01 graph wipe was the
same discipline gap). Runs against a synthetic knowledge dir with graphiti pointed at a closed port: a dry
run must not need the network and must write no state.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SYNC = REPO / "tools" / "gestalt-graphiti-sync.sh"


def _rig(tmp_path: Path) -> dict:
    kd = tmp_path / "knowledge"
    kd.mkdir()
    (kd / "fixture.md").write_text("---\ntitle: Fixture\n---\n\n## Alpha\n" + "alpha body line\n" * 40 + "## Beta\n" + "beta body line\n" * 40)
    (kd / "tiny.md").write_text("---\ntitle: Tiny\n---\n\nOne short entry.\n")
    state = tmp_path / "state" / "graphiti-sync.json"
    env = dict(os.environ, GESTALT_KNOWLEDGE_DIR=str(kd), GESTALT_GRAPHITI_SYNC_STATE=str(state),
               GRAPHITI_URL="http://127.0.0.1:9", GESTALT_SYNC_CHUNK_CHARS="700")
    return {"env": env, "state": state}


FAKE_CLI = '''#!/usr/bin/env python3
"""redis-cli stand-in: the graph holds landed episodes named in LANDED as name or name:size (size default 500)."""
import json, os, re, sys
landed = {}
for tok in os.environ.get("LANDED", "").split():
    n, _, sz = tok.partition(":")
    landed[n] = int(sz or 500)
q = sys.argv[-1]
m = re.search(r'name: "([^"]+)"', q)
rows = [[f"uuid-of-{m.group(1)}", landed[m.group(1)]]] if m and m.group(1) in landed else []
print(json.dumps([["e.uuid", "size"], rows, ["stats"]]))
'''


def test_long_entries_carry_explicit_previous_episodes_never_the_default(tmp_path) -> None:
    """graphiti's default context is the TEN most recent episodes (116 KB on a 21-part archive, row 69). An
    entry over GESTALT_SYNC_PREV_MAX_PARTS parts must pass previous_episode_uuids explicitly: the landed
    preceding chunks when the graph has them, else an empty list; a short entry keeps graphiti's default."""
    rig = _rig(tmp_path)
    kd = Path(rig["env"]["GESTALT_KNOWLEDGE_DIR"])
    (kd / "long.md").write_text("---\ntitle: Long\n---\n\n" + "".join(f"## Section {i}\n" + f"line of section {i}\n" * 40 for i in range(10)))
    cli = tmp_path / "fake-cli.py"
    cli.write_text(FAKE_CLI)
    env = dict(rig["env"], GESTALT_FALKORDB_CLI=f"python3 {cli}", GESTALT_SYNC_PREV_MAX_PARTS="6", GESTALT_SYNC_PREV_N="2",
               LANDED="kb-long#1 kb-long#2 kb-long#3 kb-long#7:133797")  # #7 landed but oversized (pre-sub-split)
    r = subprocess.run([str(SYNC), "--dry-run", "--all"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    rows = {row[1]: row for row in (line.split("\t") for line in r.stdout.splitlines() if line.startswith("would send"))}
    long_parts = sorted(n for n in rows if n.startswith("kb-long#"))
    assert len(long_parts) > 6, long_parts
    assert rows["kb-long#1"][3] == "prev=0"  # nothing precedes part 1
    assert rows["kb-long#2"][3] == "prev=1"  # part 1 landed
    assert rows["kb-long#4"][3] == "prev=2"  # parts 2 and 3 landed
    assert rows["kb-long#6"][3] == "prev=0"  # parts 4 and 5 not landed: empty list, not graphiti's ten
    assert rows["kb-long#8"][3] == "prev=0"  # part 7 landed but at 133,797 chars: an oversized predecessor is never context
    assert rows["kb-fixture#1"][3] == "prev=auto" and rows["kb-tiny"][3] == "prev=auto"  # short entries: default


def test_dry_run_lists_every_chunk_with_its_size_and_touches_nothing(tmp_path) -> None:
    rig = _rig(tmp_path)
    r = subprocess.run([str(SYNC), "--dry-run", "--all"], env=rig["env"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    rows = [line.split("\t") for line in r.stdout.strip().splitlines() if line.startswith("would send")]
    names = [row[1] for row in rows]
    assert "kb-fixture#1" in names and "kb-fixture#2" in names, r.stdout
    assert "kb-tiny" in names
    assert all(int(row[2]) > 0 for row in rows)
    assert all(int(row[2]) <= 700 or row[1] == "kb-tiny" for row in rows if row[1].startswith("kb-fixture"))
    assert "would be queued" in r.stderr
    assert not rig["state"].exists(), "a dry run must not write the state file"
    assert not any(p.name.startswith("graphiti-sync.") for p in rig["state"].parent.glob("*")), "no temp files left behind"


def test_only_resend_pins_an_invocation_to_the_named_chunks(tmp_path) -> None:
    """2026-09-06 13:04 (row 76): naming kb-capture-inbox#4 queued all 8 chunks of the entry, because a named
    chunk rides along with every chunk whose state hash is stale. GESTALT_SYNC_ONLY_RESEND=1 sends the named
    chunks and nothing else; without it, on a fresh state, every chunk is planned."""
    rig = _rig(tmp_path)
    env = dict(rig["env"], GESTALT_SYNC_RESEND_NAMES="kb-fixture#2")
    r = subprocess.run([str(SYNC), "--dry-run", "fixture"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    names = [line.split("\t")[1] for line in r.stdout.splitlines() if line.startswith("would send")]
    assert "kb-fixture#1" in names and "kb-fixture#2" in names, names  # the ride-along, on a fresh state
    env["GESTALT_SYNC_ONLY_RESEND"] = "1"
    r = subprocess.run([str(SYNC), "--dry-run", "fixture"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    names = [line.split("\t")[1] for line in r.stdout.splitlines() if line.startswith("would send")]
    assert names == ["kb-fixture#2"], names


def test_every_chunk_gets_explicit_context_by_default(tmp_path) -> None:
    """Row 77 (2026-09-06 14:09): a 3-part entry, below the old 6-part threshold, was sent with graphiti's own
    ten-most-recent window as context; mid-wave that window was the run's own chunks and the dedup prompt reached
    233,760 bytes. The default threshold is now 0: every chunk carries an explicit list, empty when nothing landed."""
    rig = _rig(tmp_path)
    cli = tmp_path / "fake-cli.py"
    cli.write_text(FAKE_CLI)
    env = dict(rig["env"], GESTALT_FALKORDB_CLI=f"python3 {cli}", LANDED="kb-fixture#1")
    r = subprocess.run([str(SYNC), "--dry-run", "--all"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    rows = {row[1]: row for row in (line.split("\t") for line in r.stdout.splitlines() if line.startswith("would send"))}
    assert rows["kb-fixture#1"][3] == "prev=0" and rows["kb-fixture#2"][3] == "prev=1" and rows["kb-tiny"][3] == "prev=0", rows
    assert "prev=auto" not in r.stdout


def test_mark_synced_writes_the_planned_hash_without_sending(tmp_path) -> None:
    """A run killed after its episode went out leaves the ledger behind the graph; --mark-synced records the
    current planned hash so the name stops reappearing in every dry run, and refuses a name not in the plan."""
    rig = _rig(tmp_path)
    r = subprocess.run([str(SYNC), "--mark-synced=kb-fixture#2"], env=rig["env"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "mark-synced: 1 chunk hash(es) written" in r.stderr and "nothing sent" in r.stderr
    import json
    state = json.loads(rig["state"].read_text())
    assert set(state["#chunks"]) == {"kb-fixture#2"} and len(state["#chunks"]["kb-fixture#2"]) == 40
    r = subprocess.run([str(SYNC), "--dry-run", "fixture"], env=rig["env"], capture_output=True, text=True, timeout=120)
    names = [line.split("\t")[1] for line in r.stdout.splitlines() if line.startswith("would send")]
    assert "kb-fixture#1" in names and "kb-fixture#2" not in names, names
    r = subprocess.run([str(SYNC), "--mark-synced=kb-fixture#9"], env=rig["env"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 3 and "not in the current plan" in r.stderr, (r.returncode, r.stderr)
    assert not any(p.name.startswith("graphiti-sync.") and p.name != "graphiti-sync.json" for p in rig["state"].parent.glob("*"))
