#!/usr/bin/env python3
"""Replay captured live queries against the current index — gbrain's label-free
regression trio (Jaccard@k, top-1 stability) over real traffic, complementing the
labeled golden set (knowledge/gbrain.md ^transfer-list item 3, final residual).

The capture log is the retrieval-hit sidecar the MCP server already writes: each
line's `slugs` field records what search returned WHEN THE QUERY ACTUALLY RAN,
which makes the log itself the baseline — no separate capture step, no labels.

    python3 evals/retrieval/replay_captured.py                # per-node log
    python3 evals/retrieval/replay_captured.py --fleet        # aggregated log
    python3 evals/retrieval/replay_captured.py --min-n 20     # refuse below n

Report-only by design (gbrain shipped their replay report-only too): live traffic
drifts for legitimate reasons — corpus growth changes results without anything
regressing — so this belongs in weekly-review eyes, not a hard CI gate. The
labeled golden set + floors are the gates.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "tools"))


def load_log(fleet: bool) -> list[dict]:
    name = "retrieval-hits-fleet.jsonl" if fleet else "retrieval-hits.jsonl"
    path = HOME / ".claude" / "gestalt" / name
    if not path.exists():
        sys.exit(f"no capture log at {path}" + (" — run tools/gestalt-hit-aggregate.sh first" if fleet else ""))
    rows = []
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
            if d.get("q") and d.get("slugs"):
                rows.append(d)
        except Exception:
            pass
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fleet", action="store_true")
    ap.add_argument("--min-n", type=int, default=10, help="refuse to report below this many distinct queries")
    args = ap.parse_args()

    import importlib.util
    spec = importlib.util.spec_from_file_location("gms", REPO / "tools" / "gestalt-mcp-server.py")
    gms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gms)

    rows = load_log(args.fleet)
    # newest capture per distinct query wins (the freshest "what it returned then")
    by_q: dict[str, dict] = {}
    for r in sorted(rows, key=lambda d: d.get("ts", 0)):
        by_q[r["q"]] = r
    if len(by_q) < args.min_n:
        sys.exit(f"only {len(by_q)} distinct captured queries (< --min-n {args.min_n}) — "
                 "not enough traffic for a meaningful replay; refusing a noise report")

    jaccards, top1_stable, changed = [], 0, []
    for q, r in by_q.items():
        then = list(dict.fromkeys(r["slugs"]))
        now_rows = gms.gestalt_search_fts(q, limit=max(len(then), 5))
        now = list(dict.fromkeys(x["slug"] for x in now_rows if x.get("slug")))
        a, b = set(then), set(now)
        j = len(a & b) / len(a | b) if (a | b) else 1.0
        jaccards.append(j)
        if then and now and then[0] == now[0]:
            top1_stable += 1
        elif then:
            changed.append((q, then[0], now[0] if now else "—", round(j, 2)))

    n = len(by_q)
    print(f"replayed {n} distinct captured queries ({'fleet' if args.fleet else 'this node'} log, {len(rows)} raw rows)")
    print(f"  mean Jaccard(result sets) : {sum(jaccards)/n:.3f}")
    print(f"  top-1 stability           : {top1_stable}/{n} ({top1_stable/n:.0%})")
    if changed:
        print("  top-1 changes (query | then -> now | jaccard):")
        for q, t, nw, j in changed[:15]:
            print(f"    {q[:50]!r} | {t} -> {nw} | {j}")
    drift = Counter(s for _, t, nw, _ in changed for s in (t, nw))
    if drift:
        print(f"  slugs most involved in drift: {dict(drift.most_common(5))}")


if __name__ == "__main__":
    main()
