"""Benchmark card for the release post, 1200 by 627. Reads the shipped results file, so the picture matches the repository.

    uv run --with matplotlib python3 docs/make_benchmark_card.py evals/retrieval/results/summary.json docs/benchmark-card.png
"""
import json
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

summary = json.load(open(sys.argv[1]))["results"]
out = sys.argv[2]

INK, MUTED, GRID = "#000000", "#555555", "#E6E6E6"
SYSTEMS = ("bm25", "dense", "hybrid_rerank")
COLORS = {"bm25": "#999999", "dense": "#0072B2", "hybrid_rerank": "#D55E00"}
LABELS = {"bm25": "keyword search (a fresh install)", "dense": "dense search (model loaded)", "hybrid_rerank": "dense + reranker (GPU)"}
NAMES = {"beir/scifact": "SciFact", "beir/nfcorpus": "NFCorpus", "beir/fiqa": "FiQA"}
ORDER = ["beir/scifact", "beir/nfcorpus", "beir/fiqa"]

fig, ax = plt.subplots(figsize=(12, 6.27), dpi=100)
fig.patch.set_facecolor("white")
width, gap = 0.25, 0.02
for gi, ds in enumerate(ORDER):
    r = summary[ds]
    tops = []
    for si, system in enumerate(SYSTEMS):
        x = gi + (si - 1) * (width + gap)
        s = r["systems"][system]
        lo, hi = s["ci95_bootstrap"]
        ax.bar(x, s["ndcg10"], width, color=COLORS[system], zorder=3)
        ax.errorbar(x, s["ndcg10"], yerr=[[s["ndcg10"] - lo], [hi - s["ndcg10"]]], color=INK, lw=1.1, capsize=3.5, zorder=4)
        ax.text(x, hi + 0.015, f"{s['ndcg10']:.3f}", ha="center", va="bottom", color=INK, fontsize=12.5, fontweight="bold", zorder=5)
        tops.append(hi)
    t = r["tests"]["rerank_vs_hybrid"]
    ax.text(gi, 0.905, f"reranker adds {t['mean_difference'] * 100:+.2f} points, p {t['p_value']:.2g}", ha="center", va="bottom", color=MUTED, fontsize=11.5)
ax.set_xticks(range(len(ORDER)))
ax.set_xticklabels([f"{NAMES[k]}\n{summary[k]['queries']} queries, {summary[k]['documents']:,} documents" for k in ORDER], color=INK, fontsize=11.5)
ax.set_xlim(-0.55, len(ORDER) - 1 + 0.55)
ax.set_ylim(0, 1.0)
ax.set_yticks([0, 0.2, 0.4, 0.6, 0.8])
ax.tick_params(axis="y", colors=MUTED, labelsize=10.5, length=0)
ax.tick_params(axis="x", length=0, pad=6)
ax.yaxis.grid(True, color=GRID, lw=1, zorder=0)
for side in ("top", "right", "left"):
    ax.spines[side].set_visible(False)
ax.spines["bottom"].set_color(GRID)
ax.set_ylabel("search quality, nDCG@10", color=MUTED, fontsize=11.5)

fig.text(0.05, 0.945, "gestalt on three public search benchmarks", color=INK, fontsize=20, fontweight="bold", ha="left")
fig.text(0.05, 0.895, "Higher is better. Left: a fresh install. Middle: with the embedding model loaded. Right: plus a reranker, GPU only.", color=MUTED, fontsize=12.5, ha="left")
from matplotlib.patches import Patch
ax.legend(handles=[Patch(facecolor=COLORS[k], label=LABELS[k]) for k in SYSTEMS], loc="upper left", frameon=False, fontsize=11.5, ncol=3, bbox_to_anchor=(0.0, 1.13), handlelength=1.2)
fig.text(0.05, 0.03, "Whiskers are 95% bootstrap intervals. stella_en_400M_v5, BM25 in SQLite FTS5, Qwen3-Reranker-0.6B. One command reruns the table.   github.com/phbui/gestalt-core",
         color=MUTED, fontsize=9.5, ha="left")
fig.subplots_adjust(left=0.07, right=0.97, top=0.77, bottom=0.17)
fig.savefig(out, facecolor="white")
print("wrote", out)
