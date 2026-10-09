#!/usr/bin/env python3
"""Collapse duplicate Episodic nodes. DESIGN + DRY RUN ONLY; execution is not armed.

WHY THIS EXISTS AND WHY IT IS CAREFUL. The previous dedupe deleted all 134 Episodic and
412 Entity nodes on 2026-09-01. The mechanism was predicate drift: the dry run counted
`d.created_at <> keep` and the execute deleted `d.created_at < keep`, an operator changed
between the two runs as a tie-guard, so what was counted was never what was deleted. A
second flaw compounded it: `max()` did not scope per name across a WITH boundary in
FalkorDB, so "keep" was global rather than per group.

THE FIX IS STRUCTURAL, NOT CAREFUL WORDING. This tool never deletes by predicate. It reads
every node once, groups in Python, computes an explicit list of UUIDs to delete, prints it,
and (when armed) deletes exactly those UUIDs by identity. The set shown by --dry-run is the
same object the executor consumes, so the two cannot diverge. There is no second query whose
predicate could differ from the first.

WHAT COUNTS AS A DUPLICATE (decided 2026-09-03): same name AND same content hash. Same name
with DIFFERENT content is a revision and is kept. Graphiti creates a new episode under the
same name whenever an entry is re-synced, so name-keyed dedupe would silently delete real
revision history. kb-capture-inbox#1..#3 are the worked example: three copies, three
different bodies, all legitimate.

PRECONDITION, NOT A SUGGESTION (<kb-entry>): the graphiti queue must
be flat. A delete cannot cancel queued writes, so a dedupe run mid-sync collapses duplicates
that immediately land again and reads as a failure. This refuses to run while the queue is
moving.

Usage:
  gestalt-graph-dedupe.py --dry-run        report groups and the exact UUIDs that would go
  gestalt-graph-dedupe.py --execute        REFUSED unless GESTALT_DEDUPE_ARMED=1 is set by a
                                           human; arming is Phi's call, per the standing
                                           quarantine from 2026-09-01.
"""
import argparse
import collections
import hashlib
import json
import os
import subprocess
import sys

GRAPH = os.environ.get("GESTALT_GRAPH", "gestalt")
CONTAINER = os.environ.get("GESTALT_FALKOR_CONTAINER", "gestalt-falkordb")


def query(cypher: str) -> str:
    return subprocess.run(
        ["docker", "exec", CONTAINER, "redis-cli", "--no-raw", "GRAPH.QUERY", GRAPH, cypher],
        capture_output=True, text=True, timeout=120).stdout


def query_scalar(cypher: str) -> str:
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "redis-cli", "--json", "GRAPH.QUERY", GRAPH, cypher],
        capture_output=True, text=True, timeout=60).stdout
    return json.loads(out)[1][0][0]


def queue_is_flat(window_min: int = 5) -> tuple[bool, str]:
    """Flat means graphiti issued no completions recently. Checked over a window longer than
    a typical episode so a mid-episode lull is not mistaken for an idle queue."""
    out = subprocess.run(["docker", "logs", "gestalt-graphiti", "--since", f"{window_min}m"],
                         capture_output=True, text=True, timeout=60)
    hits = (out.stdout + out.stderr).replace("\x00", "").count("chat/completions")
    return hits == 0, f"{hits} completions in the last {window_min}m"


def count_episodes() -> int:
    """Independent count, used to prove the reader actually read the graph."""
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "redis-cli", "--json", "GRAPH.QUERY", GRAPH,
         "MATCH (n:Episodic) RETURN count(n)"], capture_output=True, text=True, timeout=60).stdout
    try:
        return int(json.loads(out)[1][0][0])
    except Exception:
        return -1


def load_episodes() -> list[dict]:
    """One read of the whole label, via redis-cli --json.

    NOT by parsing redis-cli's human output. That format is `N) "value"` with varying
    indentation, and an earlier version tested `line.startswith('"')` after stripping, which
    never matched, so it read ZERO episodes and the tool cheerfully reported "0 duplicates,
    nothing to delete" against a graph holding 491. For a deletion tool that is the shape to
    fear: an empty read is indistinguishable from a clean graph unless something asserts
    otherwise, which is why the caller cross-checks against count_episodes().
    """
    out = subprocess.run(
        ["docker", "exec", CONTAINER, "redis-cli", "--json", "GRAPH.QUERY", GRAPH,
         "MATCH (n:Episodic) RETURN n.uuid, n.name, n.created_at, n.content"],
        capture_output=True, text=True, timeout=300).stdout
    doc = json.loads(out)
    return [dict(uuid=r[0], name=r[1], created_at=r[2], content=r[3] or "") for r in doc[1]]


