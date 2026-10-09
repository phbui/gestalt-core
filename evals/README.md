# Evals

This directory holds four kinds of evaluation. Each has its own guide.

| Path | What it measures | Guide |
|---|---|---|
| `retrieval/` | Retrieval quality on public BEIR sets, MTEB, a golden set of your own notes, calibration and sealed held-out sets. `reproduce.py` rebuilds the headline table. | `retrieval/BENCHMARKS.md` |
| `zoo/` | gestalt against other retrievers on the same queries, under one result schema and cost block. | `zoo/README.md` |
| `memory/` | Retrieval over long chat histories, LongMemEval-S and LoCoMo-10. | `memory/README.md` |
| `configs/`, `run_evals.py` | Skill evals. Every skill has a config with trigger cases and output assertions. | below |

## Skill evals

`configs/<skill>.yaml` is the source of truth for one skill. `run_evals.py` reads it.

```
python3 evals/run_evals.py validate                 # check every config is well formed
python3 evals/run_evals.py triggers                 # keyword-match each trigger case against the skill's description
python3 evals/run_evals.py run <skill> <output.txt> # run the output assertions against one captured output
python3 evals/run_evals.py baseline save            # write evals/baseline.json from the current results
python3 evals/run_evals.py baseline check           # exit 1 on a regression from that baseline
```

No baseline and no captured transcripts ship with the repository. Captures hold private content, so they stay on the machine that made them. Run `baseline save` once on your own checkout before you use `baseline check`. The trigger score is a lint on keyword overlap with the skill's description. It does not test how a model routes a request.
