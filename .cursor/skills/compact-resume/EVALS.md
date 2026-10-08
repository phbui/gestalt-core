# Compact Resume — Evals


| # | Test prompt | Should trigger? | Expected output shape |
|---|---|---|---|
| 1 | "prepare for a compact and resume" | YES | Full 6-step run: inventory, commit+persist, gestalt anchor, memory update, scheduler notes, compact report |
| 2 | "context is getting long, make sure nothing is lost" | YES | Same flow, proactively framed |
| 3 | "commit my changes" | NO | Plain `/commit` — no resume-block machinery for a scoped commit |
