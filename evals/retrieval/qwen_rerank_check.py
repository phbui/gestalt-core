#!/usr/bin/env python3
"""Does gestalt's Qwen3-Reranker scoring path match the model card? A 200-pair check, both sizes (2026-10-10).

For each of --n queries of a BEIR set it takes one judged-relevant document and the best-ranked non-relevant document of a first
stage run (a TREC run file, for instance the zoo's stella run). It scores every pair twice: through gestalt_rank.rerank_rows, the
product's path, and through the model card's reference code (AutoModelForCausalLM, left padding, the card's system prefix and
think-block suffix, exp(log_softmax([no, yes]))[yes] at the last position). Both see the same document text (gestalt_rank.rerank_text)
and the same instruction. It reports, per model, the Spearman correlation between the two paths, the largest probability gap, the
share of queries where each path ranks the relevant document above the negative, and any non-finite score.

Pass for a model: Spearman >= 0.99, no non-finite score, and gestalt's relevant-above-negative share within 0.02 of the card's.

    python3 evals/retrieval/qwen_rerank_check.py --dataset beir/scifact --run RUN --out results/qwen-rerank-check.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "tools"))

import gestalt_rank as gr

CARD_PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. "
               "Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n")
CARD_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def card_body(instruction: str, query: str, doc: str) -> str:
    return f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {doc}"


def read_run(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 4:
            out.setdefault(parts[0], []).append((int(parts[3]), parts[2]))
    return {q: [d for _, d in sorted(v)] for q, v in out.items()}


def pick_pairs(qrels: dict, queries: list, run: dict, n: int) -> list[tuple[str, str, str, str]]:
    """(query id, query text, relevant doc id, hard negative doc id) for the first n queries by id that have both."""
    out = []
    for q in sorted(queries, key=lambda q: q.query_id):
        pos = [d for d, r in sorted(qrels[q.query_id].items(), key=lambda kv: (-kv[1], kv[0])) if r > 0]
        neg = next((d for d in run.get(q.query_id, []) if qrels[q.query_id].get(d, 0) <= 0), None)
        if pos and neg:
            out.append((q.query_id, q.text, pos[0], neg))
        if len(out) >= n:
            break
    return out


def spearman(a: list[float], b: list[float]) -> float:
    def ranks(x):
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else float("nan")


def above(scores: list[float], n_q: int) -> float:
    """Share of queries whose relevant document (even index) outscores its negative (the next index)."""
    return sum(1 for i in range(n_q) if scores[2 * i] > scores[2 * i + 1]) / n_q


def card_scores(model_id: str, revision: str, instruction: str, pairs: list[tuple[str, str]], dtype: str, batch: int) -> list[float]:
    """The model card's reference scoring, verbatim in substance."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_id, revision=revision, padding_side="left")
    model = AutoModelForCausalLM.from_pretrained(model_id, revision=revision, torch_dtype=getattr(torch, dtype)).to("cuda" if torch.cuda.is_available() else "cpu").eval()
    yes, no = tok.convert_tokens_to_ids("yes"), tok.convert_tokens_to_ids("no")
    pre, suf = tok.encode(CARD_PREFIX, add_special_tokens=False), tok.encode(CARD_SUFFIX, add_special_tokens=False)
    max_length = 8192
    out: list[float] = []
    for i in range(0, len(pairs), batch):
        chunk = [card_body(instruction, q, d) for q, d in pairs[i:i + batch]]
        enc = tok(chunk, padding=False, truncation="longest_first", return_attention_mask=False, max_length=max_length - len(pre) - len(suf))
        enc["input_ids"] = [pre + ids + suf for ids in enc["input_ids"]]
        enc = tok.pad(enc, padding=True, return_tensors="pt", max_length=max_length).to(model.device)
        with torch.no_grad():
            logits = model(**enc).logits[:, -1, :]
        two = torch.stack([logits[:, no], logits[:, yes]], dim=1).float()
        out += torch.nn.functional.log_softmax(two, dim=1)[:, 1].exp().tolist()
    del model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", default="beir/scifact")
    ap.add_argument("--run", type=Path, required=True, help="TREC run file of a first stage, for the hard negatives")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--aliases", nargs="+", default=["qwen3-0.6b", "qwen3-4b"])
    ap.add_argument("--card-dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"])
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)

    import ir_datasets
    from beir_bench import scorable_queries

    qrels, queries, _ = scorable_queries(a.dataset)
    chosen = pick_pairs(qrels, queries, read_run(a.run), a.n)
    store = ir_datasets.load(a.dataset).docs_store()
    want = {d for _, _, g, ng in chosen for d in (g, ng)}
    docs = {d.doc_id: d for d in store.get_many_iter(want)}
    instruction = gr.qwen_instruction()
    maxchars = gr.rerank_maxchars()

    def row(doc_id):
        d = docs[doc_id]
        return {"id": doc_id, "slug": doc_id, "title": getattr(d, "title", "") or "", "heading": "", "content": d.text}

    report = {"dataset": a.dataset, "queries": len(chosen), "pairs": 2 * len(chosen), "instruction": instruction, "maxchars": maxchars,
              "card_dtype": a.card_dtype, "gestalt_dtype": gr.rerank_dtype(), "models": {}}
    for alias in a.aliases:
        ours, labels, texts = [], [], []
        for _, qtext, gold, neg in chosen:
            rows = [row(gold), row(neg)]
            ranked, scores, info = gr.rerank_rows(qtext, rows, alias)
            if info["fallback"] not in ("none", "too-few"):
                raise SystemExit(f"{alias}: the gestalt path fell back ({info['fallback']}), so the check cannot compare it")
            by = {r["id"]: s for r, s in zip(ranked, scores)}
            for r, lab in ((rows[0], 1), (rows[1], 0)):
                ours.append(by[r["id"]])
                labels.append(lab)
                texts.append((qtext, gr.rerank_text(r, maxchars)))
        gr.unload_reranker(force=True)
        name, revision = gr.pinned_model(alias)
        card = card_scores(name, revision, instruction, texts, a.card_dtype, a.batch)
        n_q = len(chosen)
        finite = all(math.isfinite(x) for x in ours + card)
        rho = spearman(ours, card)
        gap = max(abs(x - y) for x, y in zip(ours, card)) if all(0.0 <= x <= 1.0 for x in ours) else None
        m = {"spearman": round(rho, 4), "max_probability_gap": None if gap is None else round(gap, 4), "gestalt_scores_are_probabilities": gap is not None,
             "relevant_above_negative_gestalt": round(above(ours, n_q), 4), "relevant_above_negative_card": round(above(card, n_q), 4), "all_finite": finite,
             "revision": revision}
        m["pass"] = bool(finite and rho >= 0.99 and m["relevant_above_negative_gestalt"] >= m["relevant_above_negative_card"] - 0.02)
        report["models"][alias] = m
        print(f"{alias}: spearman {m['spearman']} gap {m['max_probability_gap']} rel>neg gestalt {m['relevant_above_negative_gestalt']} "
              f"card {m['relevant_above_negative_card']} finite {finite} -> {'PASS' if m['pass'] else 'FAIL'}", flush=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=1) + "\n")
    return 0 if all(m["pass"] for m in report["models"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
