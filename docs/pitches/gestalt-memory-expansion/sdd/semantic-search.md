# Semantic Search SDD

**Satisfies:** GME-FR-040–046, GME-INT-003, GME-NFR-004

## 1. Context

The semantic layer provides hybrid BM25 + dense vector search over gestalt knowledge entries. It uses SQLite FTS5 for full-text and sqlite-vec for embeddings, wrapped in a FastMCP server.

## 2. Index Structure

**File:** `gestalt/.search/gestalt.db` (gitignored, rebuildable)

### Schema

```sql
-- Full-text search table (FTS5)
CREATE VIRTUAL TABLE sections_fts USING fts5(
    slug,
    heading,
    block_id,
    content,
    tokenize='porter unicode61'
);

-- Vector table (sqlite-vec)
-- 768 dimensions for nomic-embed-text-v1.5
CREATE VIRTUAL TABLE sections_vec USING vec0(
    id INTEGER PRIMARY KEY,
    embedding FLOAT[768]
);

-- Metadata table (links FTS and vec by row ID)
CREATE TABLE sections_meta (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    heading TEXT NOT NULL,
    block_id TEXT,
    content TEXT NOT NULL,
    file_path TEXT NOT NULL
);

-- Ensure consistent IDs across all three tables
```

### Chunking Strategy

Each knowledge entry is chunked by `##` heading boundaries:

```
gestalt/knowledge/platform.md
├── chunk 0: slug=platform, heading="What It Does", block_id="overview"
├── chunk 1: slug=platform, heading="Architecture", block_id="architecture"
├── chunk 2: slug=platform, heading="Setup", block_id="setup"
└── ...
```

Frontmatter is excluded from chunks. Block IDs are extracted from `^block-id` anchors on heading lines or the first line of a section.

## 3. Index Builder

**File:** `gestalt/tools/gestalt-index-builder.py`

```python
#!/usr/bin/env python3
"""Build the gestalt semantic search index from knowledge/*.md files."""

import sqlite3
import sys
from pathlib import Path

# sqlite-vec and nomic loaded at runtime
import sqlite_vec
from sentence_transformers import SentenceTransformer

# Verify sqlite3 supports extension loading
_test = sqlite3.connect(":memory:")
assert hasattr(_test, 'enable_load_extension'), \
    "Python sqlite3 lacks extension loading. Use pyenv or conda Python."
_test.close()

GESTALT_DIR = Path(__file__).parent.parent
KNOWLEDGE_DIR = GESTALT_DIR / "knowledge"
SEARCH_DIR = GESTALT_DIR / ".search"
DB_PATH = SEARCH_DIR / "gestalt.db"

MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"


def parse_entry(path: Path) -> list[dict]:
    """Parse a knowledge entry into section chunks."""
    text = path.read_text()
    # Strip YAML frontmatter
    if text.startswith("---"):
        end = text.index("---", 3)
        text = text[end + 3:].strip()

    slug = path.stem
    chunks = []
    current_heading = "Overview"
    current_block_id = "overview"
    current_lines = []

    for line in text.split("\n"):
        if line.startswith("## "):
            # Save previous chunk
            if current_lines:
                content = "\n".join(current_lines).strip()
                if content:
                    chunks.append({
                        "slug": slug,
                        "heading": current_heading,
                        "block_id": current_block_id,
                        "content": content,
                        "file_path": str(path.relative_to(GESTALT_DIR)),
                    })
            # Start new chunk
            heading_text = line.lstrip("# ").strip()
            # Extract ^block-id if present
            if "^" in heading_text:
                parts = heading_text.split("^")
                current_heading = parts[0].strip()
                current_block_id = parts[1].strip()
            else:
                current_heading = heading_text
                current_block_id = heading_text.lower().replace(" ", "-")
            current_lines = []
        else:
            # Check for ^block-id on content lines
            if line.strip().startswith("^"):
                current_block_id = line.strip().lstrip("^")
            current_lines.append(line)

    # Save last chunk
    if current_lines:
        content = "\n".join(current_lines).strip()
        if content:
            chunks.append({
                "slug": slug,
                "heading": current_heading,
                "block_id": current_block_id,
                "content": content,
                "file_path": str(path.relative_to(GESTALT_DIR)),
            })

    return chunks


def build_index():
    """Full rebuild of the semantic search index."""
    SEARCH_DIR.mkdir(exist_ok=True)

    # Remove existing DB for clean rebuild
    if DB_PATH.exists():
        DB_PATH.unlink()

    # Load embedding model
    print(f"Loading embedding model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME, trust_remote_code=True)

    # Parse all entries
    entries = sorted(KNOWLEDGE_DIR.glob("*.md"))
    all_chunks = []
    for entry in entries:
        all_chunks.extend(parse_entry(entry))

    print(f"Parsed {len(entries)} entries into {len(all_chunks)} chunks")

    # Connect and create schema
    db = sqlite3.connect(str(DB_PATH))
    db.enable_load_extension(True)
    sqlite_vec.load(db)

    db.executescript("""
        CREATE VIRTUAL TABLE sections_fts USING fts5(
            slug, heading, block_id, content,
            tokenize='porter unicode61'
        );
        CREATE VIRTUAL TABLE sections_vec USING vec0(
            id INTEGER PRIMARY KEY,
            embedding FLOAT[768]
        );
        CREATE TABLE sections_meta (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            heading TEXT NOT NULL,
            block_id TEXT,
            content TEXT NOT NULL,
            file_path TEXT NOT NULL
        );
    """)

    # Insert chunks and embeddings
    texts = [c["content"] for c in all_chunks]
    print(f"Generating {len(texts)} embeddings...")
    embeddings = model.encode(texts, show_progress_bar=True)

    for i, (chunk, emb) in enumerate(zip(all_chunks, embeddings)):
        db.execute(
            "INSERT INTO sections_meta VALUES (?, ?, ?, ?, ?, ?)",
            (i, chunk["slug"], chunk["heading"], chunk["block_id"],
             chunk["content"], chunk["file_path"]),
        )
        db.execute(
            "INSERT INTO sections_fts VALUES (?, ?, ?, ?)",
            (chunk["slug"], chunk["heading"], chunk["block_id"],
             chunk["content"]),
        )
        db.execute(
            "INSERT INTO sections_vec (id, embedding) VALUES (?, ?)",
            (i, emb.tobytes()),
        )

    db.commit()
    db.close()
    print(f"Index built: {DB_PATH} ({DB_PATH.stat().st_size // 1024}KB)")


if __name__ == "__main__":
    build_index()
```

