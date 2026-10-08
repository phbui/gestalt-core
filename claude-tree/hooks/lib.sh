#!/usr/bin/env bash
# Shared library for gestalt hooks.
# Source this file and call gestalt_init to set common path/env variables.
#
# Usage (from any script in .claude/hooks/):
#   source "$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/lib.sh" && gestalt_init
#
# After gestalt_init, the following variables are set:
#   SCRIPT_DIR   — absolute path of the calling script's directory
#   GESTALT_DIR  — absolute path to the gestalt repo root
#   ENV_FILE     — path to gestalt/.env
#   WORKSPACE    — workspace root (parent of gestalt), overridable via GESTALT_WORKSPACE
#   STATE_DIR    — state dir, overridable via GESTALT_STATE_DIR
#   LOG          — path to health.log inside STATE_DIR

gestalt_init() {
    # Git Bash's MSYS runtime defaults to winsymlinks:deepcopy, so `ln -s` exits 0
    # but silently DEEP-COPIES instead of linking: `[ -L ]` is false and later edits
    # to the target never propagate. Inert on Linux/macOS — MSYS is read only by the
    # MSYS2/Git-for-Windows runtime. https://www.msys2.org/docs/symlinks/
    export MSYS=winsymlinks:nativestrict

    # SCRIPT_DIR is set by caller via readlink -f, which follows the
    # .claude/hooks -> ../gestalt/.claude/hooks symlink.
    # Two levels up from gestalt/.claude/hooks/ gives the gestalt repo root.
    GESTALT_DIR="${GESTALT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
    ENV_FILE="$GESTALT_DIR/.env"

    # Fleet-wide per-node config (memory-stack endpoints etc.) — sourced first
    # so the repo-local .env can still override it. See <kb-entry>.
    FLEET_ENV="${FLEET_ENV:-$HOME/.fleet/fleet.env}"
    if [ -f "$FLEET_ENV" ]; then
        set -a; source "$FLEET_ENV"; set +a
    fi

    if [ -f "$ENV_FILE" ]; then
        set -a; source "$ENV_FILE"; set +a
    fi

    WORKSPACE="${GESTALT_WORKSPACE:-$(cd "$GESTALT_DIR/.." && pwd)}"
    STATE_DIR="${GESTALT_STATE_DIR:-$WORKSPACE/.claude/gestalt}"
    LOG="$STATE_DIR/health.log"
}

# gestalt_hub_health — cached hub-health probe (F3/F4, <kb-entry>
# ^health-probe-cache). Reads ~/.fleet/hub-health, one line "<epoch> <letta 0|1>
# <graphiti 0|1>", normally kept fresh by fleet-heartbeat. Self-healing: if the file is
# missing or older than 90s, this does ONE curl --max-time 1 per service, concurrently,
# and writes the file itself so the next caller (this hook or another) within 90s pays
# only a file read. Sets HUB_LETTA_OK and HUB_GRAPHITI_OK to "1" or "0". Callers translate
# into their own convention (LETTA_OK=true/false, letta_ok=1/0, ...) -- this function does
# not rename itself into every caller's local variable style.
gestalt_hub_health() {
    local hh="${GESTALT_HUB_HEALTH_FILE:-$HOME/.fleet/hub-health}"
    local now line ts l g age
    now=$(date +%s)
    if [ -f "$hh" ]; then
        line=$(cat "$hh" 2>/dev/null)
        set -- $line
        ts=${1:-0}; l=${2:-0}; g=${3:-0}
        age=$((now - ts))
        if [ "$age" -ge 0 ] 2>/dev/null && [ "$age" -lt 90 ] 2>/dev/null; then
            HUB_LETTA_OK="$l"
            HUB_GRAPHITI_OK="$g"
            return 0
        fi
    fi
    # Stale or missing: probe both services concurrently, one curl each, then persist.
    local ltmp gtmp lpid gpid lrc grc
    ltmp=$(mktemp "${TMPDIR:-/tmp}/gestalt-hub-health-l.XXXXXX")
    gtmp=$(mktemp "${TMPDIR:-/tmp}/gestalt-hub-health-g.XXXXXX")
    ( curl -sf --max-time 1 "${LETTA_URL:-http://localhost:8283/v1}/health" >/dev/null 2>&1; echo $? > "$ltmp" ) &
    lpid=$!
    ( curl -sf --max-time 1 "${GRAPHITI_URL:-http://localhost:8200}/health" >/dev/null 2>&1; echo $? > "$gtmp" ) &
    gpid=$!
    wait "$lpid" 2>/dev/null
    wait "$gpid" 2>/dev/null
    lrc=$(cat "$ltmp" 2>/dev/null); grc=$(cat "$gtmp" 2>/dev/null)
    rm -f "$ltmp" "$gtmp"
    HUB_LETTA_OK=0; [ "${lrc:-1}" = "0" ] && HUB_LETTA_OK=1
    HUB_GRAPHITI_OK=0; [ "${grc:-1}" = "0" ] && HUB_GRAPHITI_OK=1
    mkdir -p "$(dirname "$hh")" 2>/dev/null
    local tmpf="$hh.tmp.$$"
    if printf '%s %s %s\n' "$now" "$HUB_LETTA_OK" "$HUB_GRAPHITI_OK" > "$tmpf" 2>/dev/null; then
        mv -f "$tmpf" "$hh" 2>/dev/null
    fi
}
