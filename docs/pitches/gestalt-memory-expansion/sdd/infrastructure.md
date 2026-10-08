# Infrastructure SDD

**Satisfies:** GME-FR-001, GME-FR-020, GME-NFR-005, GME-CONST-002

## 1. Context

All services run as Docker containers managed by a single Docker Compose file and a single systemd unit. All three services (Letta, FalkorDB, Graphiti) are active — Phase 2 is fully deployed.

## 2. Docker Compose

**File:** `gestalt/docker-compose.yml`

All Phase 2 services are active (not commented out). Docker Compose auto-reads `gestalt/.env` for environment variable substitution.

```yaml
services:
  letta-server:
    image: letta/letta:0.6.7  # Pin to tested version — update deliberately
    container_name: gestalt-letta
    ports:
      - "127.0.0.1:8283:8283"
    volumes:
      - letta-data:/root/.letta
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    restart: always
    deploy:
      resources:
        limits:
          memory: 2G
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8283/v1/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  falkordb:
    image: falkordb/falkordb:v4.2.1  # Pin to tested version
    container_name: gestalt-falkordb
    ports:
      - "6379:6379"
    volumes:
      - falkordb-data:/data
    restart: always
    deploy:
      resources:
        limits:
          memory: 512M
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 30s
      timeout: 5s
      retries: 3

  graphiti-mcp:
    image: zepai/knowledge-graph-mcp:1.0.2  # Pin to tested version (graphiti 0.28.2)
    container_name: gestalt-graphiti
    ports:
      - "8000:8000"
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - GRAPHITI_GROUP_ID=gestalt
    volumes:
      - ./graphiti/config.yaml:/app/config.yaml:ro
    depends_on:
      falkordb:
        condition: service_healthy
    restart: always
    deploy:
      resources:
        limits:
          memory: 512M

volumes:
  letta-data:
  falkordb-data:
```

### .env Configuration

All configuration lives in `gestalt/.env`. Docker Compose reads this file automatically (it sits in the Compose working directory). The systemd service also syncs key variables to a separate env file for the `EnvironmentFile=` directive.

**File:** `gestalt/.env` (created from `gestalt/.env.example` by install.sh step 3)

Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required. Letta LLM inference |
| `OPENAI_API_KEY` | — | Phase 2. Graphiti embeddings |
| `LETTA_URL` | `http://localhost:8283/v1` | Letta API base URL |
| `GRAPHITI_URL` | `http://localhost:8000` | Graphiti MCP base URL |
| `GRAPHITI_GROUP_ID` | `gestalt` | Knowledge graph namespace |
| `LETTA_MODEL` | `anthropic/claude-sonnet-4-6` | Model for Letta agent |
| `GESTALT_SESSION_RETENTION_DAYS` | `7` | Days to keep session summaries |
| `GESTALT_LETTA_MAX_MESSAGES` | `30` | Max messages sent to Letta per session |
| `GESTALT_LOCK_TIMEOUT` | `60` | Stop hook lock timeout (seconds) |

The systemd `EnvironmentFile` at `~/.claude/gestalt/env` is a minimal subset (Docker vars only) synced from `.env` by install.sh step 3. Hook scripts source `gestalt/.env` directly.

## 3. systemd Service

**File:** `/etc/systemd/system/gestalt-services.service` (written by install.sh step 4)

```ini
[Unit]
Description=Gestalt Memory Services (Letta + Graphiti + FalkorDB)
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
User=phi
WorkingDirectory=/home/user/Documents/GitHub/gestalt
EnvironmentFile=/home/user/.claude/gestalt/env
# supply ANTHROPIC_API_KEY from your secret manager and never commit it
ExecStart=/usr/bin/docker compose up --remove-orphans
ExecStop=/usr/bin/docker compose down
Restart=always
RestartSec=10
TimeoutStartSec=120
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

`ExecStartPre` refreshes the key from `~/Documents/anthropic-key.txt` into the env file on every service start, keeping the Docker environment current without requiring a service reinstall when the key rotates.

### Install Sequence

All steps are automated by `gestalt/tools/install.sh` (idempotent — safe to re-run).

```
[1/10] Check prerequisites (Docker, Python 3.11+)
[2/10] Create directories (~/.claude/gestalt, hooks, memory/sessions, memory/transcripts)
[3/10] Create gestalt/.env from template (reads ~/Documents/anthropic-key.txt)
       Sync Docker vars to ~/.claude/gestalt/env (systemd EnvironmentFile)
[4/10] Write systemd unit to /etc/systemd/system/gestalt-services.service
       systemctl daemon-reload && systemctl enable gestalt-services
[5/10] Start service and wait up to 120s for Letta to be healthy
[6/10] Create Letta agent (idempotent — skips if valid agent ID already exists)
[7/10] Deploy hook scripts as symlinks:
         ~/.claude/hooks/gestalt-session-start.sh -> gestalt/.claude/hooks/gestalt-session-start.sh
         ~/.claude/hooks/gestalt-stop.sh          -> gestalt/.claude/hooks/gestalt-stop.sh
         ~/.claude/gestalt/letta-agent-config.json -> gestalt/config/letta-agent-config.json
         ~/.claude/gestalt/hooks-config.json       -> gestalt/config/hooks-config.json
[8/10] Merge gestalt/config/hooks-config.json into ~/.claude/settings.json
[9/10] Install Python dependencies into gestalt/tools/.venv
[10/10] Print summary
```

### MCP Registration

The Graphiti MCP server (`gestalt-graphiti` container) is registered in Claude Code's MCP config by pointing at `http://localhost:8000`. This is handled separately from the systemd service — add it to `~/.claude/settings.json` under `mcpServers`.

## 4. Health Monitoring

The SessionStart hook checks service health on every session. Results logged to `~/.claude/gestalt/health.log` (rotated at 1MB).

```bash
# Health check endpoints
curl -sf http://localhost:8283/v1/health     # Letta
curl -sf http://localhost:8000/health         # Graphiti (Phase 2)
redis-cli -p 6379 ping                        # FalkorDB (Phase 2)
```

## 5. Data Persistence

| Service | Volume | Host Path | Content |
|---|---|---|---|
| Letta | `letta-data` | Docker-managed | PostgreSQL data (agents, memory, conversations) |
| FalkorDB | `falkordb-data` | Docker-managed | Redis RDB + graph data |
| Graphiti | None (stateless) | N/A | State lives in FalkorDB |

**Backup:** `docker volume ls | grep gestalt` lists volumes. Standard Docker volume backup applies.

**Recovery:** If volumes are lost, re-run `install.sh`. It detects the stale agent ID and recreates the Letta agent. The Graphiti graph rebuilds over time as episodes accumulate.
