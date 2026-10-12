#!/usr/bin/env python3
"""Collect every zoo result into one table of score against measured cost on one machine.

Reads the summary.json of each zoo results directory given (or found under --root), keeps the newest row per (system, dataset),
and writes a JSON list with nDCG@10, its bootstrap interval, batch-1 query latency p50 and p95 in ms, peak VRAM in MiB, parameter
count, dtype, index seconds and documents per second, the run it came from, the GPU and the GPU duty cycle the run used. Index
seconds are cached-run sensitive (a system whose embeddings were already in the work dir indexes in seconds), so the latency columns
are the honest cost axis. GESTALT_GPU_DUTY pauses after every model call, so a reranked row measured at duty 0.3 runs about three
times faster unthrottled; the duty comes from the summary's environment block, or from --duty-map for runs made before the zoo
recorded it, and is null when neither knows.

    python3 evals/zoo/collect_costs.py --root ~/artifacts/retrieval-upgrade/results-hub --duty-map duty.json --out docs/measured-systems.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def collect(summaries: list[Path], duty_map: dict[str, float | None] | None = None) -> list[dict]:
    rows: dict[tuple[str, str], dict] = {}
    for f in sorted(summaries, key=lambda p: p.stat().st_mtime):
        d = json.loads(f.read_text())
        run = f.parent.name
        duty = d.get("environment", {}).get("gpu_duty")
        if duty is None and duty_map:
            duty = duty_map.get(run)
        for ds, r in d.get("results", {}).items():
            if r.get("smoke_subset") or r.get("subset") or r.get("rerank_fallback_queries"):
                continue  # a smoke or --max-docs subset, or a void reranked row, must not replace a full measurement as the newest (2026-10-11)
            for name, v in r["systems"].items():
                c = v["cost"]
                rows[(name, ds)] = {
                    "system": name, "dataset": ds.split("/")[-1], "ndcg10": v["ndcg10"], "ci95": v.get("ci95_bootstrap"),
                    "query_latency_ms_p50": c["query_latency_ms_p50"], "query_latency_ms_p95": c["query_latency_ms_p95"],
                    "peak_vram_mib": c.get("peak_vram_mib"), "parameters": c.get("parameters"), "dtype": c.get("dtype"),
                    "index_seconds": c.get("index_seconds"), "docs_per_second": c.get("docs_per_second"), "index_bytes": c.get("index_bytes"),
                    "queries": r["queries"], "documents": r["documents"], "run": run, "gpu": c.get("gpu"),
                    "rerank_fallback_queries": r.get("rerank_fallback_queries", 0), "gpu_duty": duty,
                }
    return sorted(rows.values(), key=lambda x: (x["dataset"], -x["ndcg10"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, help="directory whose zoo-*/summary.json files are read")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--duty-map", type=Path, help="JSON of run directory name to GESTALT_GPU_DUTY, for runs whose summary lacks gpu_duty")
    ap.add_argument("summaries", nargs="*", type=Path)
    a = ap.parse_args(argv)
    files = list(a.summaries) + (sorted(a.root.glob("zoo-*/summary.json")) if a.root else [])
    rows = collect(files, json.loads(a.duty_map.read_text()) if a.duty_map else None)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(rows, indent=1) + "\n")
    print(f"{len(rows)} rows from {len(files)} summaries -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
