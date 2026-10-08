#!/usr/bin/env python3
"""Extraction-precision audit for the Graphiti graph (X7, 2026-10-06).

Samples random fact edges, prints each beside the text of its source episode, and writes a JSON file
for a person to label. `--score` reads the labelled file and prints precision with a Wilson interval.

    graphiti-audit.py                     # sample 40 facts, write the file under the state directory
    graphiti-audit.py -n 97 --seed 7      # a bigger sample
    graphiti-audit.py --score FILE        # precision of a labelled file

Sample size. The interval half width at 95 percent is about z*sqrt(p(1-p)/n). For a +-10 point interval you
need 35 facts if precision is near 0.9, 73 near 0.75 and 97 in the worst case (p=0.5). The default of 40 is a
first look and gives roughly +-10 points only if precision is high. Plan on 73 to 97 before trusting the
number, and audit again after any extractor model change (RC-ai-research section 3). This tool samples
uniformly. It does not stratify by episode source or edge type.

Labels per row: "correct" (the episode supports the fact), "wrong" (the episode contradicts it or the fact
misstates it), "unsupported" (the episode says nothing that backs it). Precision = correct / labelled rows.

Graph access follows tools/gestalt-graphiti-sync.sh: GESTALT_FALKORDB_CLI (default
`docker exec gestalt-falkordb redis-cli`, so it runs on the hub) and GESTALT_GRAPH (default gestalt).
From a leaf set GESTALT_FALKORDB_CLI="ssh hub docker exec -i gestalt-falkordb redis-cli". Queries are
GRAPH.RO_QUERY only. The tool never writes to the graph.

Privacy. A real sample holds private graph facts. The default output is the state directory
($GESTALT_STATE_DIR or ~/.claude/gestalt). The tool refuses to write inside the repository unless
--allow-repo is given. evals/graphiti/audit-example.json holds a synthetic example of the shape.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LABELS = ("correct", "wrong", "unsupported")
EPISODE_CHARS = 1500
BATCH = 50


class GraphError(RuntimeError):
    pass


def ro_query(cli: list[str], graph: str, q: str) -> list[list]:
    """Run one read-only query. Returns the result rows (the first JSON element is the header)."""
    try:
        r = subprocess.run([*cli, "--json", "GRAPH.RO_QUERY", graph, q], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GraphError(f"graph command failed: {e}")
    if r.returncode != 0:
        raise GraphError(f"graph command exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    try:
        return json.loads(r.stdout)[1]
    except Exception as e:
        raise GraphError(f"unparseable graph reply: {e}")


def sample_facts(cli: list[str], graph: str, n: int, seed: int) -> list[dict]:
    uuids = sorted({row[0] for row in ro_query(cli, graph, "MATCH ()-[r:RELATES_TO]->() RETURN r.uuid") if row and row[0]})
    picked = random.Random(seed).sample(uuids, min(n, len(uuids)))
    facts: list[dict] = []
    for i in range(0, len(picked), BATCH):
        q = ("MATCH (a)-[r:RELATES_TO]->(b) WHERE r.uuid IN %s "
             "RETURN r.uuid, a.name, r.name, b.name, r.fact, r.episodes, r.valid_at, r.invalid_at" % json.dumps(picked[i:i + BATCH]))
        for u, s, rel, o, fact, eps, va, ia in ro_query(cli, graph, q):
            facts.append({"id": u, "subject": s, "relation": rel, "object": o, "fact": fact,
                          "valid_at": va, "invalid_at": ia, "episode_ids": list(eps or [])})
    order = {u: k for k, u in enumerate(picked)}
    facts.sort(key=lambda f: order.get(f["id"], 0))
    ep_ids = sorted({e for f in facts for e in f["episode_ids"]})
    texts: dict[str, dict] = {}
    for i in range(0, len(ep_ids), BATCH):
        q = "MATCH (e:Episodic) WHERE e.uuid IN %s RETURN e.uuid, e.name, e.content" % json.dumps(ep_ids[i:i + BATCH])
        for u, name, content in ro_query(cli, graph, q):
            texts[u] = {"id": u, "name": name, "text": (content or "")[:EPISODE_CHARS]}
    for f in facts:
        f["episodes"] = [texts.get(e, {"id": e, "name": None, "text": None}) for e in f.pop("episode_ids")]
        f["label"] = None
    return facts


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def score(doc: dict) -> dict:
    rows = doc.get("rows", [])
    bad = [r.get("id") for r in rows if r.get("label") not in (None, *LABELS)]
    if bad:
        raise ValueError(f"unknown labels on rows {bad}: use one of {LABELS}")
    labelled = [r for r in rows if r.get("label") in LABELS]
    k = sum(1 for r in labelled if r["label"] == "correct")
    lo, hi = wilson(k, len(labelled))
    return {"rows": len(rows), "labelled": len(labelled), "unlabelled": len(rows) - len(labelled),
            "counts": {l: sum(1 for r in labelled if r["label"] == l) for l in LABELS},
            "precision": round(k / len(labelled), 4) if labelled else None,
            "wilson95": [round(lo, 3), round(hi, 3)]}


def default_out(now: datetime.date | None = None) -> Path:
    base = Path(os.environ.get("GESTALT_STATE_DIR") or Path.home() / ".claude" / "gestalt")
    return base / f"graphiti-audit-{(now or datetime.date.today()).isoformat()}.json"


def show(rows: list[dict]) -> None:
    for i, r in enumerate(rows, 1):
        print(f"[{i}] {r['subject']} --{r['relation']}--> {r['object']}")
        print(f"    FACT: {r['fact']}")
        for e in r["episodes"]:
            print(f"    EPISODE {e.get('name')}: {(e.get('text') or '(episode not found)')[:400]!r}")
        print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=40, help="facts to sample (default 40; 97 gives +-10 points at p=0.5)")
    ap.add_argument("--seed", type=int, default=20261006)
    ap.add_argument("--out", type=Path, help="output file (default: state directory)")
    ap.add_argument("--allow-repo", action="store_true", help="allow writing inside the repository")
    ap.add_argument("--score", type=Path, metavar="FILE", help="print precision of a labelled file and exit")
    a = ap.parse_args(argv)

    if a.score:
        try:
            s = score(json.loads(a.score.read_text()))
        except (OSError, ValueError) as e:
            print(f"cannot score: {e}", file=sys.stderr)
            return 2
        print(json.dumps(s, indent=2))
        if s["precision"] is not None:
            print(f"precision {s['precision']:.1%} on {s['labelled']} labelled facts, 95% Wilson interval "
                  f"{s['wilson95'][0]:.1%} to {s['wilson95'][1]:.1%} ({s['unlabelled']} rows still unlabelled)")
        return 0

    out = (a.out or default_out()).expanduser().resolve()
    if REPO in out.parents and not a.allow_repo:
        print(f"refusing to write real graph facts inside the repository: {out}", file=sys.stderr)
        return 2
    cli = shlex.split(os.environ.get("GESTALT_FALKORDB_CLI", "docker exec gestalt-falkordb redis-cli"))
    graph = os.environ.get("GESTALT_GRAPH", "gestalt")
    if not cli:
        print("GESTALT_FALKORDB_CLI is empty", file=sys.stderr)
        return 2
    try:
        rows = sample_facts(cli, graph, a.n, a.seed)
    except GraphError as e:
        print(f"graph unreachable: {e}", file=sys.stderr)
        return 3
    show(rows)
    doc = {"graph": graph, "seed": a.seed, "n": len(rows), "labels": list(LABELS), "rows": rows}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    os.chmod(out, 0o600)
    print(f"wrote {len(rows)} facts to {out}\nfill in each row's label, then run: graphiti-audit.py --score {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
