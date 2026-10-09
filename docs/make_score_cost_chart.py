#!/usr/bin/env python3
"""One chart: released retrieval systems on BEIR SciFact and NFCorpus (nDCG@10) against parameter count.

Reads released-systems.json. Each system is two points joined by a thin vertical line: a filled marker for SciFact and a hollow
one for NFCorpus, at the system's parameter count. Stars are gestalt, measured in this repository. Dots are published MTEB
results. Squares are closed APIs, in a band at the right by price. Only landmark systems are labelled, the rest are small and
grey. A dashed vertical line marks one billion parameters.

    uv run --with matplotlib --with adjustText python3 make_score_cost_chart.py --out score-vs-cost.png
"""
import argparse, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from adjustText import adjust_text

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="released-systems.json")
ap.add_argument("--out", default="score-vs-cost.png")
a = ap.parse_args()
rows = [r for r in json.load(open(a.data)) if r.get("scifact") is not None and r.get("nfcorpus") is not None]
RED, INK, GREY, API, LINE = "#c0392b", "#1f2d3d", "#a4adb6", "#6c3483", "#c9d1d9"
LABEL = {
    "NV-Embed-v2": "NV-Embed-v2 (7.9B)", "Qwen3-Embedding-8B": "Qwen3-Embedding-8B", "Qwen3-Embedding-4B": "Qwen3-Embedding-4B",
    "e5-mistral-7b-instruct": "e5-mistral (7B)", "bge-large-en-v1.5": "bge-large (335M)", "bge-base-en-v1.5": "bge-base (109M)",
    "gte-modernbert-base": "gte-modernbert (149M)", "mxbai-embed-large-v1": "mxbai-large (335M)", "all-MiniLM-L6-v2": "MiniLM-L6 (23M)",
    "nomic-embed-text-v1.5, gestalt's dense leg alone": "nomic-embed v1.5 (137M), gestalt's embedder alone",
    "gestalt hybrid + Qwen3-Reranker-0.6B": "gestalt hybrid + reranker\n137M + 0.6B, one laptop GPU", "gestalt hybrid": "gestalt hybrid\n137M, runs on a CPU",
    "OpenAI text-embedding-3-large": "OpenAI 3-large, $0.13/M tokens", "OpenAI text-embedding-3-small": "OpenAI 3-small, $0.02/M",
    "Cohere embed-english-v3.0": "Cohere v3, about $0.10/M",
}
opens = [r for r in rows if r["open"] and r.get("params_m") and "self-reported" not in r.get("note", "")]
closed = sorted([r for r in rows if not r["open"]], key=lambda r: -r["scifact"])
xmax = max(r["params_m"] for r in opens)

fig, ax = plt.subplots(figsize=(12, 7.6), dpi=180)
texts = []
def draw(r, x):
    g = r["name"].startswith("gestalt"); lab = LABEL.get(r["name"]); api = not r["open"]
    col = RED if g else (API if api else (INK if lab else GREY))
    mk = "*" if g else ("s" if api else "o")
    big = 240 if g else (56 if (lab or api) else 24)
    if g:  # only gestalt's two points are joined, so the eye finds its pair; every other pair shares an x position
        ax.plot([x, x], [r["nfcorpus"], r["scifact"]], "-", lw=1.0, color=col, alpha=0.6, zorder=2)
    ax.scatter(x, r["scifact"], s=big, marker=mk, c=col, edgecolors="white" if g else None, linewidths=0.8 if g else 0, zorder=5 if g else 3)
    ax.scatter(x, r["nfcorpus"], s=big * 0.8, marker=mk, facecolors="white", edgecolors=col, linewidths=1.6 if g else 1.1, zorder=5 if g else 3)
    if lab:
        texts.append(ax.text(x, r["scifact"], lab, fontsize=9.6 if g else 8.6, color=col, fontweight="bold" if g else "normal", zorder=6, linespacing=1.15))
for r in opens:
    draw(r, r["params_m"])
for r, x in zip(closed, [xmax * 5, xmax * 10, xmax * 20]):
    draw(r, x)
ax.axvline(1000, color=INK, lw=1.0, ls="--", alpha=0.55, zorder=1)
ax.text(1000 * 1.06, 0.302, "1 billion parameters", fontsize=8.8, color=INK, alpha=0.8, va="bottom")
ax.axvline(xmax * 2.8, color=LINE, lw=1, ls=":", zorder=1)
ax.text(xmax * 3.0, 0.302, "closed APIs, by price", fontsize=8.8, color=API, va="bottom")
ax.set_xscale("log"); ax.set_xlim(14, xmax * 45); ax.set_ylim(0.295, 0.84)
ax.set_xlabel("parameters, millions (log scale)", fontsize=10.5)
ax.set_ylabel("nDCG@10", fontsize=10.5)
fig.suptitle("Retrieval quality against model size: gestalt among the released systems", fontsize=13, x=0.045, ha="left", y=0.975)
ax.set_title("Each system is two points at its parameter count: filled for SciFact (upper band), hollow for NFCorpus (lower band).\nStars: gestalt, measured in this repository. Dots: published MTEB results, 2026-10-09. Squares: closed APIs, placed by price.",
             fontsize=8.4, color="#5d6d7e", loc="left", pad=8)
legend = [Line2D([0], [0], marker="o", color=INK, markerfacecolor=INK, markersize=7, lw=0, label="SciFact (filled)"),
          Line2D([0], [0], marker="o", color=INK, markerfacecolor="white", markersize=7, lw=0, label="NFCorpus (hollow)"),
          Line2D([0], [0], marker="*", color=RED, markerfacecolor=RED, markersize=12, lw=0, label="gestalt"),
          Line2D([0], [0], marker="s", color=API, markerfacecolor=API, markersize=7, lw=0, label="closed API")]
ax.legend(handles=legend, loc="upper left", fontsize=8.8, frameon=False)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
ax.grid(True, axis="y", lw=0.4, alpha=0.5); ax.grid(False, axis="x"); ax.tick_params(labelsize=9)
adjust_text(texts, ax=ax, expand=(1.3, 1.7), force_text=(0.35, 0.8), iter_lim=600, arrowprops=dict(arrowstyle="-", color=GREY, lw=0.6), only_move={"points": "y", "text": "xy"})
fig.tight_layout(rect=(0, 0, 1, 0.955))
fig.savefig(a.out)
print("wrote", a.out)
