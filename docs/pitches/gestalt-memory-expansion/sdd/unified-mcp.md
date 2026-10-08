# Unified MCP Server SDD

**Satisfies:** GME-FR-050–054

## 1. Context

Phase 4 consolidates the curated and semantic gestalt layers behind a single MCP server with 4 tools. This replaces the separate `gestalt-search` MCP (Phase 3) and adds curated layer access. Graphiti temporal tools are accessed directly via the `graphiti-memory` MCP server — not proxied through this server.

## 2. Tool Definitions

| Tool | Layer | Description |
|---|---|---|
| `gestalt_read(slug)` | Curated | Read a knowledge entry by slug |
| `gestalt_search(query, limit)` | Semantic | Hybrid BM25 + vector search |
| `gestalt_manifest()` | Curated | Return MANIFEST.md content |
| `gestalt_graph()` | Curated | Return GRAPH.md content |

> **Graphiti temporal tools are accessed directly via the `graphiti-memory` MCP server (not proxied through the unified server).** Use `add_memory`, `search_memory_facts`, and `search_nodes` from that server for all temporal queries. Proxying was removed because Graphiti uses Streamable HTTP transport requiring session-based MCP connections that cannot be reliably proxied over plain HTTP.

## 3. Server Implementation

**File:** `gestalt/tools/gestalt-mcp-server.py`

```python
#!/usr/bin/env python3
"""Unified gestalt MCP server — all four layers in one interface.

Exposes 4 tools (curated + semantic layers):
  gestalt_read(slug)        — Read a curated knowledge entry
  gestalt_search(query)     — Hybrid BM25 + vector search
  gestalt_manifest()        — Return MANIFEST.md
  gestalt_graph()           — Return GRAPH.md

Temporal layer (Graphiti) is accessed directly via the graphiti-memory MCP server:
  add_memory, search_memory_facts, search_nodes

Usage:
  python3 gestalt-mcp-server.py              # stdio (Claude Code)
  python3 gestalt-mcp-server.py --http 8100  # HTTP (Cursor)
"""

import sqlite3
import sys
from pathlib import Path

GESTALT_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = GESTALT_DIR / "knowledge"
DB_PATH = GESTALT_DIR / ".search" / "gestalt.db"
MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"

# Lazy imports
_mcp = None
_model = None
_sqlite_vec = None


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME, trust_remote_code=True)
    return _model


def get_sqlite_vec():
    global _sqlite_vec
    if _sqlite_vec is None:
        import sqlite_vec as sv
        _sqlite_vec = sv
    return _sqlite_vec


def get_db():
    sv = get_sqlite_vec()
    db = sqlite3.connect(str(DB_PATH))
    db.enable_load_extension(True)
    sv.load(db)
    db.row_factory = sqlite3.Row
    return db


def build_mcp():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("gestalt")

    # --- Curated Layer ---

    @mcp.tool()
    def gestalt_read(slug: str) -> str:
        """Read a gestalt knowledge entry by slug. Returns full markdown content."""
        path = KNOWLEDGE_DIR / f"{slug}.md"
        if not path.exists():
            # Try fuzzy match
            matches = [p.stem for p in KNOWLEDGE_DIR.glob("*.md") if slug.lower() in p.stem.lower()]
            if matches:
                return f"Entry '{slug}' not found. Did you mean: {', '.join(matches[:5])}?"
            return f"Entry '{slug}' not found. Use gestalt_manifest() to list available entries."
        return path.read_text()

    @mcp.tool()
    def gestalt_manifest() -> str:
        """Return the gestalt MANIFEST.md — index of all entries with links and block-ids."""
        path = GESTALT_DIR / "MANIFEST.md"
        return path.read_text() if path.exists() else "MANIFEST.md not found."

    @mcp.tool()
    def gestalt_graph() -> str:
        """Return the gestalt GRAPH.md — data flow index across all entries."""
        path = GESTALT_DIR / "GRAPH.md"
        return path.read_text() if path.exists() else "GRAPH.md not found."

    # --- Semantic Layer ---

    @mcp.tool()
    def gestalt_search(query: str, limit: int = 10) -> list[dict]:
        """Hybrid BM25 + semantic search over gestalt knowledge entries.

        Returns sections with slug, heading, block_id, content snippet, and score.
        Use for finding relevant knowledge by topic.
        """
        if not DB_PATH.exists():
            return [{"error": "Search index not built. Run: python3 gestalt/tools/gestalt-index-builder.py"}]

        if not query.strip():
            return [{"error": "Query cannot be empty"}]

        model = get_model()
        db = get_db()

        try:
            # Sanitize FTS5 query — wrap in quotes to escape operators
            fts_query = '"' + query.replace('"', '""') + '"'

            # BM25 (FTS5)
            try:
                fts_results = db.execute(
                    "SELECT rowid, slug, heading, block_id, content, rank AS score "
                    "FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, limit * 2),
                ).fetchall()
            except Exception:
                fts_results = []

            # Dense vector (sqlite-vec) — k=? for SQLite <3.41 compat
            query_emb = model.encode(query)
            vec_results = db.execute(
                "SELECT id, distance FROM sections_vec "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (query_emb.tobytes(), limit * 2),
            ).fetchall()

            # Reciprocal Rank Fusion
            K = 60
            scores = {}
            for rank, row in enumerate(fts_results):
                scores[row["rowid"]] = scores.get(row["rowid"], 0) + 1.0 / (K + rank + 1)
            for rank, row in enumerate(vec_results):
                scores[row["id"]] = scores.get(row["id"], 0) + 1.0 / (K + rank + 1)

            top_ids = sorted(scores, key=scores.get, reverse=True)[:limit]

            results = []
            for rid in top_ids:
                meta = db.execute("SELECT * FROM sections_meta WHERE id = ?", (rid,)).fetchone()
                if meta:
                    results.append({
                        "slug": meta["slug"],
                        "heading": meta["heading"],
                        "block_id": meta["block_id"],
                        "content": meta["content"][:500],
                        "file_path": meta["file_path"],
                        "score": round(scores[rid], 4),
                    })

            return results
        finally:
            db.close()

    # --- Temporal Layer ---
    # Graphiti uses Streamable HTTP transport which requires session-based MCP
    # connections. Direct HTTP proxy doesn't work. Use the graphiti-memory MCP
    # server directly for temporal queries:
    #   mcp__graphiti-memory__search_memory_facts(query, group_ids, max_facts)
    #   mcp__graphiti-memory__search_nodes(query, group_ids, max_nodes)
    #   mcp__graphiti-memory__add_memory(name, episode_body, group_id, source)

    return mcp


if __name__ == "__main__":
    mcp = build_mcp()
    if "--http" in sys.argv:
        idx = sys.argv.index("--http")
        port = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 8100
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run()
```

