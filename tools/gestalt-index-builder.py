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

Environment knobs (all optional, all read through tools/gestalt_embed_config.py unless noted):
    GESTALT_EMBED_PROFILE=nomic|qwen3-4b   which embedding model to build with
    GESTALT_EMBED_DIM=768|512|256|128|64   Matryoshka width for the nomic profile
    GESTALT_EMBED_DEVICE=cuda              device for the model. Unset means auto: sentence-transformers picks the GPU when there is one.
                                           A set value is used as given and also skips the check for a GPU that torch cannot see
    GESTALT_SEARCH_DIR=/path               build into this directory instead of <repo>/.search
    GESTALT_LATE_CHUNKING=1                embed whole documents and pool token vectors per chunk (this file only)

Every successful build writes .search/gestalt.db.stamp = "<repo HEAD sha> <builder schema
hash> <epoch>" next to the db, consumed by `fleet-sync index-publish`/`index-fetch`
(gestalt/knowledge/<kb-entry>.md ^fleet-sync-decision) and index_stamp_matches().
"""

import hashlib
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling import works under any cwd or loader
try:
    import gestalt_freshness as _fresh
except ImportError:  # a lone copy of this file: the freshness columns are created and left empty
    _fresh = None
try:
    import gestalt_embed_config as _ec
except ImportError:  # a lone copy of this file (a test fixture, a stale install): behave as before X14, unpinned
    class _ec:
        PROFILE = "nomic"
        MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
        MODEL_REVISION = None
        EMBED_DIM = FULL_DIM = 768
        DOC_PREFIX = "search_document: "
        QUERY_PREFIX = "search_query: "
        TRUST_REMOTE_CODE = True
        MODEL_KWARGS: dict = {}
        TOKENIZER_KWARGS: dict = {}
        TEXT_FORMAT = "v2-title"
        SEARCH_DIR = None

        @staticmethod
        def postprocess(vec):
            return vec

        @staticmethod
        def chunk_text(slug, title, heading, content, prefix=None):
            head = slug.replace("-", " ")
            head = f"{title} ({head})" if title else head
            return (_ec.DOC_PREFIX if prefix is None else prefix) + f"{head} — {heading}\n\n{content}"

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
# GESTALT_SEARCH_DIR builds a side index (for an A/B run) without touching the live one.
SEARCH_DIR = _ec.SEARCH_DIR or GESTALT_DIR / ".search"
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


def _late_chunking_on() -> bool:
    return os.environ.get("GESTALT_LATE_CHUNKING", "0").strip().lower() in ("1", "true", "on", "yes")


def _index_meta_stale(expect_vectors: bool) -> bool:
    """True when the stored index_meta says this index was built with another text format, profile, width or pooling.

    A change to the embed text layout or the model changes every vector, and no file mtime moves when it happens.
    An index whose sections_meta or sections_fts lacks the title column is stale, even with an empty index_meta.
    An index with no index_meta table, or an empty one, carries no other claim, so it is left to the mtime rules.
    The vector keys are compared only when vectors are expected, because an FTS-only build stores them too."""
    try:
        db = sqlite3.connect(str(DB_PATH))
        try:
            for table in ("sections_meta", "sections_fts"):
                cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
                if cols and "title" not in cols:
                    return True
            meta = dict(db.execute("SELECT key, value FROM index_meta").fetchall())
        finally:
            db.close()
    except sqlite3.Error:
        return False
    if not meta:
        return False
    if meta.get("text_format") != _ec.TEXT_FORMAT:
        return True
    if not expect_vectors:
        return False
    if meta.get("embed_profile", _ec.PROFILE) != _ec.PROFILE or meta.get("embed_dim", str(_ec.EMBED_DIM)) != str(_ec.EMBED_DIM):
        return True
    pooling = "late" if _late_chunking_on() else "standard"
    if "pooling" in meta and meta["pooling"] != pooling:
        return True
    return pooling == "late" and meta.get("pooling") == "late" and meta.get("normalized") != "1"


def needs_rebuild(expect_vectors: bool = False) -> tuple[bool, float | None]:
    """Check whether the index needs rebuilding.

    Returns (rebuild_needed, db_mtime_or_None).
    rebuild_needed is True when:
      - The DB does not exist, or
      - The DB exists but holds no rows (a previous build died partway), or
      - Any knowledge/*.md file is newer than the DB, or
      - expect_vectors and the DB carries no sections_vec table, or
      - index_meta records another text_format, embed profile, width or pooling than this run would build.

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
    if _index_meta_stale(expect_vectors):
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
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
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


# --- supersession and freshness (gestalt_freshness.py holds the ranking side) ------------------------------
GIT_DATE_BUDGET_S = 20.0  # total seconds for all `git log` date lookups in one build, then the rest fall back to mtime
GIT_DATE_CALL_TIMEOUT_S = 3


def _git_commit_date(path: Path) -> str | None:
    """The last-commit date of a file (`git log -1 --format=%cI`), or None when git has no answer."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", str(path)],
            cwd=str(GESTALT_DIR), capture_output=True, text=True, timeout=GIT_DATE_CALL_TIMEOUT_S,
        )
    except Exception:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def entry_freshness(entries) -> dict[str, dict]:
    """{relative path: freshness fields} for each entry, ready for the sections_meta columns.

    note_modified is frontmatter `modified` (else `updated`), else the git last-commit date while the time budget lasts, else the file mtime.
    A note that lists `supersedes: [x]` marks every entry with slug x as superseded_by it, unless x names its own superseded_by.
    """
    if _fresh is None:
        return {}
    out: dict[str, dict] = {}
    parsed: dict[str, dict] = {}
    spent = 0.0
    for path in entries:
        rel = str(path.relative_to(GESTALT_DIR))
        try:
            front = _fresh.parse_front(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            front = _fresh.parse_front("")
        parsed[rel] = front
        mod, src = front["modified"], "frontmatter"
        if not mod:
            if spent < GIT_DATE_BUDGET_S:
                t0 = time.monotonic()
                mod = _git_commit_date(path)
                spent += time.monotonic() - t0
                src = "git"
            if not mod:
                try:
                    mod = time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
                except OSError:
                    mod = None
                src = "mtime"
        out[rel] = {
            "superseded_by": front["superseded_by"],
            "valid_until": front["valid_until"],
            "note_modified": mod,
            "note_modified_source": src if mod else None,
            "supersedes": ",".join(front["supersedes"]) or None,
        }
    by_slug: dict[str, list[str]] = {}
    for rel in out:
        by_slug.setdefault(Path(rel).stem, []).append(rel)
    for rel in sorted(out):
        new_slug = Path(rel).stem
        for old in parsed[rel]["supersedes"]:
            for old_rel in by_slug.get(old, []):
                if old != new_slug and not parsed[old_rel]["superseded_by"] and not out[old_rel]["superseded_by"]:
                    out[old_rel]["superseded_by"] = new_slug
    return out


def _freshness_values(f: dict | None) -> tuple:
    f = f or {}
    return tuple(f.get(k) for k in ("superseded_by", "valid_until", "note_modified", "note_modified_source", "supersedes"))


def upgrade_freshness(db_path: Path | None = None) -> list[str]:
    """Upgrade an existing index in place: add the freshness columns and fill them from the sources.

    Returns the columns it added. Nothing happens to an index that already has them. The text and vectors are untouched."""
    if _fresh is None:
        return []
    db = sqlite3.connect(str(db_path or DB_PATH))
    try:
        added = _fresh.ensure_columns(db)
        if added:
            by_path = entry_freshness(all_source_entries())
            for rel, f in by_path.items():
                db.execute(
                    "UPDATE sections_meta SET superseded_by=?, valid_until=?, note_modified=?, note_modified_source=?, supersedes=? WHERE file_path=?",
                    (*_freshness_values(f), rel),
                )
            db.commit()
        return added
    finally:
        db.close()


def _frontmatter_title(front: str) -> str:
    """The flat `title:` value from a frontmatter block, or empty. Only a column-0 line counts, so a nested `title:` is ignored. One matching pair of surrounding quotes is removed and no other quote. A folded or block scalar counts as empty."""
    for line in front.splitlines():
        if line.startswith("title:"):
            val = line[len("title:"):].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            return "" if val in (">", "|", ">-", "|-") else val
    return ""


def _first_h1(text: str) -> str:
    """The first `# ` heading outside a code fence, or empty."""
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("# "):
            return line[2:].strip()
    return ""


def parse_entry(path: Path) -> list[dict]:
    """Parse a knowledge entry into section chunks.

    Every chunk carries the entry's `title`. It comes from the frontmatter, else from the first H1 of a knowledge
    entry. A rules file with no frontmatter title keeps an empty title, so its embed text stays as it was."""
    # encoding is explicit: Python defaults to the locale codec, which is
    # cp1252 on Windows and dies on the em-dashes these entries are full of.
    text = path.read_text(encoding="utf-8")
    title = ""
    # Strip YAML frontmatter
    if text.startswith("---"):
        try:
            end = text.index("---", 3)
            title = _frontmatter_title(text[3:end])
            text = text[end + 3 :].strip()
        except ValueError:
            pass
    if not title and path.parent.name != "rules":
        title = _first_h1(text)

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
                            "title": title,
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
                    "title": title,
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

    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
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
            doc = f"{DOC_PREFIX}{name.replace('-', ' ')} — {description}"
            emb = _ec.postprocess(model.encode(doc))
            if _late_chunking_on():
                emb = _l2_rows(emb)
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


def chunk_hash(t: str, salt: str = "") -> str:
    """The embedding cache key and the stored content_hash of one chunk.

    With no salt it is sha256(model name, NUL, text), the key this builder has always used. A salt is added only
    when the vector depends on something the text does not carry: a Matryoshka width or late-chunking pooling
    plus the sha of the whole document. Default builds therefore keep every cached vector."""
    if salt:
        return hashlib.sha256(f"{MODEL_NAME}\x00{salt}\x00{t}".encode("utf-8")).hexdigest()  # noqa: UP012  the explicit encoding is part of the cache-key contract a test pins
    return hashlib.sha256(f"{MODEL_NAME}\x00{t}".encode("utf-8")).hexdigest()  # noqa: UP012


def cache_salt(late: bool, doc_sha: str | None, fallback: bool = False) -> str:
    """The salt for chunk_hash. Empty for a plain full-width build.

    A chunk that a late build had to embed the ordinary way gets the `late-fallback` salt. Its vector is not pooled, so it must never sit under the late key, and it is normalised, so it must never sit under the plain key."""
    parts = []
    if _ec.EMBED_DIM != getattr(_ec, "FULL_DIM", _ec.EMBED_DIM):
        parts.append(f"dim={_ec.EMBED_DIM}")
    if late and fallback:
        parts.append("pooling=late-fallback")
    elif late:
        parts.append("pooling=late")
        parts.append(f"doc={doc_sha}")
    return ",".join(parts)


def model_init_kwargs() -> dict:
    """Keyword arguments for SentenceTransformer(MODEL_NAME, ...), taken from the active embed profile.

    The device is passed only when GESTALT_EMBED_DEVICE is set. Unset, sentence-transformers picks the GPU
    when there is one, which is how the hub has always built."""
    kw = {"revision": MODEL_REVISION, "trust_remote_code": _ec.TRUST_REMOTE_CODE}
    device = os.environ.get("GESTALT_EMBED_DEVICE", "").strip()
    if device:
        kw["device"] = device
    if _ec.MODEL_KWARGS:
        kw["model_kwargs"] = dict(_ec.MODEL_KWARGS)
    if _ec.TOKENIZER_KWARGS:
        kw["tokenizer_kwargs"] = dict(_ec.TOKENIZER_KWARGS)
    return kw


def _as_numpy(x):
    """A token-embedding tensor (or array) as float32 numpy."""
    import numpy as np

    if hasattr(x, "detach"):
        x = x.detach().cpu().float().numpy()
    return np.asarray(x, dtype=np.float32)


def _l2_rows(arr):
    """L2-normalise one vector or a batch of rows, as float32. A zero row stays zero."""
    import numpy as np

    a = np.asarray(arr, dtype=np.float32)
    n = np.linalg.norm(a, axis=-1, keepdims=True)
    return np.ascontiguousarray((a / np.maximum(n, 1e-12)).astype(np.float32))


def late_windows(n_tokens: int, cap: int) -> list[tuple[int, int]]:
    """Token ranges (start, end) that cover a document. One range when it fits, else windows of `cap` tokens with 50% overlap."""
    if n_tokens <= cap:
        return [(0, n_tokens)]
    stride = max(1, cap // 2)
    wins, start = [], 0
    while True:
        end = min(start + cap, n_tokens)
        wins.append((start, end))
        if end == n_tokens:
            return wins
        start += stride


def embed_late(model, doc_text: str, spans: list[tuple[int, int]], prefix: str = "", batch_size: int | None = None):
    """Late chunking: encode the whole document once, then mean-pool the token vectors inside each chunk span.

    `spans` are character ranges of `doc_text`. A document longer than the encoder window is cut into windows with
    50% overlap. Each chunk is pooled in the window where it sits most central, meaning the one that leaves the
    most text on its shorter side. A chunk wider than any window comes back as None, and so does a chunk with no tokens. The
    caller encodes those the ordinary way. Pooled vectors are L2-normalised. Returns (vectors, n_windows).
    `prefix` is the document prefix that `doc_text` starts with. Every window after the first gets it prepended, and the window cap leaves room for its tokens.
    All windows go to the encoder in one call, in window order, with `batch_size` as its batch size. Each window's token vectors are the same as when it is encoded alone.
    Raises on a tokenizer or model that cannot give offsets or aligned token vectors, and the caller falls back for the whole document. It also raises when a window, once cut and prefixed, holds more than `max_seq_length` tokens."""
    import numpy as np

    tok = model.tokenizer
    max_len = int(model.max_seq_length)
    cap = max_len - 2
    if cap < 8:
        raise ValueError(f"max_seq_length {max_len} is too small for late chunking")
    offs = tok(doc_text, add_special_tokens=False, return_offsets_mapping=True, truncation=False)["offset_mapping"]
    n = len(offs)
    vectors: list = [None] * len(spans)
    if n == 0:
        return vectors, 0
    if n <= cap:
        wins = [(0, n)]
    else:
        n_prefix = len(tok(prefix, add_special_tokens=False, return_offsets_mapping=True, truncation=False)["offset_mapping"]) if prefix else 0
        if cap - n_prefix < 8:
            raise ValueError(f"max_seq_length {max_len} leaves no room for the document prefix")
        wins = late_windows(n, cap - n_prefix)
    # Cut and validate every window first, then encode them in one call so the encoder batches them.
    cut: list[tuple[int, int, int, str, list]] = []  # char_lo, char_hi, prefix shift, window text, token char ranges
    for ts, te in wins:
        lo = 0 if ts == 0 else offs[ts][0]
        hi = len(doc_text) if te == n else offs[te - 1][1]
        shift = len(prefix) if ts > 0 else 0
        win_text = (prefix if ts > 0 else "") + doc_text[lo:hi]
        enc = tok(win_text, add_special_tokens=True, return_offsets_mapping=True, return_special_tokens_mask=True,
                  truncation=False)
        if len(enc["offset_mapping"]) > max_len:
            raise ValueError(f"window of {len(enc['offset_mapping'])} tokens exceeds max_seq_length {max_len}")
        ranges = [None if sp else tuple(o) for o, sp in zip(enc["offset_mapping"], enc["special_tokens_mask"])]
        cut.append((lo, hi, shift, win_text, ranges))
    encode_kw = {"batch_size": batch_size} if batch_size else {}
    token_vecs = model.encode([c[3] for c in cut], output_value="token_embeddings", **encode_kw)
    if len(token_vecs) != len(cut):
        raise ValueError(f"{len(token_vecs)} token-vector blocks for {len(cut)} windows")
    pooled: list[tuple[int, int, np.ndarray, list, int]] = []  # char_lo, char_hi, token vectors, token char ranges, prefix shift
    for (lo, hi, shift, _, ranges), raw in zip(cut, token_vecs):
        vecs = _as_numpy(raw)
        if vecs.ndim != 2 or vecs.shape[0] != len(ranges):
            raise ValueError(f"token vectors {vecs.shape} do not line up with {len(ranges)} tokens")
        pooled.append((lo, hi, vecs, ranges, shift))
    for k, (cs, ce) in enumerate(spans):
        best, best_margin = None, -1.0
        for w, (lo, hi, _, _, _) in enumerate(pooled):
            if cs < lo or ce > hi:
                continue
            margin = min(cs - lo, hi - ce)
            if margin > best_margin:
                best, best_margin = w, margin
        if best is None:
            continue
        lo, _, vecs, ranges, shift = pooled[best]
        idx = [i for i, r in enumerate(ranges) if r is not None and r[0] < ce - lo + shift and r[1] > cs - lo + shift]
        if not idx:
            continue
        v = vecs[idx].mean(axis=0)
        norm = float(np.linalg.norm(v))
        if norm > 0:
            vectors[k] = (v / norm).astype(np.float32)
    return vectors, len(wins)


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
    import os
    import shutil
    import subprocess
    import time

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
    is set, GESTALT_EMBED_DEVICE names a device, or torch/nvidia-smi are unavailable
    (nothing to compare against).
    """
    import os
    import shutil
    import subprocess

    if os.environ.get("GESTALT_ALLOW_CPU_EMBED") == "1":
        return
    if os.environ.get("GESTALT_EMBED_DEVICE", "").strip():
        return  # an explicit device is a choice, so a CPU run is not a surprise
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


def write_index_meta(db, vec_available: bool, pooling: str = "standard") -> None:
    """Record which model made the vectors, so the server can refuse a mismatched index (X14, 2026-10-06).

    An FTS-only build has no vectors, so it stores no model identity. It does store text_format, embed_profile and
    embed_dim, so needs_rebuild() can tell that the text layout changed. The server checks only the keys it finds."""
    db.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    rows = {
        "text_format": _ec.TEXT_FORMAT,
        "embed_profile": _ec.PROFILE,
        "embed_dim": str(_ec.EMBED_DIM),
    }
    if vec_available:
        rows.update({
            "model_name": MODEL_NAME,
            "model_revision": MODEL_REVISION or "",
            "doc_prefix": DOC_PREFIX,
            "query_prefix": QUERY_PREFIX,
            "pooling": pooling,
            **({"normalized": "1"} if pooling == "late" else {}),
            "built_at": str(int(time.time())),
        })
    # How the model was loaded: "native" or "remote-code". Informational only. needs_rebuild() does not compare it, so an index built without it stays current. The column is NOT NULL, so a config that does not expose LOAD_PATH leaves the key out.
    load_path = getattr(_ec, "LOAD_PATH", None)
    if vec_available and load_path:
        rows["load_path"] = str(load_path)
    db.executemany("INSERT INTO index_meta(key, value) VALUES (?, ?)", rows.items())


def _embed_chunks(model, texts: list[str], hashes: list[str], docs: dict[str, dict], late: bool,
                  cache: dict[str, bytes], note: str = "") -> list[bytes]:
    """One embedding (raw float32 bytes) per chunk text. `hashes` is updated in place for chunks that fall back.

    Reuse embeddings for chunks whose text is byte-identical to the previous build. Embedding is ~98% of build
    cost (measured: import + encode dominate, the SQLite writes are under 10 ms total), and a typical edit
    touches one entry. The hash covers the FULL prefixed text plus the model name, so changing DOC_PREFIX, the
    contextual prefix format or MODEL_NAME invalidates every cached vector instead of mixing embedding spaces.
    A Matryoshka width or late pooling adds a salt (see chunk_hash).

    In a late build each document is tried late first. A chunk that comes back as an ordinary embedding gets the
    `late-fallback` hash. When the previous build already stored that hash, its vector is reused. The document is
    still tried late on every build, so a fixed tokenizer takes effect without a manual cache clear."""
    embeddings: list = [None] * len(texts)
    todo = [i for i, h in enumerate(hashes) if h not in cache]
    print(f"Generating embeddings: {len(todo)} new, {len(hashes) - len(todo)} reused from cache ({note})...")
    for i, h in enumerate(hashes):
        if h in cache:
            embeddings[i] = cache[h]
    ordinary = list(todo)

    def settle_ordinary(i: int) -> bool:
        """Switch chunk i to its late-fallback hash. True when the cache already holds that vector."""
        hashes[i] = chunk_hash(texts[i], cache_salt(True, None, fallback=True))
        if hashes[i] in cache:
            embeddings[i] = cache[hashes[i]]
            return True
        return False

    if late and todo:
        ordinary = []
        todo_set = set(todo)
        n_docs = n_windows = n_fallback = n_fallback_chunks = n_chunk_ordinary = 0
        for path, d in docs.items():
            want = [k for k, i in enumerate(d["idx"]) if i in todo_set]
            if not want:
                continue
            n_docs += 1
            try:
                vecs, nw = embed_late(model, d["text"], d["spans"], DOC_PREFIX, batch_size=EMBED_BATCH_SIZE)
            except Exception as exc:  # any tokenizer or alignment failure: this document is embedded the ordinary way
                n_fallback += 1
                n_fallback_chunks += len(want)
                print(f"late-chunking: fallback doc={path} reason={type(exc).__name__}: {exc}", file=sys.stderr)
                ordinary.extend(d["idx"][k] for k in want if not settle_ordinary(d["idx"][k]))
                continue
            n_windows += nw
            for k in want:
                i = d["idx"][k]
                if vecs[k] is None:
                    n_chunk_ordinary += 1
                    if not settle_ordinary(i):
                        ordinary.append(i)
                else:
                    embeddings[i] = _ec.postprocess(vecs[k]).astype("float32").tobytes()
        print(f"late-chunking: docs={n_docs} windows={n_windows} fallback={n_fallback} fallback_chunks={n_fallback_chunks} chunks_ordinary={n_chunk_ordinary}", file=sys.stderr)
    if ordinary:
        fresh = _encode_paced(model, [texts[i] for i in ordinary])
        if late:
            fresh = _l2_rows(fresh)
        for slot, emb in zip(ordinary, fresh):
            embeddings[slot] = emb.tobytes()
    assert all(e is not None for e in embeddings), "an embedding slot was left unfilled"
    return embeddings


def _encode_paced(model, texts: list[str]):
    """Encode `texts` in one call, or in slices of GESTALT_EMBED_CALL_DOCS (256) when a GPU duty cycle is on.

    The duty cycle (gestalt_rank.throttle_calls) sleeps after each encode call, so one call over a whole corpus would run at
    the card's ceiling for the length of the build and sleep once at the end, which is no pacing at all. Slicing puts the
    sleep between slices. At the default duty of 1.0 the single call stays, so the vectors are the ones the old path wrote."""
    import numpy as np

    try:
        import gestalt_rank
        duty = gestalt_rank.gpu_duty()
    except ImportError:
        duty = 1.0
    if duty >= 1.0 or len(texts) <= 1:
        return _ec.postprocess(model.encode(texts, batch_size=EMBED_BATCH_SIZE, show_progress_bar=True))
    step = max(EMBED_BATCH_SIZE, int(os.environ.get("GESTALT_EMBED_CALL_DOCS", "256") or 256))
    parts = [_ec.postprocess(model.encode(texts[i:i + step], batch_size=EMBED_BATCH_SIZE, show_progress_bar=False)) for i in range(0, len(texts), step)]
    return np.concatenate(parts, axis=0)


def _require_extension_loading():
    """Exit with a clear error when sqlite3 cannot load extensions.

    Only the vec0 extension needs this. FTS5-only builds never call it.
    """
    test_db = sqlite3.connect(":memory:")
    if not hasattr(test_db, "enable_load_extension"):
        print("ERROR: Python sqlite3 lacks extension loading. Use pyenv or conda.")
        sys.exit(1)
    test_db.close()


def _collect_chunks():
    """Parse every source entry into chunks.

    Returns `(all_chunks, sens_by_path)`, or `None` when there are no entries.
    """
    # Parse all entries: knowledge/*.md + .claude/rules/*.md (F6 ^rules-unindexed)
    entries = all_source_entries()
    if not entries:
        print("No knowledge entries found. Nothing to index.")
        return None

    all_chunks = []
    sens_by_path: dict[str, str] = {}
    for entry in entries:
        all_chunks.extend(parse_entry(entry))
        sens_by_path[str(entry.relative_to(GESTALT_DIR))] = entry_sensitivity(entry)

    print(f"Parsed {len(entries)} entries into {len(all_chunks)} chunks")
    return all_chunks, sens_by_path


def _load_embedding_model(SentenceTransformer, n_chunks):
    """Load the embedding model after the GPU checks."""
    _warn_if_gpu_wasted(n_chunks)
    _wait_for_gpu_quiet(n_chunks)
    device = os.environ.get("GESTALT_EMBED_DEVICE", "").strip() or "auto"
    print(f"Loading model: {MODEL_NAME} (profile={_ec.PROFILE} dim={_ec.EMBED_DIM} device={device})")
    model = SentenceTransformer(MODEL_NAME, **model_init_kwargs())
    # The GPU duty cycle (GESTALT_GPU_DUTY, gestalt_rank.throttle_calls) paces sustained embedding the same way it paces the
    # reranker. A build is minutes of draw at the card's ceiling, which is what trips the firmware clamp (2026-10-08: the
    # seventh clamp of the day hit seven minutes into the late-chunking build). A no-op at the default duty of 1.0.
    try:
        import gestalt_rank
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gestalt_rank
    gestalt_rank.throttle_calls(model, "encode")
    return model


def _create_schema(db, vec_available):
    """Create the FTS, meta and (when vectors are on) vec0 tables."""
    db.executescript(
        """
        CREATE VIRTUAL TABLE sections_fts USING fts5(
            slug, title, heading, block_id, content,
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
            sensitivity TEXT NOT NULL DEFAULT 'unpublished',
            title TEXT NOT NULL DEFAULT '',
            superseded_by TEXT,
            valid_until TEXT,
            note_modified TEXT,
            note_modified_source TEXT,
            supersedes TEXT
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
        # The script above is the 768-wide default. tests/test_harness_fidelity.py lifts it out of this file as the
        # fixture schema, so it stays a plain literal. Another profile or Matryoshka width recreates both tables here.
        if _ec.EMBED_DIM != 768:
            for table in ("sections_vec", "skills_vec"):
                db.execute(f"DROP TABLE {table}")
                db.execute(f"CREATE VIRTUAL TABLE {table} USING vec0(id INTEGER PRIMARY KEY, embedding FLOAT[{_ec.EMBED_DIM}])")


def _chunk_texts(all_chunks, model, vec_available):
    """Build the embedding input for every chunk.

    Returns `(bodies, texts, max_chars, truncated)`.
    """
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
    # The chunk layout lives in gestalt_embed_config.chunk_text (TEXT_FORMAT v2-title): a titled chunk reads
    # "<title> (<slug words>) — <heading>", an untitled one keeps the older "<slug words> — <heading>".
    bodies = [
        _ec.chunk_text(c["slug"], c.get("title", ""), c["heading"], c["content"][:max_chars] if max_chars else c["content"], prefix="")
        for c in all_chunks
    ]
    texts = [DOC_PREFIX + b for b in bodies]
    truncated = sum(1 for c in all_chunks if max_chars and len(c["content"]) > max_chars)
    return bodies, texts, max_chars, truncated


def _late_documents(all_chunks, bodies):
    """Group chunk bodies per file for late chunking.

    The document text is the chunk bodies joined by blank lines, so every chunk's span in it is known by construction.
    """
    docs: dict[str, dict] = {}
    for i, c in enumerate(all_chunks):
        d = docs.setdefault(c["file_path"], {"idx": [], "parts": [], "spans": []})
        d["idx"].append(i)
        d["parts"].append(bodies[i])
    for d in docs.values():
        doc_text, spans, pos = DOC_PREFIX + "\n\n".join(d["parts"]), [], len(DOC_PREFIX)
        for body in d["parts"]:
            spans.append((pos, pos + len(body)))
            pos += len(body) + 2
        d["text"], d["spans"] = doc_text, spans
        d["sha"] = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()[:16]
    return docs


def _embed_stage(sqlite_vec, model, all_chunks, vec_available):
    """Chunk texts, hash them and embed them.

    Returns `(embeddings, hashes, late)`. Embeddings are all `None` in an FTS-only build.
    """
    bodies, texts, max_chars, truncated = _chunk_texts(all_chunks, model, vec_available)

    # Late chunking (GESTALT_LATE_CHUNKING=1) embeds each document once and pools token vectors per chunk.
    late = vec_available and _late_chunking_on()
    docs = _late_documents(all_chunks, bodies) if late else {}
    doc_sha_of = {i: d["sha"] for d in docs.values() for i in d["idx"]}
    hashes = [chunk_hash(t, cache_salt(late, doc_sha_of.get(i))) for i, t in enumerate(texts)]

    if vec_available:
        embeddings = _embed_chunks(model, texts, hashes, docs, late, load_embedding_cache(sqlite_vec),
                                   f"text_format {_ec.TEXT_FORMAT}, {truncated} truncated to {max_chars} chars")
    else:
        embeddings = [None] * len(texts)
        print(f"Skipping embeddings for {len(all_chunks)} chunks (FTS-only build).")
    return embeddings, hashes, late


def _insert_chunks(db, all_chunks, embeddings, hashes, sens_by_path, vec_available, fresh_by_path=None):
    """Write every chunk to sections_meta, sections_fts and (when vectors are on) sections_vec.

    fresh_by_path is entry_freshness() output. Without it the freshness columns stay NULL."""
    fresh_by_path = fresh_by_path or {}
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
            "INSERT INTO sections_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                chunk.get("title", ""),
                *_freshness_values(fresh_by_path.get(chunk["file_path"])),
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
            "INSERT INTO sections_fts(rowid, slug, title, heading, block_id, content) VALUES (?, ?, ?, ?, ?, ?)",
            (i, chunk["slug"], chunk.get("title", ""), chunk["heading"], chunk["block_id"], chunk["content"]),
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


def _publish_index(db, all_chunks, vec_available, late):
    """Write the meta table, close the scratch DB and swap it in atomically."""
    write_index_meta(db, vec_available, "late" if late else "standard")
    db.commit()
    db.close()
    # Swap the finished index in. Readers querying DB_PATH up to this instant
    # saw the previous complete index; after it they see the new one. There is
    # no moment at which the path holds a partial database.
    size_kb = BUILD_PATH.stat().st_size // 1024
    os.replace(BUILD_PATH, DB_PATH)
    mode = "" if vec_available else " (FTS-only, vector search disabled)"
    print(f"Index built: {DB_PATH} ({size_kb}KB, {len(all_chunks)} sections){mode}")


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
        _require_extension_loading()

    SEARCH_DIR.mkdir(parents=True, exist_ok=True)

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

    collected = _collect_chunks()
    if collected is None:
        return
    all_chunks, sens_by_path = collected

    # Load embedding model (FTS-only build skips this entirely)
    model = _load_embedding_model(SentenceTransformer, len(all_chunks)) if vec_available else None

    # Connect and create schema
    db = sqlite3.connect(str(BUILD_PATH))
    if vec_available:
        db.enable_load_extension(True)
        sqlite_vec.load(db)

    _create_schema(db, vec_available)
    index_skills(db, model=model)

    embeddings, hashes, late = _embed_stage(sqlite_vec, model, all_chunks, vec_available)
    _insert_chunks(db, all_chunks, embeddings, hashes, sens_by_path, vec_available, entry_freshness(all_source_entries()))
    _publish_index(db, all_chunks, vec_available, late)


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
    if {"-h", "--help"} & set(sys.argv[1:]):
        print(__doc__.strip())
        sys.exit(0)
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
        try:
            added = upgrade_freshness()
        except sqlite3.Error as e:
            added = []
            print(f"Freshness column upgrade skipped ({e}).")
        if added:
            print(f"Added freshness columns in place: {', '.join(added)}.")
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
