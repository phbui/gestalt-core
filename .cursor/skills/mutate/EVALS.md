# Mutate — Evals


Test before shipping. Run on Haiku, Sonnet, and the current flagship model separately — trigger accuracy and execution quality should be consistent across tiers.

| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "Create a new gestalt skill called `triage` that triages incoming bug reports" | YES | Parse `create skill triage`; create `.cursor/skills/triage/SKILL.md` with proper frontmatter; mirror to `.cursor/skills/triage/SKILL.md`; update help index; verify symlinks at workspace root |
| 2 | "Rename the /understand command to /investigate everywhere" | YES | Locate both `.claude` and `.cursor` versions; rename files; update cross-references; update `/help` indices; verify symlinks |
| 3 | "Save what we learned this session about platform deploy" | NO | Route to `/save` (knowledge capture into gestalt knowledge entries, not artifact creation/edit/delete) |
