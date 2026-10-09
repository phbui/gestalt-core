"""multitest.py: correct a family of p-values, and rank systems across datasets.

    python evals/retrieval/multitest.py RESULT.json [RESULT.json ...] [--alpha 0.05] [--json OUT]

Each input is a pair_runs.py result or a calibration.py result with a `compare` block. Every one contributes one named
p-value. The script prints the raw p, the Holm-Bonferroni adjusted p and the Benjamini-Hochberg adjusted p.
Holm controls the chance of any false positive. BH controls the share of false positives among the rejections.

The p-value floor. The permutation tests in this repo draw PERMUTATIONS sign flips and add one to the count. The
smallest p they can report is 1/(PERMUTATIONS+1), which is 0.00005 for 20,000. A reported p equal to the floor means
"below the floor", not zero. Holm multiplies the smallest p by the family size m. So a floor p survives alpha only when
m is below alpha*(PERMUTATIONS+1), which is 1,000 at alpha 0.05. For families under that size the floor does not
block a rejection. Several floor p-values in one family all adjust to nearly the same number, so their order is not
informative.

Also here: Friedman's chi-square test with the Nemenyi critical difference for a systems-by-datasets matrix of scores
(Demsar 2006, JMLR 7:1-30). Higher score is better. Ties get average ranks and the chi-square gets the usual tie correction.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# Nemenyi critical values q_alpha for k = 2..10 systems (Demsar 2006, Table 5).
NEMENYI_Q = {
    0.05: [1.960, 2.343, 2.569, 2.728, 2.850, 2.949, 3.031, 3.102, 3.164],
    0.10: [1.645, 2.052, 2.291, 2.459, 2.589, 2.693, 2.780, 2.855, 2.920],
}


# ---------------------------------------------------------------- p-value correction

def holm(named: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni adjusted p: the i-th smallest p times (m - i + 1), made non-decreasing, capped at 1."""
    items = sorted(named.items(), key=lambda kv: (kv[1], kv[0]))
    m, out, running = len(items), {}, 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return {k: out[k] for k in named}


def benjamini_hochberg(named: dict[str, float]) -> dict[str, float]:
    """Benjamini-Hochberg adjusted p: the i-th smallest p times m / i, made non-decreasing from the largest down, capped at 1."""
    items = sorted(named.items(), key=lambda kv: (kv[1], kv[0]))
    m, out, running = len(items), {}, 1.0
    for i in range(m - 1, -1, -1):
        k, p = items[i]
        running = min(running, m * p / (i + 1))
        out[k] = running
    return {k: out[k] for k in named}


def pvalue_from_result(path: str) -> tuple[str, float, dict]:
    """(name, p, info) from a pair_runs.py or calibration.py result JSON."""
    r = json.loads(Path(path).read_text())
    if "system_a" in r and "p_value" in r:
        return (f"{r['dataset']}:{r['system_a']}-vs-{r['system_b']}", float(r["p_value"]), {"permutations": r.get("permutations")})
    c = r.get("compare")
    if c:
        return (f"{r['dataset']}:{r['system']}:{c['a']}-vs-{c['b']}", float(c["p_value"]), {"permutations": c.get("permutations")})
    raise ValueError(f"{path} is neither a pair_runs result nor a calibration result with a compare block")


def correct_family(paths: list[str], alpha: float = 0.05) -> dict:
    """One family over several result JSONs. Duplicate names are refused, since two tests must not share a key."""
    named, info = {}, {}
    for p in paths:
        name, pv, extra = pvalue_from_result(p)
        if name in named:
            raise ValueError(f"two inputs both name the test {name!r}")
        named[name], info[name] = pv, extra
    h, b = holm(named), benjamini_hochberg(named)
    perms = [v["permutations"] for v in info.values() if v.get("permutations")]
    floor = 1 / (max(perms) + 1) if perms else None
    rows = [{"test": k, "p": named[k], "holm": round(h[k], 6), "bh": round(b[k], 6), "reject_holm": h[k] <= alpha, "reject_bh": b[k] <= alpha,
             "at_p_floor": floor is not None and named[k] <= round(floor, 5)} for k in sorted(named, key=named.get)]
    return {"alpha": alpha, "family_size": len(rows), "p_floor": floor, "rows": rows}


# ---------------------------------------------------------------- Friedman and Nemenyi

