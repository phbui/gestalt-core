"""calibration.py: does the reranker's top score tell us when retrieval fails?

For each query it reads the candidate scores a system returned, turns them into confidence signals, and asks how well
each signal predicts success. Success means the query's nDCG@10 is above zero. Failure means no relevant document made
the top 10. The label comes from the result JSON, which stores no gains, so the label is nDCG-based only. A top-k
label such as "no relevant document in the top 3" cannot be built from the JSON and is not offered.

    python evals/retrieval/calibration.py RESULT.json --system hybrid_rerank [--query-log LOG.jsonl]
        [--signals top1,margin,...] [--compare top1 dense_top] [--json OUT]

Where the scores come from. beir_bench.py writes `beir-<dataset>.<system>.run` files, but their score column is
rank-derived (11 minus the rank). That is useless as a confidence signal, so this script refuses such a file and says
so. Raw scores live in the query log (`queries-*.jsonl` in the bench work directory), one entry per system and query
with a `scores` list. Pass it with --query-log. A run file whose scores are not rank-derived is also accepted.

Signals. Every signal is oriented so that a higher value means more confident, and that direction is fixed in advance.
A signal that turns out to predict backwards shows an AUROC below 0.5. It is never flipped after the fact.

    top1       top score of the system                                    higher is more confident
    margin     top score minus second score                               higher is more confident
    entropy    softmax entropy of the top 10 scores, temperature 1        reported as minus entropy, so higher is more confident
    n_above    candidates scoring above the dataset median of top1        higher is more confident (a stated guess)
    dense_top  top score of the dense run                                 higher is more confident
    bm25_top   top score of the bm25 run                                  higher is more confident
    rrf_margin margin of the hybrid run                                   higher is more confident

Metrics. All intervals are 95 percent percentile bootstraps over queries, with bench_stats's SEED and BOOTSTRAP and the
same resampling stream. AUROC is the chance that a successful query outranks a failed one, with ties counted half.
Calibration fits one scale and one offset by logistic regression (Newton steps with a tiny ridge on the scale) on a DEV
half of the queries and reports expected calibration error on the TEST half, in 10 equal-mass bins. The split uses
SHA-256 of "SALT:query id". The risk-coverage curve keeps the most confident queries first. Risk is the failure rate
among those kept. Ties in the signal are broken by query id, so the result does not depend on input order.

The paired comparison of two signals gives the AUROC difference, a paired bootstrap interval, and a paired permutation
p. The permutation swaps the two signals' rank-normalised values inside each query. It is not bench_stats.permutation_p,
which flips the sign of per-query differences of a mean. AUROC is not a mean of per-query terms, so that test does not
apply. The p has the same floor, 1/(PERMUTATIONS+1).

Exit 0 on success (a one-class dataset prints a message and still exits 0), 2 when an input is unusable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bench_stats import BOOTSTRAP, PERMUTATIONS, SEED, TOPK, _python_stream, _rows_for

SALT = "gestalt-calibration-split-v1"
BINS = 10
COVERAGE_STEP = 0.05
SELECTIVE_AT = (0.9, 0.8, 0.7)
RIDGE = 1e-3

# name: (system the scores come from, description). All oriented higher = more confident.
SIGNALS = {
    "top1": ("primary", "top score of the system"),
    "margin": ("primary", "top score minus second score"),
    "entropy": ("primary", "minus the softmax entropy of the top 10 scores, temperature 1"),
    "n_above": ("primary", "candidates scoring above the dataset median of top1"),
    "dense_top": ("dense", "top score of the dense run"),
    "bm25_top": ("bm25", "top score of the bm25 run"),
    "rrf_margin": ("hybrid", "top score minus second score of the hybrid run"),
}


class CalibrationRefusal(Exception):
    pass


# ---------------------------------------------------------------- reading scores

# Systems whose raw scores rank smaller-is-better in the query log: FTS5 rank for bm25, L2 distance for dense. Run files already negate them.
LOWER_IS_BETTER = frozenset({"bm25", "dense"})

def read_run_scores(path: Path) -> dict[str, list[float]]:
    """Scores per query from a TREC run file, best first. Refuses a file whose scores are rank-derived."""
    out: dict[str, list[tuple[int, float]]] = {}
    derived = True
    for line in path.read_text().splitlines():
        p = line.split()
        if len(p) < 5:
            continue
        rank, score = int(p[3]), float(p[4])
        derived = derived and score == TOPK - rank + 1
        out.setdefault(p[0], []).append((rank, score))
    if derived and out:
        raise CalibrationRefusal(f"{path.name} holds rank-derived scores ({TOPK} minus rank plus 1), not raw ones. Pass the bench query log with --query-log.")
    return {q: [s for _, s in sorted(v)] for q, v in out.items()}


def read_log_scores(path: Path) -> dict[str, dict[str, list[float]]]:
    """{system: {qid: scores, best first}} from a bench query log. The last entry for a key wins. None scores are dropped."""
    out: dict[str, dict[str, list[float]]] = {}
    for i, line in enumerate(path.read_text().splitlines()):
        if not line.strip():
            continue
        e = json.loads(line)
        if i == 0 and "header" in e:
            continue
        sc = [float(s) for s in e.get("scores", []) if s is not None]
        if e["system"] in LOWER_IS_BETTER:  # the log keeps raw FTS5 ranks and L2 distances, where smaller is better
            sc = [0.0 - s for s in sc]
        out.setdefault(e["system"], {})[e["qid"]] = sorted(sc, reverse=True)
    return out


def load_scores(result_path: str, systems: list[str], query_log: str | None) -> tuple[dict, dict]:
    """({system: {qid: scores}}, {system: reason it is missing}). A missing system is not an error here."""
    got: dict[str, dict[str, list[float]]] = {}
    why: dict[str, str] = {}
    log = read_log_scores(Path(query_log)) if query_log else {}
    r = json.loads(Path(result_path).read_text())
    for s in systems:
        if s in log:
            got[s] = log[s]
            continue
        run = Path(result_path).with_name(f"{r['dataset'].replace('/', '-')}.{s}.run")
        if not run.exists():
            why[s] = f"no query-log entries and {run.name} does not exist"
            continue
        try:
            got[s] = read_run_scores(run)
        except CalibrationRefusal as e:
            why[s] = str(e)
    return got, why


# ---------------------------------------------------------------- signals

def softmax_entropy(scores: list[float]) -> float:
    m = max(scores)
    e = [math.exp(s - m) for s in scores]
    z = sum(e)
    return -sum((x / z) * math.log(x / z) for x in e if x > 0)


def margin(scores: list[float]) -> float:
    return scores[0] - scores[1] if len(scores) > 1 else float("nan")


def compute_signals(scores: dict[str, dict[str, list[float]]], primary: str, qids: list[str]) -> dict[str, np.ndarray]:
    """Oriented signal arrays aligned with qids. NaN marks a query the signal cannot be computed for."""
    nan = float("nan")
    out: dict[str, np.ndarray] = {}
    prim = scores.get(primary, {})
    if prim:
        top = [prim[q][0] for q in qids if prim.get(q)]
        med = float(np.median(top)) if top else nan
        out["top1"] = np.array([prim[q][0] if prim.get(q) else nan for q in qids])
        out["margin"] = np.array([margin(prim[q]) if prim.get(q) else nan for q in qids])
        out["entropy"] = np.array([-softmax_entropy(prim[q][:TOPK]) if prim.get(q) else nan for q in qids])
        out["n_above"] = np.array([float(sum(s > med for s in prim[q][:TOPK])) if prim.get(q) else nan for q in qids])
    for name, (src, _) in SIGNALS.items():
        if src == "primary" or src not in scores:
            continue
        d = scores[src]
        if name == "rrf_margin":
            out[name] = np.array([margin(d[q]) if d.get(q) else nan for q in qids])
        else:
            out[name] = np.array([d[q][0] if d.get(q) else nan for q in qids])
    return out


# ---------------------------------------------------------------- pure metrics

def _rank_rows(s: np.ndarray) -> np.ndarray:
    """Average ranks (1 is lowest) along the last axis of a 2-D array."""
    try:
        from scipy.stats import rankdata
        return rankdata(s, axis=1)
    except ImportError:
        out = np.empty(s.shape, dtype=float)
        for i, row in enumerate(s):
            order = np.argsort(row, kind="stable")
            sr = row[order]
            ranks = np.empty(len(row))
            j = 0
            while j < len(row):
                k = j
                while k + 1 < len(row) and sr[k + 1] == sr[j]:
                    k += 1
                ranks[order[j:k + 1]] = (j + k) / 2 + 1
                j = k + 1
            out[i] = ranks
        return out


def _auroc_rows(s: np.ndarray, y: np.ndarray) -> np.ndarray:
    y = np.broadcast_to(y, s.shape).astype(float)
    npos = y.sum(1)
    nneg = s.shape[1] - npos
    with np.errstate(invalid="ignore", divide="ignore"):
        auc = ((_rank_rows(s) * y).sum(1) - npos * (npos + 1) / 2) / (npos * nneg)
    return np.where((npos == 0) | (nneg == 0), np.nan, auc)


def auroc(signal, success) -> float:
    """Chance that a random success has a higher signal than a random failure. Ties count half. NaN if one class is missing."""
    return float(_auroc_rows(np.asarray(signal, float)[None, :], np.asarray(success, float))[0])


def fit_scale_offset(signal, success, iters: int = 100) -> tuple[float, float]:
    """Logistic fit p = sigmoid(a*signal + b) by damped Newton steps. A ridge of RIDGE on the scale keeps a separable set finite.

    The fit runs on the standardised signal and the result is mapped back to raw units. It is deterministic."""
    s, y = np.asarray(signal, float), np.asarray(success, float)
    mu, sd = float(s.mean()), float(s.std()) or 1.0
    z = (s - mu) / sd
    X = np.column_stack([z, np.ones_like(z)])

    def loss(w):
        t = X @ w
        return float(np.sum(np.logaddexp(0, t) - y * t) + 0.5 * RIDGE * w[0] ** 2)

    w = np.zeros(2)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ w)))
        g = X.T @ (p - y) + np.array([RIDGE * w[0], 0.0])
        H = (X * (p * (1 - p))[:, None]).T @ X + np.diag([RIDGE, 1e-12])
        step = np.linalg.solve(H, g)
        f0, t = loss(w), 1.0
        while loss(w - t * step) > f0 + 1e-12 and t > 1e-8:
            t /= 2
        w = w - t * step
        if np.abs(t * step).max() < 1e-10:
            break
    return float(w[0] / sd), float(w[1] - w[0] * mu / sd)


def fit_for(json_path, signal: str = "top1") -> tuple[float, float]:
    """(scale, offset) of the Platt fit for one signal in a JSON this script wrote. It is gestalt_rank.calib_fit, which the ranker reads too. Raises ValueError when the file has no ok fit."""
    import importlib.util
    path = HERE.parent.parent / "tools" / "gestalt_rank.py"
    mod = sys.modules.get("gestalt_rank")
    if mod is None or Path(getattr(mod, "__file__", "") or "").resolve() != path.resolve():
        spec = importlib.util.spec_from_file_location("gestalt_rank", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod.calib_fit(json_path, signal)


def apply_scale_offset(signal, a: float, b: float) -> np.ndarray:
    return 1 / (1 + np.exp(-(a * np.asarray(signal, float) + b)))


def _bin_bounds(n: int, bins: int) -> list[tuple[int, int]]:
    return [(int(c[0]), int(c[-1]) + 1) for c in np.array_split(np.arange(n), bins) if len(c)]


def reliability(pred, success, bins: int = BINS) -> list[dict]:
    """Equal-mass bins of the sorted predictions: bin, count, mean predicted, mean observed. Ties in pred keep input order."""
    p, y = np.asarray(pred, float), np.asarray(success, float)
    order = np.argsort(p, kind="stable")
    rows = []
    for i, (a, b) in enumerate(_bin_bounds(len(p), bins)):
        idx = order[a:b]
        rows.append({"bin": i + 1, "count": int(b - a), "mean_predicted": float(p[idx].mean()), "mean_observed": float(y[idx].mean())})
    return rows


def ece(pred, success, bins: int = BINS) -> float:
    """Expected calibration error with equal-mass bins: the count-weighted gap between mean predicted and mean observed."""
    n = len(pred)
    if not n:
        return float("nan")
    return float(sum(r["count"] * abs(r["mean_predicted"] - r["mean_observed"]) for r in reliability(pred, success, bins)) / n)


def _ece_rows(p: np.ndarray, y: np.ndarray, idx: np.ndarray) -> np.ndarray:
    order = np.lexsort((idx, p), axis=-1)
    ps, ys = np.take_along_axis(p, order, 1), np.take_along_axis(y, order, 1)
    n = p.shape[1]
    return sum((b - a) / n * np.abs(ps[:, a:b].mean(1) - ys[:, a:b].mean(1)) for a, b in _bin_bounds(n, BINS))


def risk_coverage(signal, success, tie_keys=None) -> dict:
    """Risk-coverage curve. Queries are kept most confident first, ties by tie_keys (default input order).

    aurc is the mean risk over every cut k = 1..n. The oracle puts every success first. A random order has
    risk equal to the overall failure rate. gap_closed is (random - aurc) / (random - oracle), 1 for the oracle."""
    s, fail = np.asarray(signal, float), 1.0 - np.asarray(success, float)
    n = len(s)
    keys = np.arange(n) if tie_keys is None else np.asarray(tie_keys)
    order = np.lexsort((keys, -s))
    ks = np.arange(1, n + 1)
    risk = np.cumsum(fail[order]) / ks
    n_fail = fail.sum()
    oracle = np.maximum(0.0, ks - (n - n_fail)) / ks
    aurc, aurc_o, aurc_r = float(risk.mean()), float(oracle.mean()), float(n_fail / n)
    gap = (aurc_r - aurc) / (aurc_r - aurc_o) if aurc_r > aurc_o else float("nan")
    steps = int(round(1 / COVERAGE_STEP))
    curve = []
    for i in range(1, steps + 1):
        c = round(i * COVERAGE_STEP, 2)
        curve.append({"coverage": c, "risk": float(risk[max(1, math.ceil(c * n - 1e-9)) - 1])})
    sel = {str(int(round(c * 100))): 1 - float(risk[max(1, math.ceil(c * n - 1e-9)) - 1]) for c in SELECTIVE_AT}
    return {"curve": curve, "aurc": aurc, "aurc_oracle": aurc_o, "aurc_random": aurc_r, "e_aurc": aurc - aurc_o,
            "gap_closed": None if math.isnan(gap) else gap, "selective_accuracy": sel, "overall_accuracy": 1 - aurc_r}


def _rc_rows(s: np.ndarray, y: np.ndarray, idx: np.ndarray) -> dict[str, np.ndarray]:
    n = s.shape[1]
    order = np.lexsort((idx, -s), axis=-1)
    risk = np.cumsum(np.take_along_axis(1 - y, order, 1), axis=1) / np.arange(1, n + 1)
    out = {"aurc": risk.mean(1)}
    for c in SELECTIVE_AT:
        out[f"sel{int(round(c * 100))}"] = 1 - risk[:, max(1, math.ceil(c * n - 1e-9)) - 1]
    return out


def split_is_dev(qid: str) -> bool:
    return hashlib.sha256(f"{SALT}:{qid}".encode()).digest()[0] % 2 == 0


# ---------------------------------------------------------------- bootstrap

def _chunks(n: int):
    """Resample index matrices drawn from bench_stats's stream, as bootstrap_ci draws them."""
    rs = _python_stream(SEED)
    step = _rows_for(n, BOOTSTRAP)
    for s0 in range(0, BOOTSTRAP, step):
        m = min(step, BOOTSTRAP - s0)
        yield np.floor(rs.random_sample(m * n).reshape(m, n) * float(n)).astype(np.int64)


