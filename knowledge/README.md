---
type: note
title: "About the knowledge folder"
repo: gestalt-core
tags: [setup]
aliases: [knowledge folder]
created: 2026-10-08
updated: 2026-10-08
last-verified: 2026-10-08
confidence: high
---

# knowledge/

The search index reads every `*.md` file in this folder. Each file is one entry. The five `sample-*.md` entries are synthetic and exist so the index builder, the search tools and the evaluation harness run on a fresh clone. Delete them when you add your own notes.

An entry starts with the front matter in `templates/knowledge-entry.md`. Each `##` heading can end in a block id such as `^overview`, and search results point at the section, not just the file.

Build the index with `python3 tools/gestalt-index-builder.py --fts-only` for the lexical leg only. Drop the flag to add dense vectors. The dense leg downloads nomic-embed-text-v1.5 and wants a GPU.
