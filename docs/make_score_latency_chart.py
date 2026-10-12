#!/usr/bin/env python3
"""Score against query latency for every system the zoo ran on the same machine, one band per BEIR set.

Reads measured-systems.json (written by evals/zoo/collect_costs.py from the zoo result files). Each band is nDCG@10 against the
batch-1 p50 query latency on a log axis, with its own score range, so read positions within a band and not slopes across bands.
The dashed line is the Pareto frontier: no measured system scores higher at lower latency than a point on it. Stars are gestalt's
shipped pipeline and its dense leg. Seven systems carry labels in each band and the rest are grey dots, listed in the data file.
Every run paused after each model call under GESTALT_GPU_DUTY; a diamond marks a row measured at duty 0.5 or at a duty the run
did not record, and the footnote carries the caveat. The chart is drawn at its final width, so the text sizes hold in a README.

    uv run --with matplotlib python3 make_score_latency_chart.py --out score-vs-latency.png
    uv run --with matplotlib python3 make_score_latency_chart.py --band scifact --out score-vs-latency-scifact.png
"""
import argparse, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedLocator, NullFormatter

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="measured-systems.json")
ap.add_argument("--out", default="score-vs-latency.png")
ap.add_argument("--band", choices=["scifact", "nfcorpus", "fiqa"], help="draw one band only, for the README")
a = ap.parse_args()
rows = json.load(open(a.data))
VERMILION, BLUE, GREY, INK, MUTED = "#D55E00", "#0072B2", "#B0B0B0", "#000000", "#555555"
STARS = {"stella-400m-rerank-qwen3-0.6b-blend0.5": "gestalt + reranker (opt-in, needs a GPU)", "stella-400m": "gestalt dense search"}
LABEL = {"bm25": "keyword search (BM25)", "nomic": "nomic-embed-text-v1.5", "stella-rr-qwen3-4b-d20-b05": "the 4B reranker (opt-in)", "stella-rr-ettin-150m-d40-b05": "gestalt + ettin reranker",
         "denseon": "DenseOn 150M", "gte-base-v15": "gte-base 137M", "qwen3-4b": "Qwen3-Embedding-4B"}
BEST_OTHER = {"scifact": "gte-base-v15", "nfcorpus": "qwen3-4b", "fiqa": "denseon"}  # the strongest other dense leg in each band
SKIP = {"stella-rr-qwen3-0.6b-b05"}  # the shipped system under its wave-4 name, the same model, depth and blend
# Label offsets per band in axes fraction (dx, dy, ha), placed by hand so nothing collides.
OFFSET = {
    "scifact": {"stella-400m-rerank-qwen3-0.6b-blend0.5": (-0.17, 0.09, "center"), "stella-400m": (0.05, 0.07, "center"), "bm25": (0.02, -0.06, "left"),
                "nomic": (0.03, -0.03, "left"), "stella-rr-qwen3-4b-d20-b05": (0.02, -0.05, "left"), "stella-rr-ettin-150m-d40-b05": (0.0, -0.10, "center"), "gte-base-v15": (0.0, -0.08, "center")},
    "nfcorpus": {"stella-400m-rerank-qwen3-0.6b-blend0.5": (-0.17, 0.13, "center"), "stella-400m": (0.06, 0.07, "center"), "bm25": (0.03, 0.04, "left"),
                 "nomic": (0.03, -0.03, "left"), "stella-rr-qwen3-4b-d20-b05": (0.02, -0.05, "left"), "stella-rr-ettin-150m-d40-b05": (0.03, -0.03, "left"), "qwen3-4b": (0.03, -0.05, "left")},
    "fiqa": {"stella-400m-rerank-qwen3-0.6b-blend0.5": (-0.06, 0.10, "center"), "stella-400m": (-0.02, 0.10, "right"), "bm25": (0.03, 0.0, "left"),
             "nomic": (0.03, -0.02, "left"), "stella-rr-qwen3-4b-d20-b05": (0.02, -0.05, "left"), "stella-rr-ettin-150m-d40-b05": (0.0, -0.10, "center"), "denseon": (-0.03, -0.07, "right")},
}
BANDS = {"scifact": (0.66, 0.83, "SciFact"), "nfcorpus": (0.31, 0.45, "NFCorpus"), "fiqa": (0.22, 0.62, "FiQA")}


def frontier(pts):
    best, out = -1.0, []
    for x, y in sorted(pts):
        if y > best:
            best = y; out.append((x, y))
    return out


