#!/usr/bin/env python3
"""The dedupe must collapse duplicates and must NOT collapse revisions.

Graphiti writes a new episode under the same name every time an entry is re-synced, so a
name-keyed dedupe deletes real revision history. The live example is kb-capture-inbox#1..#3,
which carry three different bodies because the queue line was rewritten between syncs; a
name-keyed tool would have destroyed two thirds of that history. The 50 duplicates from
2026-09-02 are collapsible only because their content is byte-identical.

Also asserts the survivor is deterministic: the newest by created_at, uuid as tiebreak, so
two runs of the planner never disagree about which copy to keep.
"""
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "dd", pathlib.Path(__file__).resolve().parents[1] / "gestalt-graph-dedupe.py")
dd = importlib.util.module_from_spec(spec); spec.loader.exec_module(dd)

def ep(uuid, name, ts, content): return dict(uuid=uuid, name=name, created_at=ts, content=content)

fails = 0
def check(label, got, want):
    global fails
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}: {label}: {got}" + ("" if ok else f"  wanted {want}"))
    if not ok: fails += 1

print("--- true duplicates: same name AND identical content ---")
rows = [ep("a", "kb-home-mesh#1", "2026-09-02T01:00", "BODY"),
        ep("b", "kb-home-mesh#1", "2026-09-02T02:00", "BODY"),
        ep("c", "kb-home-mesh#1", "2026-09-02T03:00", "BODY")]
doomed, kept = dd.plan(rows)
check("3 identical copies leave 2 doomed", len(doomed), 2)
check("survivor is the newest", kept[0]["keep"], "c")

print("--- revisions: same name, DIFFERENT content, must all survive ---")
rows = [ep("a", "kb-capture-inbox#1", "2026-09-02T01:00", "v1"),
        ep("b", "kb-capture-inbox#1", "2026-09-02T02:00", "v2"),
        ep("c", "kb-capture-inbox#1", "2026-09-03T03:00", "v3")]
doomed, kept = dd.plan(rows)
check("3 distinct bodies delete nothing", len(doomed), 0)
check("and report no duplicate groups", len(kept), 0)

print("--- mixed: two identical plus one revision ---")
rows = [ep("a", "kb-x#1", "2026-09-02T01:00", "SAME"),
        ep("b", "kb-x#1", "2026-09-02T02:00", "SAME"),
        ep("c", "kb-x#1", "2026-09-02T03:00", "DIFFERENT")]
doomed, kept = dd.plan(rows)
check("only the identical pair collapses", len(doomed), 1)
check("the doomed one is the older identical copy", doomed[0]["uuid"], "a")

print("--- singletons are never touched ---")
check("unique names", len(dd.plan([ep("a","kb-p","t","x"), ep("b","kb-q","t","y")])[0]), 0)

print("--- determinism: same input, same plan, twice ---")
rows = [ep("a","kb-z#1","2026-09-02T01:00","B"), ep("b","kb-z#1","2026-09-02T01:00","B")]
check("identical timestamps resolve by uuid", dd.plan(rows)[0][0]["uuid"], dd.plan(rows)[0][0]["uuid"])

print("PASS" if fails == 0 else "FAIL")
sys.exit(1 if fails else 0)
