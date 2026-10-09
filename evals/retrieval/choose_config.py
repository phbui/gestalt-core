#!/usr/bin/env python3
"""Choose a retrieval configuration on the dev split, then report it once on the test split.

    python3 evals/retrieval/choose_config.py choose --candidates A0=a0.json A2=a2.json F1=f1.json --baseline-name A0 --out decision.json
    python3 evals/retrieval/choose_config.py report-test --decision decision.json --chosen A2 --baseline-name A0

The inputs are runner outputs saved with --json (run_retrieval_evals.py). The tool reads their per-case rows, so no model
runs. Rows are matched to golden.yaml by case id, or by query and target for files written before rows carried an id.
Every file must hold every case of the split it is read on.

`choose` reads dev rows only and writes the decision. `report-test` reads test rows only, for the chosen configuration
and the baseline, and writes test-report.json once. A second report for another configuration is refused unless
--force, and a forced report is logged in the file. That is how the test split stays unspent.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import assign_splits
import run_retrieval_evals as runner

# The decision rule. It is fixed before any candidate is read, and every decision prints it.
DECISION_RULE = (
    "On dev cases only, against the named baseline, with a paired cluster bootstrap (10000 resamples, seed 12345, "
    "clusters = connected components of accepted slugs, each abstention case its own cluster). A candidate is ELIGIBLE "
    "when all of these hold: (1) recall@3 improves and the lower bound of the bootstrap 90 percent interval of the "
    "change is above zero; (2) block precision does not drop by more than 3 points (point estimate); (3) the abstention "
    "AUROC does not fall (point estimate, and both AUROCs must exist); (4) the run did not record a rerank fallback. "
    "Among eligible candidates the one with the highest dev recall@3 wins. Ties go to the higher MRR, then to the "
    "simpler configuration (fewest enabled knobs), then to the name in sort order. With no eligible candidate the "
    "baseline stays."
)
BLOCK_TOLERANCE = 0.03
METRICS = ("recall@1", "recall@3", "mrr", "block_precision", "auroc")
DEV_NOTE = "Every number in this decision comes from the dev split. The test split was not read."


from heldout import sha256_file


class Golden:
    """The golden cases with their ids, splits and bootstrap clusters."""

    def __init__(self, path: Path):
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        self.cases = data["cases"]
        self.families = [list(f) for f in (data.get("families") or [])]
        self.sha256 = runner.golden_sha256(self.cases, self.families)
        self.clusters = assign_splits.case_clusters(self.cases, self.families)
        self.by_id = {runner.case_id(c): c for c in self.cases}
        self.by_text = {(c["query"], None if runner.is_abstain(c) else c["expect_slug"]): runner.case_id(c) for c in self.cases}
        untagged = [c["query"] for c in self.cases if c.get("split") not in ("dev", "test")]
        if untagged:
            raise SystemExit(f"{len(untagged)} golden cases carry no split. Run evals/retrieval/assign_splits.py first.")

    def ids(self, split: str) -> set[str]:
        return {q for q, c in self.by_id.items() if c["split"] == split}


def load_rows(path: Path, golden: Golden, split: str) -> tuple[dict, dict[str, dict]]:
    """The run's summary and its rows of one split, keyed by case id. Refuses a file that does not hold exactly the golden cases."""
    run = json.loads(Path(path).read_text())
    rows: dict[str, dict] = {}
    unknown = 0
    for r in run.get("results", []):
        qid = r.get("qid") if r.get("qid") in golden.by_id else golden.by_text.get((r["query"], r.get("expect_slug")))
        if qid is None:
            unknown += 1
            continue
        rows[qid] = {**r, "qid": qid, "cluster": golden.clusters[qid], "abstain": runner.is_abstain(golden.by_id[qid])}
    want = golden.ids(split)
    missing = want - set(rows)
    if unknown or missing:
        raise SystemExit(f"{path}: {unknown} rows match no golden case and {len(missing)} {split} cases have no row. "
                         "The file was written against another golden set. Rerun it.")
    return run.get("summary") or {}, {q: rows[q] for q in sorted(want)}


def score_kind(summary: dict) -> str | None:
    """bm25 for an fts run, whose top scores do not compare across queries. Otherwise the rows say."""
    return "bm25" if summary.get("mode") == "fts" else None