def _chi2_sf(x: float, df: int) -> float:
    """Upper tail of the chi-square distribution: the regularised upper incomplete gamma Q(df/2, x/2), by series or continued fraction."""
    if x <= 0:
        return 1.0
    a, z = df / 2, x / 2
    lg = math.lgamma(a)
    if z < a + 1:
        term = tot = 1 / a
        for n in range(1, 1000):
            term *= z / (a + n)
            tot += term
            if abs(term) < abs(tot) * 1e-15:
                break
        return 1 - tot * math.exp(-z + a * math.log(z) - lg)
    b, c, d = z + 1 - a, 1e300, 1 / (z + 1 - a)
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        h *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return math.exp(-z + a * math.log(z) - lg) * h


def _average_ranks(values: list[float]) -> list[float]:
    """Ranks where 1 is the best (highest) value. Ties share the mean of their ranks."""
    order = sorted(range(len(values)), key=lambda i: -values[i])
    ranks = [0.0] * len(values)
    j = 0
    while j < len(order):
        k = j
        while k + 1 < len(order) and values[order[k + 1]] == values[order[j]]:
            k += 1
        for t in range(j, k + 1):
            ranks[order[t]] = (j + k) / 2 + 1
        j = k + 1
    return ranks


def nemenyi_q(k: int, alpha: float) -> float:
    if alpha in NEMENYI_Q and 2 <= k <= 10:
        return NEMENYI_Q[alpha][k - 2]
    try:
        from scipy.stats import studentized_range
        return float(studentized_range.ppf(1 - alpha, k, math.inf) / math.sqrt(2))
    except ImportError:
        raise ValueError(f"no Nemenyi value for k={k}, alpha={alpha} without scipy. The table covers k 2..10 at alpha 0.05 and 0.10")


def friedman_from_mean_ranks(mean_ranks: dict[str, float], n_datasets: int, alpha: float = 0.05, tie_correction: float = 1.0) -> dict:
    """Friedman chi-square, its p, the Nemenyi critical difference and the pairs whose mean ranks differ by more than it."""
    k, n = len(mean_ranks), n_datasets
    chi2 = 12 * n / (k * (k + 1)) * sum((r - (k + 1) / 2) ** 2 for r in mean_ranks.values()) / tie_correction
    cd = nemenyi_q(k, alpha) * math.sqrt(k * (k + 1) / (6 * n))
    names = list(mean_ranks)
    pairs = [{"a": a, "b": b, "rank_difference": round(abs(mean_ranks[a] - mean_ranks[b]), 4), "significant": abs(mean_ranks[a] - mean_ranks[b]) > cd}
             for i, a in enumerate(names) for b in names[i + 1:]]
    return {"systems": k, "datasets": n, "chi2": chi2, "df": k - 1, "p_value": _chi2_sf(chi2, k - 1), "mean_ranks": mean_ranks,
            "nemenyi_cd": cd, "alpha": alpha, "pairs": pairs}


def friedman_nemenyi(scores: dict[str, list[float]], alpha: float = 0.05) -> dict:
    """scores maps system to its score on each dataset (same order, higher is better)."""
    names = list(scores)
    n = len(scores[names[0]])
    if len(names) < 2 or n < 2 or any(len(v) != n for v in scores.values()):
        raise ValueError("need at least 2 systems and 2 datasets, with one score per dataset for every system")
    sums, tie_term = {s: 0.0 for s in names}, 0.0
    for d in range(n):
        col = [scores[s][d] for s in names]
        for s, r in zip(names, _average_ranks(col)):
            sums[s] += r
        for v in set(col):
            t = col.count(v)
            tie_term += t ** 3 - t
    k = len(names)
    correction = 1 - tie_term / (n * k * (k * k - 1))
    if correction <= 0:
        raise ValueError("every score is tied, so the ranks carry no information")
    return friedman_from_mean_ranks({s: sums[s] / n for s in names}, n, alpha, correction)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Holm and Benjamini-Hochberg over a family of result JSONs.")
    ap.add_argument("results", nargs="+")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    try:
        fam = correct_family(args.results, args.alpha)
    except (OSError, ValueError, KeyError) as e:
        print(f"multitest: {e}", file=sys.stderr)
        return 2
    print(f"family of {fam['family_size']} tests, alpha {fam['alpha']}, p floor {fam['p_floor']}")
    print(f"{'test':<60}{'p':>10}{'holm':>10}{'bh':>10}  note")
    for r in fam["rows"]:
        note = "at the p floor (below it, not zero)" if r["at_p_floor"] else ""
        print(f"{r['test']:<60}{r['p']:>10.5f}{r['holm']:>10.5f}{r['bh']:>10.5f}  {note}")
    if args.json:
        Path(args.json).write_text(json.dumps(fam, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
