# Letta Integration SDD

**Satisfies:** GME-FR-002–003, GME-INT-001, GME-INT-004–005, GME-NFR-002

## 1. Context

Letta provides the episodic memory layer: 8 self-editing memory blocks that persist agent learnings across sessions. The SessionStart hook reads them; the Stop hook feeds transcripts to the agent for processing.

## 2. Agent Configuration

**File:** `gestalt/config/letta-agent-config.json` (symlinked to `~/.claude/gestalt/letta-agent-config.json` by install.sh)

```json
{
  "name": "gestalt-subconscious",
  "model": "anthropic/claude-sonnet-4-6",
  "agent_type": "memgpt_agent",
  "system": "You are a background memory agent for a software developer. You observe coding session transcripts and maintain structured memory about their preferences, patterns, project context, and pending work. You update your memory blocks proactively based on what you observe. Be concise — memory blocks have character limits. Focus on information useful for future sessions.",
  "memory_blocks": [
    {
      "label": "core_directives",
      "value": "Observe session transcripts. Extract patterns, preferences, corrections, and pending items. Update memory blocks proactively. Be concise.",
      "limit": 2000
    },
    {
      "label": "user_preferences",
      "value": "",
      "limit": 3000,
      "description": "Coding style, tool preferences, communication preferences, workflow patterns"
    },
    {
      "label": "project_context",
      "value": "",
      "limit": 5000,
      "description": "Architecture decisions, repo relationships, known gotchas, deployment patterns"
    },
    {
      "label": "session_patterns",
      "value": "",
      "limit": 3000,
      "description": "Recurring behaviors, common struggles, time-based patterns"
    },
    {
      "label": "pending_items",
      "value": "",
      "limit": 3000,
      "description": "Unfinished work, explicit TODOs, follow-ups, planned next steps"
    },
    {
      "label": "guidance",
      "value": "",
      "limit": 2000,
      "description": "Active guidance for the next coding session"
    },
    {
      "label": "self_improvement",
      "value": "",
      "limit": 1000,
      "description": "Notes on how to improve memory formation and retrieval"
    },
    {
      "label": "tool_guidelines",
      "value": "Gestalt is the knowledge base at ~/Documents/GitHub/gestalt/. The user works across 30+ repos. Key tools: Claude Code, Cursor, Docker, Kubernetes.",
      "limit": 2000
    }
  ]
}
```

The `model` field in this config file is patched at install time by `install.sh` (step 6) before the agent is created. The active model is `anthropic/claude-sonnet-4-6`, sourced from `LETTA_MODEL` in `gestalt/.env`.

## 3. Agent Creation (Install Time)

The agent is created once by `install.sh`, not by the SessionStart hook. The hook only reads the agent ID from file.

`install.sh` step 6 handles agent creation:

1. If `letta-agent-id.txt` already exists, verifies the agent still exists via `GET /v1/agents/{id}`. If the 404s, removes the stale ID file and recreates.
2. Reads `$LETTA_MODEL` from `.env` (default: `anthropic/claude-sonnet-4-6`).
3. Patches the model into the config JSON in memory using Python.
4. POSTs to `POST /v1/agents/` with the patched config.
5. Extracts the `id` field from the response and writes it to `~/.claude/gestalt/letta-agent-id.txt`.

**Fallback behavior if agent ID file is stale:** The SessionStart hook detects a missing or empty response when the stored agent ID no longer exists (e.g., after a volume wipe). Re-run `install.sh` to recreate the agent — it handles this idempotently.

## 4. Memory Block Format

Letta returns the full agent object at `GET /v1/agents/{id}`. The SessionStart hook extracts blocks from `agent.memory.blocks[]`:

```json
{
  "id": "agent-abc123",
  "memory": {
    "blocks": [
      {
        "id": "block_abc123",
        "label": "project_context",
        "value": "Platform uses Supabase + PostGIS. Deploy via ArgoCD...",
        "limit": 5000,
        "read_only": false
      }
    ]
  }
}
```

The SessionStart hook formats these for injection:

```
## Letta Memory Blocks

## user_preferences
Prefers terse responses. Never runs /save manually. Uses Sonnet...

## project_context
Platform uses Supabase + PostGIS. Deploy via ArgoCD...

## pending_items
- Finish embeddings-db deploy PR
- Review spatial geolocation accuracy
```

Only non-empty blocks are injected. Empty blocks (`value: ""`) are skipped to save context tokens.

## 5. Transcript Processing

The Stop hook sends a compressed transcript (last `$GESTALT_LETTA_MAX_MESSAGES` messages, default 30, up to 800 chars each) to Letta's async endpoint using the `content` field:

```
POST /v1/agents/{agent_id}/messages/async
{
  "messages": [{
    "role": "user",
    "text": "Session transcript. Update memory blocks with patterns, preferences, corrections, pending items, project context:\n\n..."
  }]
}
```

Letta's Sonnet agent:
1. Reads the transcript in its context window
2. Decides which memory blocks to update (using `core_memory_replace` / `core_memory_append` tools)
3. Updates blocks in-place — changes are immediately persisted
4. The next SessionStart reads the updated blocks

**Sequential constraint:** Letta processes one message at a time per agent. If two sessions end simultaneously, the second transcript queues behind the first. This is acceptable — memory formation is not time-critical.

## 6. Agent Lifecycle

| Event | Action |
|---|---|
| Install time | Create agent from config, persist ID |
| First session ever | Read agent ID from file (created by install.sh) |
| Normal session start | GET /v1/agents/{id}, extract memory.blocks[], inject into context |
| Normal session end | Send transcript async via messages/async with text field |
| Letta server restart | Agent persists in PostgreSQL volume, no recreation needed |
| Volume lost | Agent ID file becomes stale; re-run install.sh to recreate (idempotent) |
| Letta unreachable | Skip memory injection, log warning, continue with file-based summaries |