def point_metrics(rows: list[dict], kind: str | None = None) -> dict:
    """recall@1, recall@3, MRR, block precision and the abstention AUROC over these rows, unrounded."""
    ans = [r for r in rows if not r["abstain"]]
    blocks = [r for r in ans if r.get("expect_block")]
    ab = abstain_auroc(rows, kind)
    return {
        "n": len(ans), "n_abstain": len(rows) - len(ans), "n_block": len(blocks),
        "recall@1": runner.recall_at(ans, 1), "recall@3": runner.recall_at(ans, 3), "mrr": runner.mrr(ans),
        "block_precision": sum(1 for r in blocks if r.get("block_hit")) / len(blocks) if blocks else None,
        "auroc": ab[0], "auroc_note": ab[1],
    }


def abstain_auroc(rows: list[dict], kind: str | None) -> tuple[float | None, str | None]:
    ab = [r for r in rows if r["abstain"]]
    ans = [r for r in rows if not r["abstain"]]
    if not ab or not ans:
        return None, "no abstention or no answerable cases"
    why = runner.auroc_unusable(rows, kind)
    if why:
        return None, why
    return runner.gestalt_rank.auroc([runner._top(r) for r in ans], [runner._top(r) for r in ab]), None


def _draws(n_clusters: int, iterations: int, seed: int):
    """Multiplicity of each cluster in each resample, drawn the way runner.cluster_bootstrap draws."""
    import numpy as np

    rng = random.Random(seed)
    m = np.zeros((iterations, n_clusters))
    for i in range(iterations):
        for _ in range(n_clusters):
            m[i, rng.randrange(n_clusters)] += 1
    return m


def _cluster_sums(rows: dict[str, dict], keys: list[str]):
    """Per cluster: answerable count, recall@1 hits, recall@3 hits, reciprocal-rank sum, block cases, block hits."""
    import numpy as np

    idx = {k: i for i, k in enumerate(keys)}
    s = np.zeros((6, len(keys)))
    for r in rows.values():
        if r["abstain"]:
            continue
        i, rank = idx[r["cluster"]], r.get("rank")
        s[0, i] += 1
        s[1, i] += bool(rank and rank <= 1)
        s[2, i] += bool(rank and rank <= 3)
        s[3, i] += 1.0 / rank if rank else 0.0
        if r.get("expect_block"):
            s[4, i] += 1
            s[5, i] += bool(r.get("block_hit"))
    return s


def _auroc_parts(rows: dict[str, dict], keys: list[str]):
    """Pair wins between each cluster's answerable rows and each abstention cluster, ties counting half, and answerable counts."""
    import numpy as np

    idx = {k: i for i, k in enumerate(keys)}
    neg = [(idx[r["cluster"]], runner._top(r)) for r in rows.values() if r["abstain"]]
    wins = np.zeros((len(keys), len(keys)))
    npos = np.zeros(len(keys))
    for r in rows.values():
        if r["abstain"]:
            continue
        c, p = idx[r["cluster"]], runner._top(r)
        npos[c] += 1
        for j, n in neg:
            wins[c, j] += 1.0 if p > n else 0.5 if p == n else 0.0
    is_neg = np.zeros(len(keys))
    for j, _ in neg:
        is_neg[j] = 1
    return wins, npos, is_neg


def paired_bootstrap(cand: dict[str, dict], base: dict[str, dict], iterations: int = runner.BOOTSTRAP_ITERATIONS,
                     seed: int = runner.BOOTSTRAP_SEED, with_auroc: bool = True) -> dict[str, list[float] | None]:
    """90 percent interval of (candidate minus baseline) for each metric, resampling whole clusters with one draw shared by every metric."""
    import numpy as np

    keys = sorted({r["cluster"] for r in base.values()})
    m = _draws(len(keys), iterations, seed)
    sc, sb = _cluster_sums(cand, keys), _cluster_sums(base, keys)
    out: dict[str, list[float] | None] = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        for name, num, den in (("recall@1", 1, 0), ("recall@3", 2, 0), ("mrr", 3, 0), ("block_precision", 5, 4)):
            d = m @ sb[den]
            delta = (m @ sc[num] - m @ sb[num]) / d
            vals = [float(v) for v in delta[d > 0]]
            out[name] = [round(x, 4) for x in runner.ci90(vals)] if vals else None
        out["auroc"] = None
        if with_auroc:
            per_run = []
            for rows in (cand, base):
                wins, npos, is_neg = _auroc_parts(rows, keys)
                den = (m @ npos) * (m @ is_neg)
                per_run.append((((m @ wins) * m).sum(1), den))
            (wc, den), (wb, _) = per_run
            ok = den > 0
            vals = [float(v) for v in ((wc - wb) / den)[ok]]
            out["auroc"] = [round(x, 4) for x in runner.ci90(vals)] if vals else None
    return out


