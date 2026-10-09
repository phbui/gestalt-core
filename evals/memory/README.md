# Memory retrieval harnesses

Two harnesses score gestalt's retrieval stack on long chat memory. `longmemeval_bench.py` runs LongMemEval-S. `locomo_bench.py` runs LoCoMo-10. `merge_shards.py` pools split runs. Both benchmarks score retrieval only. No model answers a question, so the numbers are retrieval recall and never QA accuracy.

## What they measure

Each question searches its own history. For LongMemEval that is the question's haystack of about 50 sessions. For LoCoMo it is the question's own conversation. The evidence is a set of sessions or turns. The harness asks how high the evidence ranks.

The index is gestalt's. `beir_bench.build_db` builds the FTS5 table and the sqlite-vec table. `run_retrieval_evals.search_scored` runs the two legs and the fusion. The text is cut by the index builder's own `_split_text` at 2,000 characters with 150 overlap. Every chunk starts with a header that holds the date and the speaker, the way gestalt starts a chunk with a title and heading. The session id never enters the text, because LongMemEval names its evidence sessions `answer_...`.

Systems are `bm25` (the FTS leg), `dense` (the vector leg), `hybrid` (RRF, or convex with `--fusion convex --alpha 0.4`) and, with `--rerank on`, `hybrid_rerank`. The base systems always run with the reranker off. The flags `--embed-profile`, `--fusion`, `--alpha`, `--rerank`, `--rerank-model` and `--rerank-depth` work as in `beir_bench.py`. `--depth` sets how many chunk candidates each system returns before chunks are folded to units. The default is 50.

Outside the author's own hub, the reranker does not switch on by itself. Pass `--rerank on` to get the `hybrid_rerank` system.

## Datasets

Datasets download into `~/.cache/gestalt-bench/` on first use. They never enter the repo. A cached file whose sha256 differs from the pin stops the run.

| Benchmark | Source | Pin | sha256 | Licence |
|---|---|---|---|---|
| LongMemEval-S | `xiaowu0162/longmemeval-cleaned` on Hugging Face, file `longmemeval_s_cleaned.json` (277 MB) | commit `98d7416c24c778c2fee6e6f3006e7a073259d48f` | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` | Code is MIT. The dataset card states no separate data licence. |
| LoCoMo-10 | `data/locomo10.json` from `snap-research/locomo` (2.8 MB) | commit `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376` | `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4` | CC BY-NC 4.0. Non-commercial use only. |

## Levels and metrics

`--granularity session` indexes one unit per session. `--granularity turn` indexes one unit per turn. `both` runs the two indexes. The summary has three levels. `session` is the session-unit index scored against the evidence sessions. `turn` is the turn-unit index scored against the evidence turns. `session_from_turns` folds the turn ranking to sessions and scores it against the evidence sessions. It is a different system from `session`. Do not merge the levels.

`recall@k` is the share of the evidence found in the top k, for k in 1, 3, 5 and 10. `any@k` asks for at least one. `all@k` asks for every one. LongMemEval's own `recall_any` and `recall_all` are `any@k` and `all@k`. MRR and nDCG@10 use binary relevance.

The core metrics carry a seeded 95 percent bootstrap interval (10,000 resamples, seed 12345). Hybrid against bm25, hybrid against dense and rerank against hybrid get a seeded paired sign-flip permutation test, on nDCG@10 and recall@10. Both resample by cluster. For LongMemEval a cluster is one question, because each question has its own haystack. For LoCoMo a cluster is one conversation, because the questions of a conversation share its history. LoCoMo intervals are therefore wide. A percentile bootstrap over 10 conversations may under-cover, and the result JSON records this in `ci_method`. The summary states the unit in `metric_notes.resampling_unit`.

Every question type (LongMemEval) and every category (LoCoMo) is reported next to the pooled `all` row.

## LongMemEval details

Session evidence is `answer_session_ids`. Turn evidence is every turn flagged `has_answer`. In this file 32 answerable questions have an evidence session with no flagged turn. Their turn gold is the flagged turns only. The 30 abstention questions (ids ending `_abs`) are searched and left out of every recall metric. They feed one number, the AUROC of the top-1 score for telling answerable from abstention questions. That score compares across different haystacks, so read it as a signal check only. A query with no comparable score is dropped from the pool and counted in `dropped_no_score`. That covers a query that returned nothing and a query where the reranker fell back.

## LoCoMo details

1,986 questions exist. Four have no evidence. The other 1,982 are scored. Evidence is a list of `dia_id` turn ids. Some strings are joined or mistyped (`D8:6; D9:17`, `D:11:26`), so every `D<n>:<m>` match counts. Ids with no matching turn are counted in the summary.

The dataset stores the category as a number. The names (1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial) follow the paper order and are inferred. Category 5 is reported as its own group. It stays out of the pooled `all` row unless you pass `--include-adversarial`. Image captions are not indexed. A conversation has only 19 to 32 sessions, so session recall@10 is a coarse test. The summary prints the Penfield caveat: the answer key has a documented 6.4 percent error rate.

## Commands

Run from the repository root. `python3` needs the bench requirements (`sentence-transformers`, `sqlite-vec`, `numpy`).

A smoke run scores the first N questions. It must not be quoted. The summary marks it `smoke_subset: true` and the merge refuses it. Use `--systems bm25` when the dense leg would take over a minute on CPU. The first 20 LongMemEval questions are all of one type, `single-session-user`.

```
python3 evals/memory/longmemeval_bench.py --out runs/smoke-lme --systems bm25 --limit-questions 20
python3 evals/memory/locomo_bench.py --out runs/smoke-locomo --conversations conv-26 --limit-questions 20 --granularity session
```

A full run:

```
python3 evals/memory/longmemeval_bench.py --out runs/lme --granularity both --rerank on --rerank-model bge
python3 evals/memory/locomo_bench.py --out runs/locomo --granularity both --rerank on --rerank-model bge
```

Variants: add `--fusion convex --alpha 0.4` for convex fusion, `--embed-profile qwen3-4b` for the larger embedder, `--rerank-model qwen3-4b` for the larger reranker. Give each variant its own `--out`.

A sharded run. `--shard K/N` scores shard K of N, whole haystacks together and in dataset order. Several processes, sessions or machines can each take a shard. Point them all at one scope directory, or give each its own run directory. Then pool.

```
for k in 1 2 3 4; do python3 evals/memory/longmemeval_bench.py --granularity session --shard $k/4 --out runs/lme-$k --scope-dir runs/lme-scopes; done
python3 evals/memory/merge_shards.py --out runs/lme runs/lme-scopes
```

A resumed run. Run the same command again. Every scope that finished is loaded from its file and skipped, so a kill loses at most the scope that was running. A scope file written under a different setting stops the run and names the setting that differs. `--rebuild` recomputes every scope and overwrites the files.

```
python3 evals/memory/longmemeval_bench.py --out runs/lme --granularity both --rerank on --rerank-model bge
```

## What the merge checks

`merge_shards.py` takes run directories or scope directories. It refuses to pool when any of these holds:

1. Two scope files differ in benchmark, dataset sha256, systems, granularity, depth, chunking, rerank setting, fusion mode, alpha, embed profile, code hash, seed or bootstrap count.
2. Any scope file comes from a smoke run.
3. A scope appears twice.
4. The scope files are not exactly the scopes of the dataset, or a scope does not hold exactly the dataset's question ids. The merge finds the dataset by the sha256 in the fingerprint. Pass `--data` if the recorded path does not exist on the merging machine.
5. A run directory's own `summary.json` disagrees with the scope files or with the dataset counts.

The pooled summary has the shape of an unsharded one. It lists every source under `merged_from` with the sha256 of its summary and its environment. `generated_utc` is the merge time.

## On-disk layout

```
runs/lme/
  summary.json          levels, tests, config, environment with code hashes, counts, dataset record
  per_question.jsonl    top 10 units and scores for every question, level and system
  scopes/
    <scope-id>.json     one file per haystack or conversation, with the run fingerprint
