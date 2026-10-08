---
type: note
title: "Reciprocal rank fusion"
repo: gestalt-core
tags: [search, retrieval]
aliases: [RRF, rank fusion]
created: 2026-10-08
updated: 2026-10-08
last-verified: 2026-10-08
confidence: high
---

## Overview ^overview

Reciprocal rank fusion merges several ranked lists into one. Each list can come from a different search method. A keyword search and a vector search are the usual pair. The fused list needs no tuning data and no knowledge of how either method scores.

## Formula ^formula

A document scores the sum, over every list that contains it, of one divided by the constant K plus its rank in that list. Rank starts at one. A document missing from a list adds nothing from that list. The fused order is the order of these sums.

## Why ranks and not scores ^why-rank-not-score

Keyword scores and vector distances live on different scales and drift as the corpus grows. A raw score from one method cannot be added to a distance from another. Ranks are comparable across any two methods, so the fusion survives a change of embedding model or a larger index. The cost is that the fused score carries no absolute meaning. Never set a cutoff on it.

## The constant K ^k-constant

K softens the gap between the first and second place. A small K lets one list's top hit dominate. A large K flattens the lists toward a tie. The common default is 60, taken from the original paper. This repository keeps 60 and does not tune it.
