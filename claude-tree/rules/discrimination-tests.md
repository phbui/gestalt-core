---
paths:
  - "**/*.py"
  - "**/*.ts"
  - "**/*.js"
  - "**/*.sh"
  - "**/*.go"
  - "**/*.rs"
  - "**/*.c"
  - "**/*.cpp"
---

# Discrimination Tests

**Every bug-fix commit touching code carries a test demonstrated to FAIL with the fix reverted** — stash the fix, show red; unstash, show green; quote both runs. A test passing both ways is vacuous (gbrain audit: 7 of 8 "regression tests" passed on master — knowledge/gbrain.md ^quality-machinery). Binds on code fixes with a testable surface only — not prose, config values, or new features. Genuinely untestable? Say "no discrimination test: <reason>" in the commit message, never ship a decorative test.
