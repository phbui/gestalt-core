---
name: help-gestalt
description: "Display a complete guide to all gestalt commands and skills — what each does, when to use it, how they chain together. Use when asked what you can do or what commands/tools are available."
disable-model-invocation: true
---

# Help — Gestalt Command Index

Show the command index — see `.cursor/references/skill-index.md`. (Formerly `/help`.)

## Trigger Examples

- `/help-gestalt` — show full index
- `/help-gestalt build` — show only `/build` section
- "what commands are available?"
- "how do I use the gestalt skills?"
- "what does /investigate do?"

## Response

1. Read `.cursor/references/skill-index.md`. If skill-index.md is missing or unreadable, fall back to listing `gestalt/.cursor/skills/*/SKILL.md` directories and summarizing each from its frontmatter `description` field.
2. If the user passed a command name (e.g. `/help-gestalt build`), present only that command's section from the Detailed Guide.
3. Otherwise, present the full index: Quick Reference table, Detailed Guide, Command Chains, Shared References.
4. If the user asks about a chain or workflow, surface the matching block from the Command Chains section.

## Evals — see EVALS.md (dev-time only)
