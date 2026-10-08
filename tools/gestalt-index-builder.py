#!/usr/bin/env python3
"""Build the gestalt semantic search index from knowledge/*.md and .claude/rules/*.md files.

Creates a SQLite database with FTS5 (full-text) and sqlite-vec (vector)
indices for hybrid BM25 + dense retrieval.

Usage:
    python3 gestalt-index-builder.py              # full rebuild (skipped if up to date)
    python3 gestalt-index-builder.py --force      # force full rebuild even if up to date
    python3 gestalt-index-builder.py --check      # report whether rebuild is needed
    python3 gestalt-index-builder.py --feed-graphiti   # build, then report modified entries
    python3 gestalt-index-builder.py --fts-only   # force lexical-only build (no embeddings)
    python3 gestalt-index-builder.py --stamp-only # stamp the existing db for HEAD, no rebuild

Every successful build writes .search/gestalt.db.stamp = "<repo HEAD sha> <builder schema
hash> <epoch>" next to the db, consumed by `fleet-sync index-publish`/`index-fetch`
(gestalt/knowledge/<kb-entry>.md ^fleet-sync-decision) and index_stamp_matches().
"""

import hashlib
import re
import os
import sqlite3
import sys
import time
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

# Advisory build lock. fcntl is POSIX-only; on Windows msvcrt.locking provides
# the same mutual exclusion over a byte range of the lock file.
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def lock_exclusive(fh, blocking: bool = True) -> None:
    """Take an exclusive advisory lock on fh, or raise BlockingIOError."""
    if fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    fh.seek(0)
    if not blocking:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise BlockingIOError(str(e)) from e
        return
    # LK_LOCK gives up after ~10s, but a real index build takes minutes, so
    # retry until the other builder releases rather than failing the wait.
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            return
        except OSError:
            time.sleep(0.5)


def unlock(fh) -> None:
    """Release the advisory lock taken by lock_exclusive."""
    if fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_UN)
        return
    fh.seek(0)
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass

GESTALT_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = GESTALT_DIR / "knowledge"
# F6 (<kb-entry>): rules carry real
# procedural answers (e.g. the Supabase schema-first rule) that a knowledge-only
# index can never surface. Indexed alongside knowledge/*.md, tagged by file_path
# prefix (".claude/rules/" vs "knowledge/") rather than a separate column, since
# every chunk already carries file_path and every reader (CLI, MCP) already
# selects it.
RULES_DIR = GESTALT_DIR / ".claude" / "rules"
SEARCH_DIR = GESTALT_DIR / ".search"
DB_PATH = SEARCH_DIR / "gestalt.db"
# Scratch target for a rebuild; swapped onto DB_PATH atomically when complete.
BUILD_PATH = SEARCH_DIR / "gestalt.db.building"
LOCK_PATH = SEARCH_DIR / "build.lock"
# Stamp: "<repo HEAD sha> <builder schema hash> <epoch>" written next to DB_PATH after every
# successful build (or via --stamp-only). Lets fleet-sync/hooks decide "is the on-disk index
# this repo's HEAD, built by this exact builder" without opening the DB. gestalt D1, <kb-entry>
# ^fleet-sync-decision.
STAMP_PATH = SEARCH_DIR / "gestalt.db.stamp"
MODEL_NAME = _ec.MODEL_NAME
MODEL_REVISION = _ec.MODEL_REVISION
# Conservative chars-per-token bound; only used to pre-truncate at or above the
# model's own cutoff, so a loose overestimate stays lossless.
CHARS_PER_TOKEN = 6
DOC_PREFIX = _ec.DOC_PREFIX
QUERY_PREFIX = _ec.QUERY_PREFIX
# Keep peak memory bounded on CPU. This box has no usable CUDA, so encoding runs
# float32 on CPU where the default batch of 32 long chunks does not fit.
EMBED_BATCH_SIZE = 8
# Sub-split ceiling for a single `## section`. Heading-only chunking let one
# research-project section reach 285,566 chars — 63% of that file in a single row.
# An oversized chunk embeds to a topical average that weakly matches every
# query (measured: research-project intruded on 18.7% of top-3 slots while holding
# 7.8% of chunks), and anything past the encoder window was silently dropped.
MAX_CHUNK_CHARS = 2000
SUBCHUNK_OVERLAP = 150


# Anchors DEFINED in a chunk. Wikilinks are removed first so `[[other#^theirs]]`
# is not claimed here, and code spans so a grep pattern like `^title:` is not an
# anchor — mirroring extract_block_ids() in tools/gestalt.
_WIKILINK_RE = re.compile(r"\[\[[^\]]*\]\]")
_CODESPAN_RE = re.compile(r"`[^`]*`")


class _AnchorFinder:
    _RE = re.compile(r"\^([a-z][a-z0-9-]*)")

    def findall(self, text: str) -> list[str]:
        cleaned = _WIKILINK_RE.sub("", _CODESPAN_RE.sub("", text))
        return self._RE.findall(cleaned)


ANCHOR_RE = _AnchorFinder()


def entry_search_excluded(path: Path) -> bool:
    """True when the entry's frontmatter says `search: exclude` (2026-10-06).

    A dated snapshot or a raw evidence dump mentions everything, so it outranks durable knowledge for
    queries it has nothing to say about. The 2026-10-06 live-state snapshot pushed a voice entry out of
    the top five on the day it was added. Such an entry stays in the repo and stays linked from its hub
    entry. It is simply not a retrieval candidate."""
    try:
        # errors="replace": on a locked clone (CI) every knowledge file is git-crypt ciphertext, not text.
        with path.open(encoding="utf-8", errors="replace") as f:
            if f.readline().strip() != "---":
                return False
            for line in f:
                if line.strip() == "---":
                    return False
                key, sep, val = line.partition(":")
                if sep and key.strip() == "search":
                    return val.strip().strip("\"'").lower() == "exclude"
    except (OSError, ValueError):
        return False
    return False


def all_source_entries() -> list[Path]:
    """Every file the index is built from: knowledge/*.md + .claude/rules/*.md, minus `search: exclude` entries."""
    return [p for p in sorted(KNOWLEDGE_DIR.glob("*.md")) if not entry_search_excluded(p)] + sorted(RULES_DIR.glob("*.md"))


def get_modified_entries(since_mtime: float | None) -> list[Path]:
    """Return source .md files (knowledge/ + .claude/rules/) modified after since_mtime.

    If since_mtime is None, all entries are considered modified.
    """
    entries = all_source_entries()
    if since_mtime is None:
        return entries
    return [p for p in entries if p.stat().st_mtime > since_mtime]


