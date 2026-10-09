# Contributing

Thank you for reading this far. Issues and pull requests are welcome. This page says how the repository is built, so your work lands and stays.

## How this repository is produced

`gestalt-core` is exported from a private knowledge base by a script that strips personal notes and settings and runs a set of leak gates. After each green CI run on the source, the export replaces the whole tracked tree in one commit.

The next export overwrites this repository. A change merged here survives only if the maintainer carries it back into the source before that export. The export refuses to run while this repository holds commits that came after the last export, so a merged change that is not carried back blocks the sync instead of vanishing. The maintainer then carries the change back and runs the sync with an explicit override. The merged change may be reworded to match the source tree.

## Before you open a pull request

1. Run the tests: `python3 -m pytest tests -q`. The suite passes on a clean checkout. Some checks skip when they need the maintainer's full corpus or a fleet machine, and pytest prints the reason for each skip. The tests use stub encoders and need no model download.
2. Run the lexical smoke: `python3 tools/gestalt-index-builder.py --fts-only` then `bash tools/gestalt search "merge two ranked lists"`. It should print `sample-rank-fusion` sections.
3. If you touched retrieval, run `python3 evals/retrieval/run_retrieval_evals.py --mode fts` and say in the pull request what moved. The golden set in `evals/retrieval/golden.yaml` is the public sample. A retrieval change that moves a BEIR number needs a rerun of `evals/retrieval/beir_bench.py` on at least SciFact, with the summary attached.
4. Keep the pins. `tools/gestalt_embed_config.py` fixes the embedding model revision and its remote code commit, and `evals/retrieval/requirements-bench.lock` fixes the benchmark environment. A pull request that changes a pin says why and shows that the benchmark numbers did not move, or reports the new ones.

## What a good pull request looks like

One change per pull request. A test for every behaviour change. A commit message that says what changed and why, in plain sentences. No new network calls, no new data collection, and no new place where note text reaches a model, unless the pull request says so in its first paragraph and `SECURITY.md` is updated with it.

## What will not be merged

Anything that bundles a benchmark dataset. The datasets have their own licences and are downloaded at run time only. Anything that weakens a safety hook without an opt-in flag. Anything that adds a hosted dependency to the default path.

## Reporting a bug

Use the bug report template. Say which mode you ran, lexical or hybrid, which Python and which operating system, and paste the command and its output. For a security problem, read `SECURITY.md` and report privately.
