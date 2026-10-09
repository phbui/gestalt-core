#!/usr/bin/env python3
"""Reproduce the stored headline numbers with one command.

Reads results/summary.json, derives the exact beir_bench.py command for each stored dataset from the
environment block (embedder profile and precision, fusion, reranker, depth and the per-dataset instruction),
runs check_env.py, runs each dataset, then compares the four headline systems with the stored cells.

    python3 evals/retrieval/reproduce.py --out out/beir-repro          # the shipped configuration
    python3 evals/retrieval/reproduce.py --out out/beir-repro --fast   # embed in float16, same numbers within 0.0005
    python3 evals/retrieval/reproduce.py --print                                # the commands only, nothing runs
    python3 evals/retrieval/reproduce.py --out /tmp/beir-smoke --smoke          # 200 documents, proves the code runs

A cell passes when its nDCG@10 is within --tolerance of the stored value (default 0.0005, the float16 study moved no
public number by more than 0.0003). Exit 0 when every cell passes, 1 when one does not, 2 when check_env says NO-GO.
The comparison is a sanity check on the pipeline, not a statistical test. pair_runs.py gives the paired test.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUMMARY = HERE / "results" / "summary.json"
HEADLINE = ("bm25", "dense", "hybrid", "hybrid_rerank")


def plan(summary: dict, out: Path, *, fast: bool = False, smoke: bool = False, batch_size: int | None = None) -> list[dict]:
    """One row per stored dataset: the environment, the argv and the stored cells. Pure, so a test can read it."""
    env = summary["environment"]
    rerank = env.get("rerank") or {}
    rows = []
    for ds, res in sorted(summary["results"].items()):
        e: dict[str, str] = {}
        e["GESTALT_EMBED_PROFILE"] = env.get("embed_profile", "nomic")
        e["GESTALT_EMBED_DTYPE"] = "float16" if fast else env.get("embed_dtype", "float32")
        e["GESTALT_EMBED_NORMALIZE"] = "1" if env.get("embed_normalize") else "0"
        e["GESTALT_TF32"] = "1" if env.get("tf32") else "0"
        if env.get("attn_impl"):
            e["GESTALT_ATTN_IMPL"] = str(env["attn_impl"])
        fusion = env.get("fusion") or {}
        resolved = fusion.get("resolved") if isinstance(fusion, dict) else None
        if resolved:
            e["GESTALT_FUSION"] = str(resolved.get("method", "rrf"))
            for key, knob in (("alpha", "GESTALT_FUSION_ALPHA"), ("w_bm25", "GESTALT_FUSION_W_BM25"), ("norm", "GESTALT_FUSION_NORM"), ("missing", "GESTALT_FUSION_MISSING")):
                if resolved.get(key) is not None:
                    e[knob] = str(resolved[key])
        else:
            e["GESTALT_FUSION"] = str(fusion.get("mode", "rrf") if isinstance(fusion, dict) else fusion or "rrf")
        argv = [sys.executable, str(HERE / "beir_bench.py"), "--datasets", ds, "--out", str(out / ds.replace("/", "-"))]
        if rerank.get("on"):
            argv += ["--rerank", "on", "--rerank-model", str(rerank.get("model", "qwen3-0.6b")), "--rerank-depth", str(rerank.get("depth", 40))]
            e["GESTALT_RERANK_DTYPE"] = env.get("rerank_dtype", "float16")
            instr = (rerank.get("instruction") or {}).get(ds)
            if instr:
                e["GESTALT_RERANK_INSTRUCTION"] = instr
            if rerank.get("maxchars"):
                e["GESTALT_RERANK_MAXCHARS"] = str(rerank["maxchars"])
        bs = batch_size or env.get("embed_batch_size")
        if bs:
            argv += ["--batch-size", str(bs)]
        if smoke:
            argv += ["--limit-docs", "200"]
        stored = {s: res["systems"][s]["ndcg10"] for s in HEADLINE if s in res.get("systems", {})}
        rows.append({"dataset": ds, "env": e, "argv": argv, "stored": stored,
                     "result_file": out / ds.replace("/", "-") / f"{ds.replace('/', '-')}.json"})
    return rows


def shell_line(row: dict) -> str:
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in row["env"].items())
    return f"env {env} " + " ".join(shlex.quote(a) for a in row["argv"])


def compare(stored: dict, got: dict, tolerance: float) -> list[tuple[str, float, float | None, bool]]:
    """(system, stored, got, pass) per headline cell. A missing system fails."""
    out = []
    for s, v in stored.items():
        g = got.get(s)
        out.append((s, v, g, g is not None and abs(g - v) <= tolerance))
    return out


def read_result(path: Path) -> dict:
    d = json.loads(path.read_text())
    return {s: d["systems"][s]["ndcg10"] for s in d.get("systems", {})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--summary", default=str(SUMMARY))
    ap.add_argument("--out", default=None, help="where the runs land, one directory per dataset")
    ap.add_argument("--fast", action="store_true", help="embed in float16")
    ap.add_argument("--smoke", action="store_true", help="200 documents per set; the numbers are not compared")
    ap.add_argument("--print", action="store_true", help="print the commands and exit")
    ap.add_argument("--tolerance", type=float, default=0.0005)
    ap.add_argument("--batch-size", type=int, default=None, help="override the stored encode batch (smaller fits a smaller card)")
    ap.add_argument("--skip-check-env", action="store_true")
    a = ap.parse_args(argv)
    summary = json.loads(Path(a.summary).read_text())
    out = Path(a.out or "beir-repro").expanduser()
    rows = plan(summary, out, fast=a.fast, smoke=a.smoke, batch_size=a.batch_size)
    if a.print:
        for r in rows:
            print(shell_line(r))
        return 0
    if not a.skip_check_env:
        rc = subprocess.call([sys.executable, str(HERE / "check_env.py")])
        if rc != 0:
            print("reproduce: check_env said NO-GO, stopping", file=sys.stderr)
            return 2
    failed = False
    for r in rows:
        print("reproduce: " + shell_line(r), flush=True)
        rc = subprocess.call(r["argv"], env={**os.environ, **r["env"]})
        if rc != 0:
            print(f"reproduce: {r['dataset']} exited {rc}", file=sys.stderr)
            return 1
        if a.smoke:
            print(f"reproduce: {r['dataset']} smoke run finished")
            continue
        got = read_result(r["result_file"])
        for s, v, g, ok in compare(r["stored"], got, a.tolerance):
            print(f"{r['dataset']:16} {s:14} stored {v:.4f}  got {g if g is None else f'{g:.4f}'}  {'PASS' if ok else 'FAIL'}")
            failed |= not ok
    if a.smoke:
        print("reproduce: smoke runs finished on every dataset. Numbers from a smoke run are never compared or quoted.")
        return 0
    print("reproduce: " + ("every headline cell matched" if not failed else "a cell did not match, see FAIL lines"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
