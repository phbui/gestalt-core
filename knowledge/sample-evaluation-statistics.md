---
type: note
title: "Reporting small evaluation results"
repo: gestalt-core
tags: [evaluation, statistics]
aliases: [confidence intervals, significance]
created: 2026-10-08
updated: 2026-10-08
last-verified: 2026-10-08
confidence: high
---

## Overview ^overview

An evaluation with a few hundred queries gives a noisy number. Report the number with an interval, say how the interval was built, and test differences with a paired test. A bare point estimate invites over-reading.

## Wilson interval for a rate ^wilson

Use it for a proportion such as recall at five. It behaves well near zero and one, where the plain normal interval does not. It treats queries as independent. Many queries about one document are not, so the true interval is wider.

## Bootstrap interval for a mean ^bootstrap

Use it for a per-query score such as nDCG at ten. Resample the queries with replacement, take the mean each time and read the 2.5th and 97.5th percentiles. Fix the random seed and the number of resamples and publish both.

## Paired permutation test ^paired-test

Compare two systems on the same queries. For each query take the difference in score. Randomly flip the sign of each difference many times and count how often the flipped mean is as extreme as the observed one. Report the observed difference and the p-value. A paired test is more sensitive than comparing two intervals, because query difficulty cancels out.

## What to state beside the result ^disclosure

The number of queries, the model and its revision, the random seed, whether anything was tuned on the test data, and the datasets the comparison covers. Say what the result does not show.
