# Retrieval zoo

The zoo runs many retrieval systems on the same BEIR set with one engine. Every system sees the same documents, queries and qrels. It is scored by beir_bench's own scorer and statistics. So any two systems pair on the same queries, and every result carries its cost.

## Run

```
python evals/zoo/zoo_run.py --dataset beir/scifact --systems bm25 nomic qwen3-4b nomic+bm25:rrf --out results/zoo-scifact
```

`--systems` takes names from `zoo.yaml`. `A+B:rrf` and `A+B:convex@0.4` fuse two declared systems on the spot. A bm25 part is always the lexical leg, so `nomic+bm25:rrf` and `bm25+nomic:rrf` score alike, and convex puts its weight on the bm25 part. Pass `lexical_first: false` to `fused` to keep the order given. The run writes `beir-scifact.json`, one TREC run file per system and `summary.json` into `--out`. A killed run resumes from `--work-dir`. Exit 3 means a reranker fell back on some query, and the run is void.

Compare two systems with `python evals/retrieval/pair_runs.py A.json B.json --system-a nomic --system-b bm25`. Check a file with `python -m evals.zoo.schema FILE...`.

## Add a system in five lines

Add an entry under `systems:` in `zoo.yaml`.

```yaml
  my-model:
    adapter: dense_hf
    args: {model: org/name, revision: <commit sha>, query_prefix: "query: ", dtype: bfloat16}
```

Pin `revision` for any run you publish. A new kind of system is one class in `evals/zoo/` that follows `adapter.Searcher`, plus one line in `ADAPTERS` in `zoo_run.py`.

## Adapters

`bm25` uses gestalt's FTS5 index and query builder. `dense_hf` encodes with any sentence-transformers model in resumable blocks and searches exactly. It keeps the vectors as the model returns them, as beir_bench does. `normalize: true` rescales them to unit length. `tests/test_zoo_parity.py` checks that bm25, dense and the rrf hybrid equal beir_bench on one corpus. `colbert` does the same with one vector per token. `fused` joins two systems with the engine's rrf or convex fusion. `rerank` re-scores the top `depth` hits of any system with a gestalt_rank reranker.

## Multi-vector leg

`colbert` runs a late-interaction model. `gte-moderncolbert` is `lightonai/GTE-ModernColBERT-v1`, pinned by revision and loaded with the Sentence Transformers 6 `MultiVectorEncoder`. It needs no `pylate`. Each document is stored as one 128-wide vector per token, capped at `max_doc_tokens` (300). A block of documents is one float16 `.npy` of all its token rows plus a receipt that lists the rows per document. The receipt is the offsets index, and a killed run resumes at the first block without a valid receipt. The fingerprint holds the model, revision, dtype and `max_doc_tokens`. Search is exact. Every query token is scored against every document token, so the cost per query grows with the corpus and there is no candidate stage. The scan runs in numpy on the CPU. `describe()` adds `vectors_per_doc_mean`, and `index_bytes` shows the size of the store. A fused system takes a third leg through `c`, for rrf only. `nomic+bm25+gte-moderncolbert:rrf` is declared in `zoo.yaml` that way.

Run it on a machine with a GPU:

```
python evals/zoo/zoo_run.py --dataset beir/scifact --systems bm25 nomic gte-moderncolbert nomic+bm25:rrf nomic+gte-moderncolbert:rrf nomic+bm25+gte-moderncolbert:rrf --out results/zoo-scifact-colbert
```

## Cost fields

Each system has a `cost` block. It is measured, never estimated. A field that cannot be measured is null.

| Field | Meaning |
| --- | --- |
| `index_seconds` | Wall time to store, index and embed the corpus, including model load. A fused or reranked system adds its parts. A resumed run counts only the work it redid. |
| `docs_per_second` | Documents divided by `index_seconds`. |
| `query_latency_ms_p50`, `query_latency_ms_p95` | Time of one `search` call with one query, over the first `--latency-queries` queries. |
| `latency_queries` | How many queries the latency percentiles used. |
| `queries_per_second` | All queries in one batched `search` call, divided by its wall time. |
| `peak_vram_mib` | `torch.cuda.max_memory_allocated` over indexing and searching. Null on a CPU. |
| `index_bytes` | Bytes in the system's work directory, documents included. Fused systems add their parts. |
| `parameters` | Model parameters. Null for BM25. Fused and reranked systems add their parts. |
| `dtype` | Model dtype. |
| `gpu` | GPU name, or null. |

Needs `pip install -r evals/retrieval/requirements-bench.txt`, which includes `jsonschema`.

## Results

The float16 table for SciFact and NFCorpus, with the parity check against the bench and the cost caveats, is in `evals/retrieval/BENCHMARKS.md` under "The same queries against other retrievers". Index throughput in a result file is honest only for a fresh embed. A resumed run reports a resume rate.
