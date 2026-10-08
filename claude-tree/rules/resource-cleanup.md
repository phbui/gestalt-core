---
---

# Resource Cleanup

When you spawn background shells, subagents, monitors, worktrees, or cloud compute, tear them down as part of finishing the work — a fetched result leaves its cluster terminated in the same turn, not "later". Inventory first (`ps`, `/tasks`, `sky status`), act only on what you can prove is yours AND finished, and say what you left running. Never `pkill -f` a broad pattern or terminate a cluster you did not create. Full playbook, billing semantics, and the process-matching traps: `gestalt/.claude/references/resource-cleanup.md`.
