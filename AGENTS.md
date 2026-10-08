# Agent instructions

You are working in a workspace that carries a knowledge system called gestalt. Its notes live in `knowledge/`, one Markdown file per entry. A search index and a set of hooks put the right notes in front of you. This file tells you how to use it.

## Before you start

1. Search before you assume. Call `gestalt_search("your topic")` through the gestalt MCP server, or run `bash tools/gestalt search "your topic"` in a shell.
2. Read the entries it points at. An entry is `knowledge/<slug>.md`. A result names a section by its block id, such as `^overview`.
3. Follow `[[wikilinks]]` to related entries.
4. Cite what you use. Say which entry a fact came from.

## Tools

The gestalt MCP server in `tools/gestalt-mcp-server.py` exposes:

- `gestalt_search(query, limit=10)`, hybrid search when `GESTALT_SEARCH_MODE=hybrid` is set, lexical search otherwise.
- `gestalt_search_fts(query, limit=10)`, the lexical leg alone. It never loads a model.
- `gestalt_read(slug)`, one entry in full.
- `gestalt_route(request)`, the skill most likely to fit a request.

The Graphiti MCP server, when its container runs, adds `search_memory_facts`, `search_nodes` and `add_memory` over a temporal graph. It is optional.

## What the hooks do without asking

- Session start: injects a short memory block, and rebuilds the search index in the background when the notes are newer than the index.
- Stop: sends message excerpts to a local Letta server when one is running, and writes a session summary.
- Every Bash call: the safety hook blocks destructive commands. The audit hook appends the command to `~/.claude/audit.log`.

The README's "What the hooks do" section says what each one reads and sends.

## Writing a note

1. Search first. If an entry exists, update it. Never create a near duplicate.
2. A new entry starts from `templates/knowledge-entry.md` and goes in `knowledge/<slug>.md`.
3. Link related entries with `[[slug]]`. Give key facts a `^block-id`.
4. Rebuild the index: `python3 tools/gestalt-index-builder.py --fts-only`. Drop the flag to embed vectors too.
5. Write in an authoritative voice. Code is the truth. Rewrite stale text instead of appending to it.

## Subagents

A subagent does not inherit this file or your system prompt. Paste the grounding block from `claude-tree/references/discipline-block.md` at the top of every subagent prompt.

## Commands

| Command | Use |
|---|---|
| `/investigate <question>` | Trace a bug or a behaviour through the code and the notes |
| `/research <topic>` | Web research with cited sources |
| `/learn <repo>` | Scan a repository and write entries for it |
| `/review` | Check the notes against the code |
| `/save` | Write what this session learned into the notes |

`claude-tree/references/skill-index.md` lists every command.