def _interval(vals: np.ndarray) -> list:
    v = np.sort(vals[np.isfinite(vals)])
    if len(v) < 20:
        return [None, None]
    return [round(float(v[int(0.025 * len(v))]), 4), round(float(v[int(0.975 * len(v)) - 1]), 4)]


def bootstrap(n: int, fn) -> dict[str, list]:
    """95 percent intervals of every statistic fn(idx) returns, where fn maps an index matrix to {name: array of m values}.

    A resample with one class only gives NaN for AUROC and is left out of that interval."""
    acc: dict[str, list[np.ndarray]] = {}
    for idx in _chunks(n):
        for k, v in fn(idx).items():
            acc.setdefault(k, []).append(v)
    return {k: _interval(np.concatenate(v)) for k, v in acc.items()}


def compare_signals(a, b, success) -> dict:
    """AUROC(a) - AUROC(b) with a paired bootstrap interval and a paired within-query swap permutation p."""
    a, b, y = (np.asarray(x, float) for x in (a, b, success))
    n = len(y)
    obs = auroc(a, y) - auroc(b, y)
    if math.isnan(obs):
        raise CalibrationRefusal("one class only, so AUROC is undefined")
    ci = bootstrap(n, lambda idx: {"d": _auroc_rows(a[idx], y[idx]) - _auroc_rows(b[idx], y[idx])})["d"]
    ra, rb = (_rank_rows(x[None, :])[0] / n for x in (a, b))
    rs = _python_stream(SEED)
    extreme, step = 0, _rows_for(n, PERMUTATIONS)
    for s0 in range(0, PERMUTATIONS, step):
        m = min(step, PERMUTATIONS - s0)
        swap = rs.random_sample(m * n).reshape(m, n) < 0.5
        d = _auroc_rows(np.where(swap, rb, ra), y) - _auroc_rows(np.where(swap, ra, rb), y)
        extreme += int((np.abs(d) >= abs(obs) - 1e-12).sum())
    return {"auroc_difference": round(obs, 4), "ci95_bootstrap": ci, "p_value": round((extreme + 1) / (PERMUTATIONS + 1), 5),
            "permutations": PERMUTATIONS, "p_floor": round(1 / (PERMUTATIONS + 1), 6), "queries": n}


