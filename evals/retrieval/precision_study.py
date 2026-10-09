"""precision_study.py: which numeric precision is cheap enough that retrieval quality does not move.

For one model role (embed or rerank) it runs beir_bench.py once per arm, each in its own child process so every arm loads a clean model.
An arm is a set of precision knobs: GESTALT_EMBED_DTYPE, GESTALT_RERANK_DTYPE, GESTALT_TF32, GESTALT_ATTN_IMPL.
The reference arm `ref` runs first with the knobs unset. Two more reference runs follow, `ref-b16` and `ref-b64`, at batch 16 and 64.
They measure the run-to-run noise floor.

Decision rule. An arm passes when the paired bootstrap lower bound of its per-query nDCG@10 difference from `ref`
is at least minus MARGIN on every dataset. MARGIN is max(0.002, FLOOR). FLOOR is the largest absolute lower bound
of the ref-b16 versus ref-b64 difference over the datasets. The cheapest passing arm is the one with the most documents per second.

Rank flips come from the TREC run files. For each query the study compares the arm's top 10 with the reference top 10.
It reports the share whose order differs, the share whose set differs, and the mean Kendall tau-b over the union of
the two lists (a document missing from a list takes rank 11).

Embed arms share nothing with each other: each gets its own work directory, because the corpus fingerprint does not name the dtype.
Rerank arms share one work directory, because the document vectors do not change. The batch knob is --batch-size for embed
and GESTALT_RERANK_BATCH for rerank. Documents per second is documents over `seconds.index_and_embed` for embed.
For rerank it is queries times depth over `seconds.search_all_queries`.

Arms are `NAME` or `NAME=KNOB=VALUE,KNOB=VALUE`. A bare NAME is one of float32, float16, bfloat16, tf32, sdpa, eager, flash_attention_2.

    python evals/retrieval/precision_study.py --role embed --datasets beir/scifact beir/nfcorpus --out out/precision/embed
    python evals/retrieval/precision_study.py --role rerank --arms bfloat16 float32 tf32 --out out/precision/rerank --dry-run

An arm whose result JSON exists and validates is not run again. --dry-run prints the commands and exits 0.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "tools"))

import pair_runs

MIN_MARGIN = 0.002
REFERENCE = "ref"
NOISE_ARMS = {"ref-b16": 16, "ref-b64": 64}
PRECISION_KNOBS = ("GESTALT_EMBED_DTYPE", "GESTALT_RERANK_DTYPE", "GESTALT_TF32", "GESTALT_ATTN_IMPL", "GESTALT_RERANK_BATCH")
DEFAULT_ARMS = {"embed": ["float16", "bfloat16", "tf32"], "rerank": ["bfloat16", "float32", "tf32"]}
DEFAULT_SYSTEM = {"embed": "hybrid", "rerank": "hybrid_rerank"}
ARM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def shortcut(role: str, name: str) -> dict[str, str]:
    dtype_knob = "GESTALT_EMBED_DTYPE" if role == "embed" else "GESTALT_RERANK_DTYPE"
    if name in ("float32", "float16", "bfloat16"):
        return {dtype_knob: name}
    if name == "tf32":
        return {dtype_knob: "float32", "GESTALT_TF32": "1"}
    if name in ("sdpa", "eager", "flash_attention_2"):
        return {"GESTALT_ATTN_IMPL": name}
    raise ValueError(f"unknown arm {name!r}. Use NAME=KNOB=VALUE,... for anything else.")


def parse_arm(role: str, spec: str) -> tuple[str, dict[str, str]]:
    name, _, rest = spec.partition("=")
    if not ARM_NAME.fullmatch(name) or name == REFERENCE or name in NOISE_ARMS:
        raise ValueError(f"bad arm name {name!r}")
    if not rest:
        return name, shortcut(role, name)
    knobs = {}
    for pair in rest.split(","):
        k, sep, v = pair.partition("=")
        if not sep or k not in PRECISION_KNOBS:
            raise ValueError(f"bad knob {pair!r} in arm {spec!r}. Known knobs: {', '.join(PRECISION_KNOBS)}")
        knobs[k] = v
    return name, knobs


def plan(role: str, arm_specs: list[str]) -> list[tuple[str, dict[str, str], int | None]]:
    """The arms in run order: (name, knobs, batch). ref first, then the two noise runs, then the others. batch None means the harness default."""
    arms: list[tuple[str, dict[str, str], int | None]] = [(REFERENCE, {}, None)]
    arms += [(n, {}, b) for n, b in NOISE_ARMS.items()]
    seen = {REFERENCE, *NOISE_ARMS}
    for spec in arm_specs:
        name, knobs = parse_arm(role, spec)
        if name in seen:
            raise ValueError(f"arm {name!r} is listed twice")
        seen.add(name)
        arms.append((name, knobs, None))
    return arms


def command(role: str, out: Path, name: str, batch: int | None, args) -> tuple[list[str], dict[str, str]]:
    """The beir_bench.py command line for one arm, and the env entries that arm adds."""
    arm_out = out / name
    cmd = [sys.executable, str(HERE / "beir_bench.py"), "--datasets", *args.datasets, "--out", str(arm_out), "--resume"]
    cmd += ["--work-dir", str(out / "work" if role == "rerank" else arm_out / "work")]
    if role == "rerank":
        cmd += ["--rerank", "on", "--rerank-model", args.rerank_model, "--rerank-depth", str(args.rerank_depth)]
    if role == "embed" and batch:
        cmd += ["--batch-size", str(batch)]
    if args.embed_profile:
        cmd += ["--embed-profile", args.embed_profile]
    if args.max_docs:
        cmd += ["--max-docs", str(args.max_docs)]
    extra = {"GESTALT_RERANK_BATCH": str(batch)} if role == "rerank" and batch else {}
    return cmd, extra


def child_env(base: dict[str, str], knobs: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in PRECISION_KNOBS}
    env.update(knobs)
    env.update(extra)
    return env


def result_path(arm_dir: Path, dataset: str) -> Path:
    return arm_dir / f"{dataset.replace('/', '-')}.json"


def arm_complete(arm_dir: Path, datasets: list[str], system: str) -> bool:
    """True when every dataset has a result JSON with per-query scores for `system`, and no reranker fallback."""
    for d in datasets:
        try:
            r = json.loads(result_path(arm_dir, d).read_text())
            s = r["systems"][system]
            if len(s["per_query"]) != len(r["query_ids"]) or not r["query_ids"] or r.get("rerank_fallback_queries", 0) > 0:
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
    return True


def run_arms(role: str, out: Path, arms, args, system: str) -> dict[str, dict]:
    """Run each arm that is not complete. Returns {arm: {"status": "skipped"|"ran"|"failed"|"dry-run", ...}}."""
    status = {}
    for name, knobs, batch in arms:
        cmd, extra = command(role, out, name, batch, args)
        if args.dry_run:
            env_text = " ".join(f"{k}={v}" for k, v in {**knobs, **extra}.items())
            print(f"{name}: " + (env_text + " " if env_text else "") + shlex.join(cmd))
            status[name] = {"status": "dry-run"}
            continue
        if arm_complete(out / name, args.datasets, system):
            print(f"precision_study: {name} already complete, not rerun", file=sys.stderr)
            status[name] = {"status": "skipped"}
            continue
        (out / name).mkdir(parents=True, exist_ok=True)
        rc = subprocess.run(cmd, env=child_env(dict(os.environ), knobs, extra)).returncode
        ok = rc == 0 and arm_complete(out / name, args.datasets, system)
        status[name] = {"status": "ran" if ok else "failed", "returncode": rc}
    return status


def kendall_tau_b(a: list[str], b: list[str]) -> float:
    """Kendall tau-b between two top-k lists over the union of their documents. A document absent from a list ranks len(list)+1."""
    docs = sorted(set(a) | set(b))
    if len(docs) < 2:
        return 1.0
    ra = [a.index(d) + 1 if d in a else len(a) + 1 for d in docs]
    rb = [b.index(d) + 1 if d in b else len(b) + 1 for d in docs]
    conc = disc = ta = tb = 0
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            x, y = ra[i] - ra[j], rb[i] - rb[j]
            if x == 0:
                ta += 1
            if y == 0:
                tb += 1
            if x and y:
                if (x > 0) == (y > 0):
                    conc += 1
                else:
                    disc += 1
    pairs = len(docs) * (len(docs) - 1) // 2
    denom = math.sqrt((pairs - ta) * (pairs - tb))
    return (conc - disc) / denom if denom else 1.0


def rank_flips(ref_run: Path, arm_run: Path, k: int = 10) -> dict:
    """Order, set and tau of the arm's top k against the reference's, over the queries both runs hold."""
    ref, arm = pair_runs.read_run(ref_run), pair_runs.read_run(arm_run)
    qids = [q for q in ref if q in arm]
    if not qids:
        return {"queries": 0, "order_differs": None, "set_differs": None, "mean_kendall_tau": None}
    order = sum(ref[q][:k] != arm[q][:k] for q in qids)
    sets = sum(set(ref[q][:k]) != set(arm[q][:k]) for q in qids)
    taus = [kendall_tau_b(ref[q][:k], arm[q][:k]) for q in qids]
    return {"queries": len(qids), "order_differs": round(order / len(qids), 4), "set_differs": round(sets / len(qids), 4),
            "mean_kendall_tau": round(sum(taus) / len(taus), 4), "_counts": [order, sets, sum(taus)]}


def pool_flips(parts: list[dict]) -> dict:
    n = sum(p["queries"] for p in parts)
    if not n:
        return {"queries": 0, "order_differs": None, "set_differs": None, "mean_kendall_tau": None}
    o, s, t = (sum(p["_counts"][i] for p in parts if p["queries"]) for i in range(3))
    return {"queries": n, "order_differs": round(o / n, 4), "set_differs": round(s / n, 4), "mean_kendall_tau": round(t / n, 4)}


def docs_per_second(role: str, results: list[dict]) -> float | None:
    """Documents (embed) or reranked pairs (rerank) per second, pooled over the datasets from the recorded timings."""
    units = secs = 0.0
    for r in results:
        s = r.get("seconds") or {}
        if role == "embed":
            units += r.get("documents", 0)
            secs += s.get("index_and_embed", 0)
        else:
            depth = ((r.get("query_log_header") or {}).get("rerank") or {}).get("depth") or 0
            units += r.get("queries", 0) * depth
            secs += s.get("search_all_queries", 0)
    return round(units / secs, 2) if secs > 0 else None


def noise_floor(out: Path, datasets: list[str], system: str) -> tuple[float, dict[str, float]]:
    """Per dataset, the wider absolute bound of the b16 versus b64 paired difference, so the floor does not depend on which run is A. The floor is the largest of them."""
    by = {}
    for d in datasets:
        res = pair_runs.pair(str(result_path(out / "ref-b16", d)), str(result_path(out / "ref-b64", d)), system, system)
        lo, hi = res["ci95_bootstrap_difference"]
        by[d] = round(max(abs(lo), abs(hi)), 4)
    return (max(by.values()) if by else 0.0), by


def evaluate(role: str, out: Path, arm_names: list[str], datasets: list[str], system: str, run_status: dict | None = None) -> dict:
    """Pair every arm with ref, apply the decision rule and build the study record. Reads files only."""
    floor, floor_by = noise_floor(out, datasets, system)
    margin = max(MIN_MARGIN, floor)
    arms = {}
    for name in arm_names:
        rec = {"datasets": {}, "pass": False}
        if run_status and run_status.get(name, {}).get("status") == "failed":
            rec["error"] = f"beir_bench exited {run_status[name].get('returncode')} or left no valid result"
            arms[name] = rec
            continue
        try:
            flips, results = [], []
            for d in datasets:
                a, r = str(result_path(out / name, d)), str(result_path(out / REFERENCE, d))
                res = pair_runs.pair(a, r, system, system)
                lo = res["ci95_bootstrap_difference"][0]
                rec["datasets"][d] = {"mean_difference": res["mean_difference"], "ci95_bootstrap_difference": res["ci95_bootstrap_difference"],
                                      "paired_queries": res["paired_queries"], "pass": lo >= -margin}
                safe = d.replace("/", "-")
                f = rank_flips(out / REFERENCE / f"{safe}.{system}.run", out / name / f"{safe}.{system}.run")
                rec["datasets"][d]["rank_flips"] = {k: v for k, v in f.items() if k != "_counts"}
                flips.append(f)
                results.append(json.loads(Path(a).read_text()))
            rec["rank_flips"] = pool_flips(flips)
            rec["docs_per_s"] = docs_per_second(role, results)
            rec["pass"] = all(v["pass"] for v in rec["datasets"].values())
        except pair_runs.Refusal as e:
            rec["error"] = str(e)
        arms[name] = rec
    ref_results = []
    for d in datasets:
        ref_results.append(json.loads(result_path(out / REFERENCE, d).read_text()))
    ref_speed = docs_per_second(role, ref_results)
    passing = [n for n, a in arms.items() if a["pass"] and a.get("docs_per_s")]
    best = max(passing, key=lambda n: arms[n]["docs_per_s"]) if passing else None
    if best:
        speed = arms[best]["docs_per_s"]
        ratio = f", {speed / ref_speed:.2f}x the reference" if ref_speed else ""
        verdict = (f"Cheapest passing arm: {best} at {speed} docs/s{ratio}. "
                   f"Every dataset's lower bound is at least -{margin}.")
    else:
        verdict = f"No arm passes the margin of -{margin}. Keep the reference precision."
    return {"role": role, "system": system, "datasets": datasets, "reference": REFERENCE, "reference_docs_per_s": ref_speed,
            "noise_floor": floor, "noise_floor_by_dataset": floor_by, "margin": margin, "min_margin": MIN_MARGIN,
            "arms": arms, "cheapest_passing_arm": best, "verdict": verdict}


def render(study: dict) -> str:
    lines = [f"Precision study: role {study['role']}, system {study['system']}, reference {study['reference']} at {study['reference_docs_per_s']} docs/s",
             f"Noise floor {study['noise_floor']} (b16 vs b64), margin {study['margin']} = max({study['min_margin']}, floor)", ""]
    lines.append(f"{'arm':14s}{'dataset':22s}{'diff':>9s}{'CI lower':>10s}{'pass':>6s}{'order':>8s}{'set':>8s}{'tau':>8s}{'docs/s':>10s}")
    for name, a in study["arms"].items():
        if "error" in a:
            lines.append(f"{name:14s}ERROR {a['error']}")
            continue
        for d, v in a["datasets"].items():
            f = v["rank_flips"]
            lines.append(f"{name:14s}{d:22s}{v['mean_difference']:+9.4f}{v['ci95_bootstrap_difference'][0]:+10.4f}{'yes' if v['pass'] else 'NO':>6s}"
                         f"{f['order_differs']:8.3f}{f['set_differs']:8.3f}{f['mean_kendall_tau']:8.4f}{a['docs_per_s'] or 0:10.1f}")
    return "\n".join(lines + ["", study["verdict"]])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", choices=["embed", "rerank"], required=True)
    ap.add_argument("--datasets", nargs="+", default=["beir/scifact", "beir/nfcorpus"])
    ap.add_argument("--arms", nargs="+", default=None, help="arm specs (default: embed float16 bfloat16 tf32, rerank bfloat16 float32 tf32)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--system", default=None, help="system to pair (default hybrid for embed, hybrid_rerank for rerank)")
    ap.add_argument("--embed-profile", default=None)
    ap.add_argument("--rerank-model", default="qwen3-0.6b")
    ap.add_argument("--rerank-depth", type=int, default=40)
    ap.add_argument("--max-docs", type=int, default=None, help="subset run. The study is then a smoke test only")
    ap.add_argument("--dry-run", action="store_true", help="print the commands and exit 0")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.out).expanduser()
    system = args.system or DEFAULT_SYSTEM[args.role]
    try:
        arms = plan(args.role, args.arms if args.arms is not None else DEFAULT_ARMS[args.role])
    except ValueError as e:
        print(f"precision_study: {e}", file=sys.stderr)
        return 2
    status = run_arms(args.role, out, arms, args, system)
    if args.dry_run:
        return 0
    names = [n for n, _, _ in arms]
    broken = [n for n in (REFERENCE, *NOISE_ARMS) if status[n]["status"] == "failed"]
    if broken:
        print(f"precision_study: reference runs failed: {', '.join(broken)}. Nothing to compare against.", file=sys.stderr)
        return 1
    try:
        study = evaluate(args.role, out, [n for n in names if n != REFERENCE and n not in NOISE_ARMS], args.datasets, system, status)
    except pair_runs.Refusal as e:
        print(f"precision_study: {e}", file=sys.stderr)
        return 2
    study["run_status"] = status
    (out / "precision-study.json").write_text(json.dumps(study, indent=2) + "\n")
    text = render(study)
    (out / "precision-study.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
