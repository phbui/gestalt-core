#!/usr/bin/env bash
# Verify Graphiti backfill landed: counts nodes/edges, confirms episodes
# processed, smoke-tests a search query. Logs to ~/.claude/gestalt/verify.log.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
export GESTALT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$GESTALT_DIR/.claude/hooks/lib.sh" && gestalt_init

GRAPHITI_URL="${GRAPHITI_URL:-http://localhost:8200}"
GROUP="${GRAPHITI_GROUP_ID:-gestalt}"
LOG="$STATE_DIR/verify.log"

echo "=== verify @ $(date -Iseconds) ===" >> "$LOG"

# 1. Graph state
nodes=$(docker exec gestalt-falkordb redis-cli --no-raw GRAPH.QUERY gestalt 'MATCH (n) RETURN count(n)' 2>/dev/null | grep -oE '[0-9]+' | head -1)
edges=$(docker exec gestalt-falkordb redis-cli --no-raw GRAPH.QUERY gestalt 'MATCH ()-[r]->() RETURN count(r)' 2>/dev/null | grep -oE '[0-9]+' | head -1)
episodes=$(docker exec gestalt-falkordb redis-cli --no-raw GRAPH.QUERY gestalt 'MATCH (n:Episodic) RETURN count(n)' 2>/dev/null | grep -oE '[0-9]+' | head -1)
echo "graph: nodes=${nodes:-?} edges=${edges:-?} episodes=${episodes:-?}" >> "$LOG"

# 2. AOF state
aof=$(docker exec gestalt-falkordb du -sb /data/appendonlydir 2>/dev/null | awk '{print $1}')
echo "aof_size_bytes=${aof:-?}" >> "$LOG"

# 3. Smoke search
MCP_HEADERS=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
SESSION=$(curl -sL --max-time 5 -D /dev/stderr -X POST "$GRAPHITI_URL/mcp" \
    "${MCP_HEADERS[@]}" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}' \
    2>&1 >/dev/null | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')

if [ -n "$SESSION" ]; then
    curl -sL --max-time 3 -X POST "$GRAPHITI_URL/mcp" \
        "${MCP_HEADERS[@]}" -H "Mcp-Session-Id: $SESSION" \
        -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null

    result=$(curl -sL --max-time 15 -X POST "$GRAPHITI_URL/mcp" \
        "${MCP_HEADERS[@]}" -H "Mcp-Session-Id: $SESSION" \
        -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"search_memory_facts\",\"arguments\":{\"query\":\"platform deploy\",\"max_facts\":5,\"group_ids\":[\"$GROUP\"]}}}")
    fact_count=$(echo "$result" | python3 -c "import sys,json,re; m=re.search(r'\"text\":\"({.*?})\"',sys.stdin.read().replace('\\\\\\\\','\\\\').replace('\\\\\"','\"'),re.S); d=json.loads(m.group(1)) if m else {}; print(len(d.get('facts',[])))" 2>/dev/null)
    echo "search 'platform deploy' returned facts=${fact_count:-?}" >> "$LOG"
else
    echo "search: SKIPPED — MCP session init failed" >> "$LOG"
fi

# 4. Verdict
if [ "${nodes:-0}" -gt 50 ] 2>/dev/null && [ "${aof:-0}" -gt 1000 ] 2>/dev/null; then
    echo "VERDICT: HEALTHY — graph populated, persistence active" >> "$LOG"
else
    echo "VERDICT: SUSPICIOUS — review numbers above" >> "$LOG"
fi
echo "" >> "$LOG"
