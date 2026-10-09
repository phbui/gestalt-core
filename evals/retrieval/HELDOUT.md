# Held-out sets

The headline BEIR sets (SciFact and NFCorpus) were used during development. Choices were made while looking at them. The held-out sets below were opened once, and the date and reason are in each set's manifest. A number from a set that was tuned on is a development number. A number from a sealed set is the claim.

## The three sets

| Set | Licence | Size | Known objections |
| --- | --- | --- | --- |
| LitSearch (`mteb/LitSearchRetrieval`) | MIT on the mirror | 64,183 docs, 597 queries, 639 qrels | Paper search on ML and NLP. The inline queries are LLM-generated. `query_flags()` separates them from the expert-written ones, so report both. |
| TechQA (`nvidia/TechQA-RAG-Eval`) | Apache-2.0 | 28,482 technotes, 910 items, 300 of them impossible | Many technotes are near-duplicates, so a gold note has close twins. The 300 impossible items score abstention through `unanswerable_ids()`. |
| LongMemEval-S (`xiaowu0162/longmemeval-cleaned`) | MIT | 500 questions, 30 abstention items ending `_abs` | It is chat, not notes. Each question has its own haystack, so it needs a per-question loop. |

LoCoMo is not used. Its licence is unverified.

## Seal and open

1. `evals/retrieval/heldout.py fetch <name>` downloads files pinned to a commit sha. It refuses a sealed set.
2. `heldout.py seal <name>` writes `<name>.manifest.json` beside the data. It holds the sha256 of every file, the repo and commit, the licence, the sorted query ids and their hash, the counts, the fetch time and an empty `opened` list.
3. `heldout.py open <name> --reason TEXT` checks the hashes, logs the time, reason and git sha, and prints the `export` line that lets the dataset objects read. Without that line the reads raise.
4. A second open is refused. `--reopen` with a reason logs it. The log stays in the manifest.
5. `heldout.py verify <name>` recomputes every hash and exits non-zero on any mismatch.

The tool never deletes data. Files live under `GESTALT_HELDOUT_DIR` (default `.heldout` in the repo root). Keep that folder out of git.

## Score a set

`heldout_datasets.register()` makes the sealed sets look like BEIR datasets to `beir_bench.py`. After `heldout.py open litsearch --reason "..."` prints its `export` line, run the same harness with the set's name:

```
python3 evals/retrieval/heldout.py fetch litsearch
python3 evals/retrieval/heldout.py seal litsearch
eval "$(python3 evals/retrieval/heldout.py open litsearch --reason 'first scoring run')"
python3 evals/retrieval/beir_bench.py --datasets heldout/litsearch --rerank on --out out/heldout-litsearch
```

The open is spent on first use. A rerun needs `--reopen` and a reason, and the manifest keeps both.
