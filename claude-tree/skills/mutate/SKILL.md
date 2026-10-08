---
name: mutate
description: "Create, modify, or delete gestalt artifacts (commands, skills, rules, agents, references) — e.g. add a new slash command to the system. Handles dual-agent sync, doc updates, and symlink verification automatically."
model: sonnet
user-invocable: true
argument-hint: "mutation type and target (e.g., 'add command deploy')"
---

# Mutate

Create, modify, or delete gestalt artifacts (commands, skills, rules, agents, references). Handles dual-agent sync, doc updates, and symlink verification automatically.

## Usage

`/mutate <action> <type> <name> [description]`

**Actions:** `create`, `edit`, `delete`, `rename`
**Types:** `command`, `skill`, `rule`, `agent`, `reference`

If the user provides a bare `/mutate` or an incomplete invocation, ask what they want to do with a brief menu:
```
What do you want to mutate?
(1) create command/skill/rule/agent/reference
(2) edit an existing artifact
(3) delete an artifact
(4) rename an artifact
```

## Process

### Step 1: Parse Intent

Extract action, type, name, and optional description from the arguments. If ambiguous, ask one clarifying question. Default assumptions:
- If the user says "make a command called X" → `create command X`
- If the user describes behavior that runs always → likely a `rule`
- If the user describes a multi-step workflow with agents → likely a `skill` (Cursor) + `command` (Claude)
- If the user describes a persona/role → likely an `agent`

### Step 2: Scaffold

Based on type, create the artifact in BOTH tool directories:

#### Command
Commands are now implemented as skills. To create a new command, use the **Skill** scaffold instead:
1. Write `gestalt/.claude/skills/<name>/SKILL.md` — with Claude-native frontmatter and the markdown body
2. Run `gestalt/tools/sync-cursor-tree.py --write`. It generates the `.cursor/` copy from `claude-tree/`. Never write `.cursor/` by hand (since 2026-10-06).
The `commands/` directory no longer exists — all commands live in `skills/<name>/SKILL.md`.

#### Skill
1. Write `gestalt/.claude/skills/<name>/SKILL.md` — with Claude-native frontmatter (`model`, `context`, `agent`, `hooks`, `user-invocable`, `argument-hint`, `paths` for path-scoped auto-activation, as needed) and the markdown body with the skill's process.
2. Run `gestalt/tools/sync-cursor-tree.py --write`. It generates the `.cursor/` copy from `claude-tree/`. Never write `.cursor/` by hand (since 2026-10-06).
3. Do NOT create `.claude/commands/<name>.md` or `.cursor/commands/<name>.md` — the SKILL.md IS the command. Skill-backed commands do not have separate command files.

#### Rule
1. Write `gestalt/.claude/rules/<name>.md`:
   - No frontmatter if always-loaded
   - `paths: ["glob"]` frontmatter if scoped
2. Run `gestalt/tools/sync-cursor-tree.py --write`. It generates the `.cursor/` copy from `claude-tree/`. Never write `.cursor/` by hand (since 2026-10-06).
   - Add `description:` line (one-line summary)
   - `alwaysApply: true` if no scope, or `globs:` + `alwaysApply: false` if scoped

#### Agent
1. Write `gestalt/.claude/agents/<name>.md` with full Claude frontmatter (model, tools, disallowedTools, permissionMode, maxTurns, etc.)
2. Write `gestalt/.cursor/agents/<name>.md` with only `name` and `description` in frontmatter, same body

#### Reference
1. Write `gestalt/.claude/references/<name>.md`
2. Copy to `gestalt/.cursor/references/<name>.md` — identical content, translate tool-specific paths

### Step 3: Content

For `create`:
- If the user provided a description or spec, use it to draft the content
- If not, scaffold a template with TODO markers and present it for the user to fill in
- For commands/skills: follow the established format — `# Title`, purpose paragraph, `## Process` with `### Step N:` sections, `## Rules` section
- For rules: state the rule clearly, explain when it applies, give examples

For `edit`:
- Read the existing artifact
- Apply the user's requested changes
- Sync to the other tool's directory

For `delete`:
- Confirm with the user before deleting
- Remove from both tool directories
- Remove symlinks

For `rename`:
- Create new files, copy content, delete old files
- Update all internal references

### Step 4: Update Docs

**This step is mandatory.** After any mutation, update all required indexes:

1. **`gestalt/.claude/skills/help-gestalt/SKILL.md`** — Quick Reference table + Detailed Guide section
2. **`gestalt/.cursor/skills/help-gestalt/SKILL.md`** — same changes, `.claude/` → `.cursor/` path translation
3. **`gestalt/.claude/rules/gestalt-operations.md`** — Commands & Skills list
4. **`gestalt/.cursor/rules/gestalt-operations.mdc`** — same list
5. **`gestalt/knowledge/the-relevant-entry.md`** — Commands & Skills tables (split from gestalt.md 2026-08-30; if the artifact is a command or skill)
6. **`gestalt/.claude/rules/gestalt.md`** — Commands list at the bottom (if the artifact is a command or skill)

For commands/skills, also add to the appropriate category:
- Gestalt Operations, Requirements Engineering, Engineering, Build, Investigation, Deliverable, or Utility

### Step 5: Verify Symlinks

Check that symlinks exist at the workspace root:
- `~/Documents/GitHub/.claude/` → `gestalt/.claude/`
- `~/Documents/GitHub/.cursor/` → `gestalt/.cursor/`

If individual file symlinks are used instead of directory symlinks, verify the new file is linked.

### Step 6: Confirm

Show a summary:
```
Created:
  .claude/skills/<name>/SKILL.md
  .cursor/skills/<name>/SKILL.md

Updated:
  help.md (both)
  gestalt-operations (both)
  knowledge/gestalt.md
  rules/gestalt.md

Symlinks: ✓
```

## Rules

- Never skip doc updates — they are the primary source of truth for discoverability
- Always sync both tool directories in the same operation
- For delete/rename, confirm before acting
- Match the format and style of existing artifacts in the same category
- Use provider-specific optimization: Claude gets Agent Teams, hooks, worktree isolation; Cursor gets Task() patterns, <Background> tags

## Verification Gate

Execute every step above — none are optional.
Before finishing: confirm all steps completed and outputs match the required formats.

## Evals — see EVALS.md (dev-time only)
