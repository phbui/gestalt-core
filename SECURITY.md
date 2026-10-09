# Security policy

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting, which is enabled for this repository: open https://github.com/phbui/gestalt-core/security/advisories/new and describe what you found. Do not open a public issue for a vulnerability. You will get a first reply within 14 days. A fix or a documented workaround follows in the next release, and the advisory is published when the fix lands.

## Supported versions

The `main` branch and the most recent tagged release receive fixes. Older tags do not.

## What this software does on your machine

Read this before you install the hooks. Everything below is by design, and each item is a place where a careless setup leaks data or runs code you did not review.

1. **Notes are an input channel to the agent.** The session hooks and the MCP server inject text from `knowledge/` into the agent's context. A note written by someone else, or copied from the web, can carry instructions the agent may follow. This is indirect prompt injection, OWASP LLM01 in the OWASP Top 10 for LLM applications, and no filter prevents it fully. Treat notes from other people as untrusted and read them before indexing.
2. **The services do not authenticate.** The MCP server's HTTP mode, the embedding shim and every port in `docker-compose.yml` accept any request that reaches them. The compose file binds its ports to `127.0.0.1` and the MCP server's HTTP mode binds to `127.0.0.1` by default. Keep them there. A browser page can reach a localhost port through DNS rebinding, so the MCP server checks the Host header, and stdio mode, which opens no port, is the safer choice for one user on one machine.
3. **The index build can run remote model code on an old library.** The embedding model `nomic-ai/nomic-embed-text-v1.5` loads through the `nomic_bert` class that transformers 5.5 and later ship, so on a current install no Python from Hugging Face runs. Tested on 26 texts, the native class and the old remote code give identical vectors, with a maximum absolute difference of 0.0. On an older library the model falls back to `trust_remote_code`, which executes Python from the `nomic-ai/nomic-bert-2048` repository. The weights are pinned to one revision and that code to one commit in `tools/gestalt_embed_config.py`, so a changed upstream file cannot run here without a change to this repository. `GESTALT_TRUST_REMOTE_CODE=0` forbids the fallback and `1` forces it. The embedding shim uses the same settings.
4. **The stop hook sends session text to Letta.** When a Letta server answers at `http://localhost:8283`, the stop hook sends up to 800 characters of each message in the session to it, and Letta forwards that text to whatever model its agent is configured with. Nothing is sent when no server answers. Do not run Letta against a remote model if your sessions hold secrets.
5. **The audit hook logs every shell command.** Each command the agent runs is appended to `~/.claude/audit.log`, secrets included. Protect that file like a shell history, and rotate any secret you typed into a command.
6. **The safety hooks are best-effort guardrails and not a sandbox.** They block the destructive commands and the self-edits they recognise. They do not confine the agent, and a command they do not recognise runs. Use the operating system's own sandboxing when you need confinement.

## Scope

In scope: the hooks under `claude-tree/hooks/`, the MCP server, the index builder, the command-line search, the evaluation harness and the Docker and systemd files shipped here. Out of scope: Claude Code, Cursor, Letta, Graphiti, FalkorDB, Ollama and the models, which have their own reporting channels.