## 4. Transport

The server supports both transports via command-line arguments:

**stdio (Claude Code):**
```bash
python3 gestalt/tools/gestalt-mcp-server.py
# FastMCP defaults to stdio
```

**HTTP (Cursor):**
```bash
python3 gestalt/tools/gestalt-mcp-server.py --http 8100
```

### Registration

**Claude Code** (replaces the Phase 3 `gestalt-search` registration):
```bash
claude mcp remove gestalt-search  # Remove Phase 3 standalone
claude mcp add gestalt \
    -- python3 /home/user/Documents/GitHub/gestalt/tools/gestalt-mcp-server.py
```

**Cursor** (`gestalt/.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "gestalt": {
      "type": "http",
      "url": "http://localhost:8100/mcp/"
    },
    "graphiti-memory": {
      "type": "http",
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

## 5. Dependencies

Same as Phase 3 — no additional dependencies for Phase 4:

```
pip install sentence-transformers sqlite-vec "mcp[cli]"
```

## 6. Phase 3 → Phase 4 Migration

Phase 4 **replaces** Phase 3 — the unified server takes over entirely; Phase 3 does not coexist alongside it. The unified server exposes 4 tools (curated + semantic layers); temporal queries go directly to the `graphiti-memory` MCP server. The migration:

1. Stop the Phase 3 `gestalt-search` MCP server
2. Remove the Claude Code registration: `claude mcp remove gestalt-search`
3. Register the unified server: `claude mcp add gestalt -- python3 .../gestalt-mcp-server.py`
4. The `gestalt_search` tool signature is identical — no calling code changes needed
5. For temporal queries, use `graphiti-memory` tools directly: `search_memory_facts`, `search_nodes`, `add_memory`

The Phase 3 `gestalt-search-server.py` must be deleted after migration. The `gestalt-index-builder.py` is still used (the unified server reads the same `.search/gestalt.db`).
