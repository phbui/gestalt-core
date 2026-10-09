# Calibration

## Uncertainty from the reranker score

The reranker gives every candidate a raw score. This section asks one question. Does the top score tell us when a query will fail?

A query succeeds when its nDCG@10 is above zero. It fails when no relevant document reaches the top 10. The result JSON stores no gains, so this is the only label we build. We do not offer a "relevant document in the top 3" label.

Each query gets seven signals. All are oriented so that a higher value means more confident, and the direction is fixed before any data is read.

- `top1` is the top score of the system.
- `margin` is the top score minus the second.
- `entropy` is minus the softmax entropy of the top 10 scores at temperature 1.
- `n_above` counts candidates above the dataset median of `top1`. Its direction is a guess.
- `dense_top`, `bm25_top` and `rrf_margin` come from the dense, bm25 and hybrid runs. They are the comparison signals.

The scores must be raw. The `.run` files from `beir_bench.py` carry a rank-derived score column (11 minus the rank), and the CLI refuses them. Raw scores are in the bench query log, `queries-*.jsonl`. Pass it with `--query-log`.

The split is by hash. A query is in the DEV half when the first byte of SHA-256 of `gestalt-calibration-split-v1:<query id>` is even. We fit one scale and one offset by logistic regression on DEV. We report calibration on TEST.

Each signal gets these numbers, each with a 95 percent bootstrap interval over queries (the `bench_stats.py` seed and resample count).

- AUROC for predicting success.
- Expected calibration error on TEST, in 10 equal-mass bins, with the reliability table.
- The risk-coverage curve in 5 percent steps, AURC, and selective accuracy at 90, 80 and 70 percent coverage.
- The share of the gap between a random order and the oracle order that the signal closes.

Run it like this.

    python evals/retrieval/calibration.py beir-scifact.json --system hybrid_rerank \
        --query-log queries-beir-scifact.jsonl --compare top1 dense_top --json calibration-scifact.json

`--compare A B` gives the AUROC difference, a paired bootstrap interval, and a paired permutation p. That p swaps the two signals' ranks inside each query. Its floor is 1/20,001.

To correct p-values across datasets or comparisons, run `python evals/retrieval/multitest.py RESULT.json ...`. It prints Holm and Benjamini-Hochberg adjusted values. The same file has Friedman's test with the Nemenyi critical difference for a systems-by-datasets table.

What this does not claim. A high AUROC says the score ranks failures below successes on these queries. It does not say the score is a probability. The calibration fit does that, and only on a held-out half of one dataset. The label counts a query as a success when any relevant document is in the top 10, so a query with one relevant hit at rank 10 counts the same as a perfect one. Results on one dataset do not transfer to another without a new fit.

The search server can use the `top1` fit. Set `GESTALT_ABSTAIN=on` and point `GESTALT_CALIB` at the JSON, and each search result carries `confidence` and `abstain` (see "Abstention and link expansion" in the README). The fit is only as good as the dataset it came from, so refit on your own queries before you trust a threshold.
