# Graphiti head-to-head (X7)

Does the temporal graph add value over plain search? This directory holds the design, the question set, the runner and the audit schema.

## State, 2026-10-08

The runner `run_e2e.py` is written and its tests pass against stubs (`tests/test_graphiti_e2e.py`). It has not been run against a live model or a live Graphiti. Graphiti and the answering models were not reachable from the seat that built it. The question set has 63 cases: 12 multi-session, 12 temporal, 13 knowledge-update, 12 abstention and 14 extraction. Every non-abstention answer is a draft written by a model from the gold entry. Each draft carries an `evidence` quote, and `tests/test_graphiti_e2e.py` checks that every quote is an exact sentence of its entry (it runs wherever `knowledge/` is decrypted). The owner has not reviewed the answers. No report may be trusted before that review. `human-labels.jsonl` holds no labels, so the judge is not calibrated, so `report` refuses to run.

## The four arms

All four answer the same questions with the same answering model and the same context token budget (1,500 tokens by default, cut by the prompt hook's own `apply_token_budget`).

1. A0, no memory. The floor.
2. A1, gestalt search only: five hits, each as heading plus snippet. By default this is the tool path, `gestalt_search` with `semantic=True`, which loads the embedding model. With `--retriever fts` it is the hook path, `gestalt_search_fts`, which is lexical only.
3. A2, Graphiti only: `search_memory_facts` through the hook's `retrieve_graphiti`.
4. A3, both, fused by the hook's `rrf_merge` and then cut by the same budget.

`--retriever hybrid|fts` (default hybrid) picks the gestalt leg of A1 and A3. Every row in `contexts.jsonl` and `answers.jsonl` records it, and the report prints it. A cached context is rebuilt when the flag or the `--ctx-tokens` budget changes. The report refuses a run that mixes retrievers. Every context and answer row also carries `ctx_sha`, the sha256 of the retriever name, the token budget and the context text. `contexts` writes after every case, so a killed run resumes where it stopped, and it skips a case only when its row is present and its hash matches. `answer` skips a row only when `(id, arm, rep, answerer, ctx_sha)` all match, so a different retriever or budget never reuses an answer. Rows from older runs have no `ctx_sha`, never match and are asked again. Run the whole grid twice, once per retriever, to see both paths. The search never writes hit-log rows, because the runner sets `GESTALT_HITS_LOG=off` before it loads the server.

A3 is not byte-for-byte today's injection. The live hook injects references to entries and no snippet text (`claude-tree/hooks/prompt-intelligence.py`, `retrieve_gestalt_sync` sets `text` to empty). A3 here feeds the snippet text so the answerer can use it. That makes A1 and A3 a fair test of content, not of the reference format.

## Case format

`cases.yaml` holds one row per question: `id`, `category`, `question`, `gold_slugs`, `answer`, `outdated`, `evidence`. The categories follow LongMemEval: multi-session, temporal, knowledge-update, abstention and extraction. `outdated` is the earlier answer a knowledge-update case replaced, or null. `evidence` lists the entry slug, the block id and the exact sentence behind each fact. An abstention case has no gold slug and no evidence. The correct reply is to decline. Questions stay generic because this directory is not encrypted. The design calls for 60 or more cases, so this set meets the floor, but it was written from the entries and not from real sessions.

## The runner

Run the steps in this order. Private output goes to `--out`, default `~/artifacts/graphiti-e2e`, never into the repo.

```
python3 evals/graphiti/run_e2e.py contexts
python3 evals/graphiti/run_e2e.py answer --answerer claude:sonnet --reps 3
python3 evals/graphiti/run_e2e.py judge --judge ollama:qwen3:14b
python3 evals/graphiti/run_e2e.py calibrate --sample
python3 evals/graphiti/run_e2e.py calibrate
python3 evals/graphiti/run_e2e.py report
```

`contexts` builds and caches one context per case and arm in `contexts.jsonl`, with the token estimate and the fetch time. `answer` asks the answering model `--reps` times per case and arm and appends to `answers.jsonl`. The answer prompt is fixed: "Answer the question using only the context. If the context does not contain the answer, reply exactly INSUFFICIENT INFORMATION." The answerer is `claude:<model>` (a `claude -p` call in safe mode with no tools and no session file) or `ollama:<model>` (a non-streaming `/api/chat` call with temperature 0, seed 1 and a context of 8,192 tokens).

**What invalidates the caches.** Every context, answer and verdict row carries `case_sha`, the sha256 of the canonical JSON of the whole case record. Editing a case's question, answer, outdated flag or any other field changes it. A changed case rebuilds its context, asks its answers again and is judged again. `answer` refuses a context built for an older version of the case, so run `contexts` first. The retriever, the token budget and the judge prompt hash also invalidate, as described above. Human labels bind to `resp_sha`, the hash of the response text, so they are unaffected. `calibrate` always demands at least 30 labels, and a smaller `--n` never lowers that gate.

`judge` wraps the model response in `<model_response>` tags and tells the judge that the text inside is data. When `OLLAMA_URL` names a host other than localhost, 127.0.0.1 or ::1, the runner prints a warning that retrieved snippets are leaving the machine. `judge` grades each answer with a LongMemEval-style template and a JSON verdict (`correct`, `uses_outdated`). The judge must come from a different model family than the answerer, and the runner refuses otherwise. A reply that is exactly INSUFFICIENT INFORMATION is settled by rule without a model call. It is correct for an abstention case and wrong for any other. The judge refuses to grade a non-abstention case whose `answer` is null. Every verdict stores `judge_prompt_sha`, a hash of the template, the category rules and the schema.

`calibrate --sample` writes a blind sheet of 30 rows, chosen with a fixed seed. It draws only from rows the model judged. Rows settled by the refusal rule are left out and counted apart. Every abstention case comes first, then the other categories in turn. A person labels each row yes or no and appends `{"id", "arm", "resp_sha", "human"}` lines to `human-labels.jsonl`, copying `resp_sha` from the sheet. A label binds to the sha256 of the response text. If the response changes, the label is rejected and listed under `rejected_labels`. `calibrate` scores the judge against the matching labels and writes `calibration.json` with agreement, Cohen's kappa, a Wilson interval, the rejected labels and the rule-judged count. It passes on at least 30 labels when the Wilson 95 percent lower bound of the agreement is 0.80 or more, or Cohen's kappa is 0.60 or more. It prints both numbers and the sample size. A change to the judge prompt changes the sha and voids the calibration.

`report` refuses to run when `answers.jsonl` holds more than one answerer and `--answerer NAME` does not select one. It prints the answerer. It also refuses to run unless calibration passed for the current `judge_prompt_sha`, every model-judged row used the same `judge_model` that calibration used, every answer is judged, and no gold answer is null. It prints, per category and arm over `--reps`: accuracy, mean context tokens, median latency, stale-fact rate and refusal rate. It then prints A3 minus A1 and A2 minus A1 per category and pooled over knowledge-update and temporal, each with the mean and a one-sided 95 percent lower bound from a paired bootstrap (10,000 draws, seed 12345, resampled by case inside each category, then combined). The sample per category is tiny, so read every interval as wide.

Each answer is logged as `{id, arm, rep, answerer, ctx_tokens, ctx_ms, ans_ms, retriever, resp, meta, judge}` in `answers.jsonl`. Progress lines go to stderr with the prefix `graphiti-e2e:`.

## Metric

Accuracy is the share of reps judged correct, averaged over cases. Context tokens use the hook's four-characters-per-token estimate. Latency is the fetch time plus the answer time. The stale-fact rate counts answers that give the `outdated` fact as current, over cases that have one. Attribution from live traffic comes from the injection log. The hook writes `injections.jsonl` in the state directory, one line per prompt, holding a prompt hash, each injected item's source, id and rank, and whether each source answered in time.

## Extraction audit

Before trusting any graph arm, audit extraction precision. `tools/graphiti-audit.py` samples facts and prints each beside its source episode. A person labels each `correct`, `wrong` or `unsupported`. `--score` prints precision with a Wilson interval. Use 73 to 97 facts for a +-10 point interval. `audit-example.json` shows the shape with synthetic content. Real samples go to the state directory, never to the repo.

## Keep-or-cut rule (PROPOSAL for the owner, not yet approved)

Keep the graph in the injected context only if A3 minus A1 has a one-sided 95 percent lower bound above zero on the pooled knowledge-update and temporal categories. Also keep it if A3 cuts mean context tokens on those categories and A3 is not worse than A1 by more than 0.05, meaning the lower 95 percent bound of A3 minus A1 is at least -0.05. Without the interval this path does not fire. Otherwise demote it to opt-in. The runner prints the verdict and both tests. The threshold is the research notes' own proposal (RC-ai-research section 3, marked there as inferred). Review date: 2026-11-15, or sooner once the answers are reviewed and the audit exists.