def plan(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group by (name, sha1(content)). Within a group keep the NEWEST by created_at, with the
    uuid as a deterministic tiebreak so two runs always choose the same survivor."""
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["name"], hashlib.sha1(r["content"].encode("utf-8", "replace")).hexdigest())].append(r)
    doomed, kept = [], []
    for (name, h), members in sorted(groups.items()):
        if len(members) == 1:
            continue
        members.sort(key=lambda r: (r["created_at"], r["uuid"]))
        kept.append(dict(name=name, hash=h[:12], keep=members[-1]["uuid"], copies=len(members)))
        doomed.extend(members[:-1])
    return doomed, kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    if not (a.dry_run or a.execute):
        print("specify --dry-run or --execute"); return 64

    flat, detail = queue_is_flat()
    print(f"queue check: {detail} -> {'FLAT' if flat else 'STILL MOVING'}")
    if not flat:
        print("REFUSING: a delete cannot cancel queued writes, so anything collapsed now can")
        print("land again (<kb-entry>). Wait for the queue to go flat.")
        return 3

    rows = load_episodes()
    expected = count_episodes()
    if expected < 0 or len(rows) != expected:
        print(f"REFUSING: read {len(rows)} episodes but the graph reports {expected}.")
        print("A partial or empty read makes 'no duplicates found' meaningless, and acting on")
        print("it would delete the wrong set or nothing at all while reporting success.")
        return 5
    doomed, kept = plan(rows)
    print(f"\nepisodes read: {len(rows)}")
    print(f"groups with true duplicates (same name AND same content): {len(kept)}")
    print(f"nodes that would be deleted: {len(doomed)}")
    print(f"nodes that would remain: {len(rows) - len(doomed)}\n")
    for g in kept:
        print(f"  {g['name']}  content={g['hash']}  copies={g['copies']}  keeping {g['keep'][:8]}")

    names = {r["name"] for r in rows}
    revisions = sum(1 for n in names
                    if len({hashlib.sha1(r['content'].encode('utf-8','replace')).hexdigest()
                            for r in rows if r['name'] == n}) > 1)
    print(f"\nnames carrying MULTIPLE DISTINCT contents (revisions, deliberately kept): {revisions}")

    if a.dry_run:
        out = os.environ.get("GESTALT_DEDUPE_PLAN", "/tmp/gestalt-dedupe-plan.json")
        with open(out, "w") as f:
            json.dump(dict(delete=[r["uuid"] for r in doomed], groups=kept), f, indent=1)
        print(f"\nDRY RUN: nothing deleted. Plan written to {out}.")
        return 0

    if os.environ.get("GESTALT_DEDUPE_ARMED") != "1":
        print("\nREFUSED: --execute needs GESTALT_DEDUPE_ARMED=1, which only a human sets.")
        print("This tool has been quarantined since 2026-09-01, when its predecessor deleted")
        print("the entire graph. Arming is Phi's decision and is not an agent's to make.")
        return 4

    # The plan recorded by the authorising dry run. The runtime plan is re-derived above and
    # must match it EXACTLY. This is the guard against the failure that destroyed the graph:
    # there, the counted set and the deleted set were produced by two different predicates.
    # Here they are the same list, and a graph that changed since authorisation is a refusal.
    plan_path = os.environ.get("GESTALT_DEDUPE_PLAN", "/tmp/gestalt-dedupe-plan.json")
    try:
        with open(plan_path) as f:
            recorded = set(json.load(f)["delete"])
    except Exception as e:
        print(f"\nREFUSED: cannot read the authorised plan at {plan_path}: {e}")
        print("Run --dry-run first; the armed path deletes only a plan a dry run recorded.")
        return 6
    runtime = {r["uuid"] for r in doomed}
    if runtime != recorded:
        print(f"\nREFUSED: the runtime plan differs from the authorised plan at {plan_path}.")
        print(f"  authorised {len(recorded)} uuids, re-derived {len(runtime)}")
        print(f"  only in authorised: {sorted(recorded - runtime)[:5]}")
        print(f"  only in re-derived: {sorted(runtime - recorded)[:5]}")
        print("The graph changed since authorisation. Re-run --dry-run and seek approval again.")
        return 7

    # Snapshot before, so the verification afterwards compares against measured reality.
    before = {k: int(query_scalar(qy)) for k, qy in (
        ("episodes", "MATCH (n:Episodic) RETURN count(n)"),
        ("entities", "MATCH (n:Entity) RETURN count(n)"),
        ("relationships", "MATCH ()-[r]->() RETURN count(r)"))}
    print(f"\nBEFORE: {before}")
    print(f"deleting {len(runtime)} episodes BY UUID, nothing else")

    deleted = 0
    for u in sorted(runtime):
        out = query(f"MATCH (n:Episodic {{uuid:'{u}'}}) DELETE n")
        if "Nodes deleted: 1" in out:
            deleted += 1
        else:
            print(f"  WARNING: uuid {u} did not delete cleanly")
    after = {k: int(query_scalar(qy)) for k, qy in (
        ("episodes", "MATCH (n:Episodic) RETURN count(n)"),
        ("entities", "MATCH (n:Entity) RETURN count(n)"),
        ("relationships", "MATCH ()-[r]->() RETURN count(r)"))}
    print(f"AFTER : {after}")
    print(f"deleted {deleted} of {len(runtime)}")

    ok = (after["episodes"] == before["episodes"] - len(runtime)
          and after["entities"] == before["entities"]
          and deleted == len(runtime))
    if not ok:
        print("MISMATCH: expected episodes to drop by exactly the deleted count and entities to")
        print("be untouched. Investigate before any further action.")
        return 8
    print("VERIFIED: episode count dropped by exactly the planned number, entities unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
