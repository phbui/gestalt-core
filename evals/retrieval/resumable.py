"""Crash-safe building blocks for long benchmark runs.

The benchmark machine can lose power to a firmware clamp at any moment. Every file a later run reads is
therefore written whole or not at all, and every unit of work is recorded the moment it finishes. A restart
redoes at most one unit.

atomic_write   writes a file through a temporary file in the same directory, fsync and rename.
BlockStore     holds corpus embeddings in fixed-size blocks, one .npy file and one .done receipt per block.
QueryLog       is an append-only JSONL file with one line per finished (system, query id).

Nothing here knows about retrieval. bench_engine.py composes these pieces.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import numpy as np

DEFAULT_BLOCK_SIZE = 20_000


class FingerprintMismatch(RuntimeError):
    """A work directory holds state from another configuration. Reusing it would mix two systems."""


class WorkDirLocked(RuntimeError):
    """Another process holds the lock on this work directory. Two runs would interleave one JSONL and share emb-*.npy."""


def _warn(msg: str) -> None:
    print(f"resumable: WARNING {msg}", file=sys.stderr, flush=True)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # some filesystems do not allow opening a directory
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: str | os.PathLike, data: bytes | str) -> None:
    """Write `data` to `path` so a reader sees the old file or the new one, never a partial one.

    The data goes to a temporary file in the same directory, which is flushed and fsynced, then renamed over
    `path`. The directory is fsynced after the rename so the new name survives a power cut."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def atomic_write_json(path: str | os.PathLike, obj) -> None:
    atomic_write(path, json.dumps(obj, indent=1) + "\n")


def sha256_hex(data: bytes | memoryview) -> str:
    return hashlib.sha256(data).hexdigest()


