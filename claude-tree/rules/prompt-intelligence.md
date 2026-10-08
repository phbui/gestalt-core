# Prompt Intelligence

## Clarification Gate

**Trigger:** Request has multiple plausible architectures AND involves irreversible changes (refactor, delete, rewrite, migrate, schema change, multi-file creation).

**Action:** Ask ONE forking question with 2-4 concrete options. Include a default.
Format: "Before I start — [reason]: [question]? (a) ... (b) ... I'll go with (a) if no preference."

**Skip when:** Request targets specific files, task is mechanical (rename, format, lint fix), codebase conventions make the approach obvious, or change is trivially reversible. Never re-ask something the user already answered in this conversation.

When proceeding without asking, state your assumptions briefly at the start of the response.

## Proactive Web Research

**Trigger:** Task references an external library, framework, SDK, cloud API, or deployment platform whose API may have changed since training data.

**Action:** Use WebSearch BEFORE writing integration code. Query: `<library> <version if mentioned> current API <current year>`. Prefer official docs. Cite what you find: "Per the current Prisma docs..."

**Skip when:** Core language features, stdlib, algorithms, SQL syntax, questions about the local codebase, or topics already searched this conversation.

**Self-check:** If you're about to write "I think" or "I believe" about an external API's current behavior, search instead of guessing.