def enabled_knobs(summary: dict, base_summary: dict) -> list[str]:
    """The knobs a run turns on, relative to the off defaults and to the baseline's embedder and text format."""
    cfg, bcfg = summary.get("config") or {}, base_summary.get("config") or {}
    on = []
    if cfg.get("rerank") == "on":
        on.append("rerank")
    if (cfg.get("dedup") if cfg.get("dedup") is not None else 1.0) < 1.0:
        on.append("dedup")
    if cfg.get("stopwords"):
        on.append("stopwords")
    if cfg.get("fusion", "rrf") != "rrf":
        on.append("fusion")
    for k in ("embed_profile", "text_format"):
        if cfg.get(k) != bcfg.get(k):
            on.append(k)
    if (summary.get("embed") or {}).get("model") != (base_summary.get("embed") or {}).get("model"):
        on.append("embed_model")
    return on


def evaluate_candidates(paths: dict[str, Path], baseline_name: str, golden: Golden, split: str,
                        iterations: int = runner.BOOTSTRAP_ITERATIONS, seed: int = runner.BOOTSTRAP_SEED) -> dict:
    """Metrics, deltas against the baseline, intervals, knobs and the eligibility verdict for every candidate, on one split."""
    if baseline_name not in paths:
        raise SystemExit(f"--baseline-name {baseline_name} is not one of the candidates ({', '.join(sorted(paths))})")
    loaded = {name: load_rows(p, golden, split) for name, p in paths.items()}
    base_summary, base_rows = loaded[baseline_name]
    base_m = point_metrics(list(base_rows.values()), score_kind(base_summary))
    table = {}
    for name, (summary, rows) in loaded.items():
        met = point_metrics(list(rows.values()), score_kind(summary))
        entry = {"metrics": _rounded(met), "knobs": enabled_knobs(summary, base_summary),
                 "rerank_fallbacks": summary.get("rerank_fallbacks")}
        if name != baseline_name:
            delta = {k: (met[k] - base_m[k]) if met[k] is not None and base_m[k] is not None else None for k in METRICS}
            entry["delta"] = _rounded(delta)
            entry["ci90"] = paired_bootstrap(rows, base_rows, iterations, seed,
                                             with_auroc=met["auroc"] is not None and base_m["auroc"] is not None)
            entry["eligible"], entry["reasons"] = eligibility(entry, delta, summary)
        table[name] = entry
    return table


def _rounded(d: dict) -> dict:
    return {k: round(v, 4) if isinstance(v, float) else v for k, v in d.items()}


def eligibility(entry: dict, delta: dict, summary: dict) -> tuple[bool, list[str]]:
    """The decision rule's four conditions. Returns the verdict and, when ineligible, every condition that failed."""
    why = []
    lo = (entry["ci90"].get("recall@3") or [None])[0]
    if delta["recall@3"] is None or delta["recall@3"] <= 0 or lo is None or lo <= 0:
        why.append(f"recall@3 change {delta['recall@3']:+.4f} with 90% lower bound {lo} is not above zero" if lo is not None
                   else "recall@3 has no bootstrap interval")
    if delta["block_precision"] is not None and delta["block_precision"] < -BLOCK_TOLERANCE - 1e-12:
        why.append(f"block precision drops {-delta['block_precision']:.4f}, more than {BLOCK_TOLERANCE}")
    if delta["auroc"] is None:
        why.append("abstention AUROC is unavailable for the candidate or the baseline")
    elif delta["auroc"] < -1e-12:
        why.append(f"abstention AUROC falls by {-delta['auroc']:.4f}")
    if summary.get("rerank_fallbacks"):
        why.append(f"the rerank fell back on {summary['rerank_fallbacks']} queries")
    return not why, why