# ---------------------------------------------------------------- one signal, end to end

def analyse_signal(signal, success, qids) -> dict:
    """Every metric for one oriented signal on the queries where it is defined."""
    sig, y = np.asarray(signal, float), np.asarray(success, float)
    ok = np.isfinite(sig)
    sig, y, q = sig[ok], y[ok], [qq for qq, k in zip(qids, ok) if k]
    by_id = sorted(range(len(q)), key=lambda i: q[i])  # fixed order, so the seeded bootstrap does not depend on input order
    sig, y, q = sig[by_id], y[by_id], [q[i] for i in by_id]
    n = len(y)
    out: dict = {"queries": int(n), "queries_without_signal": int((~ok).sum())}
    if n == 0 or y.min() == y.max():
        out["status"] = "undefined"
        if n == 0:
            out["message"] = "No query has this signal."
        else:
            which = "successes" if y[0] == 1 else "failures"
            out["message"] = (f"All {n} queries are {which}, so no signal can separate success from failure. "
                              "AUROC and the calibration fit are undefined. Use a harder dataset or more queries.")
        return out
    out["status"] = "ok"
    out["success_rate"] = float(y.mean())
    # Tie order is by query id, so the answer does not depend on the order the queries arrive in.
    rank_of = {qq: i for i, qq in enumerate(sorted(q))}
    keys = np.array([rank_of[qq] for qq in q])
    rc = risk_coverage(sig, y, keys)
    ci = bootstrap(n, lambda idx: {"auroc": _auroc_rows(sig[idx], y[idx]), **_rc_rows(sig[idx], y[idx], keys[idx])})
    out["auroc"] = {"value": round(auroc(sig, y), 4), "ci95_bootstrap": ci["auroc"]}
    rc["aurc_ci95_bootstrap"] = ci["aurc"]
    rc["selective_accuracy_ci95_bootstrap"] = {k[3:]: v for k, v in ci.items() if k.startswith("sel")}
    out["risk_coverage"] = rc
    dev = np.array([split_is_dev(qq) for qq in q])
    out["split"] = {"salt": SALT, "dev_queries": int(dev.sum()), "test_queries": int((~dev).sum())}
    if y[dev].min() == y[dev].max() or (~dev).sum() == 0:
        out["calibration"] = {"status": "undefined", "message": "The DEV half has one class or the TEST half is empty, so no calibration fit was made."}
        return out
    a, b = fit_scale_offset(sig[dev], y[dev])
    pt, yt, kt = apply_scale_offset(sig[~dev], a, b), y[~dev], keys[~dev]
    ece_ci = bootstrap(len(yt), lambda idx: {"ece": _ece_rows(pt[idx], yt[idx], kt[idx])})["ece"]
    out["calibration"] = {"status": "ok", "scale": a, "offset": b, "ece_test": round(ece(pt, yt), 4), "ci95_bootstrap": ece_ci,
                          "bins": BINS, "reliability_test": reliability(pt, yt)}
    return out


