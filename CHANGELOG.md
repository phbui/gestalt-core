# Changelog

All notable changes to this repository. The version in `VERSION` and in `CITATION.cff` match the newest entry here.

## 0.1.0, 2026-10-12

First public release.

- Markdown knowledge base indexed into SQLite: FTS5 BM25 with the porter tokenizer and dense vectors from stella_en_400M_v5 in sqlite-vec, split at headings with block ids. Embedding profiles in `tools/gestalt_embed_config.py` pin the model, its revision, its prefixes and its fusion and reranker defaults; `stella-400m` is the default, and `nomic` selects a 137M embedder with reciprocal rank fusion.
- Search over both legs, fused by the profile's rule (the dense order with lexical-only hits last under `stella-400m`, reciprocal rank fusion under `nomic`), with an optional cross-encoder reranking stage (Qwen3-Reranker-0.6B blended with the dense score by default, Qwen3-Reranker-4B or bge-reranker-v2-m3) behind environment flags.
- An MCP server in stdio and HTTP modes, a command-line search, and Claude Code and Cursor hooks, skills, rules and agents.
- An evaluation harness: a golden set with recall, MRR and abstention metrics and paired bootstrap comparisons, a BEIR harness for every public BEIR set with bootstrap intervals and paired permutation tests, an MTEB adapter, and LongMemEval-S and LoCoMo-10 retrieval harnesses.
- Reported numbers: nDCG@10 on BEIR SciFact, NFCorpus and FiQA in `evals/retrieval/BENCHMARKS.md`, with the result files in `evals/retrieval/results/`, a comparison with released systems in `docs/released-systems.json`, and every zoo system's score and latency on one GPU in `docs/measured-systems.json`.
- A resumable benchmark engine behind the BEIR and MTEB harnesses. Documents, a file-backed FTS5 index, float32 embedding blocks and every finished query persist in a work directory, so a killed run continues where it stopped. Corpora above 300,000 documents use exact blocked search on the GPU, and `--work-dir`, `--engine`, `--block-size`, `--rebuild` and `--resume` control it.
- A dev and test split of the golden set. `assign_splits.py` tags cases so that correlated cases share a side. `choose_config.py` picks a configuration on dev by a rule fixed in advance and reports the test side once.
- A regression gate that fails closed. `run_retrieval_evals.py --baseline check` exits 1 on a regression and when the banked and current runs are not comparable. `--baseline bank --config-name default|hub` banks each configuration, and a run whose reranker fell back is refused with exit code 3.
- A GPU duty cycle, `GESTALT_GPU_DUTY`, that pauses after each model call in proportion to the time the GPU was busy. The default of 1.0 adds no pause.
- Per-haystack and per-conversation resume in the LongMemEval and LoCoMo harnesses. Each finished scope writes its own file, and `merge_shards.py` pools sharded runs after it checks that they agree.
- Pinned model weights and remote modelling code, and a locked benchmark environment in `evals/retrieval/requirements-bench.lock`.
