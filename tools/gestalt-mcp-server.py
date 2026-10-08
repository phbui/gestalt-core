#!/usr/bin/env python3
"""Unified gestalt MCP server — curated + semantic layers.

Exposes 4 tools:
  gestalt_read(slug)        — Read a curated knowledge entry
  gestalt_search(query)     — Hybrid BM25 + vector search
  gestalt_manifest([query]) — Manifest summary, one slug's block, or matching blocks
  gestalt_graph()           — Return GRAPH.md

Temporal layer (Graphiti) is accessed via the separate graphiti-memory MCP server:
  mcp__graphiti-memory__search_memory_facts(query, group_ids, max_facts)
  mcp__graphiti-memory__search_nodes(query, group_ids, max_nodes)
  mcp__graphiti-memory__add_memory(name, episode_body, group_id, source)

Usage:
  python3 gestalt-mcp-server.py              # stdio (Claude Code)
  python3 gestalt-mcp-server.py --http 8100  # HTTP (Cursor)
"""

import json
import re
import os
import sqlite3
import threading
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling import works under any cwd or loader
try:
    import gestalt_embed_config as _ec
except ImportError:  # a lone copy of this file (a test fixture, a stale install): behave as before X14, unpinned
    class _ec:  # noqa: N801
        MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
        MODEL_REVISION = None
        EMBED_DIM = 768
        DOC_PREFIX = "search_document: "
        QUERY_PREFIX = "search_query: "

GESTALT_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = GESTALT_DIR / "knowledge"
DB_PATH = GESTALT_DIR / ".search" / "gestalt.db"
MODEL_NAME = _ec.MODEL_NAME

# Lazy imports
_mcp = None
_model = None
_sqlite_vec = None


# --- Search mode + model lifetime (memory audit F18, 2026-08-18) -----------------------------
# Every Claude Code session spawns its own stdio copy of this server, and each copy that ever
# ran a hybrid search kept a ~750 MB embedding model resident for the life of the seat — three
# seats on a 7 GB laptop = 2.3 GB of identical models, mostly in swap. Two levers:
#   GESTALT_SEARCH_MODE = hybrid | fts   (default: hybrid on the fleet hub, fts everywhere else;
#       the hub name is FLEET_HUB_NAME or FLEET_HUB's first label, default "hub" — same rule as
#       fleet-sync's index role gate). fts never imports torch; gestalt_search == gestalt_search_fts.
#   GESTALT_MODEL_IDLE_S = seconds (default 600): after that long without a hybrid search the
#       model is dropped (del + gc + malloc_trim) so RSS returns to the OS; the next search
#       reloads it (~2.5 s warm on this box). 0 disables unloading.
# The shared per-node HTTP mode (`--http PORT [--host 127.0.0.1]`, unit tools/fleet/units/
# gestalt-mcp.service) is the structural fix: one process, one model, every seat.
_model_lock = None
_model_last_used = 0.0
_model_timer = None


# RRF constant; evals/retrieval/run_retrieval_evals.py pins the same value as K_RRF
K = 60
# legs gestalt_search fuses (body lexical, dense vector); tests derive the score ceiling N_LEGS/(K+1) from this
N_LEGS = 2


def _rrf_fuse(legs: list[list], k: int = K) -> dict:
    # Reciprocal Rank Fusion over the given ranked legs. K is hardcoded, never a
    # threshold compared against a stale ceiling — that is exactly how four
    # unreachable relevance gates shipped (see ^rrf-thresholds). The achievable
    # ceiling is len(legs)/(K+1), derived from the legs, never stored.
    scores: dict = {}
    for leg in legs:
        for rank, key in enumerate(leg):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _hub_name() -> str:
    hub = os.environ.get("FLEET_HUB_NAME") or (os.environ.get("FLEET_HUB", "").split(".")[0]) or "hub"
    return hub.lower()


def _is_hub() -> bool:
    import socket
    return socket.gethostname().split(".")[0].lower() == _hub_name()


def search_mode() -> str:
    m = os.environ.get("GESTALT_SEARCH_MODE", "").strip().lower()
    if m in ("fts", "hybrid"):
        return m
    return "hybrid" if _is_hub() else "fts"


_MCP_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "gestalt-mcp-relay", "version": "0"}}}


def _hub_mcp_url() -> str:
    """Where a leaf node relays semantic searches; "" disables the relay, and it is always "" on the hub."""
    u = os.environ.get("GESTALT_HUB_MCP_URL")
    if u is not None:
        return u.strip()
    if _is_hub():
        return ""
    hub = os.environ.get("FLEET_HUB", "")
    if not hub:
        return ""
    return f"http://{hub}:{os.environ.get('GESTALT_HUB_MCP_PORT', '8300')}/mcp"


def _hub_looks_down() -> bool:
    """fleet-heartbeat's cache (~/.fleet/hub-health: "<epoch> <letta> <graphiti>"), the same read fleet-sync index-fetch does."""
    try:
        ts, letta, graphiti = Path("~/.fleet/hub-health").expanduser().read_text().split()[:3]
        return 0 <= time.time() - int(ts) < 90 and letta == "0" and graphiti == "0"
    except Exception:
        return False


