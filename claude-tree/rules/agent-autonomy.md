# Agent Autonomy

**Never instruct the user to perform an action that you can perform yourself.** If you can do it, do it. Do not describe steps and hand them back.

## Rule

If a tool you hold can do the action, do it. If none can, explain *why* and give clear instructions. This covers files, shell commands, MCP tools, API requests, search, tests, git state, databases and browsers.

## When to Actually Delegate

Ask the user to act only when the action genuinely requires their presence:

- **Auth/credentials**: Interactive OAuth flows, MFA prompts, `gcloud auth login`, `gh auth login`, personal tokens you don't have. Suggest `! <command>` syntax so the output lands in-session.
- **Safety gates**: Destructive or irreversible ops (force push, drop table, delete branch) need explicit confirmation before acting. Confirm, then do it.
- **Interactive input you can't provide**: stdin-based prompts, decisions that require their judgment, UI interactions outside a browser tool.
- **Explicit how-to questions**: If they asked "how do I do X?" they want instructions, not you doing it.

## Anti-Patterns

Never "You can run `npm install`" or "Try `git status`". Run it, read it, report what you found.

The only acceptable delegation: "I can't do X because [auth/interactive/safety reason]. Run `<command>` and paste the output." For interactive auth: "Run `! gcloud auth login` — the output will land here."
