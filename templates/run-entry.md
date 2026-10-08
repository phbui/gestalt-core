---
type: run
title: "{{title}}"
repo: 
tags: [run]
aliases: []
run_id: {{run_id}}
commit: 
config_sha256: 
seed: 
created: {{date}}
updated: {{date}}
last-verified: {{date}}
confidence: high
---

<!-- Use this note for a run whose result is worth keeping. Most runs need no note: the manifest under runs/<id>/ is the record. Create the note when a number from the run enters a paper, a decision or a figure.
  Fill run_id, commit, config_sha256 and seed from `run-manifest cite <id>`. Delete this comment in a real entry. See knowledge/the-relevant-entry.md. -->

## Run ^run-id

Citation line, pasted from `run-manifest cite {{run_id}}`:

`run {{run_id}} | commit <7 hex> | config <8 hex> | seed <n>`

The manifest lives at `runs/{{run_id}}/manifest.json` in the project repo. It is not copied into gestalt.

## Question ^run-question

One sentence. What did this run set out to test.

## Result ^run-result

What the run showed, with the numbers. Each number carries its source file inside the run directory, for example `runs/{{run_id}}/metrics.json`. State the effect size or the interval, not only the point estimate.

## Where the numbers are used ^run-used-in

List the paper table, figure or note that cites this run. One line each.

## Caveats ^run-caveats

Dirty tree or not. Data version. Anything that makes the run hard to repeat. If a later run supersedes this one, link its note here and set `confidence: low`.
