#!/usr/bin/env python3
"""One chart: released retrieval systems on BEIR SciFact and NFCorpus (nDCG@10) against parameter count.

Reads released-systems.json. Each system is two points at its parameter count, SciFact in the upper band and NFCorpus in the
lower band. The vertical axis is broken between the bands, so the empty range between about 0.46 and 0.63 does not take half
the picture. Stars are gestalt, measured in this repository. Dots are published MTEB results. Squares are closed APIs, in a
band at the right by price. Only landmark systems are labelled, the rest are small and grey. A dashed vertical line marks one
billion parameters.

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
RED, INK, GREY, API, LINE, BAND = "#c0392b", "#1f2d3d", "#a4adb6", "#6c3483", "#c9d1d9", "#f4f6f8"
LABEL = {
    "NV-Embed-v2": "NV-Embed-v2 (7.9B)", "Qwen3-Embedding-8B": "Qwen3-Embedding-8B", "Qwen3-Embedding-4B": "Qwen3-Embedding-4B",
    "e5-mistral-7b-instruct": "e5-mistral (7B)", "bge-large-en-v1.5": "bge-large (335M)", "bge-base-en-v1.5": "bge-base (109M)",
    "gte-modernbert-base": "gte-modernbert (149M)", "mxbai-embed-large-v1": "mxbai-large (335M)", "all-MiniLM-L6-v2": "MiniLM-L6 (23M)",
    "nomic-embed-text-v1.5, gestalt's dense leg alone": "nomic-embed v1.5 (137M),\ngestalt's embedder alone",
    "gestalt hybrid + Qwen3-Reranker-0.6B": "gestalt hybrid + reranker\n137M + 0.6B, one laptop GPU", "gestalt hybrid": "gestalt hybrid\n137M, runs on a CPU",
    "OpenAI text-embedding-3-large": "OpenAI 3-large, $0.13/M tokens", "OpenAI text-embedding-3-small": "OpenAI 3-small, $0.02/M",
    "Cohere embed-english-v3.0": "Cohere v3, about $0.10/M",
}
# Eight labels a band can carry without collisions: gestalt's two, the ceiling, the best 4B, the size-class neighbours, the best API, the floor.
LABEL_BY_BAND = {
    "scifact": {"gestalt hybrid + Qwen3-Reranker-0.6B", "gestalt hybrid", "NV-Embed-v2", "Qwen3-Embedding-4B", "bge-base-en-v1.5", "gte-modernbert-base",
                "nomic-embed-text-v1.5, gestalt's dense leg alone", "OpenAI text-embedding-3-large", "all-MiniLM-L6-v2"},
    "nfcorpus": {"gestalt hybrid + Qwen3-Reranker-0.6B", "gestalt hybrid", "NV-Embed-v2", "Qwen3-Embedding-4B", "bge-base-en-v1.5", "mxbai-embed-large-v1",
                 "nomic-embed-text-v1.5, gestalt's dense leg alone", "OpenAI text-embedding-3-large", "all-MiniLM-L6-v2"},
}
opens = [r for r in rows if r["open"] and r.get("params_m") and "self-reported" not in r.get("note", "")]
closed = sorted([r for r in rows if not r["open"]], key=lambda r: -r["scifact"])
xmax = max(r["params_m"] for r in opens)
api_x = {r["name"]: x for r, x in zip(closed, [xmax * 5, xmax * 10, xmax * 20])}
XLIM = (14, xmax * 45)

fig, (top, bot) = plt.subplots(2, 1, figsize=(12, 8.2), dpi=180, sharex=True, gridspec_kw={"height_ratios": [1, 1], "hspace": 0.06})
for ax, key, lo, hi, band in ((top, "scifact", 0.625, 0.825, "SciFact"), (bot, "nfcorpus", 0.295, 0.47, "NFCorpus")):
    ax.set_ylim(lo, hi); ax.set_xscale("log"); ax.set_xlim(*XLIM)
    ax.text(0.012, 0.95, f"{band}, nDCG@10", transform=ax.transAxes, fontsize=11, fontweight="bold", color=INK, va="top")
    ax.axvline(1000, color=INK, lw=1.0, ls="--", alpha=0.5, zorder=1)
    ax.axvline(xmax * 2.8, color=LINE, lw=1, ls=":", zorder=1)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(True, axis="y", lw=0.4, alpha=0.5); ax.grid(False, axis="x"); ax.tick_params(labelsize=9)
    texts = []
    keep = LABEL_BY_BAND[key]
    for r in opens + closed:
        x = r["params_m"] if r["open"] else api_x[r["name"]]
        g = r["name"].startswith("gestalt"); lab = LABEL.get(r["name"]) if r["name"] in keep else None; api = not r["open"]
        col = RED if g else (API if api else (INK if lab else GREY))
        mk = "*" if g else ("s" if api else "o")
        ax.scatter(x, r[key], s=240 if g else (54 if (lab or api) else 22), marker=mk, c=col, edgecolors="white" if g else None, linewidths=0.8 if g else 0, zorder=5 if g else 3)
        if lab:
            texts.append(ax.text(x, r[key], lab, fontsize=9.4 if g else 8.4, color=col, fontweight="bold" if g else "normal", zorder=6, linespacing=1.15))
    adjust_text(texts, ax=ax, expand=(1.3, 1.7), force_text=(0.35, 0.8), iter_lim=600, arrowprops=dict(arrowstyle="-", color=GREY, lw=0.6), only_move={"points": "y", "text": "xy"})
top.spines["bottom"].set_visible(False); top.tick_params(axis="x", which="both", bottom=False)
top.text(1000 * 1.06, 0.628, "1 billion parameters", fontsize=8.6, color=INK, alpha=0.8, va="bottom")
top.text(xmax * 3.0, 0.628, "closed APIs, by price", fontsize=8.6, color=API, va="bottom")
# axis break marks
d = 0.012
for ax, y in ((top, 0), (bot, 1)):
    ax.plot((-d, +d), (y - d, y + d), transform=ax.transAxes, color=INK, clip_on=False, lw=0.9)
bot.set_xlabel("parameters, millions (log scale)", fontsize=10.5)
fig.suptitle("Retrieval quality against model size: gestalt among the released systems", fontsize=13, x=0.045, ha="left", y=0.985)
top.set_title("Stars: gestalt, measured in this repository. Dots: published MTEB results, 2026-10-09. Squares: closed APIs, placed by price. The vertical axis is broken between the two datasets.",
              fontsize=8.4, color="#5d6d7e", loc="left", pad=8)
legend = [Line2D([0], [0], marker="*", color=RED, markerfacecolor=RED, markersize=12, lw=0, label="gestalt, measured here"),
          Line2D([0], [0], marker="o", color=INK, markerfacecolor=INK, markersize=7, lw=0, label="open model, published score"),
          Line2D([0], [0], marker="s", color=API, markerfacecolor=API, markersize=7, lw=0, label="closed API, published score")]
top.legend(handles=legend, loc="lower right", fontsize=8.6, frameon=False, bbox_to_anchor=(0.74, 0.02))
fig.tight_layout(rect=(0, 0, 1, 0.965))
fig.savefig(a.out)
print("wrote", a.out)
