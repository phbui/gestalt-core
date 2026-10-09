#!/usr/bin/env python3
"""Tag every golden case with `split: dev` or `split: test`.

Every knob decision (rerank, reranker, depth, fusion and alpha, stopwords, dedup, embedder) is made on dev. Test is
reported once, for the chosen configuration, by choose_config.py report-test. A case and every case it is correlated
with must land on the same side, or a decision made on dev leaks into test.

Clusters. Two answerable cases belong together when they share an accepted answer: the target slug, an expect_any slug,
or a slug family (run_retrieval_evals.accept_set). A cluster is a connected component of that relation. Its hash is the
sha1 of its sorted member slugs, and the cluster goes to dev when the hash mod 10 is below 7. An abstention case has no
answer, so it is its own cluster, hashed by its query.

Bucket floor. Every bucket must hold at least 15 percent of its cases in test. When the hash split leaves a bucket under
that, the dev clusters holding cases of that bucket move to test one at a time, the cluster whose hash mod 10 is highest
first (the one nearest the test side of the cut), ties by the full hash. Buckets are visited in name order and the pass
repeats until no bucket moves. A move only adds test cases, so the pass ends, and the result depends only on the cases.

    python3 evals/retrieval/assign_splits.py            # tag untagged cases, print the check
    python3 evals/retrieval/assign_splits.py --check    # print the check, write nothing, exit 1 on a violation
    python3 evals/retrieval/assign_splits.py --reassign # recompute every tag

Idempotent: a case that already carries a split keeps it, and a new case joins the side its cluster is already on.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Iterable
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_retrieval_evals as runner

DEV_BELOW = 7  # hash mod 10 below this goes to dev: about 70 percent dev, 30 percent test
MIN_TEST_SHARE = 0.15


def components(accept_sets: Iterable[Iterable[str]]) -> list[frozenset[str]]:
    """Connected components over slugs, where every slug in one accept set is linked to the others. Sorted by smallest member."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for acc in accept_sets:
        acc = sorted(acc)
        for s in acc:
            find(s)
        for s in acc[1:]:
            ra, rb = find(acc[0]), find(s)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, set[str]] = {}
    for s in parent:
        groups.setdefault(find(s), set()).add(s)
    return sorted((frozenset(g) for g in groups.values()), key=min)


def case_clusters(cases: list[dict], families: list[list[str]] | None = None) -> dict[str, str]:
    """Case id to cluster label. An answerable case's label is the smallest slug of its component. An abstention case's label is "abstain:" plus its case id."""
    comps = components(runner.accept_set(c, families) for c in cases if not runner.is_abstain(c))
    label = {s: min(comp) for comp in comps for s in comp}
    out = {}
    for c in cases:
        qid = runner.case_id(c)
        out[qid] = "abstain:" + qid if runner.is_abstain(c) else label[c["expect_slug"]]
    return out


def cluster_hash(members: Iterable[str], salt: str = "") -> int:
    """sha1 of the salt and the sorted members, as an integer. Python's hash() is salted per process, so it cannot be used."""
    return int(hashlib.sha1((salt + "\n".join(sorted(members))).encode()).hexdigest(), 16)


def cluster_members(cases: list[dict], families: list[list[str]] | None = None) -> dict[str, frozenset[str]]:
    """Cluster label to the strings its hash is taken over: the component's slugs, or an abstention case's query."""
    comps = components(runner.accept_set(c, families) for c in cases if not runner.is_abstain(c))
    out: dict[str, frozenset[str]] = {min(comp): comp for comp in comps}
    for c in cases:
        if runner.is_abstain(c):
            out["abstain:" + runner.case_id(c)] = frozenset([c["query"]])
    return out


def is_dev(members: Iterable[str], salt: str = "") -> bool:
    return cluster_hash(members, salt) % 10 < DEV_BELOW


