# Agent Memory Index

> Shared project-scoped memory for gestalt agents (builder, designer, reviewer, learner).
> First 200 lines loaded at startup. Topic files loaded on demand.

## Conventions

(none yet — agents append here as they learn this workspace)

## Heuristics
- Skip directories: node_modules, .venv, dist, build, __pycache__, .git, .next, .turbo, coverage, .pytest_cache, .mypy_cache, .ruff_cache
- Python repos: prefer `uv` + `ruff`; run tests with `uv run pytest`
- TypeScript repos: `pnpm` + `biome`/`eslint`; run tests with `npx vitest`
- Prefer `Taskfile.yaml` targets over ad-hoc commands when a repo has one
