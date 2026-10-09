#!/usr/bin/env python3
"""Merge the summary.json files of several beir_bench runs into the one summary the public docs read.

Each dataset needs its own reranker instruction, so the headline datasets come from separate runs and separate --out
directories. This tool joins their summaries when, and only when, they are the same experiment: the same code hashes,
the same environment apart from the reranker instruction, no smoke subset and no reranker fallback. The instruction becomes
a per-dataset map under environment.rerank.instruction. Anything else is a refusal with the field named, exit 2.

    python3 evals/retrieval/merge_summaries.py results/beir-final-scifact/summary.json results/beir-final-nfcorpus/summary.json --out results/summary.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

INSTRUCTION_KEY = "instruction"


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"merge_summaries: cannot read {path}: {e}")


def environment_differences(a: dict, b: dict) -> list[str]:
    """Keys whose values differ, ignoring rerank.instruction. Empty means the two runs are the same experiment."""
    diff = []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if k == "rerank" and isinstance(va, dict) and isinstance(vb, dict):
            va = {x: y for x, y in va.items() if x != INSTRUCTION_KEY}
            vb = {x: y for x, y in vb.items() if x != INSTRUCTION_KEY}
        if va != vb:
            diff.append(k)
    return diff


def merge(summaries: list[tuple[Path, dict]]) -> dict:
    """One summary with every dataset of the inputs. Refuses with SystemExit on any disagreement."""
    if not summaries:
        raise SystemExit("merge_summaries: no inputs")
    first_path, first = summaries[0]
    for path, s in summaries:
        for key in ("environment", "results"):
            if key not in s:
                raise SystemExit(f"merge_summaries: {path} has no '{key}'")
        if s.get("smoke_subset"):
            raise SystemExit(f"merge_summaries: {path} is a smoke subset, never merged into a published summary")
        for name, r in s["results"].items():
            if r.get("rerank_fallback_queries"):
                raise SystemExit(f"merge_summaries: {path} dataset {name} has {r['rerank_fallback_queries']} reranker fallbacks, void")
            if r.get("smoke_subset"):
                raise SystemExit(f"merge_summaries: {path} dataset {name} is a subset")
        diff = environment_differences(first["environment"], s["environment"])
        if diff:
            raise SystemExit(f"merge_summaries: {path} differs from {first_path} in environment {diff}, not the same experiment")
    results: dict = {}
    instructions: dict = {}
    for path, s in summaries:
        for name, r in s["results"].items():
            if name in results:
                raise SystemExit(f"merge_summaries: dataset {name} appears twice ({path})")
            results[name] = r
            instr = (s["environment"].get("rerank") or {}).get(INSTRUCTION_KEY)
            if instr is not None:
                instructions[name] = instr
    env = json.loads(json.dumps(first["environment"]))
    if isinstance(env.get("rerank"), dict) and instructions:
        env["rerank"][INSTRUCTION_KEY] = instructions
    out = {"environment": env, "generated_utc": max(s.get("generated_utc", "") for _, s in summaries), "results": results,
           "merged_from": [f"{p.parent.name}/{p.name}" for p, _ in summaries]}  # the directory and file only, never a home path
    if "smoke_subset" in first:
        out["smoke_subset"] = False
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("summaries", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    merged = merge([(p, load(p)) for p in args.summaries])
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(args.out)
    print(f"merge_summaries: {len(merged['results'])} datasets -> {args.out}: " + ", ".join(sorted(merged["results"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
