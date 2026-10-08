#!/usr/bin/env bash
# Claude Code status line — gestalt memory system
# Receives JSON on stdin from Claude Code
# Shows: gestalt entries · search sections · letta status · graphiti status · model · context%

# --- Load shared library ---
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
source "$SCRIPT_DIR/lib.sh" && gestalt_init

LETTA_URL="${LETTA_URL:-http://localhost:8283/v1}"
GRAPHITI_URL="${GRAPHITI_URL:-http://localhost:8200}"

input=$(cat)

# --- Fleet status bus (best-effort, non-blocking; see <kb-entry>) ---
[ -x "$SCRIPT_DIR/fleet-publish.sh" ] && printf '%s' "$input" | "$SCRIPT_DIR/fleet-publish.sh" statusline >/dev/null 2>&1 &

# --- Claude context ---
model=$(echo "$input" | jq -r '.model.display_name // empty')
remaining=$(echo "$input" | jq -r '.context_window.remaining_percentage // empty')

# --- Model abbreviation ---
model_short=""
case "${model:-}" in
  *Opus*)   model_short="opus" ;;
  *Sonnet*) model_short="sonnet" ;;
  *Haiku*)  model_short="haiku" ;;
  "")       ;;
  *)        model_short="$model" ;;
esac

# --- Context color ---
ctx_color="" ctx_val=""
if [ -n "$remaining" ]; then
  ctx_val=$(printf '%.0f' "$remaining")
  if [ "$ctx_val" -gt 50 ]; then ctx_color="32"
  elif [ "$ctx_val" -gt 20 ]; then ctx_color="33"
  else ctx_color="31"; fi
fi

# --- Gestalt entries ---
entry_count=0
[ -d "$GESTALT_DIR/knowledge" ] && entry_count=$(find "$GESTALT_DIR/knowledge" -maxdepth 1 -name '*.md' 2>/dev/null | wc -l)

# --- Search index section count ---
search_sections=""
if [ -f "$GESTALT_DIR/.search/gestalt.db" ]; then
  search_sections=$(sqlite3 "$GESTALT_DIR/.search/gestalt.db" "SELECT COUNT(*) FROM sections_meta" 2>/dev/null || echo "")
fi

# --- Service health (cached 30s to avoid latency on every render) ---
CACHE="/tmp/.gestalt-statusline-cache"
CACHE_AGE=30
NOW=$(date +%s)
CACHE_MTIME=$(stat -c%Y "$CACHE" 2>/dev/null || stat -f%m "$CACHE" 2>/dev/null || echo 0)

if [ -f "$CACHE" ] && [ $((NOW - CACHE_MTIME)) -lt $CACHE_AGE ]; then
  source "$CACHE"
else
  # Shared, self-healing 90s cache (lib.sh gestalt_hub_health) replaces the two
  # sequential curls that used to run here every 30s -- <kb-entry>
  # F3/F4 ^health-probe-cache. Costs <100ms once the shared cache is warm.
  gestalt_hub_health
  letta_ok="${HUB_LETTA_OK:-0}"
  graphiti_ok="${HUB_GRAPHITI_OK:-0}"

  letta_blocks=""
  if [ "$letta_ok" = "1" ] && [ -f "$STATE_DIR/letta-agent-id.txt" ]; then
    aid=$(cat "$STATE_DIR/letta-agent-id.txt")
    letta_blocks=$(curl -sf --max-time 2 "$LETTA_URL/agents/$aid" 2>/dev/null | python3 -c "
import sys,json
try:
  a=json.load(sys.stdin)
  blocks=a.get('memory',{}).get('blocks',[])
  filled=sum(1 for b in blocks if b.get('value','').strip())
  print(f'{filled}/{len(blocks)}')
except: pass
" 2>/dev/null || echo "")
  fi

  # Session summary count
  session_count=$(ls "$WORKSPACE/.claude/memory/sessions/"*.md 2>/dev/null | wc -l)

  cat > "$CACHE" <<EOF
letta_ok=$letta_ok
graphiti_ok=$graphiti_ok
letta_blocks=$letta_blocks
session_count=$session_count
EOF
fi

# --- Build output ---
DIM='\033[2m'
RST='\033[0m'
GRN='\033[32m'
RED='\033[31m'
BLU='\033[1;34m'
SEP="${DIM} · ${RST}"

# Gestalt + entries
printf "${BLU}gestalt${RST}"
printf "${DIM} %s entries${RST}" "$entry_count"

# Search index
[ -n "$search_sections" ] && printf "${SEP}${DIM}search %s${RST}" "$search_sections"

# Letta
if [ "${letta_ok:-0}" = "1" ]; then
  printf "${SEP}${GRN}letta${RST}"
  [ -n "${letta_blocks:-}" ] && printf "${DIM} %s${RST}" "$letta_blocks"
else
  printf "${SEP}${RED}letta ✗${RST}"
fi

# Graphiti
if [ "${graphiti_ok:-0}" = "1" ]; then
  printf "${SEP}${GRN}graphiti${RST}"
else
  printf "${SEP}${DIM}graphiti ✗${RST}"
fi

# Sessions
[ "${session_count:-0}" -gt 0 ] 2>/dev/null && printf "${SEP}${DIM}%s sessions${RST}" "$session_count"

# Model
[ -n "$model_short" ] && printf "${SEP}${DIM}%s${RST}" "$model_short"

# Context %
[ -n "$ctx_val" ] && printf " \033[${ctx_color}m${ctx_val}%%${RST}"
