# A Fix Needs a Guard: history and the ledger

Rule: `gestalt/.cursor/rules/fix-needs-a-guard.mdc`.

## History

Instituted 2026-10-05 to 10-29 after the point-cloud WebGL failure returned two days after its first fix. This sentence lived in the rule until 2026-11-09. It moved here when the ledger sentences were added, because the unconditional-rules budget had 22 bytes left.

## The ledger

`tools/fix-ledger` holds one row per fix in `~/.fleet/fix-ledger.jsonl`. Each row has an id, the defect, and a guard command that exits 0 while the fix holds. `fix-ledger check` re-runs every resolved guard and prints one `FIX <id> HOLDS|REOPENED|TIMEOUT|DISMISSED-SKIP` line per row. A failing guard reopens its row, and the command exits non-zero. `fix-ledger-check.timer` runs the check daily at 06:30 and appends `PASS` or `FAIL` to `~/.fleet/fix-ledger.log`. A reopened row pushes to the phone through `FLEET_NTFY_URL`. `resolve` and `dismiss` take `--expect <status>` and refuse when the row has moved. A dismissed row never reopens.

The design is the flat form of TraceRoot's signal lifecycle. TraceRoot itself has no gate that re-runs a check after a fix (2026-11-09 reading of its command-line and SDK repositories).