def _vector_leg_missing() -> bool:
    """True when the DB has a sections_meta table but no sections_vec TABLE in its schema.

    Read the schema from sqlite_master BY NAME rather than by querying the table. sections_vec
    is a vec0 virtual table, so `SELECT count(*) FROM sections_vec` under an interpreter that
    has not loaded the sqlite_vec extension raises "no such module: vec0", which is
    indistinguishable from a genuinely absent table if you only inspect the exception. That
    ambiguity produced a false "the index is broken" reading on 2026-09-02 before it was
    checked properly. sqlite_master is plain SQL, needs no extension, and answers the schema
    question unambiguously.
    """
    try:
        db = sqlite3.connect(str(DB_PATH))
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        db.close()
    except sqlite3.Error:
        return False          # unreadable is already handled by the caller's own guard
    return "sections_meta" in names and "sections_vec" not in names


def needs_rebuild(expect_vectors: bool = False) -> tuple[bool, float | None]:
    """Check whether the index needs rebuilding.

    Returns (rebuild_needed, db_mtime_or_None).
    rebuild_needed is True when:
      - The DB does not exist, or
      - The DB exists but holds no rows (a previous build died partway), or
      - Any knowledge/*.md file is newer than the DB, or
      - expect_vectors and the DB carries no sections_vec table.

    The vector case is the same failure as the empty-DB case one level up. A build that ran
    under a python without sqlite_vec produces a complete, current, FTS-only index: every
    mtime is fresh and sections_meta is fully populated, so an mtime-and-rowcount check calls
    it up to date and NEVER repairs it. That is how the hub served FTS-only artifacts for two
    weeks, and on 2026-09-02 a plain rebuild again printed "up to date" over a db whose
    sections_vec table did not exist. Only --force could fix it, which requires somebody to
    already suspect the problem. expect_vectors is off by default so that a deliberate
    --fts-only build, which legitimately has no vector table, does not rebuild forever.

    The empty-DB case is not hypothetical: an OOM-killed build leaves a
    schema-only file whose mtime is newer than every entry, so an mtime-only
    check reports "up to date" over an index that returns nothing forever.
    """
    if not DB_PATH.exists():
        return True, None
    db_mtime = DB_PATH.stat().st_mtime
    try:
        db = sqlite3.connect(str(DB_PATH))
        row_count = db.execute("SELECT COUNT(*) FROM sections_meta").fetchone()[0]
        db.close()
    except sqlite3.Error:
        return True, db_mtime  # missing/corrupt schema — rebuild
    if row_count == 0:
        return True, db_mtime
    if expect_vectors and _vector_leg_missing():
        return True, db_mtime
    modified = get_modified_entries(db_mtime)
    return len(modified) > 0, db_mtime


def _builder_schema_hash() -> str:
    """sha1 of this file's own source. A builder code change (schema, chunking, anything)
    invalidates any artifact stamped by the old version, even when a HEAD sha alone would
    look fine — the fetch/publish path in fleet-sync must never install an artifact built
    by stale code just because the git sha happens to match."""
    return hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]


def _repo_head(cwd: Path | None = None) -> str | None:
    """`git rev-parse HEAD` for the gestalt repo (or an explicit cwd), or None on any failure."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd or GESTALT_DIR),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def write_stamp(sha: str | None = None) -> str | None:
    """Write STAMP_PATH = '<repo HEAD sha> <builder schema hash> <epoch>' next to DB_PATH.

    Returns the sha written, or None if the repo HEAD could not be determined (STAMP_PATH
    is left untouched in that case rather than written with a blank sha).
    """
    sha = sha or _repo_head()
    if not sha:
        return None
    SEARCH_DIR.mkdir(exist_ok=True)
    STAMP_PATH.write_text(f"{sha} {_builder_schema_hash()} {int(time.time())}\n")
    return sha


def restamp_if_only_head_moved() -> str | None:
    """Refresh the stamp's sha when HEAD moved but nothing indexed changed; returns the sha written.

    Refuses when the stamp is missing, unreadable, or carries another builder version's hash:
    that index needs a rebuild, and relabelling it would publish an artifact built by stale code."""
    try:
        parts = STAMP_PATH.read_text().split()
    except OSError:
        return None
    if len(parts) < 2 or parts[1] != _builder_schema_hash():
        return None
    head = _repo_head()
    if not head or parts[0] == head:
        return None
    return write_stamp(head)


def index_stamp_matches(repo_head: str | None = None) -> bool:
    """True when STAMP_PATH's sha == repo_head (default: current HEAD) AND its schema hash
    matches this builder file's *current* hash — a builder change invalidates old artifacts
    even when the git sha field would still match."""
    if not STAMP_PATH.exists():
        return False
    repo_head = repo_head or _repo_head()
    if not repo_head:
        return False
    try:
        parts = STAMP_PATH.read_text().split()
    except OSError:
        return False
    if len(parts) < 2:
        return False
    stamp_sha, stamp_hash = parts[0], parts[1]
    return stamp_sha == repo_head and stamp_hash == _builder_schema_hash()


SENSITIVITY_VALUES = ("public", "unpublished", "restricted")
DEFAULT_SENSITIVITY = "unpublished"  # what every entry without the key has always been (L7, 2026-10-06)


def entry_sensitivity(path: Path) -> str:
    """The entry's frontmatter `sensitivity` (L7, 2026-10-06), or the default when absent or not a known value.

    Frontmatter is flat key: value lines. An unknown value falls back to the default here and is
    caught by the hygiene test, so a typo can never crash an index build."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):  # ValueError: ciphertext on a locked clone is not UTF-8
        return DEFAULT_SENSITIVITY
    if not text.startswith("---"):
        return DEFAULT_SENSITIVITY
    end = text.find("\n---", 3)
    for line in text[3:end if end != -1 else 0].splitlines():
        key, sep, val = line.partition(":")
        if sep and key.strip() == "sensitivity":
            val = val.strip().strip("\"'").lower()
            return val if val in SENSITIVITY_VALUES else DEFAULT_SENSITIVITY
    return DEFAULT_SENSITIVITY


