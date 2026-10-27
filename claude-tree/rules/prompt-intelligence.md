# Prompt Intelligence

## Clarification Gate

**Trigger:** Multiple plausible architectures AND irreversible changes (refactor, delete, rewrite, migrate, schema change, multi-file creation).

**Action:** Ask ONE forking question with 2-4 concrete options. Include a default.
Format: "Before I start — [reason]: [question]? (a) ... (b) ... I'll go with (a) if no preference."

**Skip when:** the request targets specific files, the task is mechanical (rename, format, lint fix), codebase conventions make the approach obvious, or the change is trivially reversible. Never re-ask what the user already answered.

When proceeding without asking, state your assumptions briefly at the start of the response.

## Proactive Web Research

**Trigger:** The task references an external library, framework, SDK, cloud API, or deployment platform whose API may have changed since training.

**Action:** Use WebSearch BEFORE writing integration code. Query: `<library> <version if mentioned> current API <current year>`. Prefer official docs and cite them ("Per the current Prisma docs...").

**Skip when:** core language features, stdlib, algorithms, SQL syntax, local-codebase questions, or topics already searched.

**Self-check:** If you're about to write "I think" or "I believe" about an external API's current behavior, search instead of guessing.
