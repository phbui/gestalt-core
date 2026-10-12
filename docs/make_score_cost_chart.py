#!/usr/bin/env python3
"""Released retrieval systems on BEIR SciFact and NFCorpus (nDCG@10) against parameter count, two panels.

Reads released-systems.json. Open models sit at their parameter count on a log axis, one panel per dataset with its own score range.
Stars are gestalt, measured in this repository. Dots are published MTEB results; five carry labels in each panel and the rest are grey.
Closed APIs have no parameter count, so they are not placed on the axis: their scores and prices are one line of text under the chart.
Self-reported model-card scores are left out, as the table says. The chart is drawn at its final width, so the text sizes hold in a README.

    uv run --with matplotlib python3 make_score_cost_chart.py --out score-vs-cost.png
"""
import argparse, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedLocator, NullFormatter

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="released-systems.json")
ap.add_argument("--out", default="score-vs-cost.png")
a = ap.parse_args()
rows = [r for r in json.load(open(a.data)) if r.get("scifact") is not None and r.get("nfcorpus") is not None]
VERMILION, BLUE, GREY, INK, MUTED = "#D55E00", "#0072B2", "#B0B0B0", "#000000", "#555555"
opens = [r for r in rows if r["open"] and r.get("params_m") and "self-reported" not in r.get("note", "")]
closed = sorted([r for r in rows if not r["open"]], key=lambda r: -r["scifact"])
STARS = {"gestalt dense + Qwen3-Reranker-0.6B": "gestalt + reranker (opt-in, needs a GPU)", "gestalt dense leg, stella_en_400M_v5 served here": "gestalt dense search"}
LABEL = {"NV-Embed-v2": "NV-Embed-v2, 7.9B", "Qwen3-Embedding-4B": "Qwen3-Embedding-4B",
         "all-MiniLM-L6-v2": "MiniLM-L6, 23M"}
OFFSET = {  # axes-fraction offsets (dx, dy, ha) per panel, placed by hand
    "scifact": {"gestalt dense + Qwen3-Reranker-0.6B": (0.0, 0.11, "center"), "gestalt dense leg, stella_en_400M_v5 served here": (-0.04, 0.0, "right"),
                "NV-Embed-v2": (0.03, 0.05, "left"), "Qwen3-Embedding-4B": (0.03, -0.07, "left"), "all-MiniLM-L6-v2": (0.03, 0.05, "left")},
    "nfcorpus": {"gestalt dense + Qwen3-Reranker-0.6B": (0.0, 0.11, "center"), "gestalt dense leg, stella_en_400M_v5 served here": (-0.04, 0.0, "right"),
                 "NV-Embed-v2": (0.03, 0.05, "left"), "Qwen3-Embedding-4B": (0.03, -0.07, "left"), "all-MiniLM-L6-v2": (0.03, 0.05, "left")},
}
fig, (top, bot) = plt.subplots(2, 1, figsize=(9, 8.2), dpi=160, sharex=True, gridspec_kw={"hspace": 0.12})
for ax, key, lo, hi, band in ((top, "scifact", 0.62, 0.83, "SciFact"), (bot, "nfcorpus", 0.29, 0.47, "NFCorpus")):
    ax.set_ylim(lo, hi); ax.set_xscale("log"); ax.set_xlim(14, 20000)
    ax.set_ylabel(f"{band}\nsearch quality, nDCG@10", fontsize=10, fontweight="bold", color=INK, labelpad=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color("#CCCCCC"); ax.spines["bottom"].set_color("#CCCCCC")
    ax.grid(True, axis="y", lw=0.5, color="#E6E6E6"); ax.grid(False, axis="x"); ax.tick_params(labelsize=9, colors=MUTED)
    for r in opens:
        x, y = r["params_m"], r[key]
        star, lab = r["name"] in STARS, r["name"] in STARS or r["name"] in LABEL
        col = VERMILION if star else (BLUE if lab else GREY)
        ax.scatter(x, y, s=190 if star else (46 if lab else 16), marker="*" if star else "o", c=col, alpha=1 if lab else 0.55,
                   edgecolors="white" if star else "none", linewidths=0.8 if star else 0, zorder=5 if star else (4 if lab else 2))
        if lab:
            dx, dy, ha = OFFSET[key][r["name"]]
            tx = ax.transAxes.inverted().transform(ax.transData.transform((x, y)))
            ax.annotate(STARS.get(r["name"]) or LABEL[r["name"]], (x, y), xytext=(tx[0] + dx, tx[1] + dy), textcoords="axes fraction", fontsize=9.6 if star else 9,
                        color=col, fontweight="bold" if star else "normal", ha=ha, va="center", zorder=6, arrowprops=dict(arrowstyle="-", color="#BBBBBB", lw=0.7, shrinkA=0, shrinkB=4))
bot.xaxis.set_major_locator(FixedLocator([100, 1000, 10000])); bot.xaxis.set_minor_formatter(NullFormatter())
bot.set_xticklabels(["100M", "1B", "10B"])
bot.set_xlabel("model size in parameters (log scale, each tick is ten times the last). Left is smaller.", fontsize=9.5, color=MUTED)
fig.suptitle("Search quality against model size, on two public benchmarks", fontsize=11.5, fontweight="bold", color=INK, x=0.02, ha="left", y=0.99)
top.set_title("Higher is better. Left is smaller. Stars are gestalt, measured here.\nDots are scores other teams published (MTEB, 2026-10-09). Each panel has its own vertical scale.",
              fontsize=9.5, color=MUTED, loc="left", pad=8)
legend = [Line2D([0], [0], marker="*", color=VERMILION, markerfacecolor=VERMILION, markersize=12, lw=0, label="gestalt"),
          Line2D([0], [0], marker="o", color=BLUE, markerfacecolor=BLUE, markersize=6.5, lw=0, label="open model, published score"),
          Line2D([0], [0], marker="o", color=GREY, markerfacecolor=GREY, markersize=5, lw=0, label=f"{len(opens) - len(STARS) - len(LABEL)} other open models, in docs/released-systems.json")]
top.legend(handles=legend, loc="lower right", fontsize=8.5, frameon=False, handletextpad=0.4)
api = "; ".join(f"{r['name'].replace('text-embedding-', '')} {r['scifact']:.3f} / {r['nfcorpus']:.3f} at {r['cost']}" for r in closed)
fig.text(0.02, 0.006, f"Closed APIs have no parameter count and are not on the axis. SciFact / NFCorpus, price per million tokens:\n{api}.\n"
         "Self-reported model-card scores are left out. Data: docs/released-systems.json.", fontsize=7.8, color=MUTED, va="bottom", linespacing=1.4)
fig.tight_layout(rect=(0, 0.06, 1, 0.97))
fig.savefig(a.out)
print(a.out)
