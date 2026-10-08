---
paths:
  - "docs/pitches/**/*.md"
  - "docs/systems/**/*.md"
  - "docs/requirements/**/*.md"
---

# Requirements Engineering

This rule provides guidance for requirements engineering activities based on IEEE 29148 methodology adapted for Shape Up.

## Core Principle

**Requirements describe WHAT must remain true. Designs describe HOW that truth is achieved.**

This separation allows:
- Multiple valid implementations of the same requirements
- Iterative delivery without losing the thread
- Safe refactoring (agents can change design freely as long as requirements remain satisfied)
- Clear verification criteria

## The Litmus Test

When writing any statement, ask:
> "Does this answer 'how would an engineer implement this?' or 'what must always be true, regardless of implementation?'"

If it answers "how" → it belongs in SDD. If it answers "what must be true" → it belongs in SRS.

## The Three Artifacts

| Artifact | Purpose | Contains |
|----------|---------|----------|
| **SRS** | Defines WHAT the system must do | Verifiable requirements, quality attributes, constraints, acceptance criteria |
| **SDD** | Defines HOW the system is built | Architecture, components, data flows, technology choices |
| **ADR** | Records WHY significant decisions were made | Context, options considered, chosen option, consequences |

## Requirement Categorization Heuristic

| Question | If Yes → |
|----------|----------|
| Does this describe a guarantee we are willing to defend? | **SRS** |
| Does this describe how we currently meet that guarantee? | **SDD** |
| Does this explain why we chose this over alternatives? | **ADR** |
| Does this explain what we're betting on right now? | **Pitch** |

## Requirement ID Schema

- **Format:** `[SYSTEM/PITCH]-[TYPE]-[NNN]`
- **Types:** FR (Functional), NFR (Non-Functional), CONST (Constraint), INT (Interface)
- **Examples:** `AUTH-FR-001`, `C6-360-NFR-003`, `SAAS-INT-015`

## Shape Up Phase Mapping

| Phase | Create/Update |
|-------|--------------|
| Shaping | Pitch SRS (draft) |
| Betting | Pitch SRS (final), Pitch SDD (skeleton) |
| Building | Pitch SDD (living), ADRs (as needed) |
| Cooldown | System SRS, System SDD, promote ADRs |

## Directory Structure

```
docs/
├── systems/           # System-level documentation
│   └── {system}/
│       ├── index.md   # System overview, ownership, boundaries
│       ├── srs.md     # Living requirements (authoritative)
│       ├── sdd.md     # Current design (descriptive)
│       └── adr-*.md   # Architectural decisions
│
└── pitches/           # Pitch-scoped documentation
    └── {pitch-slug}/
        ├── srs.md     # Pitch requirements + promotion plan
        ├── sdd.md     # Pitch design
        └── adr-*.md   # Pitch-scoped decisions
```

## Templates

Always use these templates when creating new documents:



> **No template files exist in this workspace (verified 2026-08-31 audit):** the `@docs/requirements/` paths below are aspirational — no repo here carries `docs/requirements/`, and the `/requirements` skill's SKILL.md references the same absent files. Until the templates are created, author SRS/SDD/ADR documents directly from the `/requirements` skill's own subcommands (`requirements srs|sdd|adr`), which structure the document without needing a template file. Treat the @-paths as the intended location once templates land, not a current dependency.
- **SRS:** @docs/requirements/reference/templates/srs-template.md
- **SDD:** @docs/requirements/reference/templates/sdd-template.md
- **ADR:** @docs/requirements/reference/templates/adr-template.md

## Writing Requirements

Good requirements are:
- **Verifiable:** Can be tested or measured objectively
- **Unambiguous:** Single interpretation possible
- **Traceable:** Links to stakeholder need and design elements
- **Independent:** Can be verified without other requirements

### Requirement Statement Format

```markdown
##### [ID]: Short title, representative of the requirement...

- _Statement_: The system shall [action] [object] [constraint/condition].
- _Rationale:_ [why this requirement exists]
- _Acceptance:_ [how to verify this requirement is met]
- _Source:_ [pitch, ADR, or backfill provenance]
```

## Traceability

Every requirement should answer:
1. **Why does this requirement exist?** → Follow provenance to pitch, ADR, or business need
2. **What happens if we change this?** → Follow forward traces to dependent designs
3. **Is this requirement still valid?** → Check if the originating pitch/need is still relevant

### Provenance Annotations

For pitch-originated requirements:
```markdown
- _Source: [2026-01 Mobile Auth](../pitches/2026-01-mobile-auth/srs.md), MOBILE-AUTH-FR-003_
```

For backfilled requirements:
```markdown
- _Source: Backfill — observed behavior, no originating pitch_
- _Source: Backfill — customer contract requirement (Acme Corp SLA)_
```

## ADR Integration

ADRs must include an **"Impact on Requirements"** section:

```markdown
### Impact on Requirements

- **Adds:** PITCH-FR-012 — System must handle out-of-order messages
- **Modifies:** PITCH-NFR-003 — Latency target changed from 10s to 25s p95
- **Constrains:** PITCH-CONST-001 — Streaming architecture required
- **Removes:** PITCH-FR-005 — Batch retry logic no longer needed
```

## Promotion Workflow

During Cooldown, pitch requirements are promoted to system-level:

1. Review the Promotion Plan in the pitch SRS
2. Add new requirements to system SRS with proper IDs
3. Add provenance annotations pointing back to the pitch
4. Update system SDD if architecture changed
5. Promote ADRs with lasting impact to system `adr/` folder
6. Mark pitch SRS as "Completed" or "Superseded"

## Reference Documentation

For detailed guidance, read these docs in the `docs/requirements/` folder:

- **index.md** — Overview and navigation
- **concepts.md** — Philosophy and principles
- **artifacts.md** — SRS, SDD, and ADR details
- **system-hierarchy.md** — Levels of detail
- **shape-up-integration.md** — Phase-by-phase guide
- **traceability.md** — Requirement connections
- **docs-organization.md** — File structure
- **backfilling.md** — Documenting existing systems