def make_fingerprint(**fields) -> str:
    """SHA-256 over the named fields, in sorted key order, as canonical JSON.

    BlockStore expects: model, revision, profile, dim, doc_prefix, text_format, dataset, ids_sha256, texts_sha256."""
    return sha256_hex(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8"))


class BlockStore:
    """Corpus embeddings as blocks of `block_size` documents under `dir`.

    Block i covers documents [i * block_size, (i + 1) * block_size). It is two files: emb-NNNNN.npy (float32,
    rows x dim) and emb-NNNNN.done, a JSON receipt with the row count, the SHA-256 of the array bytes and the
    fingerprint. The receipt is written after the array, so a kill between the two leaves an array without a
    receipt, and the block is encoded again. A block is valid only when its receipt parses, names this
    fingerprint and row count, and the array on disk hashes to the receipt.

    manifest.json records the fingerprint and the block size. A directory built for another fingerprint is
    refused, unless `rebuild` is set, in which case its stale files are removed first. A directory that matches
    is kept whatever `rebuild` says, so repeating a command line with --rebuild after a crash loses nothing.

    The constructor takes an exclusive, non-blocking flock on <dir>/.lock and holds it until close() or until the
    object is collected. A second run on the same directory fails at once with WorkDirLocked, naming the holder's pid.
    A manifest that does not parse is renamed to manifest.json.corrupt-<time> with a warning. The blocks stay,
    because each one proves itself by its receipt (fingerprint, start, rows and SHA-256)."""

    def __init__(self, dir: str | os.PathLike, fingerprint: str, block_size: int = DEFAULT_BLOCK_SIZE, rebuild: bool = False,
                 readonly: bool = False):
        if block_size < 1:
            raise ValueError("block_size must be at least 1")
        if readonly and rebuild:
            raise ValueError("a read-only BlockStore cannot rebuild")
        self.dir = Path(dir)
        self.fingerprint = fingerprint
        self.block_size = block_size
        self.readonly = readonly
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock_fh = self._acquire_lock()
        try:
            self._open_manifest(rebuild)
        except BaseException:
            self.close()  # a refused run must not keep the lock
            raise

    def _open_manifest(self, rebuild: bool) -> None:
        fingerprint, block_size = self.fingerprint, self.block_size
        manifest = self.dir / "manifest.json"
        want = {"fingerprint": fingerprint, "block_size": block_size}
        have = None
        if manifest.exists():
            try:
                have = json.loads(manifest.read_text())
            except ValueError:
                have = None
                if self.readonly:
                    raise FingerprintMismatch(f"{manifest} does not parse and this store is read-only.")
                aside = manifest.with_name(f"manifest.json.corrupt-{time.strftime('%Y%m%dT%H%M%S')}")
                os.replace(manifest, aside)
                _warn(f"{manifest} does not parse. Moved it to {aside.name}. The embedding blocks stay and are checked one by one against their receipts.")
        wipe = False
        if have is not None and {k: have.get(k) for k in want} != want:
            if not rebuild:
                raise FingerprintMismatch(
                    f"{self.dir} holds embeddings for another configuration: fingerprint {str(have.get('fingerprint'))[:12]} with "
                    f"block size {have.get('block_size')}. This run needs {fingerprint[:12]} with block size {block_size}. "
                    "Pass --rebuild to discard them, or point --work-dir elsewhere.")
            have, wipe = None, True
        if have is None:
            if self.readonly:
                raise FingerprintMismatch(f"{self.dir} has no readable manifest for {fingerprint[:12]} and this store is read-only.")
            if wipe:  # --rebuild over a manifest of another configuration. A lost manifest wipes nothing.
                for p in self.dir.glob("emb-*"):
                    p.unlink()
            atomic_write_json(manifest, want)

    def _acquire_lock(self):
        """Take the flock on <dir>/.lock or raise WorkDirLocked naming the pid that holds it.

        A writer takes it exclusive and records its pid. A read-only store takes it shared, so any number of donors
        can read a finished directory at once, and a writer is still refused while they do."""
        path = self.dir / ".lock"
        fh = open(path, "a+")
        try:
            fcntl.flock(fh, (fcntl.LOCK_SH if self.readonly else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "unknown"
            fh.close()
            raise WorkDirLocked(f"{self.dir} is in use by pid {holder} (lock file {path}). Wait for that run to finish, or point --work-dir elsewhere.") from None
        if self.readonly:
            return fh
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        return fh

    def close(self) -> None:
        """Release the work-directory lock. Safe to call twice."""
        if not self._lock_fh.closed:
            self._lock_fh.close()

    def __enter__(self) -> BlockStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _paths(self, i: int) -> tuple[Path, Path]:
        return self.dir / f"emb-{i:05d}.npy", self.dir / f"emb-{i:05d}.done"

    def n_blocks(self, n_docs: int) -> int:
        return (n_docs + self.block_size - 1) // self.block_size

    def block_valid(self, i: int, rows: int) -> bool:
        """True when block `i` can be trusted as holding `rows` vectors for this fingerprint.

        The receipt must parse and name this fingerprint, this row count and this block's start row (i * block_size),
        and the .npy must be a float32 matrix of that shape whose bytes hash to the receipt. Any failure returns
        False, and encode_missing then encodes the block again."""
        npy, done = self._paths(i)
        try:
            receipt = json.loads(done.read_text())
        except (OSError, ValueError):
            return False
        if receipt.get("fingerprint") != self.fingerprint or receipt.get("rows") != rows or receipt.get("start") != i * self.block_size:
            return False
        try:
            arr = np.load(npy, mmap_mode="r")
        except (OSError, ValueError):
            return False
        if arr.dtype != np.float32 or arr.ndim != 2 or arr.shape[0] != rows:
            return False
        return sha256_hex(np.ascontiguousarray(arr).data) == receipt.get("sha256")

    def _save_block(self, i: int, start: int, vecs: np.ndarray) -> None:
        npy, done = self._paths(i)
        vecs = np.ascontiguousarray(vecs, dtype=np.float32)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{npy.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                np.save(fh, vecs)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, npy)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        atomic_write_json(done, {"block": i, "start": start, "rows": int(vecs.shape[0]), "dim": int(vecs.shape[1]),
                                 "sha256": sha256_hex(vecs.data), "fingerprint": self.fingerprint})

    def encode_missing(self, texts: Sequence[str], encode_fn: Callable[[list[str]], np.ndarray],
                       progress: Callable[[int, int, float], None] | None = None,
                       reuse: Callable[[int, int], tuple[np.ndarray, np.ndarray] | None] | None = None,
                       after_block: Callable[[], None] | None = None) -> int:
        """Encode every block without a valid receipt. Returns the number of documents encoded.

        `texts` needs only len() and slicing, so a lazy view over a database works. `encode_fn` maps a list of
        texts to a float32 array of the same length. At most one block of fresh vectors is held at a time.
        `progress(done_blocks, total_blocks, docs_per_s)` runs after each block. `reuse(start, end)` may return
        (mask, vectors) where mask marks rows whose vector is supplied, so only the other rows are encoded.
        `after_block` runs after each written block (the engine frees GPU memory there)."""
        n = len(texts)
        total = self.n_blocks(n)
        encoded = 0
        t0 = time.time()
        done = 0
        for i in range(total):
            start, end = i * self.block_size, min(n, (i + 1) * self.block_size)
            if self.block_valid(i, end - start):
                done += 1
                continue
            chunk = list(texts[start:end])
            given = reuse(start, end) if reuse else None
            if given is not None and given[0].any():
                mask, vecs = given
                vecs = np.array(vecs, dtype=np.float32, copy=True)
                todo = [j for j in range(len(chunk)) if not mask[j]]
                if todo:
                    vecs[todo] = np.asarray(encode_fn([chunk[j] for j in todo]), dtype=np.float32)
            else:
                todo = chunk
                vecs = np.asarray(encode_fn(chunk), dtype=np.float32)
            if vecs.ndim != 2 or vecs.shape[0] != len(chunk):
                raise ValueError(f"encode_fn returned shape {vecs.shape} for {len(chunk)} texts")
            self._save_block(i, start, vecs)
            encoded += len(todo)
            del vecs
            done += 1
            if after_block:
                after_block()
            if progress:
                el = time.time() - t0
                progress(done, total, encoded / el if el > 0 else 0.0)
        return encoded

    def iter_blocks(self, n_docs: int) -> Iterator[tuple[int, np.ndarray]]:
        """Yield (start row, memory-mapped float32 array) for every block, in order. Call after encode_missing."""
        for i in range(self.n_blocks(n_docs)):
            yield i * self.block_size, np.load(self._paths(i)[0], mmap_mode="r")


class QueryLog:
    """Append-only JSONL record of finished queries, safe against a kill at any byte.

    Line 1 is {"header": {...}}, the configuration the entries belong to. A log whose header differs from the
    one this run passes is refused, because entries from two configurations must never be mixed. With
    `rebuild` such a log is discarded instead. A log with a matching header is always kept. Each later
    line is one entry, at least {"system": ..., "qid": ...}. The last entry for a key wins.

    Lines are flushed at once and fsynced at most every `sync_every` lines or `sync_seconds` seconds, so a
    power cut loses at most that window and a process kill loses nothing. A final line cut short by a kill
    is detected on open and truncated away."""

    def __init__(self, path: str | os.PathLike, header: dict, sync_every: int = 20, sync_seconds: float = 2.0, rebuild: bool = False):
        self.path = Path(path)
        self.header = json.loads(json.dumps(header))  # normalise tuples and the like to what JSON reads back
        self.sync_every, self.sync_seconds = sync_every, sync_seconds
        self.entries: dict[tuple[str, str], dict] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                self._load()
            except FingerprintMismatch:
                if not rebuild:
                    raise
                self.entries.clear()
                self.path.unlink()
        if not self.path.exists():
            atomic_write(self.path, json.dumps({"header": self.header}) + "\n")
        self._fh = open(self.path, "ab")
        self._pending = 0
        self._last_sync = time.monotonic()

    def _load(self) -> None:
        raw = self.path.read_bytes()
        good_end = 0
        lines = []
        pos = 0
        while pos < len(raw):
            nl = raw.find(b"\n", pos)
            if nl < 0:
                break  # a final line without its newline was cut short
            try:
                lines.append(json.loads(raw[pos:nl]))
            except ValueError:
                break
            pos = good_end = nl + 1
        if good_end < len(raw):
            rest = raw[good_end:]
            n_lost = rest.count(b"\n") + (0 if rest.endswith(b"\n") else 1)
            if n_lost > 1:  # one bad last line is a write cut short by a kill. More than that is damage in the middle.
                _warn(f"{self.path} has a bad line at byte {good_end}. Dropped {n_lost} lines ({len(rest)} bytes) from there to the end. "
                      "The queries they held run again.")
            with open(self.path, "r+b") as fh:
                fh.truncate(good_end)
                fh.flush()
                os.fsync(fh.fileno())
        if not lines or "header" not in lines[0]:
            raise FingerprintMismatch(f"{self.path} has no header line. Remove it or pass --rebuild.")
        if lines[0]["header"] != self.header:
            diff = sorted(k for k in set(lines[0]["header"]) | set(self.header) if lines[0]["header"].get(k) != self.header.get(k))
            raise FingerprintMismatch(
                f"{self.path} was written by another configuration (differs in {', '.join(diff)}). "
                "Pass --rebuild to discard it, or point --work-dir elsewhere.")
        for e in lines[1:]:
            self.entries[(e["system"], e["qid"])] = e

    def done(self) -> set[tuple[str, str]]:
        return set(self.entries)

    def append(self, entry: dict) -> None:
        self._fh.write((json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8"))
        self._fh.flush()
        self.entries[(entry["system"], entry["qid"])] = entry
        self._pending += 1
        if self._pending >= self.sync_every or time.monotonic() - self._last_sync >= self.sync_seconds:
            self.sync()

    def sync(self) -> None:
        if self._pending:
            os.fsync(self._fh.fileno())
            self._pending = 0
        self._last_sync = time.monotonic()

    def close(self) -> None:
        if not self._fh.closed:
            self.sync()
            self._fh.close()

    def __enter__(self) -> QueryLog:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