def assign(cases: list[dict], families: list[list[str]] | None = None, reassign: bool = False) -> tuple[dict[str, str], list[str]]:
    """Case id to split, and notes on what was adjusted. Existing tags are kept unless reassign. Raises ValueError when existing tags straddle a cluster."""
    clusters = case_clusters(cases, families)
    members = cluster_members(cases, families)
    by_cluster: dict[str, list[dict]] = {}
    for c in cases:
        by_cluster.setdefault(clusters[runner.case_id(c)], []).append(c)
    split: dict[str, str] = {}
    locked: set[str] = set()
    notes: list[str] = []
    for cl, cs in by_cluster.items():
        tags = {c.get("split") for c in cs if c.get("split")} if not reassign else set()
        if len(tags) > 1:
            raise ValueError(f"cluster {cl} straddles the split: its cases carry {sorted(tags)}. Fix the tags or pass --reassign")
        if tags:
            split[cl] = tags.pop()
            locked.add(cl)
        else:
            split[cl] = "dev" if is_dev(members[cl]) else "test"

    def share(bucket: str) -> float:
        cs = [c for c in cases if runner.bucket_of(c) == bucket]
        return sum(1 for c in cs if split[clusters[runner.case_id(c)]] == "test") / len(cs)

    moved = True
    while moved:
        moved = False
        for bucket in sorted({runner.bucket_of(c) for c in cases}):
            while share(bucket) < MIN_TEST_SHARE:
                cand = sorted({clusters[runner.case_id(c)] for c in cases if runner.bucket_of(c) == bucket} - locked,
                              key=lambda cl: (cluster_hash(members[cl]) % 10, cluster_hash(members[cl])), reverse=True)
                cand = [cl for cl in cand if split[cl] == "dev"]
                if not cand:
                    notes.append(f"bucket {bucket}: test share {share(bucket):.0%} is under {MIN_TEST_SHARE:.0%} and no untagged dev cluster is left to move")
                    break
                split[cand[0]] = "test"
                moved = True
                notes.append(f"bucket {bucket}: moved cluster {cand[0]} ({len(by_cluster[cand[0]])} cases) to test to reach {MIN_TEST_SHARE:.0%}")
    return {runner.case_id(c): split[clusters[runner.case_id(c)]] for c in cases}, notes


def verify(cases: list[dict], families: list[list[str]] | None, splits: dict[str, str]) -> dict:
    """Counts per split and per bucket, cluster counts, straddling clusters and buckets under the test floor."""
    clusters = case_clusters(cases, families)
    rep: dict = {"splits": {}, "buckets": {}, "straddling": [], "under_floor": [], "untagged": 0}
    seen: dict[str, set[str]] = {}
    for c in cases:
        qid = runner.case_id(c)
        s = splits.get(qid)
        if s not in ("dev", "test"):
            rep["untagged"] += 1
            continue
        seen.setdefault(clusters[qid], set()).add(s)
        row = rep["splits"].setdefault(s, {"cases": 0, "clusters": set()})
        row["cases"] += 1
        row["clusters"].add(clusters[qid])
        b = rep["buckets"].setdefault(runner.bucket_of(c), {"dev": 0, "test": 0, "dev_clusters": set(), "test_clusters": set()})
        b[s] += 1
        b[f"{s}_clusters"].add(clusters[qid])
    rep["straddling"] = sorted(cl for cl, ss in seen.items() if len(ss) > 1)
    for row in rep["splits"].values():
        row["clusters"] = len(row["clusters"])
    for name, b in rep["buckets"].items():
        b["dev_clusters"], b["test_clusters"] = len(b["dev_clusters"]), len(b["test_clusters"])
        n = b["dev"] + b["test"]
        b["test_share"] = round(b["test"] / n, 3) if n else 0.0
        if b["test_share"] < MIN_TEST_SHARE:
            rep["under_floor"].append(name)
    return rep


