# Third-party notices

gestalt-core is Apache-2.0. Nothing below is bundled in this repository. Each item is installed, downloaded or pulled at run time by the person who runs the software, under that item's own terms. The Python packages come from `evals/retrieval/requirements-bench.txt`, and `evals/retrieval/requirements-bench.lock` holds the exact versions behind the stored numbers. The licence names were read from each project's repository or model card on 2026-10-08. Check them again before relying on them, because they can change.

## Python packages, installed from `tools/requirements.txt` and `evals/retrieval/requirements-bench.txt`

| Package | Licence |
|---|---|
| sentence-transformers | Apache-2.0 |
| transformers | Apache-2.0 |
| torch | BSD-3-Clause |
| numpy | BSD-3-Clause |
| sqlite-vec | MIT or Apache-2.0, at your choice |
| mcp (the Python SDK) | MIT |
| httpx | BSD-3-Clause |
| einops | MIT |
| PyYAML | MIT |
| ir_datasets | Apache-2.0 |
| mteb | Apache-2.0 |
| pytest | MIT |

The exact versions of the benchmark environment are in `evals/retrieval/requirements-bench.lock`, which lists their transitive dependencies as well.

## Models, downloaded from Hugging Face on first use

| Model | Licence | Note |
|---|---|---|
| nomic-ai/nomic-embed-text-v1.5 | Apache-2.0 | Weights pinned to one revision. Its modelling code comes from nomic-ai/nomic-bert-2048 and is pinned to one commit. Both pins are in `tools/gestalt_embed_config.py`. |
| Qwen/Qwen3-Reranker-0.6B | Apache-2.0 | Optional reranker. |
| Qwen/Qwen3-Embedding-4B | Apache-2.0 | Optional embedding profile. |
| BAAI/bge-reranker-v2-m3 | Apache-2.0 | Optional reranker. |

## Services, optional, started from `docker-compose.yml` and `graphiti/`

| Service | Licence |
|---|---|
| Letta | Apache-2.0 |
| Graphiti | Apache-2.0 |
| FalkorDB | Server Side Public License, version 1. Source available, not an OSI open-source licence. It is pulled as a container image and never linked into this code. |
| Ollama | MIT |

## Benchmark datasets, downloaded by the harnesses and never stored here

| Dataset | Terms | What that means for you |
|---|---|---|
| BEIR SciFact | The two primary sources disagree. The BEIR paper (appendix E) lists CC BY-NC 2.0. The SciFact repository states CC BY 4.0 for the annotations and ODC-By 1.0 for the abstracts. Some Hugging Face mirror cards state yet other licences. | Cite the SciFact authors (Wadden et al., 2020). Until the conflict is settled, treat SciFact as non-commercial. |
| BEIR NFCorpus | Free for academic use. Other uses need the terms of NutritionFacts.org, from which the documents come. | Academic and research use only unless you obtain other terms. |
| The other public BEIR sets | The BEIR paper (appendix E) lists MS MARCO as MIT for non-commercial research purposes, FEVER, NQ and DBPedia as CC BY-SA 3.0, ArguAna and Touché-2020 as CC BY 4.0, CQADupStack as Apache-2.0 and SCIDOCS as GPL-3.0. It states no licence for Climate-FEVER. | Check the set you run. |
| LongMemEval | The code is MIT. The data states no licence. The filler conversations come from ShareGPT and UltraChat. | Link to it and do not redistribute the data. |
| LoCoMo | CC BY-NC 4.0 | Non-commercial use only. `locomo10.json` must never be committed here. |
| MTEB tasks | Each task carries its own licence in its metadata. | Check the task you run. |

Because of the SciFact, NFCorpus and LoCoMo terms, the benchmark pipeline as a whole is not free for commercial use even though the code is. A commercial user can run the code and the Apache-2.0 models, and must choose datasets whose terms allow it.
