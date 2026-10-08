# Agent Design Citations

External sources backing the team-design and hallucination-mitigation rules in `.claude/skills/prompt/SKILL.md`. Reasoning support, not executable — verify links before recommending, since model/version references date quickly.

## Team-Design Rules Cite

- Anthropic — Multi-agent research system (scaling, Opus-lead + Sonnet-workers, two-layer parallelism, dedicated CitationAgent): https://www.anthropic.com/engineering/multi-agent-research-system
- Anthropic — Building effective agents (orchestrator-workers, parallelization, evaluator-optimizer patterns): https://www.anthropic.com/research/building-effective-agents
- Claude Code — Agent Teams (3-5 default, ≤6 ceiling, subagents vs teams, isolation): https://code.claude.com/docs/en/agent-teams
- Claude Code — Subagents (model routing, scope, return contracts): https://code.claude.com/docs/en/sub-agents
- Anthropic Cookbooks — Orchestrator-workers pattern (runtime XML decomposition): https://github.com/anthropics/claude-cookbooks/blob/main/patterns/agents/orchestrator_workers.ipynb

## Hallucination-Mitigation Rules Cite (May 2026)

- Anthropic Managed Agents — multi-agent (subagent context isolation; system prompt does NOT propagate to workers): https://platform.claude.com/docs/en/managed-agents/multi-agent
- Anthropic — What's new in Opus 4.7 (adaptive thinking, task budgets, literal instruction following, default effort change) — (historical — see whats-new-claude-4-8 for current): https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-7
- Anthropic — What's new in Opus 4.8 (current effort defaults — high on all surfaces; behavioral re-tuning): https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-8
- Anthropic — April 23 postmortem (medium-effort default regression shipped 2026-03-04, reverted 2026-04-07): https://www.anthropic.com/engineering/april-23-postmortem
- Anthropic — Reduce hallucinations (quote-first grounding pattern for documents >20K tokens): https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations
- Anthropic — Adaptive thinking docs (manual `budget_tokens` deprecated on Opus 4.7; tool_choice footguns): https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
- Anthropic — Context windows + context rot (accuracy degrades as token count grows): https://platform.claude.com/docs/en/docs/build-with-claude/context-windows
- Liu et al. "Lost in the Middle" / Chroma 2025 18-model long-context study (U-shaped attention degradation): https://www.morphllm.com/lost-in-the-middle-llm
- Community: Claude Code issue #50235 — Opus 4.7 1M-context fabrication modes (tool-output temporal decay, narrative-confirming reconciliation, bucket-bypass drift, post-compression compliance softening): https://github.com/anthropics/claude-code/issues/50235
- Community: Claude Code issue #46727 — Opus 4.6 / Max 20x context-rot fabrication at 30-40% fill: https://github.com/anthropics/claude-code/issues/46727
- Yin et al. arXiv 2510.22977 — Reasoning Trap: extended reasoning amplifies tool-hallucination opportunity: https://arxiv.org/pdf/2510.22977
- arXiv 2602.10959 — RoPE position interpolation aliasing in PTQ extended-context models: https://arxiv.org/pdf/2602.10959

## Build On (Don't Duplicate) These Existing Gestalt Patterns

- `gestalt/.claude/rules/proactive-parallelism.md` — when to spawn 2-6 agents, agent-type guide, decision heuristic
- `gestalt/.claude/references/orchestration.md` — partitioning by file, prompt structure (CONTEXT/TASK/RULES/RETURN), prompt hardening
- `gestalt/.claude/references/claude-orchestration.md` — Agent Teams vs Subagents, worktree isolation, hooks, memory rules
