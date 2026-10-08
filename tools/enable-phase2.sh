#!/usr/bin/env bash
# Gestalt Memory Expansion — Enable Phase 2 (Graphiti temporal graph)
# Thin wrapper: delegates all logic to enable-phase2.py.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/enable-phase2.py" "$@"