def choose(table: dict, baseline_name: str) -> str:
    eligible = [n for n, e in table.items() if e.get("eligible")]
    if not eligible:
        return baseline_name
    return min(eligible, key=lambda n: (-table[n]["metrics"]["recall@3"], -table[n]["metrics"]["mrr"], len(table[n]["knobs"]), n))


def code_hashes() -> dict[str, str]:
    return {p.name: sha256_file(p) for p in (Path(__file__).resolve(), HERE / "run_retrieval_evals.py", HERE / "assign_splits.py")}


def print_table(table: dict, baseline_name: str, split: str) -> None:
    print(f"\n  {split} split, deltas against {baseline_name}, 90% paired cluster-bootstrap interval in brackets\n")
    print(f"  {'name':<24} {'r@1':>7} {'r@3':>7} {'MRR':>7} {'block':>7} {'AUROC':>7}  verdict")
    for name in sorted(table, key=lambda n: (n != baseline_name, n)):
        e = table[name]
        m = e["metrics"]
        cells = " ".join(f"{m[k]:>7.3f}" if m[k] is not None else f"{'n/a':>7}" for k in METRICS)
        verdict = "baseline" if name == baseline_name else {True: "ELIGIBLE", False: "ineligible"}.get(e.get("eligible"), "")
        print(f"  {name:<24} {cells}  {verdict}")
        if name != baseline_name:
            d, ci = e["delta"], e["ci90"]
            parts = [f"{k} {d[k]:+.3f}" + (f" [{ci[k][0]:+.3f}, {ci[k][1]:+.3f}]" if ci.get(k) else "")
                     for k in METRICS if d[k] is not None]
            print(f"  {'':<24} {'; '.join(parts)}")
            for r in e.get("reasons", []):
                print(f"  {'':<24} - {r}")
        if e["knobs"]:
            print(f"  {'':<24} knobs: {', '.join(e['knobs'])}")


def cmd_choose(args) -> int:
    golden = Golden(args.golden)
    paths = dict(parse_candidates(args.candidates))
    table = evaluate_candidates(paths, args.baseline_name, golden, "dev", args.iterations, args.seed)
    chosen = choose(table, args.baseline_name)
    print("Decision rule (fixed before reading the candidates):\n  " + DECISION_RULE)
    print_table(table, args.baseline_name, "dev")
    print(f"\n  CHOSEN: {chosen}" + (" (no candidate was eligible, the baseline stays)" if chosen == args.baseline_name else ""))
    print(f"  {DEV_NOTE}")
    decision = {
        "chosen": chosen, "baseline": args.baseline_name, "rule": DECISION_RULE, "note": DEV_NOTE, "split": "dev",
        "n_cases": len(golden.ids("dev")), "n_clusters": len({golden.clusters[q] for q in golden.ids("dev")}),
        "iterations": args.iterations, "seed": args.seed, "golden_sha256": golden.sha256, "code_sha256": code_hashes(),
        "candidates": {n: {"path": str(Path(p).resolve()), "sha256": sha256_file(p)} for n, p in paths.items()},
        "table": table,
    }
    runner.atomic_write_text(args.out, json.dumps(decision, indent=2))
    print(f"Wrote {args.out}")
    return 0


LEDGER_NAME = "test-ledger.json"  # one entry per golden_sha256, beside the decision file


