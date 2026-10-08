#!/bin/bash
# Build the Graphiti MCP server from upstream main (see graphiti/Dockerfile for why).
set -euo pipefail
CORE="${GRAPHITI_CORE_VERSION:-0.29.3}"
REF="${GRAPHITI_REF:-main}"
DOCKER_BUILDKIT=1 docker build -f docker/Dockerfile.standalone \
  --build-arg GRAPHITI_CORE_VERSION="$CORE" \
  -t gestalt-graphiti-mcp:main-upstream \
  "https://github.com/getzep/graphiti.git#${REF}:mcp_server"
echo "Built gestalt-graphiti-mcp:main-upstream (graphiti-core $CORE, ref $REF)."
echo "Next: docker compose build graphiti-mcp && docker compose up -d graphiti-mcp"
