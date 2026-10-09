#!/usr/bin/env python3
"""Pool the per-scope results of one LongMemEval-S or LoCoMo-10 run into the result of the whole run.

usage: python3 evals/memory/merge_shards.py --out DIR SOURCE [SOURCE ...]

A SOURCE is a run directory (it holds `scopes/`) or a scope directory (it holds the scope files). Sharded runs give each shard a directory of its own,
or point every shard at one `--scope-dir`. Either way the merge reads the scope files and checks, before it pools anything, that

  - every scope file carries the same fingerprint: benchmark, dataset sha256, systems, granularity, depth, chunking, rerank setting, fusion mode
    and alpha, embed profile, code hashes, seed and bootstrap count,
  - no file comes from a smoke run,
  - the scope files are exactly the scopes of the dataset, each with exactly the dataset's question ids. A missing, extra or repeated scope or question is an
    error that names it. The dataset is the file whose sha256 the fingerprint holds, found through --data, the path a scope file recorded, or the cache.

Run directories that also hold a summary.json get one more check: the summary must agree with the fingerprint and with the dataset's counts.

The pooled summary is built by the same code as an unsharded run, from the same exact per-question scores. Each source's environment and the sha256 of its summary
are kept under `merged_from`. When the reranker fell back on any query in any source, the pooled `hybrid_rerank` rows are void and the exit code is 3.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

BENCH_MODULES = {"longmemeval_s": "longmemeval_bench", "locomo10": "locomo_bench"}


def _scope_dir(src: Path) -> Path:
    if (src / "scopes").is_dir():
        return src / "scopes"
    if src.is_dir() and any(src.glob("*.json")):
        return src
    raise SystemExit(f"{src} has no scopes/ directory and no scope files. Was it run with this version of the benchmark?")


def _load_sources(sources: list[Path]) -> tuple[list[dict], list[dict]]:
    """Every scope file of every source, and one record per source. An unreadable scope file is an error here, because a merge must not skip a scope."""
    files, records = [], []
    for src in sources:
        sdir = _scope_dir(src)
        found = []
        for p in sorted(sdir.glob("*.json")):
            if p.name == "summary.json":
                continue
            d = C.read_scope_file(p)
            if d is None:
                raise SystemExit(f"{p} is not a readable scope file. Recompute that scope with the benchmark script, then merge again.")
            d["_path"] = str(p)
            C.log_line(f"merging {p}: scope {d['scope']}, {len(d['qids'])} questions")
            found.append(d)
        summary_path = src / "summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else None
        records.append({"dir": str(src), "scopes": len(found), "summary": summary, "summary_sha256": C.sha256_file(summary_path) if summary else None,
                        "first_meta": (found[0].get("meta") if found else {}) or {}})
        files += found
    return files, records


def _find_dataset(fp: dict, files: list[dict], data: Path | None) -> Path:
    name = fp["benchmark"]
    candidates = [data] if data else []
    candidates += [Path(f["meta"]["dataset_path"]) for f in files if f.get("meta", {}).get("dataset_path")]
    candidates.append(C.CACHE_DIR / C.DATASETS[name]["file"])
    for c in candidates:
        if c and Path(c).exists() and C.sha256_file(Path(c)) == fp["dataset_sha256"]:
            return Path(c)
    raise SystemExit(f"cannot find the dataset with sha256 {fp['dataset_sha256']}. Pass it with --data.")


def merge(sources: list[Path], out: Path, data: Path | None = None, include_adversarial: bool = False) -> dict:
    """Pool the per-scope result files of every shard into one summary, refusing a shard whose fingerprint differs from the first."""
    files, records = _load_sources([Path(s) for s in sources])
    if not files:
        raise SystemExit("no scope files found")
    fp = files[0]["fingerprint"]
    for f in files[1:]:
        diff = C.fingerprint_diff(f["fingerprint"], fp)
        if diff:
            raise SystemExit(f"{f['_path']} differs from {files[0]['_path']} in {', '.join(diff)}")
    if fp["smoke_subset"]:
        raise SystemExit("these scope files come from a smoke run (smoke_subset is true). A smoke run is never pooled into a result.")
    if fp["benchmark"] not in BENCH_MODULES:
        raise SystemExit(f"unknown benchmark {fp['benchmark']!r}")
    bench = importlib.import_module(BENCH_MODULES[fp["benchmark"]])

    by_scope: dict[str, dict] = {}
    for f in files:
        if f["scope"] in by_scope:
            raise SystemExit(f"scope {f['scope']} appears twice: {by_scope[f['scope']]['_path']} and {f['_path']}")
        by_scope[f["scope"]] = f

    dataset = _find_dataset(fp, files, data)
    info = C.dataset_record(dataset, fp["benchmark"])
    qs, _, counts = bench.load_questions(dataset)
    expected: dict[str, list[str]] = {}
    for q in qs:
        expected.setdefault(q["scope"], []).append(q["qid"])
    missing = [s for s in expected if s not in by_scope]
    extra = [s for s in by_scope if s not in expected]
    if missing or extra:
        raise SystemExit("the scope files are not the dataset's scopes. " + (f"missing: {', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}. " if missing else "")
                         + (f"extra: {', '.join(extra[:10])}{' ...' if len(extra) > 10 else ''}." if extra else ""))
    for scope, qids in expected.items():
        have = by_scope[scope]["qids"]
        if have != qids:
            gone, added = sorted(set(qids) - set(have)), sorted(set(have) - set(qids))
            raise SystemExit(f"scope {scope} does not hold the dataset's questions. missing: {gone[:5]}. extra: {added[:5]}.")

    for rec in records:
        s = rec["summary"]
        if s is None:
            continue
        label = rec["dir"]
        env = s.get("environment", {})
        for what, got, want in (("smoke_subset", s.get("smoke_subset"), False), ("dataset.sha256", s["dataset"]["sha256"], fp["dataset_sha256"]),
                                ("config.seed", s["config"].get("seed"), fp["seed"]), ("config.bootstrap", s["config"].get("bootstrap"), fp["bootstrap"]),
                                ("config.fusion", s["config"].get("fusion"), fp["fusion"]), ("config.alpha", s["config"].get("alpha"), fp["alpha"]),
                                ("config.systems", s["config"].get("systems"), fp["systems"]), ("config.rerank", s["config"].get("rerank"), fp["rerank"]),
                                ("environment.embed_profile", env.get("embed_profile"), fp["embed_profile"]), ("environment.code_sha256", env.get("code_sha256"), fp["code_sha256"]),
                                ("counts", s.get("counts"), counts)):
            if got != want:
                raise SystemExit(f"{label}/summary.json differs from the scope files in {what}")
        if fp["benchmark"] == "locomo10" and s["config"].get("include_adversarial", False) != include_adversarial:
            raise SystemExit(f"{label}/summary.json differs from this merge in config.include_adversarial. Pass --include-adversarial to match the shards, or omit it if they omitted it.")

    levels = C.levels_for(fp["granularity"])
    res = C.assemble([by_scope[s] for s in expected], fp["systems"], levels)
    env = {"merged": True, "embed_profile": fp["embed_profile"], "code_sha256": fp["code_sha256"]}
    kwargs = {"include_adversarial": include_adversarial} if fp["benchmark"] == "locomo10" else {}
    summary = bench.build_summary(info, counts, res, fp, env, None, **kwargs)
    summary["merged_from"] = [{"dir": r["dir"], "scope_files": r["scopes"], "summary_sha256": r["summary_sha256"], "shard": (r["summary"] or {}).get("shard"),
                               "environment": (r["summary"] or {}).get("environment") or r["first_meta"].get("environment")} for r in records]
    summary["merged_scopes"] = len(expected)
    C.write_outputs(out, summary, res["lines"])
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--data", default=None, help="the dataset JSON, when the scope files do not name a path that exists here")
    ap.add_argument("--include-adversarial", action="store_true", help="LoCoMo: pool category 5 into the `all` row. Must match the shard runs")
    ap.add_argument("sources", nargs="+", metavar="SOURCE")
    args = ap.parse_args(argv)
    s = merge([Path(x) for x in args.sources], Path(args.out), Path(args.data) if args.data else None, args.include_adversarial)
    print(f"{s['benchmark']}  merged {s['merged_scopes']} scopes from {len(s['merged_from'])} sources  sha256 {s['dataset']['sha256']}")
    for lv, body in s["levels"].items():
        print(C.fmt_table(f"level: {lv}", body["systems"], list(body["systems"])))
    return C.exit_code(s)


if __name__ == "__main__":
    raise SystemExit(main())
