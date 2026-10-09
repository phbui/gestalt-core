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

It also reports a read rate from the click feedback the server writes (spec 6, 2026-10-08): the share of
non-hook searches after which the model opened one of the top-k entries with gestalt_read. A read row
joins its search by session id, or by the query the server remembered when the entry was opened.

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


def _log_path(fleet: bool) -> Path:
    name = "retrieval-hits-fleet.jsonl" if fleet else "retrieval-hits.jsonl"
    return HOME / ".claude" / "gestalt" / name


def _all_rows(fleet: bool) -> list[dict]:
    path = _log_path(fleet)
    if not path.exists():
        sys.exit(f"no capture log at {path}" + (" — run tools/gestalt-hit-aggregate.sh first" if fleet else ""))
    rows = []
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
            if isinstance(d, dict):
                rows.append(d)
        except Exception:
            pass
    return rows


def load_log(fleet: bool) -> list[dict]:
    """Search rows: the ones with a query and result slugs. A read row has neither, so it is skipped here."""
    return [d for d in _all_rows(fleet) if d.get("q") and d.get("slugs")]


def load_reads(fleet: bool) -> list[dict]:
    return [d for d in _all_rows(fleet) if d.get("kind") == "read" and d.get("slug")]


READ_WINDOW_S = 600


def read_rates(searches: list[dict], reads: list[dict], depths=(1, 3, 5), window: int = READ_WINDOW_S) -> dict:
    """Read rate at each depth over per-search events, hook searches excluded.

    A read follows a search when it came within `window` seconds after it and joins by session id when both rows carry one. A row without a session id joins by preceding_q. It counts at depth d when the entry sits in the search's first d slugs. no_read is the share of searches with no matching read of any result."""
    events = [e for e in searches if e.get("src", "mcp") != "hook"]
    hit = {d: 0 for d in depths}
    none = 0
    for e in events:
        ts, slugs = e.get("ts", 0), e.get("slugs") or []
        mine = [r for r in reads
                if 0 <= r.get("ts", 0) - ts <= window and r["slug"] in slugs
                and ((r.get("sid") == e["sid"]) if (e.get("sid") and r.get("sid")) else (r.get("preceding_q") == e.get("q")))]
        if not mine:
            none += 1
            continue
        for d in depths:
            if any(r["slug"] in slugs[:d] for r in mine):
                hit[d] += 1
    return {"n": len(events), "hits": hit, "no_read": none}


def report_read_rate(fleet: bool, min_n: int) -> None:
    sys.path.insert(0, str(REPO / "evals" / "retrieval"))
    from run_retrieval_evals import wilson

    searches, reads = load_log(fleet), load_reads(fleet)
    rr = read_rates(searches, reads)
    if rr["n"] < min_n:
        print(f"read rate: only {rr['n']} non-hook search events (< --min-n {min_n}), {len(reads)} read rows — refusing to report a rate")
        return
    print(f"read rate over {rr['n']} non-hook search events and {len(reads)} read rows (a read within {READ_WINDOW_S} s of the search):")
    for d, k in rr["hits"].items():
        lo, hi = wilson(k, rr["n"])
        print(f"  read-rate@{d}: {k}/{rr['n']} = {k / rr['n']:.1%}  (Wilson 95% {lo:.1%} to {hi:.1%})")
    print(f"  searches with no read of any result: {rr['no_read']}/{rr['n']} = {rr['no_read'] / rr['n']:.1%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fleet", action="store_true")
    ap.add_argument("--min-n", type=int, default=10, help="refuse to report below this many distinct queries")
    args = ap.parse_args()

    import importlib.util
    spec = importlib.util.spec_from_file_location("gms", REPO / "tools" / "gestalt-mcp-server.py")
    gms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gms)

    report_read_rate(args.fleet, args.min_n)

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
