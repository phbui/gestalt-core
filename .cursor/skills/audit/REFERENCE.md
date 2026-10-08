# Audit — Phase 6 & 7 Templates

Orchestrator-facing output templates for the final two phases of `/audit`. Referenced from SKILL.md; read this when you reach Phase 6.

## Phase 6: Update Artifacts (Orchestrator)

The orchestrator handles all documentation updates directly — no agents needed.

1. **SDD Implementation Log** — Add a dated audit entry:
   ```markdown
   ### {date}: Final Audit

   **Audit scope:** {file count} files across {repo count} repos

   **Findings:** {CRITICAL count} critical, {MAJOR count} major, {MINOR count} minor
   **False positives discarded:** {count}

   **Structural fixes (Batch 1):**
   - {summary of key structural changes}

   **Quality fixes (Batch 2):**
   - {summary of key quality improvements}

   **Optimization fixes (Batch 3):**
   - {summary of key performance improvements}

   **Requirements status after audit:**
   - {PASS count} complete, {PARTIAL count} partial, {MISSING count} missing
   ```

2. **SDD Progress Tracking** — Update requirement statuses if audit changed any.

3. **SRS** — Flag any requirement gaps discovered during audit.

4. **ADRs** — Note if any ADR compliance issues were found and resolved.

## Phase 7: Final Report (Orchestrator)

```
## Audit Complete: {pitch_name}

### Before → After
- Critical issues: {before} → {after}
- Major issues: {before} → {after}
- Minor issues: {before} → {after}
- Requirements: {before_pass}/{total} → {after_pass}/{total}

### Structural Fixes (Batch 1)
- {grouped summary}

### Quality Fixes (Batch 2)
- {grouped summary}

### Optimization Fixes (Batch 3)
- {grouped summary}

### Verification
- Regression check: {PASS or issues}
- Lint & contracts: {PASS or issues}

### Remaining Items (if any)
- {issues requiring manual resolution, with justification}

### Artifacts Updated
- SDD: Implementation Log entry added, Progress Tracking updated
- {other docs updated}

Total findings: {N}. All reviewers reported: yes/no.
The pitch implementation has been audited and all actionable fixes applied.
```