def parse_entry(path: Path) -> list[dict]:
    """Parse a knowledge entry into section chunks."""
    # encoding is explicit: Python defaults to the locale codec, which is
    # cp1252 on Windows and dies on the em-dashes these entries are full of.
    text = path.read_text(encoding="utf-8")
    # Strip YAML frontmatter
    if text.startswith("---"):
        try:
            end = text.index("---", 3)
            text = text[end + 3 :].strip()
        except ValueError:
            pass

    slug = path.stem
    chunks = []
    current_heading = "Overview"
    current_block_id = "overview"
    current_lines = []

    in_fence = False
    for line in text.split("\n"):
        # A fenced code block can contain lines like "## install deps"; those are
        # shell comments, not headings, and must not start a new chunk.
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if line.startswith("## ") and not in_fence:
            # Save previous chunk
            if current_lines:
                content = "\n".join(current_lines).strip()
                if content:
                    chunks.append(
                        {
                            "slug": slug,
                            "heading": current_heading,
                            "block_id": current_block_id,
                            "content": content,
                            "file_path": str(path.relative_to(GESTALT_DIR)),
                        }
                    )
            # Start new chunk
            heading_text = line.lstrip("# ").strip()
            if "^" in heading_text:
                parts = heading_text.split("^")
                current_heading = parts[0].strip()
                current_block_id = parts[1].strip()
            else:
                current_heading = heading_text
                current_block_id = (
                    heading_text.lower().replace(" ", "-").replace("/", "-")
                )
            current_lines = []
        else:
            if line.strip().startswith("^") and len(line.strip()) > 1:
                current_block_id = line.strip().lstrip("^")
            current_lines.append(line)

    # Save last chunk
    if current_lines:
        content = "\n".join(current_lines).strip()
        if content:
            chunks.append(
                {
                    "slug": slug,
                    "heading": current_heading,
                    "block_id": current_block_id,
                    "content": content,
                    "file_path": str(path.relative_to(GESTALT_DIR)),
                }
            )

    return _split_oversized(chunks)


def _split_text(body: str, limit: int, overlap: int) -> list[str]:
    """Recursively split on progressively finer separators until under `limit`.

    Paragraph-only splitting is not sufficient: the largest real section in this
    corpus held 20,114 chars with a single blank line in it, so a `\n\n`-only
    splitter silently left the ceiling unenforced. The empty separator is the
    guaranteed terminator — it always makes progress.
    """
    if len(body) <= limit:
        return [body]

    for sep in ("\n\n", "\n", ". ", " ", ""):
        if sep and sep not in body:
            continue
        pieces = list(body) if sep == "" else body.split(sep)
        joiner = "" if sep == "" else sep

        parts: list[str] = []
        buf: list[str] = []
        size = 0
        for piece in pieces:
            # A single piece bigger than the limit cannot be placed by this
            # separator; fall through to the next, finer one.
            if len(piece) > limit and sep != "":
                parts = []
                break
            if size + len(piece) > limit and buf:
                parts.append(joiner.join(buf))
                tail = joiner.join(buf)[-overlap:] if overlap else ""
                buf = [tail] if tail else []
                size = len(tail)
            buf.append(piece)
            size += len(piece) + len(joiner)
        else:
            if buf:
                parts.append(joiner.join(buf))
            parts = [p for p in (x.strip() for x in parts) if p]
            if parts and all(len(p) <= limit for p in parts):
                return parts

    # Unreachable in practice: the "" separator always yields per-character
    # pieces that fit. Kept as a hard guarantee rather than an assumption.
    return [body[i : i + limit] for i in range(0, len(body), limit)]


# F8 (<kb-entry>): a paragraph ending in
# its own `^anchor` (the corpus convention — e.g. "...as designed. ^git-crypt-merge")
# names the content that precedes it, back to the previous anchor or the start of
# the section. Splitting on those boundaries FIRST keeps that anchor as the chunk's
# real id instead of drowning it in a synthetic `heading-pN` fragment alongside
# unrelated neighbouring text — which is what silently demoted the git-crypt-merge
# chunk to rank #3 for "git-crypt merge conflict" before this fix.
_PARA_ANCHOR_RE = re.compile(r"\^([a-z][a-z0-9-]*)\s*$")


def _heading_slug(para: str) -> str:
    """Same derivation `parse_entry` uses for an anchor-less `##` heading."""
    text = para.lstrip().lstrip("#").strip()
    text = _PARA_ANCHOR_RE.sub("", text).strip()
    return text.lower().replace(" ", "-").replace("/", "-") or "section"


def _anchor_paragraph_groups(content: str) -> list[tuple[str, str | None]]:
    """Group `\\n\\n`-separated paragraphs so each run ending in `^anchor`
    becomes one (text, anchor) pair.

    A `###`/`####` sub-heading is ALSO a boundary, not just a trailing content
    anchor — without this, a sub-heading's own `^anchor` (a section landmark,
    e.g. "### Memory Layers ^memory-layers") matched the same regex as a
    content anchor and silently absorbed whatever anchorless prose preceded
    it, while everything AFTER kept accumulating until the next REAL content
    anchor — dragging unrelated sub-sections into that anchor's group.
    Verified 2026-08-18: the Repo Layout tree + the whole Memory Layers
    table/docker/Notion prose were all landing inside `^git-crypt-merge`.

    Content under a sub-heading that carries no content-anchor of its own
    falls back to that heading's own slug (mirroring how `parse_entry`
    derives an id for an anchor-less `##` heading) rather than a generic
    `-tail` counter, which collided across every such sub-heading in a
    multi-section chunk.
    """
    paragraphs = content.split("\n\n")
    groups: list[tuple[str, str | None]] = []
    buf: list[str] = []
    heading_id: str | None = None  # fallback id for content under the last heading seen
    for para in paragraphs:
        stripped = para.lstrip()
        cleaned = _WIKILINK_RE.sub("", _CODESPAN_RE.sub("", para)).rstrip()
        m = _PARA_ANCHOR_RE.search(cleaned)
        if stripped.startswith("#"):
            if buf:
                groups.append(("\n\n".join(buf), heading_id))
                buf = []
            if m:
                # The heading carries its own anchor: that IS its identity —
                # give it a tiny group of its own, and content that follows
                # (with no anchor of its own) falls back to a distinct
                # "<anchor>-body" id rather than colliding with it.
                groups.append((para, m.group(1)))
                heading_id = f"{m.group(1)}-body"
            else:
                # No anchor: fold the heading into the body that follows it,
                # under a slug derived from its own text.
                heading_id = _heading_slug(para)
                buf.append(para)
            continue
        buf.append(para)
        if m:
            if len(buf) > 1:
                # (heading_id may be None here: filler before the FIRST anchor of a
                # heading with no sub-heading — flush it under None, the caller's
                # defensive fallback id, instead of dropping it; found 2026-08-18.)
                # We are inside a sub-heading's scope and other, unrelated
                # anchorless paragraphs already accumulated (e.g. a table, an
                # unrelated aside) before this one closed on its own anchor.
                # A content anchor labels the paragraph it ends, not whatever
                # unrelated prose happened to precede it in the same
                # sub-section — so flush that prose under heading_id first,
                # and give the anchor ONLY the paragraph it actually closes.
                groups.append(("\n\n".join(buf[:-1]), heading_id))
            groups.append((buf[-1], m.group(1)))
            buf = []
    if buf:
        groups.append(("\n\n".join(buf), heading_id))
    return groups