### Integration with `gestalt rebuild`

**Modified file:** `gestalt/tools/gestalt` — add after the existing `regenerate_graph` call:

```bash
cmd_rebuild() {
    regenerate_manifest
    regenerate_graph
    regenerate_sources   # if exists
    regenerate_branches  # if exists

    # Phase 3: Rebuild semantic search index
    if command -v python3 &>/dev/null && [ -f "$GESTALT_DIR/tools/gestalt-index-builder.py" ]; then
        echo "Rebuilding semantic search index..."
        python3 "$GESTALT_DIR/tools/gestalt-index-builder.py" 2>&1 || \
            echo "WARN: Semantic index rebuild failed (non-fatal)"
    fi
}
```

## 4. FastMCP Search Server

**File:** `gestalt/tools/gestalt-search-server.py`

```python
#!/usr/bin/env python3
"""Gestalt semantic search MCP server — hybrid BM25 + dense vector retrieval."""

import sqlite3
from pathlib import Path

import sqlite_vec
from mcp.server.fastmcp import FastMCP
from sentence_transformers import SentenceTransformer

# Verify sqlite3 supports extension loading
_test = sqlite3.connect(":memory:")
assert hasattr(_test, 'enable_load_extension'), \
    "Python sqlite3 lacks extension loading. Use pyenv or conda Python."
_test.close()

GESTALT_DIR = Path(__file__).parent.parent
DB_PATH = GESTALT_DIR / ".search" / "gestalt.db"
MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"

mcp = FastMCP("gestalt-search")

# Lazy-load model on first query
_model = None

def get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME, trust_remote_code=True)
    return _model


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.row_factory = sqlite3.Row
    return db


@mcp.tool()
def gestalt_search(query: str, limit: int = 10) -> list[dict]:
    """Search gestalt knowledge base with hybrid BM25 + semantic retrieval.

    Returns matching sections with slug, heading, block_id, content, and score.
    """
    if not DB_PATH.exists():
        return [{"error": "Index not built. Run: python3 gestalt/tools/gestalt-index-builder.py"}]

    model = get_model()
    db = get_db()

    # BM25 search (FTS5)
    fts_results = db.execute("""
        SELECT rowid, slug, heading, block_id, content,
               rank AS score
        FROM sections_fts
        WHERE sections_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """, (query, limit * 2)).fetchall()

    # Dense vector search (sqlite-vec)
    query_emb = model.encode(query)
    vec_results = db.execute("""
        SELECT id, distance
        FROM sections_vec
        WHERE embedding MATCH ?
        AND k = ?
        ORDER BY distance
    """, (query_emb.tobytes(), limit * 2)).fetchall()

    # Reciprocal Rank Fusion (RRF)
    K = 60  # RRF constant
    scores = {}

    for rank, row in enumerate(fts_results):
        rid = row["rowid"]
        scores[rid] = scores.get(rid, 0) + 1.0 / (K + rank + 1)

    for rank, row in enumerate(vec_results):
        rid = row["id"]
        scores[rid] = scores.get(rid, 0) + 1.0 / (K + rank + 1)

    # Sort by combined score, take top N
    top_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)[:limit]

    # Fetch metadata for top results
    results = []
    for rid in top_ids:
        meta = db.execute(
            "SELECT * FROM sections_meta WHERE id = ?", (rid,)
        ).fetchone()
        if meta:
            results.append({
                "slug": meta["slug"],
                "heading": meta["heading"],
                "block_id": meta["block_id"],
                "content": meta["content"][:500],  # Truncate for context budget
                "file_path": meta["file_path"],
                "score": round(scores[rid], 4),
            })

    db.close()
    return results


if __name__ == "__main__":
    mcp.run()  # stdio transport
```

### MCP Registration

```bash
claude mcp add gestalt-search \
    -- python3 /home/user/Documents/GitHub/gestalt/tools/gestalt-search-server.py
```

Adds to `~/.claude/settings.json` → `mcpServers`:
```json
{
  "gestalt-search": {
    "type": "stdio",
    "command": "python3",
    "args": ["/home/user/Documents/GitHub/gestalt/tools/gestalt-search-server.py"]
  }
}
```

## 5. Dependencies

**File:** `gestalt/tools/requirements.txt`
```
sentence-transformers>=3.0
sqlite-vec>=0.1.0
mcp[cli]>=1.0
httpx>=0.27
```

Install with:
```
pip install -r gestalt/tools/requirements.txt
```

Or in a dedicated venv:
```bash
cd gestalt/tools
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 6. Gitignore

**Add to `gestalt/.gitignore`:**
```
.search/
```

The `.search/gestalt.db` file is derived and rebuildable. It should not be committed.