```

Every output is written atomically: a temp file in the same directory, fsync, then rename. A crash leaves the old file or the whole new one. A scope file that cannot be parsed is ignored and recomputed.

## Cost

Time is dominated by embedding. Times divide the chunk count by the measured rate of 44 to 66 documents per second in float32 on one GPU.

| Run | Chunks to embed | Time per granularity |
|---|---|---|
| LongMemEval-S, session units | 157,632 | 40 to 60 minutes |
| LongMemEval-S, turn units | 301,139 | 76 to 114 minutes |
| LoCoMo-10, session units | 559 | minutes |
| LoCoMo-10, turn units | 5,882 | minutes |

No chunk text repeats across LongMemEval haystacks, because every chunk carries its session date. Measured on the pinned file at both granularities, the repeat count is zero. The encoder therefore keeps document vectors for one scope and drops them at the next. It keeps query vectors for the run.

Disk: the LongMemEval JSON is 277 MB and the scope files are a few MB per hundred questions. RAM: loading the JSON takes a few GB. One index at a time lives in memory. The reranker needs a GPU for any practical run. The first embedding run downloads the model weights, which needs network access.

## Honesty rules

Retrieval recall is not QA accuracy. Never put these numbers beside a vendor QA number. Report turn and session level separately. Report every category. State the dataset version and the sha256. The summary carries all of these.

Published retrieval write-ups for LoCoMo score different things. One scores turns on `dia_id` (recall@5 of 52.4 percent, recall@10 of 59.9 without an LLM). Another scores sessions (recall@10 of 0.795). The two are not comparable, and neither is comparable to a run that differs in chunking, header or depth. Compare only runs of this harness.

A 2026 study found an off-the-shelf cross-encoder over a fused top 10 hurt LoCoMo Hit@1. Read the `hybrid_rerank` rows against `hybrid` on every category before you trust the reranker here.

When the reranker fails on any query, the summary carries `rerank_invalid`, every level lists `hybrid_rerank` under `void_systems`, and the process exits with code 3. A void row is the hybrid order under another name. Do not quote it. The merge sums the fallbacks and exits with code 3 too.

Before a long run, read "What can go wrong" in `evals/retrieval/BENCHMARKS.md` (in the public repo) and run `python evals/retrieval/check_env.py`. The same guards apply here: progress lines, the stall watchdog, the fallback refusal and the resume engine.