def analyse(result: dict, scores: dict, system: str, signal_names: list[str] | None = None) -> dict:
    """All metrics for one result JSON. scores is the dict load_scores returns. The key `_arrays` holds the raw arrays for comparisons."""
    qids_all = list(result["query_ids"])
    per_query = result["systems"][system]["per_query"]
    order = sorted(range(len(qids_all)), key=lambda i: qids_all[i])
    qids = [qids_all[i] for i in order]
    success = np.array([1.0 if per_query[i] > 0 else 0.0 for i in order])
    sigs = compute_signals(scores, system, qids)
    out = {"dataset": result.get("dataset"), "system": system, "queries": len(qids),
           "label": "success = per-query nDCG@10 above 0 (a relevant document in the top 10). nDCG-based only: "
                    "the result JSON stores no gains, so no top-k label.", "signals": {}}
    for n in signal_names or list(sigs):
        if n not in sigs:
            out["signals"][n] = {"status": "missing", "message": f"signal {n} could not be built from the available scores"}
            continue
        out["signals"][n] = {"definition": SIGNALS[n][1], "direction": "higher is more confident", **analyse_signal(sigs[n], success, qids)}
    out["_arrays"] = (sigs, success, qids)
    return out


# ---------------------------------------------------------------- output

def code_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def fmt_auroc(v: dict) -> str:
    return f"{v['value']:.3f} [{v['ci95_bootstrap'][0]}, {v['ci95_bootstrap'][1]}]"