bands = [a.band] if a.band else list(BANDS)
fig, axes = plt.subplots(len(bands), 1, figsize=(9, 5.3 if a.band else 10.8), dpi=160, sharex=True, gridspec_kw={"hspace": 0.12}, squeeze=False)
axes = axes[:, 0]
for ax, key in zip(axes, bands):
    lo, hi, band = BANDS[key]
    pts = [r for r in rows if r["dataset"] == key and r["system"] not in SKIP and r["ndcg10"] >= lo]
    ax.set_ylim(lo, hi); ax.set_xscale("log"); ax.set_xlim(4, 70000)
    ax.set_ylabel(f"{band}\nsearch quality, nDCG@10", fontsize=10, fontweight="bold", color=INK, labelpad=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color("#CCCCCC"); ax.spines["bottom"].set_color("#CCCCCC")
    ax.grid(True, axis="y", lw=0.5, color="#E6E6E6"); ax.grid(False, axis="x"); ax.tick_params(labelsize=9, colors=MUTED)
    fr = frontier([(r["query_latency_ms_p50"], r["ndcg10"]) for r in pts])
    ax.plot([x for x, _ in fr], [y for _, y in fr], color="#444444", lw=1.1, ls="--", alpha=0.6, zorder=1, drawstyle="steps-post")
    labelled = set(STARS) | {"bm25", "nomic", "stella-rr-qwen3-4b-d20-b05", "stella-rr-ettin-150m-d40-b05", BEST_OTHER[key]}
    for r in pts:
        n, x, y = r["system"], r["query_latency_ms_p50"], r["ndcg10"]
        star, lab = n in STARS, n in labelled
        hollow = r.get("gpu_duty") != 0.3
        col = VERMILION if star else (BLUE if lab else GREY)
        ax.scatter(x, y, s=190 if star else (46 if lab else 16), marker="*" if star else ("D" if hollow else "o"), c=col, alpha=1 if (star or lab) else 0.55,
                   edgecolors="white" if star else "none", linewidths=0.8 if star else 0, zorder=5 if star else (4 if lab else 2))
        if lab:
            dx, dy, ha = OFFSET[key][n]
            tx = ax.transAxes.inverted().transform(ax.transData.transform((x, y)))
            ax.annotate(STARS.get(n) or LABEL[n], (x, y), xytext=(tx[0] + dx, tx[1] + dy), textcoords="axes fraction", fontsize=9.6 if star else 9,
                        color=col, fontweight="bold" if star else "normal", ha=ha, va="center", zorder=6,
                        arrowprops=dict(arrowstyle="-", color="#BBBBBB", lw=0.7, shrinkA=0, shrinkB=4))
axes[-1].xaxis.set_major_locator(FixedLocator([10, 100, 1000, 10000])); axes[-1].xaxis.set_minor_formatter(NullFormatter())
axes[-1].set_xticklabels(["10 ms", "100 ms", "1 s", "10 s"])
axes[-1].set_xlabel("time per query (log scale, each tick is ten times the last). Left is faster.", fontsize=9.5, color=MUTED)
title = "Search quality against speed, on three public benchmarks" if not a.band else "Search quality against speed, on the SciFact benchmark"
fig.suptitle(title, fontsize=11.5, fontweight="bold", color=INK, x=0.02, ha="left", y=0.995 if a.band else 0.99)
axes[0].set_title(("Higher is better. Left is faster. Every system ran on the same laptop GPU. The dashed line joins the systems\nnothing beats on both counts. Each panel has its own vertical scale." if not a.band else
                   "Higher is better. Left is faster. Every system ran on the same laptop GPU.\nThe dashed line joins the systems nothing beats on both counts."),
                  fontsize=9.5, color=MUTED, loc="left", pad=8)
legend = [Line2D([0], [0], marker="*", color=VERMILION, markerfacecolor=VERMILION, markersize=12, lw=0, label="gestalt"),
          Line2D([0], [0], marker="o", color=BLUE, markerfacecolor=BLUE, markersize=6.5, lw=0, label="other systems, measured the same way"),
          Line2D([0], [0], marker="D", color=GREY, markerfacecolor=GREY, markersize=5.5, lw=0, label="measured with a different GPU pause, see note")]
axes[0].legend(handles=legend, loc="lower right", fontsize=8.5, frameon=False, handletextpad=0.4, bbox_to_anchor=(0.99, 0.0))
fig.text(0.02, 0.006, "Note: every run paused the GPU between model calls to protect the laptop (duty 0.3; diamonds 0.5 or unrecorded), so the reranked systems run two to three\n"
         "times faster on an unthrottled card. The plain search systems barely move. Data: docs/measured-systems.json, written by evals/zoo/collect_costs.py.",
         fontsize=7.8, color=MUTED, va="bottom", linespacing=1.4)
if a.band:
    fig.subplots_adjust(left=0.12, right=0.985, top=0.84, bottom=0.21)
else:
    fig.tight_layout(rect=(0, 0.045, 1, 0.97))
fig.savefig(a.out)
print(a.out)