def _size_split(c: dict, text: str, block_id: str) -> list[dict]:
    """Split `text` under MAX_CHUNK_CHARS if needed.

    Part 0 keeps `block_id` verbatim so an anchor stays a valid
    `[[slug#^anchor]]` target; later parts get `-pN` suffixes, same convention
    as the pre-F8 splitter.
    """
    parts = _split_text(text, MAX_CHUNK_CHARS, SUBCHUNK_OVERLAP)
    out: list[dict] = []
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        sub = dict(c)
        sub["content"] = part
        sub["block_id"] = block_id if i == 0 else f"{block_id}-p{i + 1}"
        out.append(sub)
    return out


def _split_oversized(chunks: list[dict]) -> list[dict]:
    """Enforce MAX_CHUNK_CHARS on every chunk, anchors first, size second.

    Parts inherit the parent heading/slug so citations still resolve.
    """
    out: list[dict] = []
    # A derived id (a sub-heading's own slug, or "<anchor>-body") is reused by
    # every anchorless run under that heading — e.g. the Memory Layers table
    # AND, much later, an unrelated stray paragraph before the RRF section's
    # own anchor both fell back to "memory-layers-body" before this
    # uniquifier, colliding two topically unrelated chunks onto one id. Scoped
    # to the WHOLE file (every `## ` chunk shares one `used_ids`), not just
    # one `## ` chunk — the same sub-heading title recurring under two
    # different `## ` sections (e.g. two "### Single-file datasets" under
    # different repos in the same entry) collided across chunk boundaries
    # otherwise. Every id assigned is uniquified (content anchors pass
    # through unchanged unless they, too, repeat).
    used_ids: dict[str, int] = {}

    def _uniquify(base_id: str) -> str:
        n = used_ids[base_id] = used_ids.get(base_id, 0) + 1
        return base_id if n == 1 else f"{base_id}-{n}"

    for c in chunks:
        if len(c["content"]) <= MAX_CHUNK_CHARS:
            # Register even an untouched chunk's id (usually the file's own
            # heading-derived block_id) so a LATER oversized chunk's
            # anchor-first split cannot silently reuse it — e.g. a top-level
            # "## Stated target register: Hemingway ^hemingway-target" chunk
            # registers "hemingway-target" here, so a much later sub-heading
            # that merely *references* that same token bare (not inside a
            # `[[wikilink]]`, so the wikilink-stripping in
            # _anchor_paragraph_groups does not catch it) gets suffixed to
            # "hemingway-target-2" instead of colliding with the real one.
            used_ids[c["block_id"]] = used_ids.get(c["block_id"], 0) + 1
            out.append(c)
            continue

        groups = _anchor_paragraph_groups(c["content"])
        if not any(anchor is not None for _, anchor in groups):
            # No internal ^anchors to split on — size-only, as before F8.
            out.extend(_size_split(c, c["content"], c["block_id"]))
            continue

        none_groups_seen = 0
        for text, anchor in groups:
            text = text.strip()
            if not text:
                continue
            if anchor is None:
                # Only the very first group can still be anchor=None: content
                # before any heading or content-anchor has been seen at all.
                # Every later fragment resolves to a real id — its own content
                # anchor, its enclosing sub-heading's slug, or "<anchor>-body"
                # — via _anchor_paragraph_groups. Kept as a defensive fallback
                # (not a dead branch) for a group with no heading and no
                # content anchor anywhere in it.
                none_groups_seen += 1
                fallback_id = c["block_id"] if none_groups_seen == 1 else f"{c['block_id']}-tail"
                out.extend(_size_split(c, text, _uniquify(fallback_id)))
            else:
                out.extend(_size_split(c, text, _uniquify(anchor)))
    return out