def text_table(res: dict) -> str:
    lines = [f"{res['dataset']}  system={res['system']}  queries={res['queries']}", res["label"], ""]
    lines.append(f"{'signal':<11}{'n':>6}  {'AUROC [95% CI]':<26}{'AURC':>7}{'gap':>7}{'sel@90':>8}{'sel@80':>8}{'sel@70':>8}  ECE(test) [95% CI]")
    first = None
    for n, s in res["signals"].items():
        if s.get("status") != "ok":
            lines.append(f"{n:<11}{s.get('queries', 0):>6}  {s.get('message', s.get('status'))}")
            continue
        first = first or n
        rc, ca = s["risk_coverage"], s["calibration"]
        sel = rc["selective_accuracy"]
        ece_txt = f"{ca['ece_test']:.3f} {ca['ci95_bootstrap']}" if ca["status"] == "ok" else ca["message"]
        gap = "n/a" if rc["gap_closed"] is None else f"{rc['gap_closed']:.2f}"
        lines.append(f"{n:<11}{s['queries']:>6}  {fmt_auroc(s['auroc']):<26}{rc['aurc']:>7.3f}{gap:>7}{sel['90']:>8.3f}{sel['80']:>8.3f}{sel['70']:>8.3f}  {ece_txt}")
    if first and res["signals"][first]["calibration"]["status"] == "ok":
        lines += ["", f"Reliability on the TEST half for {first} (bin, count, mean predicted, mean observed):"]
        lines += [f"  {r['bin']:>2} {r['count']:>5} {r['mean_predicted']:>7.3f} {r['mean_observed']:>7.3f}" for r in res["signals"][first]["calibration"]["reliability_test"]]
    if "compare" in res:
        c = res["compare"]
        lines += ["", f"AUROC({c['a']}) - AUROC({c['b']}) = {c['auroc_difference']:+.4f}  95% CI {c['ci95_bootstrap']}  p = {c['p_value']} (floor {c['p_floor']})"]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reranker-score uncertainty: AUROC, calibration, risk-coverage.")
    ap.add_argument("result")
    ap.add_argument("--system", default="hybrid_rerank")
    ap.add_argument("--query-log", help="bench query log (queries-*.jsonl) holding raw scores")
    ap.add_argument("--signals", help="comma list. Default: every signal that can be built")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--json", help="write every number here")
    args = ap.parse_args(argv)
    try:
        result = json.loads(Path(args.result).read_text())
        if args.system not in result.get("systems", {}) or "query_ids" not in result:
            raise CalibrationRefusal(f"{args.result} has no per-query scores for system {args.system!r}")
        scores, why = load_scores(args.result, list(dict.fromkeys([args.system, "dense", "bm25", "hybrid"])), args.query_log)
        if args.system not in scores:
            raise CalibrationRefusal(f"no raw scores for {args.system}: {why[args.system]}")
        names = args.signals.split(",") if args.signals else None
        for n in names or []:
            if n not in SIGNALS:
                raise CalibrationRefusal(f"unknown signal {n!r}. Known: {', '.join(SIGNALS)}")
        res = analyse(result, scores, args.system, names)
        sigs, success, _ = res.pop("_arrays")
        if args.compare:
            a, b = args.compare
            for n in (a, b):
                if n not in sigs:
                    raise CalibrationRefusal(f"cannot compare: signal {n} is not available")
            ok = np.isfinite(sigs[a]) & np.isfinite(sigs[b])
            res["compare"] = {"a": a, "b": b, **compare_signals(sigs[a][ok], sigs[b][ok], success[ok])}
    except (CalibrationRefusal, OSError, ValueError, KeyError) as e:
        print(f"calibration: {e}", file=sys.stderr)
        return 2
    res["scores_unavailable"] = why
    res["definitions"] = {k: {"source_system": v[0], "description": v[1], "direction": "higher is more confident"} for k, v in SIGNALS.items()}
    res["settings"] = {"split_salt": SALT, "bins": BINS, "seed": SEED, "bootstrap": BOOTSTRAP, "permutations": PERMUTATIONS,
                       "ridge_on_scale": RIDGE, "coverage_step": COVERAGE_STEP, "code_sha256": code_hash()}
    print(text_table(res))
    for s, w in why.items():
        print(f"note: {s} signals unavailable: {w}")
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
