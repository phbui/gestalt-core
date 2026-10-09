#!/usr/bin/env python3
"""One chart: released retrieval systems, mean nDCG@10 over BEIR SciFact and NFCorpus against parameter count.

Reads released-systems.json. Stars are gestalt, measured in this repository. Dots are published MTEB results. Squares are closed
APIs in a band at the right, labelled by price per million tokens. Only landmark systems are labelled; the rest are small grey
dots so the picture stays readable. The faint line joins the open systems that no smaller open system beats.

    uv run --with matplotlib --with adjustText python3 make_score_cost_chart.py --out score-vs-cost.png
"""
import argparse, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from adjustText import adjust_text

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="released-systems.json")
ap.add_argument("--out", default="score-vs-cost.png")
a = ap.parse_args()
rows = [r for r in json.load(open(a.data)) if r.get("scifact") is not None and r.get("nfcorpus") is not None]
for r in rows:
    r["mean"] = (r["scifact"] + r["nfcorpus"]) / 2

RED, INK, GREY, API, FRONT = "#c0392b", "#1f2d3d", "#9aa5b1", "#6c3483", "#b8c2cc"
LABEL = {  # the landmarks a reader needs, by data-file name
    "NV-Embed-v2": "NV-Embed-v2 (7.9B)", "Qwen3-Embedding-8B": "Qwen3-Embedding-8B", "Qwen3-Embedding-4B": "Qwen3-Embedding-4B",
    "e5-mistral-7b-instruct": "e5-mistral (7B)", "bge-large-en-v1.5": "bge-large (335M)", "bge-base-en-v1.5": "bge-base (109M)",
    "gte-modernbert-base": "gte-modernbert (149M)", "mxbai-embed-large-v1": "mxbai-large (335M)", "all-MiniLM-L6-v2": "MiniLM-L6 (23M)",
    "nomic-embed-text-v1.5, gestalt's dense leg alone": "nomic-embed v1.5 (137M), gestalt's embedder alone",
    "gestalt hybrid + Qwen3-Reranker-0.6B": "gestalt hybrid + reranker\n137M + 0.6B, one laptop GPU", "gestalt hybrid": "gestalt hybrid\n137M, runs on a CPU",
    "OpenAI text-embedding-3-large": "OpenAI 3-large, $0.13/M tokens", "OpenAI text-embedding-3-small": "OpenAI 3-small, $0.02/M",
    "Cohere embed-english-v3.0": "Cohere v3, about $0.10/M",
}
opens = [r for r in rows if r["open"] and r.get("params_m") and "self-reported" not in r.get("note", "")]
closed = sorted([r for r in rows if not r["open"]], key=lambda r: -r["mean"])

fig, ax = plt.subplots(figsize=(12, 7.2), dpi=180)
xmax = max(r["params_m"] for r in opens)
# frontier of open models: higher mean with fewer parameters
pts = sorted((r["params_m"], r["mean"]) for r in opens if not r["name"].startswith("gestalt"))
front, best = [], -1
for x, y in pts:
    if y > best:
        front.append((x, y)); best = y
ax.plot([p[0] for p in front], [p[1] for p in front], "-", lw=1.2, color=FRONT, zorder=1)
texts = []
for r in opens:
    g = r["name"].startswith("gestalt"); lab = LABEL.get(r["name"])
    if g:
        ax.scatter(r["params_m"], r["mean"], s=260, marker="*", c=RED, edgecolors="white", linewidths=0.8, zorder=5)
    else:
        ax.scatter(r["params_m"], r["mean"], s=46 if lab else 22, c=INK if lab else GREY, zorder=3 if lab else 2)
    if lab:
        texts.append(ax.text(r["params_m"], r["mean"], lab, fontsize=9.5 if g else 8.6, color=RED if g else INK, fontweight="bold" if g else "normal", zorder=6, linespacing=1.15))
band_x = [xmax * 5, xmax * 10, xmax * 20]
for r, x in zip(closed, band_x):
    ax.scatter(x, r["mean"], s=52, marker="s", c=API, zorder=4)
    texts.append(ax.text(x, r["mean"], LABEL.get(r["name"], r["short"]), fontsize=8.6, color=API, zorder=6))
ax.axvline(xmax * 2.8, color=FRONT, lw=1, ls=":")
lo = min(r["mean"] for r in rows); hi = max(r["mean"] for r in rows)
ax.text(xmax * 3.0, lo - 0.004, "closed APIs, by price", fontsize=8.6, color=API, va="bottom")
ax.set_xscale("log"); ax.set_xlim(14, xmax * 45); ax.set_ylim(lo - 0.012, hi + 0.012)
ax.set_xlabel("parameters, millions (log scale)", fontsize=10.5)
ax.set_ylabel("mean nDCG@10 over BEIR SciFact and NFCorpus", fontsize=10.5)
fig.suptitle("Retrieval quality against model size: gestalt among the released systems", fontsize=13, x=0.045, ha="left", y=0.985)
ax.set_title("Stars: gestalt, measured in this repository. Dots: published MTEB results, 2026-10-09. Squares: closed APIs, by price. Faint line: the open models that no smaller open model beats.",
             fontsize=8.4, color="#5d6d7e", loc="left", pad=8)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
ax.grid(True, axis="y", lw=0.4, alpha=0.5); ax.grid(False, axis="x")
ax.tick_params(labelsize=9)
adjust_text(texts, ax=ax, expand=(1.25, 1.6), arrowprops=dict(arrowstyle="-", color=GREY, lw=0.6), only_move={"points": "y", "text": "xy"})
fig.tight_layout(rect=(0, 0, 1, 0.965))
fig.savefig(a.out)
print("wrote", a.out)
