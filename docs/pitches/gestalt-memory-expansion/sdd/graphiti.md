# Graphiti Integration SDD

**Satisfies:** GME-FR-021–024, GME-FR-028–030, GME-INT-002

## 1. Context

Graphiti provides the temporal knowledge graph layer. Facts have bi-temporal validity windows (`tvalid` / `tinvalid`). When new facts contradict old ones, Graphiti automatically invalidates the old edges — providing automatic staleness detection.

## 2. Graphiti Configuration

**File:** `gestalt/graphiti/config.yaml`

```yaml
llm:
  provider: "anthropic"
  model: "claude-sonnet-4-6"
  max_tokens: 4096
  providers:
    anthropic:
      api_key: ${ANTHROPIC_API_KEY}
      max_retries: 3

embedding:
  # Graphiti requires an OpenAI API key for embeddings. Local nomic-embed-text is used only for Phase 3 semantic search.
  provider: "openai"
  model: "text-embedding-3-small"
  providers:
    openai:
      api_key: ${OPENAI_API_KEY}

graphiti:
  group_id: ${GRAPHITI_GROUP_ID:gestalt}

database:
  provider: "falkordb"
  host: "falkordb"
  port: 6379

server:
  transport: "http"
  port: 8000

# Anthropic rate limit tuning
concurrency:
  semaphore_limit: 8
```

## 3. MCP Registration

**Claude Code (HTTP transport):**
```bash
claude mcp add graphiti-memory --transport http http://localhost:8000/mcp/
```

This adds to `~/.claude/settings.json` → `mcpServers`:
```json
{
  "mcpServers": {
    "graphiti-memory": {
      "type": "http",
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

**Cursor (HTTP transport):**

**File:** `gestalt/.cursor/mcp.json`
```json
{
  "mcpServers": {
    "graphiti-memory": {
      "type": "http",
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

## 4. MCP Tool Interface

### add_memory

```
Tool: add_memory
Parameters:
  - name: string (required) — label for the episode
  - episode_body: string (required) — content to ingest
  - group_id: string (optional, default: "gestalt")
  - source: "text" | "json" | "message" (optional, default: "text")
Returns: { "message": "Episode 'name' queued for processing in group 'gestalt'" }
```

Called by:
- Stop hook (session summary as episode)
- SessionStart hook (git changes as episodes, Phase 2)
- `/learn` skill (knowledge entries as episodes)
- `/review` skill (corrections as episodes)

### search_memory_facts

```
Tool: search_memory_facts
Parameters:
  - query: string (required) — natural language search
  - group_ids: list[string] (optional, default: ["gestalt"])
  - max_facts: int (optional, default: 10)
Returns: {
  "message": "Facts retrieved successfully",
  "facts": [
    {
      "uuid": "...",
      "name": "platform deploy uses ArgoCD",
      "fact": "The platform deploys to Kubernetes via ArgoCD with GitOps",
      "created_at": "2026-03-15T...",
      "expired_at": null,  // null = currently valid
      "episodes": ["episode-uuid-1", "episode-uuid-2"]
    }
  ]
}
```

### search_nodes

```
Tool: search_nodes
Parameters:
  - query: string (required)
  - group_ids: list[string] (optional)
  - max_nodes: int (optional, default: 10)
  - entity_types: list[string] (optional) — filter by type
Returns: {
  "message": "Nodes retrieved successfully",
  "nodes": [{ "uuid": "...", "name": "platform", "summary": "..." }]
}
```

## 5. Episode Feed Points

```mermaid
graph LR
    subgraph Triggers["Episode Sources"]
        STOP[Stop Hook<br/>session summary]
        SS[SessionStart Hook<br/>git changes]
        LEARN[/learn skill<br/>knowledge entries]
        REVIEW[/review skill<br/>corrections]
    end

    subgraph Graphiti["Graphiti Processing"]
        EP[Episodes] --> EX[Entity Extraction<br/>Sonnet LLM]
        EX --> EN[Entity Nodes<br/>repos, tools, patterns]
        EX --> ED[Entity Edges<br/>facts with tvalid/tinvalid]
        ED --> CD[Contradiction Detection<br/>auto-invalidate old facts]
        EN --> CM[Community Detection<br/>label propagation]
    end

    STOP -->|add_memory| EP
    SS -->|add_memory| EP
    LEARN -->|add_memory| EP
    REVIEW -->|add_memory| EP
```

## 6. `/recall` Command Design

See [skill-mods.md](./skill-mods.md) Section 5 for the `/recall` command design.

## 7. Graceful Degradation

All Graphiti calls are wrapped in availability checks:

```bash
# In hook scripts:
if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    # ... Graphiti calls ...
fi
```

In gestalt skills (Python/agent context):
```
Before calling any Graphiti MCP tool, check if the graphiti-memory MCP server
is registered and responding. If not, skip the Graphiti step and log:
"Graphiti unavailable — skipping temporal feed. Run /recall after Graphiti is online."
```

The staleness-detector agent and `bin/check-staleness.sh` continue to function independently of Graphiti.