def build_index(force_fts_only: bool = False):
    """Full rebuild of the semantic search index.

    Serialized against other builders by an exclusive flock. Several Claude
    sessions share this workspace and each regenerates indices after a gestalt
    write, so concurrent builds are normal rather than exceptional. Without the
    lock they race destructively: this function unlinks DB_PATH and recreates
    it, so a second builder deletes the file a first one is midway through
    populating, and the first dies at INSERT with "attempt to write a readonly
    database" after having already spent minutes on embeddings. Observed
    2026-07-31 with two builders from a different session.
    """
    # Lazy imports — only needed at build time. F13 (<kb-entry>
    # ^silent-sqlite-vec): a node missing either module used to abort the ENTIRE
    # build with exit 1, so sections_fts/sections_meta never got written either —
    # the lexical leg degraded to "no index at all" rather than "vectors missing".
    # One clear log line, then continue with vec_deps=None: the schema, FTS tables,
    # and embeddings are all made conditional on it below.
    vec_deps: tuple | None = None
    if force_fts_only:
        # gestalt D1: role-gated build path (GESTALT_INDEX_BUILD_ROLE=hub) forces this on
        # non-hub nodes so the embedding model never even attempts to load there — deliberate,
        # not a fallback from a missing dependency.
        print("--fts-only: vector search disabled by request, skipping embedding model.")
    else:
        try:
            import sqlite_vec
            from sentence_transformers import SentenceTransformer

            vec_deps = (sqlite_vec, SentenceTransformer)
        except ImportError as e:
            print(
                f"Missing dependency: {e.name or e} — install: pip install sqlite-vec "
                "sentence-transformers — continuing FTS-only (vector search disabled)."
            )

    SEARCH_DIR.mkdir(exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        lock_exclusive(lock_file, blocking=False)
    except BlockingIOError:
        print(f"Another index build holds {LOCK_PATH}; waiting for it to finish...")
        lock_exclusive(lock_file)
        # The builder we waited on just wrote a complete index from the same
        # sources. Rebuilding on top of it would only repeat the work.
        rebuild_needed, _ = needs_rebuild(expect_vectors=not force_fts_only)
        if not rebuild_needed:
            print("Index was rebuilt by the other builder; nothing to do.")
            unlock(lock_file)
            lock_file.close()
            return
        print("Other builder finished but the index is still stale; rebuilding.")
    try:
        if vec_deps is not None:
            _build_index_locked(*vec_deps)
        else:
            _build_index_locked(None, None)
    finally:
        unlock(lock_file)
        lock_file.close()


def index_skills(db, model=None) -> int:
    """Index every SKILL.md body for request routing: FTS5 always, dense optional.

    Routing on a skill's name and one-line description alone measurably degrades as
    a catalog grows — SkillRouter (arxiv 2603.22455) reports routing F1 dropping
    16-23 points between a 10-skill and a 110-skill catalog, and finds the skill
    BODY to be the decisive signal: removing it costs 29-44 points. This repo has
    31 skills, so it is already inside that regime, and the body text is sitting
    right there on disk unused.

    The FTS5 leg stays cheap by construction: milliseconds per build, a few hundred
    KB, no model at query time. When `model` is passed (it is already loaded during
    a full index build anyway), each skill additionally gets ONE dense vector over
    "name — description + truncated body" written to skills_vec. That vector is
    only ever consulted by gestalt_route's opt-in semantic leg — the default
    routing path remains lexical and torch-free, because routing is also called
    from per-prompt hooks where a model load is fatal (see test_routing.py::
    test_route_does_not_import_torch and the caba378 hook post-mortem).
    """
    skills_dir = GESTALT_DIR / ".claude" / "skills"
    if not skills_dir.is_dir():
        return 0

    n = 0
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        name = path.parent.name
        text = path.read_text(encoding="utf-8", errors="replace")

        # Pull `description:` out of the frontmatter, if present. It is the single
        # highest-signal line, so it gets its own indexed column and can be weighted
        # separately from the body at query time.
        description = ""
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                for line in text[3:end].splitlines():
                    if line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip("\"'")
                        break
        body = text[text.find("\n---", 3) + 4:] if text.startswith("---") else text

        db.execute(
            "INSERT INTO skills_meta (id, name, description, lines) VALUES (?, ?, ?, ?)",
            (n, name, description, len(text.splitlines())),
        )
        db.execute(
            "INSERT INTO skills_fts(rowid, name, description, body) VALUES (?, ?, ?, ?)",
            # rowid pinned to skills_meta.id for the same reason sections_fts pins
            # to sections_meta.id: FTS5 would otherwise start its own rowid space at
            # 1 while enumerate() starts at 0, and every hit would resolve to its
            # neighbour. That exact off-by-one already shipped once here.
            (n, name.replace("-", " "), description, body),
        )
        if model is not None:
            # One vector per skill over name + curated description ONLY.
            # Measured 2026-08-11 on the 40-case set: embedding the full body
            # scored 26/40 top-1 hybrid vs 28/40 lexical on the consolidated
            # 21-skill catalog — a 276-line multi-mode SKILL.md dilutes into a
            # vector that matches nothing well. Description-only keeps the
            # vector focused on what the skill is FOR. Asymmetric-retrieval
            # prefix must match gestalt_route's query-side "search_query: ".
            doc = f"search_document: {name.replace('-', ' ')} — {description}"
            emb = model.encode(doc)
            db.execute(
                "INSERT INTO skills_vec (id, embedding) VALUES (?, ?)",
                (n, emb.tobytes()),
            )
        n += 1

    print(f"Indexed {n} skills for routing" + (" (with dense vectors)" if model else ""))
    return n


def load_embedding_cache(sqlite_vec) -> dict[str, bytes]:
    """Map content_hash -> embedding bytes from the CURRENT live index.

    Lets a rebuild reuse vectors for chunks whose embedded text did not change,
    which is what makes an edit to one entry cost seconds instead of the ~10.5 min
    a full re-encode of every chunk takes. Returns {} whenever the cache cannot be
    trusted — no index yet, or an index predating the content_hash column — so the
    caller transparently falls back to embedding everything.

    Reads the live DB, never the scratch build file, and opens it read-only so a
    concurrent reader is unaffected. sections_vec is a vec0 virtual table, so the
    sqlite-vec extension must be loaded before it can be selected from — hence the
    parameter rather than a bare sqlite3.connect.
    """
    if not DB_PATH.exists():
        return {}
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        db.enable_load_extension(True)
        sqlite_vec.load(db)
    except (sqlite3.Error, AttributeError):
        return {}
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(sections_meta)")}
        if "content_hash" not in cols:
            # Index built before incremental embedding existed. Every vector is
            # still valid, but without hashes there is no way to match them to
            # chunks, so this one build re-encodes and writes hashes for next time.
            return {}
        rows = db.execute(
            "SELECT m.content_hash, v.embedding FROM sections_meta m "
            "JOIN sections_vec v ON v.id = m.id "
            "WHERE m.content_hash IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        db.close()
    return {h: emb for h, emb in rows if h and emb}


def _wait_for_gpu_quiet(n_chunks: int) -> None:
    """GPU mutex against ingestion (2026-08-29 lesson, <kb-entry>):
    a sentence-transformers embed build sharing the card with qwen3 generation
    wedged Ollama twice in one night — the wedge cycles also correlated with graph
    damage. Before a large embed on a GPU node, wait for the card to go quiet
    (util < 30% and >= 3GB free, two consecutive polls 10s apart), up to
    GESTALT_GPU_WAIT_S (default 900). On timeout: proceed with a loud warning
    rather than fail — a busy-forever GPU should not permanently block index
    builds, it should make noise. GESTALT_GPU_FORCE=1 skips the wait entirely.
    """
    import os, shutil, subprocess, time

    if os.environ.get("GESTALT_GPU_FORCE") == "1" or n_chunks < 500:
        return
    if not shutil.which("nvidia-smi"):
        return
    deadline = time.time() + int(os.environ.get("GESTALT_GPU_WAIT_S", "900"))
    quiet_streak = 0
    warned = False
    while time.time() < deadline:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip().splitlines()[0]
            util, free = (int(x.strip()) for x in out.split(","))
        except Exception:
            return  # can't read the GPU — don't block on a broken probe
        if util < 30 and free >= 3000:
            quiet_streak += 1
            if quiet_streak >= 2:
                if warned:
                    print("GPU quiet — proceeding with embed build.")
                return
        else:
            quiet_streak = 0
            if not warned:
                print(f"GPU busy (util {util}%, {free}MiB free) — waiting for it to go quiet "
                      f"before embedding (ingestion shares this card; GESTALT_GPU_FORCE=1 overrides)...")
                warned = True
        time.sleep(10)
    print("WARNING: GPU never went quiet within GESTALT_GPU_WAIT_S — embedding anyway; "
          "watch for Ollama wedging (<kb-entry>).")


def _warn_if_gpu_wasted(n_chunks: int) -> None:
    """Detect the 2026-08-18 incident class: an NVIDIA GPU is present but torch
    cannot see it (usually a torch cuXXX wheel newer than the installed driver
    silently resolving and falling back to CPU), so a full embed run pins every
    CPU thread for tens of minutes on a corpus this GPU would clear in seconds.
    Refuses on a corpus large enough to matter unless GESTALT_ALLOW_CPU_EMBED=1
    is set, or torch/nvidia-smi are unavailable (nothing to compare against).
    """
    import os, shutil, subprocess

    if os.environ.get("GESTALT_ALLOW_CPU_EMBED") == "1":
        return
    if n_chunks < 500:
        return  # small corpus: CPU finishes in seconds regardless, not worth the friction
    if not shutil.which("nvidia-smi"):
        return  # no NVIDIA GPU on this node — CPU is the only option, nothing wasted
    try:
        import torch

        if torch.cuda.is_available():
            return
        driver = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return  # torch itself unimportable, or nvidia-smi failed — can't compare, don't block
    print(
        f"ERROR: nvidia-smi reports a GPU (driver {driver}) but torch.cuda.is_available() "
        f"is False — {torch.__version__} was built for a newer CUDA than this driver "
        "supports, so it silently fell back to CPU. Embedding "
        f"{n_chunks} chunks on CPU pins every core for many minutes (verified: 22 threads, "
        "~1050% CPU, 26+ min on this machine's 42-chunk corpus, 2026-08-18). Fix: reinstall "
        "torch against the index matching this driver, e.g. "
        "`uv pip install --index-url https://download.pytorch.org/whl/cu128 --force-reinstall torch` "
        "(cu128 for driver 570-579; check `nvidia-smi`'s 'CUDA Version' header for others). "
        "To proceed on CPU anyway, set GESTALT_ALLOW_CPU_EMBED=1.",
        file=sys.stderr,
    )
    sys.exit(1)


def write_index_meta(db, vec_available: bool) -> None:
    """Record which model made the vectors, so the server can refuse a mismatched index (X14, 2026-10-06).

    An FTS-only build has no vectors, so it stores no model identity and the server treats it as an
    FTS-only index anyway."""
    db.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if not vec_available:
        return
    rows = {
        "model_name": MODEL_NAME,
        "model_revision": MODEL_REVISION or "",
        "embed_dim": str(_ec.EMBED_DIM),
        "doc_prefix": DOC_PREFIX,
        "query_prefix": QUERY_PREFIX,
        "built_at": str(int(time.time())),
    }
    db.executemany("INSERT INTO index_meta(key, value) VALUES (?, ?)", rows.items())


def _build_index_locked(sqlite_vec, SentenceTransformer):
    """Body of the rebuild. Caller must hold the build lock.

    F13: `sqlite_vec`/`SentenceTransformer` are `None` when those deps are
    missing (the caller already logged the one clear line). FTS5 ships inside
    sqlite3 itself, so the lexical leg needs neither — every vec0-table,
    embedding, and vector-insert step below is gated on `vec_available`.
    """
    vec_available = sqlite_vec is not None and SentenceTransformer is not None

    if vec_available:
        # Verify sqlite3 supports extension loading — only needed to load the
        # vec0 extension; FTS5-only builds never call enable_load_extension.
        test_db = sqlite3.connect(":memory:")
        if not hasattr(test_db, "enable_load_extension"):
            print("ERROR: Python sqlite3 lacks extension loading. Use pyenv or conda.")
            sys.exit(1)
        test_db.close()

    SEARCH_DIR.mkdir(exist_ok=True)

    # Build into a scratch file and swap it in atomically at the end, rather
    # than unlinking the live DB up front. Rebuilding in place left search
    # returning nothing for the entire ~15 minute build, and the session-start
    # hook fires a --force rebuild whenever knowledge/*.md changed, so the
    # window reliably landed exactly when someone opened a session and searched.
    # os.replace() is atomic within a filesystem, so readers keep querying the
    # previous index until the new one is complete and never observe a partial
    # one. A leftover scratch file from a killed build is discarded here.
    if BUILD_PATH.exists():
        BUILD_PATH.unlink()

    # Parse all entries: knowledge/*.md + .claude/rules/*.md (F6 ^rules-unindexed)
    entries = all_source_entries()
    if not entries:
        print("No knowledge entries found. Nothing to index.")
        return

    all_chunks = []
    sens_by_path: dict[str, str] = {}
    for entry in entries:
        all_chunks.extend(parse_entry(entry))
        sens_by_path[str(entry.relative_to(GESTALT_DIR))] = entry_sensitivity(entry)

    print(f"Parsed {len(entries)} entries into {len(all_chunks)} chunks")

    # Load embedding model (FTS-only build skips this entirely)
    model = None
    if vec_available:
        _warn_if_gpu_wasted(len(all_chunks))
        _wait_for_gpu_quiet(len(all_chunks))
        print(f"Loading model: {MODEL_NAME}...")
        model = SentenceTransformer(MODEL_NAME, revision=MODEL_REVISION, trust_remote_code=True)

    # Connect and create schema
    db = sqlite3.connect(str(BUILD_PATH))
    if vec_available:
        db.enable_load_extension(True)
        sqlite_vec.load(db)

    db.executescript(
        """
        CREATE VIRTUAL TABLE sections_fts USING fts5(
            slug, heading, block_id, content,
            tokenize='porter unicode61'
        );
        CREATE TABLE sections_meta (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            heading TEXT NOT NULL,
            block_id TEXT,
            content TEXT NOT NULL,
            file_path TEXT NOT NULL,
            content_hash TEXT,
            anchors TEXT,
            sensitivity TEXT NOT NULL DEFAULT 'unpublished'
        );
        CREATE INDEX idx_sections_meta_hash ON sections_meta(content_hash);
        -- Skill routing corpus. Separate table from sections_fts so skill text can
        -- never surface as a knowledge result, and vice versa. The FTS leg is the
        -- default (routing runs per-request and must cost milliseconds); skills_vec
        -- backs gestalt_route's OPT-IN semantic leg only.
        CREATE VIRTUAL TABLE skills_fts USING fts5(
            name, description, body,
            tokenize='porter unicode61'
        );
        CREATE TABLE skills_meta (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            lines INTEGER NOT NULL
        );
    """
    )
    # sections_vec/skills_vec are vec0 virtual tables — creating one requires the
    # extension to be loaded, so they only exist when vec_available. Every reader
    # that needs them (get_db(), gestalt_search) already goes through get_sqlite_vec()
    # and fails informatively; gestalt_search_fts()/the CLI never reference them.
    if vec_available:
        db.executescript(
            """
            CREATE VIRTUAL TABLE sections_vec USING vec0(
                id INTEGER PRIMARY KEY,
                embedding FLOAT[768]
            );
            CREATE VIRTUAL TABLE skills_vec USING vec0(
                id INTEGER PRIMARY KEY,
                embedding FLOAT[768]
            );
        """
        )
    index_skills(db, model=model)

    # Generate embeddings
    # Pre-truncate before encoding. The model truncates at max_seq_length anyway,
    # so cutting here is lossless — but without it the tokenizer materializes the
    # full sequence first, and a few very large blocks (research-project has had two at
    # ~140k chars / ~35k tokens) are enough to OOM-kill the process on batch 0,
    # because sentence-transformers sorts longest-first. Was a silent failure:
    # the build died with SIGKILL leaving a schema-only DB, and needs_rebuild()
    # compares mtimes, so --check then reported "up to date" over an empty index.
    # hashes are always computed (sections_meta.content_hash is used by incremental
    # re-parsing regardless of whether vectors exist); embeddings stay all-None in
    # an FTS-only build.
    if vec_available:
        max_chars = model.max_seq_length * CHARS_PER_TOKEN
    else:
        max_chars = None
    # nomic-embed-text-v1.5 is an asymmetric retrieval model: its model card
    # requires a task-instruction prefix, "search_document: " when embedding
    # corpus text and "search_query: " when embedding a query. Without them both
    # sides land in the wrong region of the space and recall degrades.
    # Query-side counterpart lives in gestalt-mcp-server.py / the retrieval eval.
    # Deterministic contextual prefixing: anchor each chunk with its document
    # and section so a mid-document fragment keeps topical identity. This is the
    # cheap variant of Anthropic's contextual retrieval (which generates the
    # context with an LLM per chunk); no API calls, same intent.
    texts = [
        DOC_PREFIX
        + f"{c['slug'].replace('-', ' ')} — {c['heading']}\n\n"
        + (c["content"][:max_chars] if max_chars else c["content"])
        for c in all_chunks
    ]
    truncated = sum(1 for c in all_chunks if max_chars and len(c["content"]) > max_chars)

    hashes = [
        hashlib.sha256(f"{MODEL_NAME}\x00{t}".encode("utf-8")).hexdigest() for t in texts
    ]

    embeddings: list = [None] * len(texts)
    if vec_available:
        # Reuse embeddings for chunks whose text is byte-identical to the previous
        # build. Embedding is ~98% of build cost (measured: import + encode dominate;
        # the SQLite writes are under 10 ms total), and a typical edit touches one
        # entry, so a full re-encode of every chunk was paying ~10.5 min to recompute
        # vectors that cannot have changed. The hash covers the FULL prefixed text
        # plus the model name, so changing DOC_PREFIX, the contextual prefix format,
        # or MODEL_NAME correctly invalidates every cached vector instead of silently
        # mixing embedding spaces.
        cache = load_embedding_cache(sqlite_vec)
        todo = [i for i, h in enumerate(hashes) if h not in cache]

        print(
            f"Generating embeddings: {len(todo)} new, {len(hashes) - len(todo)} reused "
            f"from cache ({truncated} truncated to {max_chars} chars)..."
        )

        for i, h in enumerate(hashes):
            if h in cache:
                embeddings[i] = cache[h]
        if todo:
            fresh = model.encode(
                [texts[i] for i in todo],
                batch_size=EMBED_BATCH_SIZE,
                show_progress_bar=True,
            )
            for slot, emb in zip(todo, fresh):
                embeddings[slot] = emb.tobytes()

        assert all(e is not None for e in embeddings), "an embedding slot was left unfilled"
    else:
        print(f"Skipping embeddings for {len(all_chunks)} chunks (FTS-only build).")

    # Insert chunks
    for i, (chunk, emb) in enumerate(zip(all_chunks, embeddings)):
        # Every ^anchor inside this chunk, not just the one that named it.
        # Entries mark sub-facts with a trailing inline `^anchor` mid-section, and the
        # chunker only derives block_id from headings and standalone anchor lines — so
        # 12 of 48 anchored eval targets had no chunk whose block_id matched, and were
        # scored as misses despite living inside a chunk that retrieval DID return.
        # Recording them makes "the answer is in this chunk" checkable instead of
        # requiring a LIKE scan over content.
        anchors = ",".join(sorted(set(ANCHOR_RE.findall(chunk["content"]))))
        db.execute(
            "INSERT INTO sections_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                i,
                chunk["slug"],
                chunk["heading"],
                chunk["block_id"],
                chunk["content"],
                chunk["file_path"],
                hashes[i],
                anchors,
                sens_by_path.get(chunk["file_path"], DEFAULT_SENSITIVITY),
            ),
        )
        # Pin the FTS rowid to sections_meta.id. Without an explicit rowid, FTS5
        # auto-assigns from 1 while sections_meta.id comes from enumerate() and
        # starts at 0, so the two id spaces sit one apart. The search tool fuses
        # fts.rowid with vec.id in a single score dict and then resolves against
        # sections_meta.id, so every BM25 hit silently returned its neighbouring
        # chunk. Verified 2026-07-31: MATCH 'normalize_advantage' hit rowid 594
        # ("Build complete") and the tool displayed id 594 ("Build discipline").
        db.execute(
            "INSERT INTO sections_fts(rowid, slug, heading, block_id, content) VALUES (?, ?, ?, ?, ?)",
            (i, chunk["slug"], chunk["heading"], chunk["block_id"], chunk["content"]),
        )
        # emb is already raw bytes here: cache hits come back as stored BLOBs and
        # freshly-encoded vectors were converted with .tobytes() above, so the two
        # paths meet in the same representation. sections_vec does not exist at
        # all in an FTS-only build (F13) — skip the insert, not just the value.
        if vec_available:
            db.execute(
                "INSERT INTO sections_vec (id, embedding) VALUES (?, ?)",
                (i, emb),
            )

    write_index_meta(db, vec_available)
    db.commit()
    db.close()
    # Swap the finished index in. Readers querying DB_PATH up to this instant
    # saw the previous complete index; after it they see the new one. There is
    # no moment at which the path holds a partial database.
    size_kb = BUILD_PATH.stat().st_size // 1024
    os.replace(BUILD_PATH, DB_PATH)
    mode = "" if vec_available else " (FTS-only, vector search disabled)"
    print(f"Index built: {DB_PATH} ({size_kb}KB, {len(all_chunks)} sections){mode}")


def report_graphiti_entries(db_mtime_before: float | None):
    """Print entries that were modified before this build (need Graphiti feeding).

    db_mtime_before is the DB mtime captured BEFORE the rebuild. Any knowledge
    entry newer than that timestamp is considered new or modified.
    """
    modified = get_modified_entries(db_mtime_before)
    if not modified:
        print("No entries modified since last Graphiti feed — nothing to do.")
        return

    slugs = [p.stem for p in modified]
    print()
    print("The following entries were modified and should be fed to Graphiti:")
    for slug in slugs:
        print(f"  - {slug}")
    print()
    print(
        "Run /learn on these repos to feed them: "
        + ", ".join(slugs)
    )


def _fleet_env(key: str) -> str:
    """One variable from the environment, else from ~/.fleet/fleet.env (hooks source it; a manual run does not)."""
    v = os.environ.get(key)
    if v is not None:
        return v
    try:
        for line in Path("~/.fleet/fleet.env").expanduser().read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _role_forces_fts_only() -> bool:
    """gestalt D1: with GESTALT_INDEX_BUILD_ROLE=hub only the hub node embeds; every other node builds FTS-only."""
    if _fleet_env("GESTALT_INDEX_BUILD_ROLE").strip().lower() != "hub":
        return False
    import socket
    hub = (_fleet_env("FLEET_HUB_NAME") or _fleet_env("FLEET_HUB").split(".")[0] or "hub").lower()
    return socket.gethostname().split(".")[0].lower() != hub


def _reexec_in_venv_if_needed(argv: list[str]) -> None:
    """A plain `python3` build silently drops the vector leg (sqlite-vec lives only in the venv), which is how
    the hub published FTS-only artifacts for two weeks. On a node allowed to embed, re-run under the venv."""
    if os.environ.get("GESTALT_NO_VENV_REEXEC") or {"--fts-only", "--stamp-only", "--check"} & set(argv):
        return
    venv_py = Path("~/.claude/gestalt/venv/bin/python3").expanduser()
    if not venv_py.exists():
        return
    # Detect "already in the venv" with sys.prefix, NOT by resolving the interpreter path.
    # A venv's bin/python3 is a symlink chain to the base interpreter, so venv_py.resolve()
    # yields /usr/bin/python3.N, which equals sys.executable resolved for ANY python3. The
    # old comparison was therefore always true, the re-exec never happened, and a hook that
    # called plain python3 silently built an FTS-only index while believing it was in the
    # venv. That is how the hub shipped a vectorless index on 2026-09-04 (and very likely
    # how it did so for two weeks before the guard was written). sys.prefix is the venv root
    # inside a venv and the system prefix outside it, which is the actual question here.
    if Path(sys.prefix).resolve() == venv_py.parent.parent.resolve():
        return
    import importlib.util
    if importlib.util.find_spec("sqlite_vec") is not None:
        return
    print(f"Re-running under {venv_py} so the build carries the vector leg (GESTALT_NO_VENV_REEXEC=1 skips).")
    sys.stdout.flush()
    os.execve(str(venv_py), [str(venv_py), sys.argv[0], *argv], {**os.environ, "GESTALT_NO_VENV_REEXEC": "1"})


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--fts-only" not in args and _role_forces_fts_only():
        print("GESTALT_INDEX_BUILD_ROLE=hub on a non-hub node: FTS-only build (only the hub embeds; fleet-sync index-fetch brings its vectors).")
        args.add("--fts-only")
    _reexec_in_venv_if_needed(sorted(args))
    force = "--force" in args
    check = "--check" in args
    feed_graphiti = "--feed-graphiti" in args
    fts_only = "--fts-only" in args
    stamp_only = "--stamp-only" in args

    if stamp_only:
        # Stamp the current DB for the current HEAD without touching it — used by fleet-sync
        # index-publish/tests to mark an already-built db.gestalt as belonging to this commit,
        # and to avoid a --force embedding rebuild just to produce a stamp (gestalt D1).
        if not DB_PATH.exists():
            print(f"--stamp-only: no index at {DB_PATH} to stamp.")
            sys.exit(1)
        sha = write_stamp()
        if sha:
            print(f"Stamped {STAMP_PATH} for HEAD {sha[:8]} (no rebuild).")
            sys.exit(0)
        print("--stamp-only: could not determine git HEAD (not a git repo here?).")
        sys.exit(1)

    rebuild_needed, db_mtime = needs_rebuild(expect_vectors=not fts_only)

    if check:
        if not DB_PATH.exists():
            print(f"No index at {DB_PATH} — rebuild required.")
        elif rebuild_needed:
            print(f"Index exists at {DB_PATH} but knowledge/rules files have changed — rebuild required.")
        else:
            print(f"Index is up to date: {DB_PATH}")
        sys.exit(0)

    if not rebuild_needed and not force:
        print("Index is up to date — skipping rebuild. Use --force to override.")
        restamped = restamp_if_only_head_moved()
        if restamped:
            print(f"Stamp refreshed for HEAD {restamped[:8]} (no indexed input changed).")
        if feed_graphiti:
            report_graphiti_entries(db_mtime)
        sys.exit(0)

    if force and not rebuild_needed:
        print("Index is up to date but --force specified — rebuilding anyway.")

    build_index(force_fts_only=fts_only)
    stamped = write_stamp()
    if stamped:
        print(f"Stamped {STAMP_PATH} for HEAD {stamped[:8]}.")
    else:
        print(f"Could not stamp {STAMP_PATH} — not a git repo? (harmless, index still built)")

    if feed_graphiti:
        report_graphiti_entries(db_mtime)
