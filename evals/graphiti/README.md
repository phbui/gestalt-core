# Graphiti head-to-head (X7)

Does the temporal graph add value over plain search? This directory holds the design, the first question set and the audit schema. Nothing here has been run. The hub is busy, so every run is a later step.

## The four arms

All four answer the same questions with the same answering model and the same context token budget.

1. A0, no memory. The floor.
2. A1, `gestalt_search` only.
3. A2, Graphiti only (`search_memory_facts` plus `search_nodes`).
4. A3, both. This is today's injection.

## Case format

`cases.yaml` holds one row per question: `id`, `category`, `question`, `gold_slugs`, `answer`. The categories follow LongMemEval: multi-session, temporal, knowledge-update, abstention and extraction. `answer` is null until the owner fills it from the entry. An abstention case has no gold slug, and the correct reply is to say the information does not exist. The first set has 17 questions. The design calls for 60 or more, written from real sessions, before a decision. Questions stay generic because this directory is not encrypted.

## Metric

A judge with a fixed rubric scores each answer as correct or not. Validate the judge first against 30 human labels and require at least 90 percent agreement. Also record context tokens, latency, abstention correctness and the stale-fact rate (the answer used a fact whose `invalid_at` is in the past). Report per category. Compare arms on the same questions, paired, with a bootstrap of the accuracy difference resampled by question.

Attribution comes from the injection log. The hook writes `injections.jsonl` in the state directory, one line per prompt, holding a prompt hash, each injected item's source, id and rank, and whether each source answered in time. A later pass joins answer claims to items.

## Extraction audit

Before trusting any graph arm, audit extraction precision. `tools/graphiti-audit.py` samples facts and prints each beside its source episode. A person labels each `correct`, `wrong` or `unsupported`. `--score` prints precision with a Wilson interval. Use 73 to 97 facts for a +-10 point interval. `audit-example.json` shows the shape with synthetic content. Real samples go to the state directory, never to the repo.

## Keep-or-cut rule (PROPOSAL for the owner, not yet approved)

Keep the graph in the injected context only if A3 minus A1 has a one-sided 95 percent lower bound above zero on the knowledge-update and temporal categories. Also keep it if A3 cuts context tokens at equal accuracy. Otherwise demote it to opt-in. The threshold is the research notes' own proposal (RC-ai-research section 3, marked there as inferred). Review date: 2026-11-15, or sooner once 60 questions and the audit exist.
