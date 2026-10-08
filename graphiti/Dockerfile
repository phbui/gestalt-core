# Graphiti MCP server built from upstream main instead of the published
# zepai/knowledge-graph-mcp:1.0.2 tag. Why: 1.0.2 (graphiti-core 0.28.2)
# ignores llm.providers.openai.api_url in mcp_server/src/services/factories.py,
# so the LLM leg always hits api.openai.com and cannot use a local Ollama.
# The fix (getzep/graphiti PR #1146, merged 2026-06-05) auto-routes non-OpenAI
# api_url values through OpenAIGenericClient; no published tag ships it, so we
# build the standalone image from source (tools/build-graphiti.sh produces the
# :main-upstream base). This Dockerfile only re-tags it so that
# `docker compose build graphiti-mcp` keeps working.
FROM gestalt-graphiti-mcp:main-upstream

# Cross-host access. mcp_server constructs FastMCP('Graphiti Agent Memory') with the SDK's
# default host=127.0.0.1, which switches on DNS-rebinding protection with a localhost-only
# allowed_hosts list; the later `mcp.settings.host = 0.0.0.0` does not undo it, so /health
# answers from anywhere but /mcp returns 421 "Invalid Host header" to every non-localhost
# Host (getzep/graphiti #1205, #1470, #1745 — all open, no config knob as of 2026-08). Passing
# host='0.0.0.0' at construction leaves transport_security unset = no Host check, which is what
# a tailnet-only service wants. Verified: node.example.ts.net:8200/mcp → 200 after this.
RUN sed -i "s/^    'Graphiti Agent Memory',$/    'Graphiti Agent Memory', host='0.0.0.0',/" /app/mcp/src/graphiti_mcp_server.py \
 && grep -q "'Graphiti Agent Memory', host='0.0.0.0'," /app/mcp/src/graphiti_mcp_server.py \
 && python3 -c "import ast,sys; ast.parse(open('/app/mcp/src/graphiti_mcp_server.py').read())"

# Queue-worker heartbeat (gestalt, 2026-08-30): the in-process episode worker can die
# while /health stays green (observed during the 2026-08-29 Ollama wedges — episodes
# were consumed by a dead pipeline for hours). patch-queue-heartbeat.py makes the
# worker touch /tmp/queue-heartbeat per iteration + per idle minute; the compose
# healthcheck treats a stale beat as unhealthy and kills PID 1, so restart:
# unless-stopped revives the server with fresh workers. NEVER seed the beat in the
# image: a build-time file carries the build's mtime forever, so every fresh
# container would start "stale" and crash-loop (observed on the first deploy,
# 2026-08-30 — restarted at 21s instead of surviving idle). The healthcheck's own
# touch-if-missing arms a fresh container instead; rm -f guards layer cache.
COPY patch-queue-heartbeat.py /tmp/patch-queue-heartbeat.py
RUN python3 /tmp/patch-queue-heartbeat.py && rm /tmp/patch-queue-heartbeat.py \
 && grep -q "_gestalt_touch_heartbeat" /app/mcp/src/services/queue_service.py \
 && rm -f /tmp/queue-heartbeat

# Worker strong reference (gestalt, 2026-09-06, upstream getzep/graphiti#1574): the per-group
# worker was spawned with a bare create_task and no reference, so the loop could collect it and
# "queued" episodes were never processed (three entries with zero nodes, 2026-09-05).
# patch-worker-ref.py keeps each worker task in a module-level set. Staged for the next rebuild.
COPY patch-worker-ref.py /tmp/patch-worker-ref.py
RUN python3 /tmp/patch-worker-ref.py && rm /tmp/patch-worker-ref.py \
 && grep -q "_gestalt_worker_tasks" /app/mcp/src/services/queue_service.py

# Edge search without the per-row re-MATCH (gestalt, 2026-09-06, upstream getzep/graphiti PR #1500, open):
# every FalkorDB edge search re-matched each relationship the index had already yielded by uuid against
# the whole graph, 131 s on 3,569 edges against the 120 s query timeout, and the episode was dropped after
# its LLM work (<kb-entry> row 70). patch-edge-search.py takes the endpoints from the yielded
# relationship instead (3 ms on the same graph). Staged for the next rebuild.
COPY patch-edge-search.py /tmp/patch-edge-search.py
RUN python3 /tmp/patch-edge-search.py && rm /tmp/patch-edge-search.py \
 && grep -q "startNode(e) AS n, endNode(e) AS m" /app/mcp/.venv/lib/python3.*/site-packages/graphiti_core/search/search_utils.py \
 && grep -q "startNode(e) AS n, endNode(e) AS m" /app/mcp/.venv/lib/python3.*/site-packages/graphiti_core/driver/falkordb/operations/search_ops.py \
 && grep -q "startNode(rel) AS n, endNode(rel) AS m" /app/mcp/.venv/lib/python3.*/site-packages/graphiti_core/driver/falkordb/operations/search_ops.py