def _hub_reachable(url: str) -> bool:
    """Short TCP connect before the MCP handshake: a hub whose port is closed on the tailnet drops the SYN rather than
    refusing it, so without this the fallback path paid the full GESTALT_HUB_TIMEOUT_S (measured 20 s, 2026-09-02)."""
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=float(os.environ.get("GESTALT_HUB_CONNECT_S", "3"))):
            return True
    except OSError:
        return False


def _hub_search(query: str, limit: int) -> list[dict]:
    """One MCP session on the hub's gestalt server, one gestalt_search(semantic=True) call, rows tagged via=hub."""
    import urllib.request
    url = _hub_mcp_url()
    if not _hub_reachable(url):
        raise ConnectionError(f"hub port closed or unreachable at {url}")
    timeout = float(os.environ.get("GESTALT_HUB_TIMEOUT_S", "20"))
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def post(payload: dict, extra: dict | None = None) -> tuple[str | None, str]:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={**hdr, **(extra or {})}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.headers.get("mcp-session-id"), r.read().decode("utf-8", "replace")

    sid, _ = post(_MCP_INIT)
    if not sid:
        raise RuntimeError("hub MCP initialize returned no session id")
    session = {"Mcp-Session-Id": sid}
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    _, text = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "gestalt_search", "arguments": {"query": query, "limit": limit, "semantic": True}}}, session)
    body = next((ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")), text)
    res = json.loads(body).get("result") or {}
    if res.get("isError"):
        raise RuntimeError(f"hub gestalt_search error: {res.get('content')}")
    out = (res.get("structuredContent") or {}).get("result")
    if not isinstance(out, list):
        raise RuntimeError("hub gestalt_search returned no structuredContent")
    for row in out:
        if isinstance(row, dict):
            row["via"] = "hub"
    return out


def _model_idle_s() -> int:
    try:
        return int(os.environ.get("GESTALT_MODEL_IDLE_S", "600"))
    except ValueError:
        return 600


def _release_model_memory() -> None:
    """Best-effort: hand the model's pages back to the OS (glibc keeps freed arenas otherwise)."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def unload_model(force: bool = False) -> bool:
    """Drop the embedding model if idle (or forced). Returns True when something was released."""
    global _model, _model_timer
    idle = _model_idle_s()
    if _model is None:
        return False
    if not force and (idle <= 0 or time.monotonic() - _model_last_used < idle):
        return False
    _model = None
    _model_timer = None
    _release_model_memory()
    print("gestalt-mcp: embedding model unloaded after idle", file=sys.stderr)
    return True


def _arm_unload_timer() -> None:
    global _model_timer
    idle = _model_idle_s()
    if idle <= 0:
        return
    if _model_timer is not None:
        _model_timer.cancel()
    t = threading.Timer(idle + 1, unload_model)
    t.daemon = True
    t.start()
    _model_timer = t


def get_model():
    global _model, _model_lock, _model_last_used
    if _model_lock is None:
        _model_lock = threading.Lock()
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(MODEL_NAME, revision=_ec.MODEL_REVISION, trust_remote_code=True, device="cpu")
        _model_last_used = time.monotonic()
        _arm_unload_timer()
    return _model


def get_sqlite_vec():
    global _sqlite_vec
    if _sqlite_vec is None:
        import sqlite_vec as sv
        _sqlite_vec = sv
    return _sqlite_vec


_meta_logged: set = set()


def _vector_leg_ok(db) -> bool:
    """Compare the index's index_meta with this server's constants (X14, 2026-10-06).

    A missing table (an older index) means proceed as before and log once. A present table that disagrees
    means the vectors came from a different model, so the vector leg is off and the caller answers from FTS."""
    try:
        meta = dict(db.execute("SELECT key, value FROM index_meta").fetchall())
    except sqlite3.Error:
        if "missing" not in _meta_logged:
            _meta_logged.add("missing")
            print("gestalt-mcp: index has no index_meta table (older build) — not checking the embedding model", file=sys.stderr)
        return True
    want = {
        "model_name": _ec.MODEL_NAME,
        "model_revision": _ec.MODEL_REVISION or "",
        "embed_dim": str(_ec.EMBED_DIM),
        "doc_prefix": _ec.DOC_PREFIX,
        "query_prefix": _ec.QUERY_PREFIX,
    }
    if not meta:  # an FTS-only build writes the table empty
        return True
    bad = {k: (meta.get(k), v) for k, v in want.items() if k in meta and meta[k] != v}
    if not bad:
        return True
    if "mismatch" not in _meta_logged:
        _meta_logged.add("mismatch")
        print(f"gestalt-mcp: index_meta disagrees with this server ({bad}) — vector leg disabled, answering from full text. Rebuild the index.", file=sys.stderr)
    return False


_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _knowledge_path(slug: str) -> Path | None:
    """Map a slug to a file under knowledge/, or None (X11, 2026-10-06).

    The slug is a bare name. Separators, `..` and absolute paths never match the pattern. The resolved
    path must also stay under knowledge/, which catches a symlink that points elsewhere."""
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        return None
    root = KNOWLEDGE_DIR.resolve()
    path = (KNOWLEDGE_DIR / f"{slug}.md").resolve()
    return path if path.is_relative_to(root) else None


_QUERY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,79}")
_MANIFEST_MAX_HITS = 10


def _manifest_blocks(text: str) -> list[tuple[str, str]]:
    """Split a manifest file into (slug, block text) pairs, one per `### slug` heading."""
    out: list[tuple[str, str]] = []
    slug, buf = None, []
    for line in text.split("\n"):
        if line.startswith("### "):
            if slug is not None:
                out.append((slug, "\n".join(buf).rstrip() + "\n"))
            slug, buf = line[4:].strip(), [line]
        elif slug is not None:
            buf.append(line)
    if slug is not None:
        out.append((slug, "\n".join(buf).rstrip() + "\n"))
    return out


def manifest_lookup(query: str = "") -> str:
    """Summary with no query, else the block for one slug, else the blocks matching a short query.

    Reads MANIFEST.md and MANIFEST-papers.md (paper-* entries moved there on 2026-10-06, roadmap L2).
    The query only filters manifest text and never touches the filesystem. It is still validated like a
    slug (no separators, no `..`) so gestalt_read and gestalt_manifest reject the same inputs."""
    main_path, papers_path = GESTALT_DIR / "MANIFEST.md", GESTALT_DIR / "MANIFEST-papers.md"
    if not main_path.exists():
        return "MANIFEST.md not found."
    main_text = main_path.read_text(encoding="utf-8")
    papers_text = papers_path.read_text(encoding="utf-8") if papers_path.exists() else ""
    main_blocks, paper_blocks = _manifest_blocks(main_text), _manifest_blocks(papers_text)
    rules = [ln for ln in main_text.split("\n") if ln.startswith("- `")]
    query = (query or "").strip()
    if not query:
        return (
            f"Gestalt manifest summary: {len(rules)} rules, {len(main_blocks)} knowledge entries in MANIFEST.md, "
            f"{len(paper_blocks)} paper-* entries in MANIFEST-papers.md.\n"
            "Look up one entry with gestalt_manifest('<slug>'), for example gestalt_manifest('<kb-entry>').\n"
            "Find entries with gestalt_manifest('<short query>'): every word must appear in the slug, title or block ids. "
            "Each hit shows the entry's type, title, outgoing links and block ids.\n"
            "Read an entry's text with gestalt_read('<slug>'). Search content with gestalt_search."
        )
    if not _QUERY_RE.fullmatch(query) or ".." in query:
        return (f"Invalid query {query[:80]!r}: use a slug such as '<kb-entry>' or a few plain words. "
                "Call gestalt_manifest() with no argument for a summary.")
    blocks = main_blocks + paper_blocks
    for slug, block in blocks:
        if slug == query:
            return block
    for line in rules:
        if line.startswith(f"- `{query}`"):
            return line + "\n"
    terms = query.lower().split()
    hits = []
    for slug, block in blocks:
        lines = block.split("\n")
        hay = " ".join([slug, lines[1] if len(lines) > 1 else ""] + [ln for ln in lines if ln.startswith("Blocks:")]).lower()
        if all(t in hay for t in terms):
            hits.append(block)
    hits += [ln + "\n" for ln in rules if all(t in ln.lower() for t in terms)]
    if not hits:
        return f"No manifest entry matches {query!r}. Call gestalt_manifest() with no argument for a summary."
    shown = "\n".join(hits[:_MANIFEST_MAX_HITS])
    more = len(hits) - _MANIFEST_MAX_HITS
    return shown + (f"\n({more} more matches. Narrow the query or use a full slug.)\n" if more > 0 else "")


def get_db():
    sv = get_sqlite_vec()
    db = sqlite3.connect(str(DB_PATH))
    db.enable_load_extension(True)
    sv.load(db)
    db.row_factory = sqlite3.Row
    return db


def fts_tokens(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression: per-token OR, never a phrase query.

    Quoting the whole string makes it a *phrase* query, so a multi-word
    natural-language search would require that exact contiguous run of words and
    match nothing. Per-token quoting still escapes FTS5 operators (AND/OR/NEAR/*/^).
    Returns None when the query holds no word characters, since an empty MATCH is
    a syntax error.
    """
    tokens = [t for t in re.split(r"\W+", query) if t]
    return " OR ".join('"' + t + '"' for t in tokens) if tokens else None


def get_fts_db():
    """Read-only connection for the FTS5-only path.

    Deliberately does NOT call get_sqlite_vec(): FTS5 ships inside SQLite itself,
    so the lexical leg needs no extension, no torch, and no embedding model. That
    is the whole point — this connection opens in ~1 ms where get_db() plus
    get_model() costs ~14 s.
    """
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _match_snippet(content: str, query: str, width: int = 500) -> str:
    """Window the snippet around the densest cluster of query terms instead of the
    chunk head. Root cause of the 2026-08-30 "right hit looks irrelevant" failure:
    <kb-entry> sections run to many KB, the match sat mid-chunk, and content[:500]
    showed an unrelated opening — the top result read as noise (an campus VPN query
    whose answer WAS hit #1). Case-insensitive, cheap (one lowercase pass), and
    graceful: no term found -> chunk head as before."""
    if len(content) <= width:
        return content
    low = content.lower()
    terms = [w for w in query.lower().split() if len(w) >= 3]
    positions = [i for w in terms for i in [low.find(w)] if i >= 0]
    if not positions:
        return content[:width]
    # densest cluster = median of term hit positions; center the window there
    positions.sort()
    center = positions[len(positions) // 2]
    start = max(0, min(center - width // 3, len(content) - width))
    snip = content[start:start + width]
    return ("…" + snip) if start > 0 else snip


def _norm_terms(text: str) -> list[str]:
    return [t for t in re.split(r"\W+", (text or "").lower()) if t]


def _annotate_results(results: list[dict], query: str) -> list[dict]:
    """Stamp each result with `evidence` (why it matched) and `create_safety`
    (exists|probable|unknown) so a caller deciding "does an entry for this already
    exist?" has a labeled reason instead of a raw score. Match-TYPE only — never a
    bm25/RRF threshold (^bm25-thresholds: the distributions overlap; a numeric
    cutoff here would be exactly the fuzzy-blended-score failure that produced
    gbrain's duplicate-stub incident, knowledge/gbrain.md ^maxpool-incident).
    Also appends slugs to the local retrieval-hit sidecar (knowledge/gbrain.md
    ^transfer-list item 10, scoped: per-node JSONL, no fleet aggregation yet —
    NEVER written into .search/, which is a published fleet artifact)."""
    q_terms = _norm_terms(query)
    q_join = "-".join(q_terms)
    for r in results:
        if r.get("error"):
            continue
        slug = (r.get("slug") or "").lower()
        h_terms = _norm_terms(r.get("heading"))
        if q_join and q_join == slug:
            r["evidence"], r["create_safety"] = "slug-exact", "exists"
        elif q_terms and q_terms == h_terms:
            r["evidence"], r["create_safety"] = "title-exact", "exists"
        elif q_terms and h_terms and all(t in h_terms for t in q_terms):
            r["evidence"], r["create_safety"] = "title-terms", "probable"
        elif r.get("block_id"):
            r["evidence"], r["create_safety"] = "anchor-section", "unknown"
        else:
            r["evidence"], r["create_safety"] = "body-fts", "unknown"
    try:
        hits_path = os.environ.get("GESTALT_HITS_LOG", "")
        if hits_path.lower() not in ("0", "off", "disabled"):
            path = Path(hits_path) if hits_path else Path.home() / ".claude" / "gestalt" / "retrieval-hits.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            slugs = list(dict.fromkeys(r["slug"] for r in results if r.get("slug")))
            if slugs:
                with path.open("a") as f:
                    f.write(json.dumps({"ts": int(time.time()), "q": query[:200], "slugs": slugs}) + "\n")
    except Exception:
        pass  # fail-open: a hit log must never break search
    return results


# --- L7 sensitivity (2026-10-06) ---------------------------------------------------------------
# An entry may carry `sensitivity: public | unpublished | restricted` in its frontmatter. A restricted
# entry never leaves through gestalt_search, gestalt_search_fts or gestalt_read unless the caller passes
# include_restricted=true, and the result says one matched and was withheld. No entry is restricted
# today, so nothing changes until the owner marks one. The index stores the value per section (a
# sections_meta column). An index built before that column, or a hub relay row, falls back to the
# entry's own frontmatter, so a stale index never leaks a restricted entry.
def _frontmatter_sensitivity(text: str) -> str:
    if not text.startswith("---"):
        return "unpublished"
    end = text.find("\n---", 3)
    for line in text[3:end if end != -1 else 0].splitlines():
        key, sep, val = line.partition(":")
        if sep and key.strip() == "sensitivity":
            return val.strip().strip("\"'").lower() or "unpublished"
    return "unpublished"


def _file_sensitivity(file_path: str) -> str:
    try:
        return _frontmatter_sensitivity((GESTALT_DIR / file_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return "unpublished"


def _withheld_note(slug: str) -> str:
    return (f"A restricted entry matched ({slug}) and was withheld. "
            "Pass include_restricted=true to read it.")


def _filter_restricted(rows: list[dict], include_restricted: bool) -> list[dict]:
    """Drop restricted rows and add one withheld notice per entry, so the caller knows it exists."""
    if include_restricted:
        for r in rows:
            r.pop("sensitivity", None)
        return rows
    out, noted = [], set()
    for r in rows:
        if "error" in r:
            out.append(r)
            continue
        sens = r.pop("sensitivity", None) or _file_sensitivity(r.get("file_path") or "")
        if sens != "restricted":
            out.append(r)
        elif r.get("slug") not in noted:
            noted.add(r.get("slug"))
            out.append({"slug": r.get("slug"), "heading": "withheld", "block_id": None, "file_path": r.get("file_path"),
                        "withheld": "restricted", "content": _withheld_note(r.get("slug"))})
    return out


def _has_sensitivity_col(db) -> bool:
    return any(r[1] == "sensitivity" for r in db.execute("PRAGMA table_info(sections_meta)"))


def _gestalt_search_fts_raw(query: str, limit: int = 10) -> list[dict]:
    """Lexical-only (BM25/FTS5) search. No embedding model, no vector leg.

    For latency-bound callers — anything running per-prompt or per-turn in a hook,
    where loading a sentence-transformers model is not affordable. Measured on this
    corpus: ~1-6 ms in-process, ~0.22 s for a whole cold `python3` process, against
    ~14-18 s for the hybrid path.

    Returns the same shape as gestalt_search() so callers can swap one for the
    other, plus `bm25`: FTS5's relevance score (negative; more negative = better).

    Do NOT gate on `bm25` with a fixed threshold. Measured 2026-08-10 against
    evals/retrieval/golden.yaml, real user queries score -7.04 to -19.64 while
    synthetic off-topic English scores -5.83 to -9.24 — the distributions overlap,
    and dividing by query length overlaps them further (real -0.783..-2.950 vs
    off-topic -0.785..-1.496). BM25 is a ranking score, not a calibrated relevance
    measure, exactly as RRF is (see ^rrf-thresholds). Control cost by keeping the
    injected payload small, not by thresholding.
    """
    if not DB_PATH.exists():
        return []

    fts_query = fts_tokens(query)
    if fts_query is None:
        return []

    db = get_fts_db()
    try:
        sens_col = "m.sensitivity" if _has_sensitivity_col(db) else "NULL"
        rows = db.execute(
            f"SELECT m.slug, m.heading, m.block_id, m.content, m.file_path, {sens_col} AS sensitivity, "
            "bm25(sections_fts) AS bm25 "
            "FROM sections_fts f JOIN sections_meta m ON m.id = f.rowid "
            "WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, limit),
        ).fetchall()
    except Exception:
        return []
    finally:
        db.close()

    return _annotate_results([
        {
            "slug": r["slug"],
            "heading": r["heading"],
            "block_id": r["block_id"],
            "content": _match_snippet(r["content"] or "", query),
            "file_path": r["file_path"],
            "sensitivity": r["sensitivity"],
            "bm25": round(r["bm25"], 3),
        }
        for r in rows
    ], query)


def _leaf_may_load_model() -> bool:
    """Laptops never load the embedding model (<kb-entry>, 2026-09-02): the hub holds the
    fleet's only resident model, and a leaf that loads its own costs 750 MB on a machine already in swap.
    GESTALT_LEAF_LOCAL_MODEL=1 re-enables the local leg on a node that can afford it."""
    return _is_hub() or os.environ.get("GESTALT_LEAF_LOCAL_MODEL", "") == "1"


def _route_dense_leg(request: str) -> list[int]:
    """Ranked skill ids from the dense leg, or [] when unavailable.

    Separate function so the default routing path never touches it: importing
    torch inside gestalt_route's hot path would re-create the caba378 hook bug
    (a 522 MB model load per prompt). Callers gate on `semantic`.
    """
    if not _leaf_may_load_model():
        return []
    try:
        model = get_model()
        db = get_db()
    except Exception:
        return []
    if not _vector_leg_ok(db):
        db.close()
        return []
    try:
        emb = model.encode(_ec.QUERY_PREFIX + request)
        return [
            r["id"]
            for r in db.execute(
                "SELECT id, distance FROM skills_vec "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (emb.tobytes(), 15),
            ).fetchall()
        ]
    except Exception:
        # skills_vec absent (index predates dense routing) or vec extension
        # unavailable — semantic leg silently contributes nothing.
        return []
    finally:
        db.close()


def gestalt_route(request: str, limit: int = 5, semantic: bool | None = None) -> list[dict]:
    """Shortlist candidate skills for a request. Lexical by default (~1 ms,
    model-free); opt-in hybrid via `semantic`.

    **A weak advisory signal, not a router.** Measured 2026-08-11 on the 40 cases
    in tests/test_routing.py against the consolidated 21-skill catalog:
    lexical **28/40 top-1, 31/40 top-3**; hybrid (semantic=True) **27/40 top-1,
    34/40 top-3**. Hybrid is the better shortlist generator (top-3 is this tool's
    job); lexical is marginally better at top-1. Treat the list as a reminder
    that a skill exists, and decide yourself — you can read the descriptions and
    interpret intent, which neither leg can.

    History: the 31-skill catalog measured 23/40 top-1 (57%) / 31/40 top-3 (77%)
    lexical — the 2026-08-11 consolidation of 10 zero-invocation skills into 2
    parameterized ones was itself the largest routing improvement. An earlier
    15-case set reported 60%/93%; artifact of too few, too-easy cases — never
    quote it.

    Two lexical legs, description and body, fused by RRF. Measured alternatives
    on the 40 cases (31-skill catalog):

        description only                  21/40 top-1   32/40 top-3
        name + description/body (former)  19/40         32/40
        description + body (this)         23/40         31/40

    Dense vectors embed name+description ONLY: full-body embedding measured
    26/40 top-1 on the 21-skill catalog because a multi-mode SKILL.md dilutes
    into a vector that matches nothing well.

    **The name leg was removed because it measurably hurt.** Most slugs are ordinary
    English words — review, fix, build, help, save, commit — so a query merely *using*
    the word got routed to that skill: "I need to review a pull request diff" returned
    `review` ahead of `pr`. Matching a skill's name is not evidence of intent to invoke
    it.

    Per-column bm25 weights are also avoided. `name` is a 1-3 token field and `body`
    runs to thousands, so their contributions differ by ~3 orders of magnitude and a
    weight of 10 on name changed effectively nothing (it once ranked the correct
    `review` skill 7th). Weighting incommensurable scales is the same error as
    thresholding a ranking score — see ^bm25-thresholds. RRF fuses by order and is
    indifferent to scale, which is exactly why it is the right tool here.

    The residual failures are semantic, not lexical: "what can you do" -> help,
    "poke holes in this plan" -> discuss, "trace why X breaks" -> investigate. No
    lexical method reaches those, which capped the lexical-only variant at ~57% top-1.

    `semantic` adds a third RRF leg over per-skill dense vectors (skills_vec):
      - None (default): AUTO — use the dense leg only if the embedding model is
        ALREADY resident in this process (e.g. a prior gestalt_search loaded it).
        A cold process stays lexical and torch-free, which the per-prompt hook
        requires (caba378 post-mortem; test_route_does_not_import_torch).
      - True: force the dense leg, loading the model if needed (~14 s cold, once
        per process). Right choice inside the long-lived MCP server, where
        /prompt's grounding step loads the model via gestalt_search anyway.
      - False: lexical only.

    Returns [] when the skills index is absent, so /prompt falls back to reading
    references/skill-index.md. Never threshold on `rrf`; it orders candidates and means
    nothing absolute.
    """
    if not DB_PATH.exists():
        return []
    fts_query = fts_tokens(request)
    if fts_query is None:
        return []

    if semantic is None:
        semantic = _model is not None
    dense_leg: list[int] = _route_dense_leg(request) if semantic else []

    db = get_fts_db()
    try:
        # Two legs, each its own ranking: the curated one-line description, and the
        # full body. No name leg (it hurt, see the docstring) and no cross-column
        # weights (incommensurable scales).
        def _leg(weights: str) -> list[str]:
            return [
                r["name"]
                for r in db.execute(
                    "SELECT s.name FROM skills_fts f JOIN skills_meta s ON s.id = f.rowid "
                    f"WHERE skills_fts MATCH ? ORDER BY bm25(skills_fts, {weights})",
                    (fts_query,),
                ).fetchall()
            ]

        desc_leg = _leg("0.0, 1.0, 0.0")
        body_leg = _leg("0.0, 0.0, 1.0")
        rows = db.execute("SELECT id, name, description, lines FROM skills_meta").fetchall()
        meta = {r["name"]: r for r in rows}
        id_to_name = {r["id"]: r["name"] for r in rows}
    except Exception:
        # No skills_fts table yet (index predates skill routing). Silence is correct
        # here only because the caller has a documented fallback.
        return []
    finally:
        db.close()

    legs: list[list[str]] = [desc_leg, body_leg]
    if dense_leg:
        legs.append([id_to_name[i] for i in dense_leg if i in id_to_name])
    scores = _rrf_fuse(legs)

    ordered = sorted(scores, key=lambda n: -scores[n])[:limit]
    return [
        {
            "skill": n,
            "description": meta[n]["description"] if n in meta else "",
            "lines": meta[n]["lines"] if n in meta else 0,
            "rrf": round(scores[n], 4),
        }
        for n in ordered
    ]


def gestalt_search_fts(query: str, limit: int = 10, include_restricted: bool = False) -> list[dict]:
    """Lexical-only (BM25/FTS5) search. No embedding model, no vector leg. Same shape as gestalt_search().

    For latency-bound callers such as per-prompt hooks. `bm25` is FTS5's relevance score (negative;
    more negative = better). Do not gate on it with a fixed threshold: see _gestalt_search_fts_raw.
    A restricted entry is withheld and reported as a notice row unless include_restricted=true."""
    return _filter_restricted(_gestalt_search_fts_raw(query, limit), include_restricted)


def _gestalt_search_raw(query: str, limit: int = 10, semantic: bool | None = None) -> list[dict]:
    """Hybrid BM25 + semantic search over gestalt knowledge entries.

    Returns sections with slug, heading, block_id, content snippet, and score.
    Use for finding relevant knowledge by topic.

    `semantic`: True asks for the hybrid leg: a non-hub node relays the query to the
    hub's server (rows carry `via: hub`) and only runs locally if the hub is unreachable;
    the hub answers from its own vectors. Leaf nodes default to FTS-only (the 2026-08-18
    shared-MCP change), so recall-sensitive callers like /prompt grounding should pass
    True. False forces FTS; None keeps the node's mode default.
    """
    if not DB_PATH.exists():
        return [{"error": "Search index not built. Run: python3 gestalt/tools/gestalt-index-builder.py"}]

    if not query.strip():
        return [{"error": "Query cannot be empty"}]

    # F13 (<kb-entry>): a node missing
    # sqlite-vec/sentence-transformers used to raise an uncaught ImportError here,
    # which every caller in this repo wraps in a bare `except Exception: pass`
    # (post-compact-reinject.sh, prompt-intelligence.py, auto-promote.py) — so the
    # hybrid leg degraded to a silent no-op with zero visible symptom. Log ONE
    # clear line to stderr (stdout is the MCP stdio JSON-RPC channel — never print
    # there) naming the module and the install command, then fall back to the
    # FTS-only leg so the caller still gets real results instead of nothing.
    mode_fts = (search_mode() == "fts") if semantic is None else (not semantic)
    if mode_fts:
        return gestalt_search_fts(query, limit)  # public name: the FTS leg already withholds restricted rows
    # Leaf nodes relay the semantic leg to the hub's server (<kb-entry>): the hub
    # holds the fleet's only vector index and only resident model, so a laptop never loads 750 MB of
    # weights to embed one query. Falls through to the local path, then FTS, when the hub is unreachable.
    if _hub_mcp_url() and not _hub_looks_down():
        try:
            return _hub_search(query, limit)
        except Exception as e:
            print(f"gestalt-mcp: hub relay failed ({type(e).__name__}: {str(e)[:120]}) — trying local hybrid, then FTS", file=sys.stderr)
    if not _leaf_may_load_model():
        print("gestalt-mcp: leaf node keeps the embedding model unloaded (GESTALT_LEAF_LOCAL_MODEL=1 overrides) — using FTS", file=sys.stderr)
        return gestalt_search_fts(query, limit)
    try:
        db = get_db()      # cheap import check first — never load the model if sqlite-vec is missing
        has_vec = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sections_vec'"
        ).fetchone() is not None
        if not has_vec:
            # FTS-only index on this node (built without the embedding leg, or fetched from a
            # node that built it that way): loading the model would cost ~750 MB and then fail on
            # the missing vec table — measured on the laptop 2026-08-18. Fall back before loading.
            print("gestalt-mcp: index has no sections_vec (FTS-only build) — hybrid search unavailable, using FTS", file=sys.stderr)
            db.close()
            return gestalt_search_fts(query, limit)
        if not _vector_leg_ok(db):
            db.close()
            return gestalt_search_fts(query, limit)
        model = get_model()
    except ImportError as e:
        print(
            f"Missing dependency: {e.name or e} — install: pip install sqlite-vec "
            "sentence-transformers — continuing FTS-only (vector search disabled).",
            file=sys.stderr,
        )
        # get_db() itself can be what raised, leaving db unbound — hence the guard.
        try:
            db.close()
        except Exception:
            pass
        return gestalt_search_fts(query, limit)

    try:
        # Sanitize FTS5 query. Quote each token separately and OR them,
        # rather than quoting the whole string: wrapping the entire query in
        # one pair of quotes makes it a PHRASE query, so any multi-word
        # natural-language search required that exact contiguous phrase and
        # therefore returned no BM25 rows at all. Losing the BM25 leg left
        # RRF ranking on dense vectors alone. Per-token quoting still escapes
        # FTS5 operators (AND/OR/NEAR/*/^) safely.
        tokens = [t for t in re.split(r"\W+", query) if t]
        # A query of only punctuation leaves no tokens; an empty MATCH is a
        # syntax error, so skip the BM25 leg and let the vector leg answer.
        # Tried and rejected 2026-08-10: appending the full query as an extra
        # OR'd phrase clause ('"a" OR "b" OR "a b"') to add a phrase-adjacency
        # precision signal. Measured on evals/retrieval/golden.yaml it changed
        # nothing at all — recall@1/3/5, MRR and block precision were byte-identical
        # — because natural-language queries never appear contiguously in a chunk,
        # so the extra clause never fires. Do not re-add without a query mix where
        # exact phrases are actually expected.
        fts_query = " OR ".join('"' + t + '"' for t in tokens) if tokens else None

        # BM25 (FTS5)
        fts_results = []
        if fts_query:
            try:
                fts_results = db.execute(
                    "SELECT rowid, slug, heading, block_id, content, rank AS score "
                    "FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, limit * 2),
                ).fetchall()
            except Exception:
                fts_results = []

        # Dense vector (sqlite-vec) — k=? for SQLite <3.41 compat
        # Asymmetric-retrieval prefix required by nomic-embed-text-v1.5;
        # must match DOC_PREFIX used at index time in gestalt-index-builder.py.
        query_emb = model.encode(_ec.QUERY_PREFIX + query)
        vec_results = db.execute(
            "SELECT id, distance FROM sections_vec "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (query_emb.tobytes(), limit * 2),
        ).fetchall()

        # Heading/anchor leg — section identity, separate from body content.
        #
        # Without it, "right document, wrong section" was the dominant failure:
        # block precision measured 64.6% once the golden set covered sections that
        # nothing had probed before. The heading IS already inside the embedded text,
        # but as ~5 tokens in a ~2,000-char chunk it is diluted to nothing, and the
        # FTS leg searches every column unweighted.
        #
        # A third RANKED leg rather than a column weight, for the reason established
        # in ^bm25-thresholds and again in ^skill-routing: `heading` and `content`
        # have incommensurable lengths, so a bm25 column weight on them means nothing
        # predictable. RRF fuses by order and is indifferent to scale.
        # Reciprocal Rank Fusion over two legs: body-lexical and dense-vector.
        scores = _rrf_fuse([
            [row["rowid"] for row in fts_results],
            [row["id"] for row in vec_results],
        ])

        top_ids = sorted(scores, key=scores.get, reverse=True)[:limit]

        results = []
        for rid in top_ids:
            meta = db.execute("SELECT * FROM sections_meta WHERE id = ?", (rid,)).fetchone()
            if meta:
                results.append({
                    "slug": meta["slug"],
                    "heading": meta["heading"],
                    "block_id": meta["block_id"],
                    "content": _match_snippet(meta["content"], query),
                    "file_path": meta["file_path"],
                    "sensitivity": meta["sensitivity"] if "sensitivity" in meta.keys() else None,
                    "score": round(scores[rid], 4),
                })

        return _annotate_results(results, query)
    finally:
        db.close()

def gestalt_search(query: str, limit: int = 10, semantic: bool | None = None, include_restricted: bool = False) -> list[dict]:
    """Hybrid BM25 + semantic search over gestalt knowledge entries.

    Returns sections with slug, heading, block_id, content snippet, and score.
    `semantic`: True asks for the hybrid leg (a leaf relays it to the hub), False forces FTS, None keeps the node default.
    A restricted entry is withheld and reported as a notice row unless include_restricted=true.
    A hub relay never carries include_restricted: the hub answers with restricted entries withheld."""
    rows = _gestalt_search_raw(query, limit, semantic)
    if include_restricted and any(r.get("withheld") for r in rows):
        rows = _gestalt_search_fts_raw(query, limit)  # the FTS fallback leg had withheld rows: redo it unfiltered
    return _filter_restricted(rows, include_restricted)


def _transport_security(host: str):
    """DNS-rebinding protection stays ON; the allow list names exactly the addresses this listener answers on.

    The SDK auto-allows only localhost, and only when `host` is passed to the constructor, so the hub's
    tailnet bind answered 421 "Invalid Host header" to every client (resident-hub, 2026-09-02). Extra names
    via GESTALT_MCP_ALLOWED_HOSTS (comma-separated host or host:port); never a wildcard host.
    """
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        import socket
        hosts.append(f"{host}:*")
        hosts.append(f"{os.environ.get('FLEET_HUB', 'node.example.ts.net')}:*")
        hosts.append(f"{socket.gethostname().split('.')[0].lower()}:*")
    for extra in os.environ.get("GESTALT_MCP_ALLOWED_HOSTS", "").split(","):
        extra = extra.strip()
        if extra:
            hosts.append(extra if ":" in extra else f"{extra}:*")
    hosts = list(dict.fromkeys(hosts))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts],
    )


def build_mcp(host: str = "127.0.0.1"):
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP
    except ImportError:  # mcp >=2.0 renamed FastMCP -> MCPServer (py.sdk.modelcontextprotocol.io/migration/)
        from mcp.server.mcpserver import MCPServer as FastMCP
    try:
        mcp = FastMCP("gestalt", host=host, transport_security=_transport_security(host))
    except TypeError:  # an SDK whose constructor lacks these kwargs: bind is set below, protection stays SDK-default
        mcp = FastMCP("gestalt")

    # --- Curated Layer ---

    @mcp.tool()
    def gestalt_read(slug: str, include_restricted: bool = False) -> str:
        """Read a gestalt knowledge entry by slug. Returns full markdown content.

        A restricted entry (frontmatter sensitivity: restricted) is withheld unless include_restricted=true."""
        path = _knowledge_path(slug)
        if path is None:
            return f"Invalid slug {str(slug)[:80]!r}: use a bare entry name, for example '<kb-entry>'. Use gestalt_manifest() to list available entries."
        if not path.exists():
            # Try fuzzy match
            matches = [p.stem for p in KNOWLEDGE_DIR.glob("*.md") if slug.lower() in p.stem.lower()]
            if matches:
                return f"Entry '{slug}' not found. Did you mean: {', '.join(matches[:5])}?"
            return f"Entry '{slug}' not found. Use gestalt_manifest() to list available entries."
        text = path.read_text(encoding="utf-8")
        if not include_restricted and _frontmatter_sensitivity(text) == "restricted":
            return _withheld_note(slug)
        return text

    @mcp.tool()
    def gestalt_manifest(query: str = "") -> str:
        """Look up entries in the gestalt manifest (links and block-ids per entry, from MANIFEST.md and MANIFEST-papers.md).

        With no argument, returns a short summary: counts of rules, knowledge entries and paper-* entries, and how to look up a slug.
        With a slug (for example '<kb-entry>' or 'paper-attention'), returns that entry's block.
        With a few words, returns up to 10 entries whose slug, title or block ids contain every word.
        """
        return manifest_lookup(query)

    @mcp.tool()
    def gestalt_graph() -> str:
        """Return the gestalt GRAPH.md — data flow index across all entries."""
        path = GESTALT_DIR / "GRAPH.md"
        return path.read_text(encoding="utf-8") if path.exists() else "GRAPH.md not found."

    # --- Semantic Layer ---

    mcp.tool()(gestalt_search)
    mcp.tool()(gestalt_search_fts)
    mcp.tool()(gestalt_route)

    # --- Temporal Layer ---
    # Graphiti uses Streamable HTTP transport which requires session-based MCP
    # connections. Direct HTTP proxy doesn't work. Use the graphiti-memory MCP
    # server directly for temporal queries:
    #   mcp__graphiti-memory__search_memory_facts(query, group_ids, max_facts)
    #   mcp__graphiti-memory__search_nodes(query, group_ids, max_nodes)
    #   mcp__graphiti-memory__add_memory(name, episode_body, group_id, source)

    return mcp


if __name__ == "__main__":
    if "--http" in sys.argv:
        idx = sys.argv.index("--http")
        port = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("--") else 8300
        host = "127.0.0.1"
        if "--host" in sys.argv:
            host = sys.argv[sys.argv.index("--host") + 1]
        mcp = build_mcp(host)
        # shared per-node server: one process, one model, every seat (see F18 note above)
        mcp.settings.host = host
        mcp.settings.port = port
        print(f"gestalt-mcp: streamable-http on {host}:{port}/mcp mode={search_mode()} idle={_model_idle_s()}s", file=sys.stderr)
        mcp.run(transport="streamable-http")
    else:
        build_mcp().run()