def print_report(rep: dict) -> None:
    for s in ("dev", "test"):
        row = rep["splits"].get(s, {"cases": 0, "clusters": 0})
        print(f"  {s:<5} {row['cases']:>4} cases in {row['clusters']:>3} clusters")
    print(f"\n  {'bucket':<10} {'dev':>4} {'test':>5} {'test share':>11} {'dev cl':>7} {'test cl':>8}")
    for name in sorted(rep["buckets"]):
        b = rep["buckets"][name]
        print(f"  {name:<10} {b['dev']:>4} {b['test']:>5} {b['test_share']:>11.1%} {b['dev_clusters']:>7} {b['test_clusters']:>8}")
    print(f"\n  straddling clusters: {len(rep['straddling'])}" + (f" ({', '.join(rep['straddling'])})" if rep["straddling"] else ""))
    print(f"  buckets under the {MIN_TEST_SHARE:.0%} test floor: {', '.join(rep['under_floor']) or 'none'}")
    if rep["untagged"]:
        print(f"  untagged cases: {rep['untagged']}")


def write_tags(path: Path, cases: list[dict], splits: dict[str, str]) -> int:
    """Write `split:` into each case of golden.yaml in place, as text, so comments and layout survive. Returns the number of lines changed.

    A case block starts at a `  - query:` line. Its split goes on the line after its `bucket:` line, or replaces an existing `split:` line. Each block's query is parsed and checked against the case it is matched with."""
    import yaml

    lines = path.read_text(encoding="utf-8").split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith("  - query: ")]
    if len(starts) != len(cases):
        raise ValueError(f"{len(starts)} case blocks in the text and {len(cases)} cases in the YAML: the layout is not one block per case")
    bounds = list(zip(starts, starts[1:] + [len(lines)]))
    edits: list[tuple[int, str, bool]] = []  # (line index, text, replace)
    for (lo, hi), case in zip(bounds, cases):
        if yaml.safe_load(lines[lo][4:])["query"] != case["query"]:
            raise ValueError(f"case block at line {lo + 1} does not hold {case['query']!r}")
        tag = f"    split: {splits[runner.case_id(case)]}"
        block = range(lo + 1, hi)
        at = next((i for i in block if lines[i].startswith("    split:")), None)
        if at is not None:
            if lines[at] != tag:
                edits.append((at, tag, True))
            continue
        bucket_line = next((i for i in block if lines[i].startswith("    bucket:")), None)
        if bucket_line is None:
            raise ValueError(f"case {case['query']!r} has no bucket line to place its split after")
        edits.append((bucket_line + 1, tag, False))
    for i, text, replace in sorted(edits, reverse=True):
        if replace:
            lines[i] = text
        else:
            lines.insert(i, text)
    if edits:
        runner.atomic_write_text(path, "\n".join(lines))
    return len(edits)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--golden", type=Path, default=runner.GOLDEN)
    ap.add_argument("--reassign", action="store_true", help="recompute every tag, including cases that already carry one")
    ap.add_argument("--check", action="store_true", help="print the check of the current tags and write nothing")
    args = ap.parse_args(argv)
    import yaml

    data = yaml.safe_load(args.golden.read_text(encoding="utf-8"))
    cases, families = data["cases"], [list(f) for f in (data.get("families") or [])]
    if args.check:
        splits = {runner.case_id(c): c.get("split") for c in cases}
        notes: list[str] = []
    else:
        try:
            splits, notes = assign(cases, families, reassign=args.reassign)
        except ValueError as e:
            print(f"Refusing: {e}")
            return 1
        changed = write_tags(args.golden, cases, splits)
        print(f"{args.golden.name}: {changed} split lines written")
        data = yaml.safe_load(args.golden.read_text(encoding="utf-8"))
        written = {runner.case_id(c): c.get("split") for c in data["cases"]}
        if written != splits:
            print("The file does not hold the assigned splits after the write.")
            return 1
    for n in notes:
        print(f"  {n}")
    rep = verify(cases, families, splits)
    print_report(rep)
    return 1 if rep["straddling"] or rep["under_floor"] or rep["untagged"] else 0


if __name__ == "__main__":
    sys.exit(main())
