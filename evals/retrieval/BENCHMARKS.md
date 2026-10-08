# Retrieval benchmarks

This page reports how gestalt's search stack scores on two public BEIR datasets. It is a sanity check on the pipeline. It does not rank gestalt against other systems on your notes.

## Result

nDCG@10 on the BEIR test split. Each cell is the mean over queries with a 95% bootstrap interval.

| Dataset | Docs | Queries | BM25 leg | Dense leg | Hybrid (RRF, K=60) |
|---|---|---|---|---|---|
| SciFact | 5,183 | 300 | 0.682 [0.638, 0.725] | 0.694 [0.650, 0.736] | **0.737 [0.696, 0.777]** |
| NFCorpus | 3,633 | 323 | 0.322 [0.287, 0.356] | 0.344 [0.308, 0.379] | **0.361 [0.325, 0.396]** |

Paired sign-flip permutation tests over queries, 20,000 permutations, seed 12345:

| Dataset | Hybrid minus BM25 | Hybrid minus dense |
|---|---|---|
| SciFact | +0.055, p < 0.0001 | +0.044, p = 0.00035 |
| NFCorpus | +0.039, p < 0.0001 | +0.017, p = 0.0066 |

The smallest p-value this test can report is 1 in 20,001, which is 0.00005.

## Published numbers for comparison

These come from other people's runs. They were not rerun here.

| Dataset | BM25, BEIR paper | BM25, Pyserini flat | BM25, Pyserini multifield | BGE-base-en-v1.5, Pyserini |
|---|---|---|---|---|
| SciFact | 0.665 | 0.679 | 0.665 | 0.741 |
| NFCorpus | 0.325 | 0.322 | 0.325 | 0.373 |

Sources: Thakur et al., BEIR, Table 2, https://arxiv.org/abs/2104.08663, and the Pyserini BEIR reproductions, https://castorini.github.io/pyserini/2cr/beir.html. The BM25 numbers differ by implementation. Tokenizer, stemmer and title handling move them by a point or two.

## What the numbers say

The BM25 leg lands where published BM25 does on both datasets. On SciFact the hybrid is 5.5 points over the BM25 leg and 4.4 points over the dense leg. On NFCorpus it is 3.9 points over BM25 and 1.7 over dense. The permutation tests reject a zero difference in all four comparisons. No correction for the four comparisons was applied, and the smallest p-value is well under any such correction. The hybrid comes within half a point of the published BGE-base score on SciFact and sits about one point under it on NFCorpus.

They do not say gestalt is better than a stronger retriever, a reranked pipeline, or a bigger embedding model. They do not say anything about retrieval over your own notes.

## How it was run

`beir_bench.py` builds an in-memory index with gestalt's own schema. FTS5 uses the `porter unicode61` tokenizer. Vectors sit in a `vec0` table. The queries go through `search()` in `run_retrieval_evals.py`, which is the function the index server mirrors. It builds the FTS query by quoting each token and joining them with OR, takes the top 20 from each leg, and fuses them with reciprocal rank fusion at K=60. The dense-only row queries the same vector table with the same query vector. Every row is cut to the top 10 before scoring.

The embedding model is `nomic-ai/nomic-embed-text-v1.5` at revision `e9b6763023c676ca8431644204f50c2b100d9aab`, with the `search_document: ` and `search_query: ` prefixes from `tools/gestalt_embed_config.py`. Vectors are not normalised. The vec0 table ranks by L2 distance.

Each BEIR document is one unit. The title and the abstract are joined and indexed whole. Gestalt's production index splits notes into sections, so this benchmark does not exercise the chunker.

Nothing was tuned on these datasets. The author states this. The tree cannot prove it, because the choices of K=60 and the embedding model predate this benchmark and were made for a private corpus of engineering notes.

## What is stored

`results/` holds one JSON file per dataset with every per-query score, one TREC-style run file per system, and `summary.json` with the environment and the SHA-256 of the three code files behind the numbers. The score column in the run files is derived from rank, because `search()` returns an order and not a score.

## Limits

Two datasets are a small sample. Both are scientific or biomedical text, and gestalt is built for engineering notes.

The embedding model may have seen text from these datasets during training. That was not checked.

The seeds are fixed and the library versions are in `summary.json`. A different GPU or library build can still move the fourth decimal. One run was made, and it has not been repeated on other hardware.

## Reproduce

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r tools/requirements.txt -r evals/retrieval/requirements-bench.txt
python3 evals/retrieval/beir_bench.py --datasets beir/scifact beir/nfcorpus --out evals/retrieval/results
```

Use a GPU. Embedding about 8,800 abstracts takes a few minutes on one and tens of minutes on a laptop CPU. The first run downloads the datasets through `ir_datasets` and the model from Hugging Face. Add `--limit-docs 300` for a quick smoke test on a subset. That prints a clearly marked subset result and must not be quoted.

## Dataset licences

The datasets are downloaded at run time and never committed here. SciFact's annotations are CC BY 4.0 and its abstracts are ODC-By 1.0, per the SciFact repository's LICENSE.md. The BEIR paper's appendix lists a different SciFact licence, so check the source before you redistribute. NFCorpus is free for academic use, and other uses need the NutritionFacts.org terms. The embedding model is Apache-2.0.
