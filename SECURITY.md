# Security policy

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting, which is enabled for this repository: open https://github.com/phbui/gestalt-core/security/advisories/new and describe what you found. Do not open a public issue for a vulnerability. You will get a first reply within 14 days. A fix or a documented workaround follows in the next release, and the advisory is published when the fix lands.

## Supported versions

The `main` branch and the most recent tagged release receive fixes. Older tags do not.

## What this software does on your machine

Read this before you install the hooks. Everything below is by design, and each item is a place where a careless setup leaks data or runs code you did not review.

1. **Notes are an input channel to the agent.** The session hooks and the MCP server inject text from `knowledge/` into the agent's context. A note written by someone else, or copied from the web, can carry instructions the agent may follow. This is indirect prompt injection, OWASP LLM01 in the OWASP Top 10 for LLM applications, and no filter prevents it fully. Treat notes from other people as untrusted and read them before indexing.
2. **The services do not authenticate.** The MCP server's HTTP mode, the embedding shim and every port in `docker-compose.yml` accept any request that reaches them. The compose file binds its ports to `127.0.0.1` and the MCP server's HTTP mode binds to `127.0.0.1` by default. The embedding shim binds the Docker bridge address `172.17.0.1` by default, which containers on this host reach and the network does not. Keep them there. A browser page can reach a localhost port through DNS rebinding, so the MCP server checks the Host header, and stdio mode, which opens no port, is the safer choice for one user on one machine.
3. **The index build runs pinned model code from Hugging Face.** The default embedder, `NovaSearch/stella_en_400M_v5`, ships its own modelling code in the model repository, and loading it executes that Python on every version of transformers. The weights and the code are pinned to one revision in `tools/gestalt_embed_config.py`, so a changed upstream file cannot run here without a change to this repository. `GESTALT_TRUST_REMOTE_CODE=0` forbids remote code, and under it the stella profile does not load. The `nomic` profile loads through the `nomic_bert` class that transformers 5.5 and later ship, so on a current install it runs no code from the Hub, and on an older library it falls back to code pinned to one commit of `nomic-ai/nomic-bert-2048`. The embedding shim uses the same settings. The first index build also downloads the model, which is the one network call a default install makes.
4. **The stop hook sends session text to Letta.** When a Letta server answers at `http://localhost:8283`, the stop hook sends up to 800 characters of each message in the session to it, and Letta forwards that text to whatever model its agent is configured with. Nothing is sent when no server answers. Do not run Letta against a remote model if your sessions hold secrets.
5. **The audit hook logs every shell command.** Each command the agent runs is appended to `~/.claude/audit.log`, secrets included. Protect that file like a shell history, and rotate any secret you typed into a command.
6. **The safety hooks are best-effort guardrails and not a sandbox.** They block the destructive commands and the self-edits they recognise. They do not confine the agent, and a command they do not recognise runs. Use the operating system's own sandboxing when you need confinement.

## Scope

In scope: the hooks under `claude-tree/hooks/`, the MCP server, the index builder, the command-line search, the evaluation harness and the Docker and systemd files shipped here. Out of scope: Claude Code, Cursor, Letta, Graphiti, FalkorDB, Ollama and the models, which have their own reporting channels.