def cmd_report_test(args) -> int:
    decision_path = Path(args.decision)
    if not decision_path.exists():
        print(f"Refusing: no decision at {decision_path}. Run `choose` on dev first. Test is read only for a decided configuration.")
        return 1
    decision = json.loads(decision_path.read_text())
    out_path = Path(args.out) if args.out else decision_path.with_name("test-report.json")
    if args.baseline_name != decision["baseline"]:
        print(f"Refusing: the decision was made against {decision['baseline']}, not {args.baseline_name}.")
        return 1
    if args.chosen != decision["chosen"] and not args.force:
        print(f"Refusing: the decision chose {decision['chosen']}, not {args.chosen}. Reporting another configuration on test "
              "would make test a second dev split. Pass --force to override, and the override is logged.")
        return 1
    golden = Golden(args.golden)
    if golden.sha256 != decision["golden_sha256"]:
        print("Refusing: golden.yaml changed since the decision. Rerun `choose` on dev.")
        return 1
    paths = {}
    for name in {args.chosen, args.baseline_name}:
        meta = decision["candidates"].get(name)
        if meta is None:
            print(f"Refusing: {name} was not a candidate of the decision.")
            return 1
        if sha256_file(meta["path"]) != meta["sha256"]:
            print(f"Refusing: {meta['path']} changed since the decision.")
            return 1
        paths[name] = Path(meta["path"])
    # The ledger sits beside the decision and is keyed by golden_sha256, so a different --out cannot reopen the test side.
    ledger_path = decision_path.with_name(LEDGER_NAME)
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    previous = ledger.get(golden.sha256)
    if previous is None and out_path.exists():
        previous = json.loads(out_path.read_text())  # a report written before the ledger existed
    if previous and previous["chosen"] == args.chosen:
        print(f"Test was already reported for {args.chosen} ({previous.get('report', out_path)}). It is reported once:")
        print_table(previous["table"], previous["baseline"], "test")
        return 0
    if previous and not args.force:
        print(f"Refusing: test was already reported for {previous['chosen']} ({previous.get('report', out_path)}). A second configuration on test "
              "spends the split. Pass --force to override, and the override is logged.")
        return 1
    table = evaluate_candidates(paths, args.baseline_name, golden, "test", decision["iterations"], decision["seed"])
    for e in table.values():  # eligibility is a dev decision. On test it is not applied
        e.pop("eligible", None)
        e.pop("reasons", None)
    print_table(table, args.baseline_name, "test")
    report = {
        "chosen": args.chosen, "baseline": args.baseline_name, "split": "test", "decision": str(decision_path.resolve()),
        "decision_sha256": sha256_file(decision_path), "golden_sha256": golden.sha256, "code_sha256": code_hashes(),
        "n_cases": len(golden.ids("test")), "table": table,
        "force_log": (previous or {}).get("force_log", []),
    }
    if args.force and (previous or args.chosen != decision["chosen"]):
        report["force_log"].append({"at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                                    "chosen": args.chosen, "decision_chose": decision["chosen"],
                                    "previously_reported": previous["chosen"] if previous else None})
    runner.atomic_write_text(out_path, json.dumps(report, indent=2))
    ledger[golden.sha256] = {**report, "report": str(out_path.resolve())}
    runner.atomic_write_text(ledger_path, json.dumps(ledger, indent=2))
    print(f"Wrote {out_path} and the ledger {ledger_path}")
    return 0


def parse_candidates(items: list[str]) -> list[tuple[str, Path]]:
    out = []
    for item in items:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise SystemExit(f"--candidates takes NAME=path, got {item!r}")
        out.append((name, Path(path)))
    if len({n for n, _ in out}) != len(out):
        raise SystemExit("candidate names must be unique")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--golden", type=Path, default=runner.GOLDEN)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("choose", help="pick a configuration on dev and write the decision")
    c.add_argument("--candidates", nargs="+", required=True, metavar="NAME=PATH")
    c.add_argument("--baseline-name", required=True)
    c.add_argument("--out", type=Path, default=Path("decision.json"))
    c.add_argument("--iterations", type=int, default=runner.BOOTSTRAP_ITERATIONS)
    c.add_argument("--seed", type=int, default=runner.BOOTSTRAP_SEED)
    r = sub.add_parser("report-test", help="report the decided configuration and the baseline on test, once")
    r.add_argument("--decision", type=Path, default=Path("decision.json"))
    r.add_argument("--chosen", required=True)
    r.add_argument("--baseline-name", required=True)
    r.add_argument("--out", type=Path, default=None, help="default: test-report.json next to the decision")
    r.add_argument("--force", action="store_true", help="report a configuration other than the decided one, or a second one. Logged in the report")
    args = ap.parse_args(argv)
    return cmd_choose(args) if args.cmd == "choose" else cmd_report_test(args)


if __name__ == "__main__":
    sys.exit(main())
