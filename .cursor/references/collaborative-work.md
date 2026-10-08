# Collaborative Work Pattern

Standard interaction pattern for commands that create or modify requirements documents (SRS, SDD, ADR, Promote). Referenced by `/srs`, `/sdd`, `/adr`, `/promote`.

## Rules

1. **Ask when unsure.** If you don't 100% understand a code path, concept, option, or constraint — ASK for clarification. Do not assume.
2. **Explore first.** Read the codebase to understand current implementation before writing design or requirements.
3. **Present drafts.** Show drafts for review before finalizing. Never write a complete document without checkpoint.
4. **Summarize changes.** After any modification, summarize concisely: what was added, modified, or removed, with requirement/decision IDs.
5. **Confirm before multi-doc changes.** When a change affects multiple documents (e.g., ADR impacts SRS, SDD impacts requirements), summarize ALL planned changes and get user confirmation before proceeding.
6. **Work incrementally.** Make changes in small, reviewable steps — not all at once.
