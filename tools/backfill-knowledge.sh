#!/usr/bin/env bash
# Pump every gestalt knowledge entry into Graphiti. Historically its own script;
# now a thin wrapper over the single hash-gated sync path (F9, gestalt-efficiency-
# audit-2026-08 ^memory-triplication) so the documented command name still works.
# Chunking of >12 KB entries into kb-<slug>#<n> episodes (<kb-entry>) lives in
# the sync tool (GESTALT_SYNC_CHUNK_CHARS). HEAVY on the hub: hours of local-model GPU load —
# run it deliberately and announced, never detached from a session start (hub-invariants).
# BACKFILL_DRY_RUN=1 → --dry-run.
set -uo pipefail
[ "${BACKFILL_DRY_RUN:-0}" = 1 ] && set -- --dry-run "$@"
exec "$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/gestalt-graphiti-sync.sh" --all "$@"
