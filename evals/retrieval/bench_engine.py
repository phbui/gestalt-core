"""The resumable retrieval engine behind beir_bench.py and mteb_bench.py.

It scores gestalt's search pipeline on corpora of up to about nine million documents on one machine, and it
survives a kill at any moment. The scorer, the fusion and the reranker are the code the server runs. One depth
differs: the reranked system fuses legs twice as deep as the served search before the same reranker (see retrieve). Every phase writes its progress as it goes and a restart continues from there.

Phases of one corpus, all under one work directory:
  1. docs     the corpus streams into corpus.sqlite (table docs: ord, doc_id, text) in transactions of
              100,000 rows. A restart skips the rows already stored.
  2. fts      a file-backed FTS5 table in the same file, with the schema and tokenizer of beir_bench.build_db,
              so BM25 scores are identical. Built from the docs table in transactions of 100,000 rows, and
              resumed from the row count committed with the last transaction.
  3. embed    document vectors in float32 blocks of 20,000 rows (resumable.BlockStore). Only blocks without
              a valid receipt are encoded.
  4. queries  every finished (system, query) goes to queries.jsonl at once (resumable.QueryLog). A restart
              skips finished queries.

The search itself mirrors run_retrieval_evals.search_scored with rerank=False for the base systems:
gestalt_rank.fts_match builds the MATCH expression, gestalt_rank.pool_size sets how deep each leg reads,
and fuse_pools fuses the legs through gestalt_rank.fuse, whose arithmetic lives in fusion.py. The server calls the
same function, so there is one copy of the fusion. fuse_pools is not a call into gestalt_rank.hybrid_search, and
tests/test_beir_bench.py checks its rankings against the in-memory path.

Grid systems. A system named hybrid:wrrf@W, hybrid:convex@A or hybrid:rescue fuses the same two pools as hybrid,
at the same depth, with weighted RRF at lexical weight W, convex fusion at lexical weight A, or the dense-first
rescue. beir_bench --fusion-grid adds them. Nothing is embedded or searched again for them.

Dense search has two backends with one interface, topk(query_matrix, k) -> (ids, distances):
  VecDense    the sqlite-vec vec0 table of the old in-memory path. Used up to DENSE_THRESHOLD documents.
  ExactDense  exact search by blocked matrix products straight from the memory-mapped blocks, on the GPU
              when torch sees one. It returns sqlite-vec's L2 distance and sqlite-vec's tie order.
GESTALT_BENCH_DENSE=vec|exact|auto picks one. auto, the default, uses VecDense up to the threshold.

Two space-saving FTS5 options were measured before this design was fixed, on 20,000 real paragraphs and 379
queries built by gestalt_rank.fts_match (SQLite 3.45.1; rerun it on another SQLite before trusting it). A contentless table (content='') gave
identical ids and identical BM25 scores for every top-100 list, at 45% of the file size, so the engine uses
it. detail=none and detail=column reject the quoted-token queries fts_match builds ("phrase queries are not
supported") and crashed the process on that sample, so the engine does not use them.
"""
from __future__ import annotations

import json
import math
import os
import _thread
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "tools"))

import gestalt_rank  # noqa: E402
from resumable import BlockStore, FingerprintMismatch, QueryLog, atomic_write_json, make_fingerprint  # noqa: E402

K_RRF = gestalt_rank.RRF_K  # one definition, in gestalt_rank
DENSE_THRESHOLD = 300_000
FTS_TXN_ROWS = 100_000
QUERY_BATCH = 4096
VEC_CHUNK_ROWS = 1024  # sqlite-vec's default vec0 chunk size. Its tie order depends on it (see ExactDense).
VEC_MAX_K = 4096  # sqlite-vec refuses a larger k
# The text a benchmark document embeds and indexes: its title, a space, its body. Part of the corpus fingerprint.
TEXT_FORMAT = "title-space-text-v1"
TOKEN_BUDGET_DEFAULT = 16_384
ATTN_BUDGET_DEFAULT = 4_000_000
BASE_SYSTEMS = ("bm25", "dense", "hybrid")


def doc_text(title: str | None, body: str | None) -> str:
    """The one place a benchmark document becomes text. beir_bench and mteb_bench both call it."""
    return ((title or "") + " " + (body or "")).strip()


def select_capped(docs: Iterable, relevant: set[str], cap: int, doc_id: Callable = lambda d: d.doc_id) -> list:
    """Cap a corpus stream at `cap` documents. Every relevant document stays. The rest fills in corpus order.

    The one subset rule for BEIR and MTEB. The stream is read once and never held whole, so a nine-million
    document set fits. The result can exceed `cap` when the relevant documents alone do. It keeps corpus order.
    With no relevant set it is the first `cap` documents."""
    keep, fill_room, found = [], max(cap - len(relevant), 0), 0
    for d in docs:
        if doc_id(d) in relevant:
            keep.append(d)
            found += 1
        elif fill_room > 0:
            keep.append(d)
            fill_room -= 1
        if fill_room <= 0 and found >= len(relevant):
            break
    return keep


def budgets() -> dict:
    """The encode budgets in force. GESTALT_BENCH_TOKEN_BUDGET and GESTALT_BENCH_ATTN_BUDGET override them."""
    def read(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        try:
            return max(1, int(raw)) if raw else default
        except ValueError:
            raise SystemExit(f"{name}={raw!r} is not an integer")
    return {"token_budget": read("GESTALT_BENCH_TOKEN_BUDGET", TOKEN_BUDGET_DEFAULT),
            "attn_budget": read("GESTALT_BENCH_ATTN_BUDGET", ATTN_BUDGET_DEFAULT)}


# ---------------------------------------------------------------- encoding

def token_lengths(model, texts: list[str]) -> list[int]:
    """Tokens per text from the model's own tokenizer, truncated as the model truncates. Without a tokenizer,
    1.4 tokens per whitespace word, which is close for English wordpiece and BPE vocabularies."""
    tok = getattr(model, "tokenizer", None)
    if tok is not None and callable(tok):
        max_len = getattr(model, "max_seq_length", None)
        kw = {"truncation": True, "max_length": max_len} if max_len else {}
        return [len(ids) for ids in tok(texts, add_special_tokens=True, **kw)["input_ids"]]
    return [max(1, math.ceil(len(t.split()) * 1.4)) for t in texts]


def plan_subbatches(lengths: Sequence[int], max_batch: int, token_budget: int, attn_budget: int) -> list[list[int]]:
    """Index lists, longest texts first, each sized n = min(max_batch, token_budget // L, attn_budget // L^2), n >= 1.

    L is the longest length in the sub-batch. Eager attention costs batch x heads x L^2 per layer, so one fixed
    batch size either wastes short texts or overfills the GPU on long ones. Measured 2026-10-08: batch 64 at
    L=1943 needed 11.6 GB of attention scores per layer on a 16 GB card, spilled into shared memory and ran
    about five times slower."""
    order = sorted(range(len(lengths)), key=lambda i: -lengths[i])
    out, i = [], 0
    while i < len(order):
        longest = max(1, lengths[order[i]])
        n = max(1, min(max_batch, token_budget // longest, attn_budget // (longest * longest)))
        out.append(order[i:i + n])
        i += n
    return out


def encode_documents(model, texts: list[str], max_batch: int, doc_prefix: str, postprocess: Callable,
                     on_subbatch: Callable[[int, int], None] | None = None) -> np.ndarray:
    """Document vectors in input order, float32, with the profile's post-processing.

    Each sub-batch from plan_subbatches is one encode call. The vectors equal those of one big call up to
    floating-point noise from different padding. `on_subbatch(docs_done, docs_total)` runs after each sub-batch."""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    full = [doc_prefix + t for t in texts]
    b = budgets()
    plan = plan_subbatches(token_lengths(model, full), max_batch, b["token_budget"], b["attn_budget"])
    out: np.ndarray | None = None
    done = 0
    for idx in plan:
        vecs = np.asarray(model.encode([full[i] for i in idx], batch_size=len(idx), convert_to_numpy=True, show_progress_bar=False))
        if out is None:
            out = np.zeros((len(texts), vecs.shape[1]), dtype=np.float32)
        out[idx] = vecs
        done += len(idx)
        if on_subbatch:
            on_subbatch(done, len(texts))
    return np.ascontiguousarray(np.asarray(postprocess(out), dtype=np.float32))


def encode_queries(model, texts: list[str], query_prefix: str, postprocess: Callable) -> np.ndarray:
    """Query vectors, one encode call per query, exactly as run_retrieval_evals._embed_query makes them.

    One call per query keeps every query vector bit-identical to the old per-query path. Batching would change
    the padding and could move a near-tie."""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    return np.stack([np.asarray(postprocess(model.encode(query_prefix + t, convert_to_numpy=True)), dtype=np.float32) for t in texts])


def cap_gpu_memory() -> float | None:
    """GESTALT_BENCH_VRAM_FRACTION (unset = no cap): limit this process to that share of the card, so a run that outgrows it
    raises an out-of-memory error instead of spilling into shared system memory (NVIDIA's driver does that silently, with a
    large slowdown). Returns the fraction applied, or None."""
    raw = os.environ.get("GESTALT_BENCH_VRAM_FRACTION", "").strip()
    if not raw:
        return None
    try:
        fraction = float(raw)
    except ValueError:
        fraction = float("nan")
    if not 0 < fraction <= 1:
        raise SystemExit(f"GESTALT_BENCH_VRAM_FRACTION={raw!r} must be a number above 0 and at most 1")
    import torch
    if torch.cuda.is_available():
        torch.cuda.set_per_process_memory_fraction(fraction)
        return fraction
    return None


def free_gpu_cache() -> None:
    """Hand cached GPU memory back between blocks, so a long run does not creep towards the card's limit."""
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


# ---------------------------------------------------------------- the corpus file

class _TextView(Sequence):
    """len() and slices over the docs table, so BlockStore reads a block of texts at a time."""

    def __init__(self, idx: FtsIndex):
        self.idx = idx

    def __len__(self) -> int:
        return self.idx.n_docs()

    def __getitem__(self, s):
        if isinstance(s, int):
            return self.idx.text(s)
        start, stop, _ = s.indices(len(self))
        return [r[0] for r in self.idx.db.execute("SELECT text FROM docs WHERE ord >= ? AND ord < ? ORDER BY ord", (start, stop))]


# sqlite3.OperationalError texts that mean the database itself failed, never the MATCH string (FtsIndex.search raises these).
DB_FAULTS = ("database is locked", "disk i/o error", "disk image is malformed", "unable to open", "no such table", "readonly database", "database or disk is full")


class FtsIndex:
    """corpus.sqlite: the documents, their FTS5 index and a progress table, in one WAL-mode file.

    The FTS5 table has beir_bench.build_db's columns and tokenizer, so its BM25 scores equal the in-memory
    index's. It is contentless (content='') because the texts already live in the docs table. Rows go in
    as (rowid = ord, slug = 'd<ord>', heading '', block_id '', content = text), as build_db inserts them,
    because the slug token counts towards the document length BM25 normalises by."""

    match_failures = 0  # MATCH strings FTS5 refused, each scored as an empty lexical leg (see search)

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(slug, title, heading, block_id, content, tokenize='porter unicode61', content='');
            CREATE TABLE IF NOT EXISTS docs (ord INTEGER PRIMARY KEY, doc_id TEXT NOT NULL, text TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def n_docs(self) -> int:
        return int(self.get("docs_rows", 0))

    def docs_complete(self) -> bool:
        return bool(self.get("docs_complete", False))

    def load_docs(self, stream: Iterable[tuple[str, str]], progress: Callable[[int], None] | None = None) -> int:
        """Store (doc_id, text) pairs in stream order. Rows already stored are skipped, so the stream must be
        deterministic. Each transaction commits its rows and the new row count together. Returns the count."""
        if self.docs_complete():
            return self.n_docs()
        have = self.n_docs()
        batch: list[tuple[int, str, str]] = []
        n = 0
        for doc_id, text in stream:
            if n >= have:
                batch.append((n, doc_id, text))
                if len(batch) >= FTS_TXN_ROWS:
                    self._commit_docs(batch, n + 1)
                    batch = []
                    if progress:
                        progress(n + 1)
            n += 1
        with self.db:
            if batch:
                self.db.executemany("INSERT INTO docs (ord, doc_id, text) VALUES (?, ?, ?)", batch)
            self._set("docs_rows", n)
            self._set("docs_complete", True)
            ids_h, texts_h = self._content_hashes()
            self._set("ids_sha256", ids_h)
            self._set("texts_sha256", texts_h)
        if progress:
            progress(n)
        return n

    def _commit_docs(self, batch, rows: int) -> None:
        with self.db:
            self.db.executemany("INSERT INTO docs (ord, doc_id, text) VALUES (?, ?, ?)", batch)
            self._set("docs_rows", rows)

    def _content_hashes(self) -> tuple[str, str]:
        import hashlib

        hi, ht = hashlib.sha256(), hashlib.sha256()
        for doc_id, text in self.db.execute("SELECT doc_id, text FROM docs ORDER BY ord"):
            hi.update(doc_id.encode("utf-8") + b"\0")
            ht.update(text.encode("utf-8") + b"\0")
        return hi.hexdigest(), ht.hexdigest()

    def build_fts(self, progress: Callable[[int, int], None] | None = None) -> None:
        """Index every stored document. Resumes at the row count committed with the last transaction, which
        equals max(rowid) + 1 because rows go in by ord."""
        if not self.docs_complete():
            raise RuntimeError("load_docs must finish before build_fts")
        total = self.n_docs()
        done = int(self.get("fts_rows", 0))
        while done < total:
            end = min(total, done + FTS_TXN_ROWS)
            with self.db:
                self.db.execute(
                    "INSERT INTO sections_fts (rowid, slug, heading, block_id, content) "
                    "SELECT ord, 'd' || ord, '', '', text FROM docs WHERE ord >= ? AND ord < ? ORDER BY ord", (done, end))
                self._set("fts_rows", end)
            done = end
            if progress:
                progress(done, total)
        if not self.get("fts_optimized", False):
            with self.db:  # one segment answers queries faster. The scores do not depend on the segment layout.
                self.db.execute("INSERT INTO sections_fts (sections_fts) VALUES ('optimize')")
                self._set("fts_optimized", True)

    def search(self, match: str | None, limit: int) -> list[tuple[int, float]]:
        """(ord, FTS5 rank) best first: the query search_scored runs, on this file."""
        if not match:
            return []
        try:
            return [(r[0], r[1]) for r in self.db.execute(
                "SELECT rowid, rank FROM sections_fts WHERE sections_fts MATCH ? ORDER BY rank LIMIT ?", (match, limit))]
        except sqlite3.OperationalError as e:
            # A MATCH the FTS5 parser rejects ("fts5: syntax error", "unterminated string", "unknown special query") means "no
            # lexical hits", as gestalt_rank.hybrid_search scores it. A locked, missing or unreadable database is a fault and must
            # stop the run, not score as an empty leg. Every parse failure is counted and said once, so a run with many is visible.
            if any(f in str(e).lower() for f in DB_FAULTS):
                raise
            self.match_failures += 1
            print(f"bench_engine: FTS MATCH failed and scores as no lexical hits ({e}); failures so far {self.match_failures}", file=sys.stderr, flush=True)
            return []

    def texts(self) -> _TextView:
        return _TextView(self)

    def text(self, ord_: int) -> str:
        return self.db.execute("SELECT text FROM docs WHERE ord = ?", (ord_,)).fetchone()[0]

    def doc_ids(self, ords: Sequence[int]) -> list[str]:
        if not ords:
            return []
        got = dict(self.db.execute(f"SELECT ord, doc_id FROM docs WHERE ord IN ({','.join('?' * len(ords))})", list(ords)).fetchall())
        return [got[o] for o in ords]

    def lookup(self, doc_ids: Sequence[str]) -> dict[str, tuple[int, str]]:
        """doc_id -> (ord, text) for the ids this corpus holds. Builds the doc_id index on first use."""
        self.db.execute("CREATE INDEX IF NOT EXISTS docs_doc_id ON docs (doc_id)")
        out = {}
        for i in range(0, len(doc_ids), 500):
            part = list(doc_ids[i:i + 500])
            for ord_, did, text in self.db.execute(f"SELECT ord, doc_id, text FROM docs WHERE doc_id IN ({','.join('?' * len(part))})", part):
                out[did] = (ord_, text)
        return out


# ---------------------------------------------------------------- dense backends

def sqlite_vec_order(dist: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Argsort of each row the way sqlite-vec 0.1.9 orders a k-NN result: distance ascending, then the
    1024-row chunk ascending, then the row id DESCENDING inside a chunk.

    Measured 2026-10-08 on 5,000-row tables with every vector duplicated 2 to 50 times: 720 of 720 result
    lists matched this rule, boundary members included."""
    return np.lexsort((-idx, idx // VEC_CHUNK_ROWS, dist), axis=-1)


class VecDense:
    """The old path's dense leg: an in-memory vec0 table, filled from the BlockStore in row order."""

    name = "vec"

    def __init__(self, store: BlockStore, n_docs: int, dim: int):
        import sqlite_vec

        self.n = n_docs
        self.db = sqlite3.connect(":memory:")
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.execute(f"CREATE VIRTUAL TABLE sections_vec USING vec0(id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])")
        for start, block in store.iter_blocks(n_docs):
            self.db.executemany("INSERT INTO sections_vec (id, embedding) VALUES (?, ?)",
                                ((start + j, np.asarray(v, dtype=np.float32).tobytes()) for j, v in enumerate(block)))
        self.db.commit()

    def topk(self, queries: np.ndarray, k: int) -> tuple[list[np.ndarray], list[np.ndarray]]:
        if k > VEC_MAX_K:
            raise ValueError(f"sqlite-vec answers at most k={VEC_MAX_K}, asked for {k}. Use ExactDense.")
        ids, dists = [], []
        for q in np.asarray(queries, dtype=np.float32):
            rows = self.db.execute("SELECT id, distance FROM sections_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                                   (q.tobytes(), k)).fetchall()
            ids.append(np.array([r[0] for r in rows], dtype=np.int64))
            dists.append(np.array([r[1] for r in rows], dtype=np.float32))
        return ids, dists


class ExactDense:
    """Exact k-NN over the memory-mapped blocks, all queries of a call in one pass over the corpus.

    Each block moves to the device once. For every query chunk, a matrix product gives approximate squared
    distances, the candidates within a small tolerance of the k-th are kept, and their L2 distance is
    recomputed directly as sqrt(sum((q - d)^2)) in float32, the formula sqlite-vec evaluates. The direct form
    keeps duplicate documents exactly tied, which the expanded dot-product form does not. A running top k
    per query merges the blocks in sqlite-vec's tie order, so the result matches VecDense id for id.
    float32 throughout. torch runs it on CUDA when available, else on the CPU."""

    name = "exact"
    TOL = 1e-4  # squared-distance slack around the k-th candidate, far above float32 rounding of ~1e-6

    def __init__(self, store: BlockStore, n_docs: int, device: str | None = None, budget_bytes: int = 1 << 30):
        import torch

        self.torch = torch
        self.store, self.n = store, n_docs
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.budget = budget_bytes

    def topk(self, queries: np.ndarray, k: int) -> tuple[list[np.ndarray], list[np.ndarray]]:
        """The k nearest documents of each query row, as (ids, L2 distances), one array per query, nearest first.

        `queries` is an (nq, dim) float32 matrix and k is capped at the corpus size. Ids are document ordinals
        across all blocks. Equal distances come back in sqlite-vec's tie order (sqlite_vec_order), so the lists
        equal VecDense.topk id for id. The corpus is read once for the whole call."""
        torch = self.torch
        Q = np.ascontiguousarray(queries, dtype=np.float32)
        nq, dim = Q.shape
        k = min(k, self.n)
        best_d = np.full((nq, 0), np.inf, dtype=np.float32)
        best_i = np.zeros((nq, 0), dtype=np.int64)
        qt = torch.from_numpy(Q).to(self.device)
        qn = (qt * qt).sum(1)
        for start, block in self.store.iter_blocks(self.n):
            bt = torch.from_numpy(np.array(block, dtype=np.float32)).to(self.device)  # a copy: the mmap is read-only
            bn = (bt * bt).sum(1)
            nb = bt.shape[0]
            span = max(1, min(nq, QUERY_BATCH, self.budget // max(1, nb * 4)))
            new_d, new_i = [], []
            for c0 in range(0, nq, span):
                qc = qt[c0:c0 + span]
                approx = (qn[c0:c0 + span, None] + bn[None, :] - 2.0 * (qc @ bt.T)).clamp_min_(0)
                kk = min(nb, k)
                kth = torch.topk(approx, kk, dim=1, largest=False).values[:, -1:]
                width = int((approx <= kth + self.TOL).sum(1).max().item())
                kk = min(nb, max(kk, width))
                cand = torch.topk(approx, kk, dim=1, largest=False).indices
                del approx
                exact = torch.empty(cand.shape, dtype=torch.float32, device=self.device)
                step = max(1, self.budget // max(1, kk * dim * 4))
                for r0 in range(0, cand.shape[0], step):
                    rows = bt[cand[r0:r0 + step]]
                    exact[r0:r0 + step] = ((qc[r0:r0 + step, None, :] - rows) ** 2).sum(-1).sqrt()
                    del rows
                new_d.append(exact.cpu().numpy())
                new_i.append(cand.cpu().numpy().astype(np.int64) + start)
            # pad ragged candidate widths of the chunks with +inf so the merge can stack them
            width = max(a.shape[1] for a in new_d)
            nd = np.full((nq, width), np.inf, dtype=np.float32)
            ni = np.full((nq, width), np.iinfo(np.int64).max // 2, dtype=np.int64)
            r = 0
            for d_, i_ in zip(new_d, new_i):
                nd[r:r + len(d_), :d_.shape[1]] = d_
                ni[r:r + len(i_), :i_.shape[1]] = i_
                r += len(d_)
            all_d = np.concatenate([best_d, nd], axis=1)
            all_i = np.concatenate([best_i, ni], axis=1)
            order = sqlite_vec_order(all_d, all_i)[:, :k]
            best_d = np.take_along_axis(all_d, order, axis=1)
            best_i = np.take_along_axis(all_i, order, axis=1)
            del bt, bn
        return [best_i[j] for j in range(nq)], [best_d[j] for j in range(nq)]


def dense_mode(n_docs: int, mode: str | None = None) -> str:
    """vec or exact. GESTALT_BENCH_DENSE=vec|exact|auto, auto meaning vec up to DENSE_THRESHOLD documents."""
    mode = (mode or os.environ.get("GESTALT_BENCH_DENSE", "auto") or "auto").strip().lower()
    if mode not in ("vec", "exact", "auto"):
        raise SystemExit(f"GESTALT_BENCH_DENSE={mode!r} is not vec, exact or auto")
    if mode == "auto":
        return "vec" if n_docs <= DENSE_THRESHOLD else "exact"
    return mode


def open_dense(store: BlockStore, n_docs: int, dim: int, mode: str | None = None):
    return VecDense(store, n_docs, dim) if dense_mode(n_docs, mode) == "vec" else ExactDense(store, n_docs)


# ---------------------------------------------------------------- fusion and retrieval

GRID_PREFIX = "hybrid:"


def grid_system(name: str) -> tuple[str, float | None] | None:
    """(method, weight) for a grid system name such as hybrid:wrrf@0.2, hybrid:convex@0.1 or hybrid:rescue. None for any other name."""
    if not name.startswith(GRID_PREFIX):
        return None
    method, _, w = name[len(GRID_PREFIX):].partition("@")
    if method == "rescue" and not w:
        return method, None
    if method in ("wrrf", "convex") and w:
        weight = float(w)
        if not 0.0 <= weight <= 1.0:
            raise ValueError(f"{name}: the weight must be from 0 to 1")
        return method, weight
    raise ValueError(f"{name} is not a grid system. Use hybrid:wrrf@W, hybrid:convex@A or hybrid:rescue")


def fuse_pools(fts_rows: list[tuple[int, float]], vec_rows: list[tuple[int, float]], fusion: str, alpha: float,
               w_bm25: float | None = None) -> list[tuple[int, float]]:
    """Fuse the lexical pool (id, FTS5 rank) and the dense pool (id, L2 distance) into [(id, score)] best first.

    The fusion of run_retrieval_evals.search_scored: gestalt_rank.fuse with the same method, then a stable sort by score.
    rrf is reciprocal rank fusion with K=60, lexical leg first. alpha is the lexical weight under convex and w_bm25 the
    lexical weight under wrrf (None reads GESTALT_FUSION_W_BM25)."""
    # Leg depths differ by system, and retrieve sets them (see the note there).
    scores = gestalt_rank.fuse(fts_rows, vec_rows, fusion, alpha, w_bm25)
    return [(i, scores[i]) for i in sorted(scores, key=lambda i: scores[i], reverse=True)]


def rerank_failed(info: dict) -> bool:
    """True when the reranker did not run and the row is the fusion order under another name."""
    return info.get("fallback") not in ("none", "too-few")


class Status:
    """status.json for an orchestrator, and one progress line with a projection on stderr. Both throttled."""

    def __init__(self, path: Path | None, label: str, every_s: float = 10.0):
        self.path, self.label, self.every = path, label, every_s
        self._last = 0.0
        self._t0: dict[str, tuple[float, int]] = {}

    def __call__(self, phase: str, done: int, total: int, unit: str = "", force: bool = False) -> None:
        now = time.time()
        t0, d0 = self._t0.setdefault(phase, (now, done))
        if not force and done < total and now - self._last < self.every:
            return
        self._last = now
        rate = (done - d0) / (now - t0) if now > t0 and done > d0 else 0.0
        left = f", about {(total - done) / rate:.0f}s left" if rate > 0 and done < total else ""
        print(f"{self.label} {phase} {done}/{total}{(' ' + unit) if unit else ''}, {now - t0:.0f}s elapsed{left}", file=sys.stderr, flush=True)
        if self.path:
            atomic_write_json(self.path, {"phase": phase, "done": done, "total": total,
                                          "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))})


def retrieve(queries: list[tuple[str, str]], *, fts: FtsIndex, dense, embed: Callable[[list[str]], np.ndarray], log: QueryLog,
             systems: Sequence[str], limit: int, rerank: dict | None = None, fusion: str = "rrf", alpha: float = 0.5,
             fill_from_hybrid: bool = False, batch: int = QUERY_BATCH, status: Callable | None = None) -> None:
    """Run every system on every query not yet in `log`, writing each finished (system, query) at once.

    systems   any of bm25, dense, hybrid, hybrid_rerank
    limit     ranked documents kept per system
    rerank    {"model": alias, "depth": d} for hybrid_rerank: the top d of a hybrid search with limit d,
              re-scored by gestalt_rank.rerank_rows. A query whose rerank fell back is logged with its
              fallback and is run again by the next call, so a retry after a GPU fault repairs it.
    fill_from_hybrid  after the reranked head, continue with the hybrid order up to `limit` (mteb's top_k)

    Each query vector is computed once and serves dense, hybrid and hybrid_rerank. The lexical leg runs
    once per query at the deepest pool any system needs. Measured on 20,000 paragraphs, an FTS5 LIMIT 20
    list is the head of the LIMIT 100 list for every query, so one deep read serves the shallower legs."""
    if "hybrid_rerank" in systems and not rerank:
        raise ValueError("hybrid_rerank needs rerank={'model': ..., 'depth': ...}")
    # hybrid fuses legs of pool_size(limit, False) = 2 * limit rows, 20 at limit 10, as gestalt_search does without a reranker.
    # hybrid_rerank fuses legs of pool_size(depth, False) = 2 * depth rows, 80 at depth 40, then hands the top `depth` fused rows
    # to the reranker. The served path (gestalt_rank.hybrid_search) fuses legs of pool_size(limit, True, depth) = max(2 * limit,
    # depth) = 40 rows before the same reranker, so the benchmark's reranker sees a head drawn from a wider fused pool than the
    # server's. The scorer, the fusion and the reranker are the served code; the pool depth is the one documented difference.
    # Do not change either without rerunning the published numbers.
    pool = gestalt_rank.pool_size(limit, False)
    pool_rr = gestalt_rank.pool_size(rerank["depth"], False) if rerank else 0
    grid = {s: grid_system(s) for s in systems if grid_system(s)}
    pooled = "hybrid" in systems or bool(grid)  # the systems that fuse legs of depth `pool`
    need_fts = any(s in systems for s in ("bm25", "hybrid", "hybrid_rerank")) or bool(grid)
    need_vec = any(s in systems for s in ("dense", "hybrid", "hybrid_rerank")) or bool(grid)
    fts_k = max([limit if "bm25" in systems else 0, pool if pooled else 0, pool_rr])
    vec_k = max([limit if "dense" in systems else 0, pool if pooled else 0, pool_rr])

    def finished(system: str, qid: str) -> bool:
        e = log.entries.get((system, qid))
        return e is not None and not e.get("fallback")

    pending = [q for q in queries if not all(finished(s, q[0]) for s in systems)]
    total, done = len(queries), len(queries) - len(pending)
    if status:
        status("queries", done, total, "queries", force=True)
    for b0 in range(0, len(pending), batch):
        part = pending[b0:b0 + batch]
        vec_ids = vec_d = None
        if need_vec:
            qv = embed([t for _, t in part])
            vec_ids, vec_d = dense.topk(qv, vec_k)
        for j, (qid, text) in enumerate(part):
            fts_rows = fts.search(gestalt_rank.fts_match(text), fts_k) if need_fts else []
            vec_rows = list(zip(vec_ids[j].tolist(), vec_d[j].tolist())) if need_vec else []

            def fused(depth: int) -> list[tuple[int, float]]:  # the one call into the fusion step
                return fuse_pools(fts_rows[:depth], vec_rows[:depth], fusion, alpha)

            hybrid = fused(pool) if ("hybrid" in systems or fill_from_hybrid) else []
            out: dict[str, tuple[list[int], list, str | None]] = {}
            if "bm25" in systems:
                out["bm25"] = ([i for i, _ in fts_rows[:limit]], [s for _, s in fts_rows[:limit]], None)
            if "dense" in systems:
                out["dense"] = ([i for i, _ in vec_rows[:limit]], [d for _, d in vec_rows[:limit]], None)
            if "hybrid" in systems:
                out["hybrid"] = ([i for i, _ in hybrid[:limit]], [s for _, s in hybrid[:limit]], None)
            for name, (method, weight) in grid.items():
                if finished(name, qid):
                    continue
                g = fuse_pools(fts_rows[:pool], vec_rows[:pool], method, weight if method == "convex" else 0.5,
                               weight if method == "wrrf" else None)
                out[name] = ([i for i, _ in g[:limit]], [s for _, s in g[:limit]], None)
            if "hybrid_rerank" in systems and not finished("hybrid_rerank", qid):
                head = fused(pool_rr)[:rerank["depth"]]
                rows = [{"id": i, "slug": f"d{i}", "title": "", "heading": "", "content": fts.text(i)} for i, _ in head]
                ranked, rr_scores, info = gestalt_rank.rerank_rows(text, rows, rerank["model"])
                if info.get("model") not in (None, rerank["model"]):
                    # resolve_alias may swap qwen3-4b for bge when VRAM is short. A run that scored under another model than
                    # the one it reports is wrong, so the swap counts as a fallback and voids the run like any other.
                    info = {**info, "fallback": f"alias-downgrade:{info['model']}"}
                ids = [r["id"] for r in ranked]
                scores = list(rr_scores) if rr_scores is not None else [s for _, s in head]
                if fill_from_hybrid:
                    seen = set(ids)
                    tail = [(i, s) for i, s in hybrid if i not in seen]
                    ids, scores = ids + [i for i, _ in tail], scores + [None] * len(tail)
                out["hybrid_rerank"] = (ids[:limit], scores[:limit], info.get("fallback") if rerank_failed(info) else None)
            for system, (ords, scores, fallback) in out.items():
                if finished(system, qid):
                    continue
                entry = {"system": system, "qid": qid, "ids": fts.doc_ids(ords), "scores": [None if s is None else float(s) for s in scores]}
                if fallback:
                    entry["fallback"] = fallback
                log.append(entry)
            done += 1
            if status:
                status("queries", done, total, "queries")
    log.sync()
    if status:
        status("queries", total, total, "queries", force=True)


def scores_from_log(log: QueryLog, systems: Sequence[str], qids: Sequence[str]) -> dict[str, dict[str, list]]:
    """{system: {qid: logged scores, best first}}. bm25 logs FTS5 ranks and dense logs L2 distances, both smaller is better."""
    return {s: {q: log.entries[(s, q)]["scores"] for q in qids} for s in systems}


def runs_from_log(log: QueryLog, systems: Sequence[str], qids: Sequence[str]) -> tuple[dict[str, dict[str, list[str]]], int]:
    """({system: {qid: ranked doc ids}}, number of queries whose rerank fell back) for the given queries."""
    runs = {s: {q: log.entries[(s, q)]["ids"] for q in qids} for s in systems}
    fallbacks = sum(1 for q in qids if log.entries.get(("hybrid_rerank", q), {}).get("fallback")) if "hybrid_rerank" in systems else 0
    return runs, fallbacks


# ---------------------------------------------------------------- one corpus, end to end

def model_fields(ec, text_format: str = TEXT_FORMAT) -> dict:
    """The embedding configuration a vector depends on. Corpora built under equal fields can share vectors."""
    return {"model": ec.MODEL_NAME, "revision": ec.MODEL_REVISION, "profile": ec.PROFILE, "dim": ec.EMBED_DIM,
            "doc_prefix": ec.DOC_PREFIX, "text_format": text_format,
            # Added 2026-10-09: a resumed work directory must never serve vectors made under another precision or normalisation.
            "dtype": str(getattr(ec, "EMBED_DTYPE", "float32")), "normalize": bool(getattr(ec, "EMBED_NORMALIZE", False)),
            "attn_impl": getattr(ec, "ATTN_IMPL", None), "tf32": bool(getattr(ec, "TF32", False))}


def donor_reuse(cur: FtsIndex, donor_dir: Path, fields: dict) -> Callable | None:
    """A BlockStore reuse callback that copies vectors from another corpus with the same documents.

    FEVER (5,416,568 documents) and Climate-FEVER (5,416,593) ship as two corpora of the same Wikipedia
    abstracts (ir_datasets metadata.json). A row is copied only when the donor holds the same doc id with the
    same text under the same embedding fields, and the donor's block is valid. Everything else is encoded."""
    meta_path = donor_dir / "corpus.sqlite"
    if not meta_path.exists():
        return None
    donor = FtsIndex(meta_path)
    if donor.get("model_fields") != fields or not donor.get("embed_complete", False):
        donor.close()
        return None
    store = BlockStore(donor_dir / "emb", donor.get("fingerprint"), int(donor.get("block_size")), readonly=True)
    n_donor = donor.n_docs()

    def reuse(start: int, end: int):
        rows = cur.db.execute("SELECT doc_id, text FROM docs WHERE ord >= ? AND ord < ? ORDER BY ord", (start, end)).fetchall()
        found = donor.lookup([d for d, _ in rows])
        mask = np.zeros(len(rows), dtype=bool)
        vecs = None
        by_block: dict[int, list[tuple[int, int]]] = {}
        for j, (did, text) in enumerate(rows):
            hit = found.get(did)
            if hit and hit[1] == text:
                by_block.setdefault(hit[0] // store.block_size, []).append((j, hit[0] % store.block_size))
        for b, pairs in by_block.items():
            rows_in_block = min(store.block_size, n_donor - b * store.block_size)
            if not store.block_valid(b, rows_in_block):
                continue
            arr = np.load(store.dir / f"emb-{b:05d}.npy", mmap_mode="r")
            if vecs is None:
                vecs = np.zeros((len(rows), arr.shape[1]), dtype=np.float32)
            for j, r in pairs:
                vecs[j] = arr[r]
                mask[j] = True
        return (mask, vecs) if vecs is not None else None

    return reuse


# ---------------------------------------------------------------- the embed phase guard

EXIT_STALL = 4  # the same code as run_retrieval_evals.EXIT_QUERY_STALL, so a retry loop treats an embed stall like a query stall
EMBED_PROGRESS_SUBBATCHES = 10
EMBED_PROGRESS_SECONDS = 60.0
PROBE_TIMEOUT = 30.0
EXIT_GRACE = 10.0


def embed_timeout() -> float:
    """GESTALT_EVAL_EMBED_TIMEOUT seconds the embed phase may go without finishing one sub-batch (default 600, 0 turns the guard off).
    The first sub-batch also loads CUDA kernels, so raise the knob for a cold start on a slow card."""
    raw = os.environ.get("GESTALT_EVAL_EMBED_TIMEOUT", "").strip()
    if not raw:
        return 600.0
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not value >= 0:  # also rejects nan
        raise ValueError(f"GESTALT_EVAL_EMBED_TIMEOUT={raw!r} must be a number of seconds, 0 or more")
    return value


class EmbedGuard:
    """Progress lines and a stall watchdog for the embed phase.

    Seen 2026-10-09: the GPU passthrough dropped three minutes into an embed run, and the process spun at 100 percent CPU
    inside dead CUDA calls for 58 minutes with no output. The encode loop calls `beat()` after every sub-batch. A watcher
    thread declares a stall when no beat arrives within `timeout` seconds, writes one line and interrupts the main thread.
    A dead CUDA context can also block a normal interpreter exit (inferred from that 58-minute spin). So the thread ends the
    process with `exit_fn` (os._exit) when the main thread has not left within `grace` seconds. `timeout` 0 turns the
    watchdog off. The progress lines stay on."""

    def __init__(self, label: str, total_docs: int, total_blocks: int, timeout: float, *, exit_fn=os._exit, grace: float = EXIT_GRACE,
                 poll: float | None = None, every: int = EMBED_PROGRESS_SUBBATCHES, seconds: float = EMBED_PROGRESS_SECONDS,
                 clock=time.monotonic, interrupt=_thread.interrupt_main):
        self.label, self.total_docs, self.total_blocks, self.timeout = label, total_docs, total_blocks, timeout
        self.exit_fn, self.grace, self.every, self.seconds, self.clock, self.interrupt = exit_fn, grace, every, seconds, clock, interrupt
        self._poll = poll if poll is not None else max(0.05, min(1.0, timeout / 4)) if timeout else 1.0
        self.block, self.docs = 0, 0  # blocks and documents finished
        self._base_docs = 0
        self._t0 = self._beat_at = self._printed = clock()
        self._subs = 0
        self._limit = timeout
        self._probing = False
        self._declared: float | None = None
        self._stop = threading.Event()
        self._thread = None
        self.message = ""

    def start(self, blocks_done: int, docs_done: int) -> None:
        self.block, self.docs, self._base_docs = blocks_done, docs_done, docs_done
        self._t0 = self._beat_at = self._printed = self.clock()
        if self.timeout:
            self._spawn()

    def _spawn(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._watch, name="embed-watchdog", daemon=True)
            self._thread.start()

    def beat(self, docs_in_block: int = 0, block_docs: int = 0, final: bool = False) -> None:
        """One sub-batch finished (`final` False, `docs_in_block` done in this block) or one block finished (`final` True, `block_docs` documents)."""
        self._beat_at = now = self.clock()
        self._subs += 1
        if final:
            self.block += 1
            self.docs += block_docs
        shown = self.block if final else min(self.block + 1, self.total_blocks)
        if final or self._subs % self.every == 0 or now - self._printed >= self.seconds:
            self._print(now, shown, self.docs + (0 if final else docs_in_block))

    def _print(self, now: float, shown: int, docs: int) -> None:
        elapsed = now - self._t0
        rate = (docs - self._base_docs) / elapsed if elapsed > 0 else 0.0
        left = f", about {(self.total_docs - docs) / rate:.0f} s left" if rate > 0 and docs < self.total_docs else ""
        print(f"{self.label} embed block {shown}/{self.total_blocks}, docs {docs}/{self.total_docs}, {rate:.0f} docs/s, {elapsed:.0f} s elapsed{left}",
              file=sys.stderr, flush=True)
        self._printed = now

    def probe(self, run: Callable[[], object], block: int) -> None:
        """Run a tiny GPU operation under PROBE_TIMEOUT. A CUDA error or a hang is a lost GPU: log it and exit 4. Never falls back to the CPU."""
        self._beat_at = self.clock()
        self._limit, self._probing = (min(PROBE_TIMEOUT, self.timeout) if self.timeout else PROBE_TIMEOUT), True
        self._spawn()
        try:
            run()
        except RuntimeError as err:  # torch.AcceleratorError subclasses RuntimeError
            if "cuda" not in str(err).lower() and type(err).__name__ != "AcceleratorError":
                raise
            self.fail(f"{self.label} GPU lost after block {block}: {err}")
        finally:
            self._limit, self._probing = self.timeout, False
            self._beat_at = self.clock()

    def fail(self, message: str) -> None:
        """Write `message`, start the exit deadline and leave through SystemExit(4). The thread stays alive to enforce the deadline."""
        self._declared = self.clock()
        self.message = message
        print(message, file=sys.stderr, flush=True)
        self._spawn()
        raise SystemExit(EXIT_STALL)

    def _watch(self) -> None:
        while not self._stop.wait(self._poll):
            now = self.clock()
            if self._declared is not None:
                if now - self._declared >= self.grace:
                    sys.stderr.flush()
                    self.exit_fn(EXIT_STALL)
                    return
                continue
            limit = self._limit
            if limit and now - self._beat_at > limit:
                self._declared = now
                if self._probing:
                    self.message = f"{self.label} GPU lost after block {self.block}: the liveness probe did not return in {limit:g} s"
                else:
                    self.message = (f"{self.label} embed stall: no sub-batch finished in {now - self._beat_at:.0f}s, last block {self.block}, docs {self.docs}. "
                                    f"That is past GESTALT_EVAL_EMBED_TIMEOUT={limit:g}s, so the process exits with code {EXIT_STALL}. Run the same command again to resume.")
                print(self.message, file=sys.stderr, flush=True)
                self.interrupt()

    def close(self) -> None:
        self._stop.set()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._declared is None:
            self.close()
        elif exc_type is KeyboardInterrupt:  # the watchdog's interrupt_main reached the main thread
            raise SystemExit(EXIT_STALL)
        return False  # after a declared stall the thread stays alive to enforce the exit deadline


def gpu_probe(model) -> Callable[[], object] | None:
    """A tiny tensor op on the model's CUDA device, or None when the model is not on CUDA."""
    device = str(getattr(model, "device", "") or "")
    if not device.startswith("cuda"):
        return None
    import torch
    return lambda: torch.ones(1, device=device).sum().item()


def prepare_corpus(work: Path, corpus_id: str, stream_fn: Callable[[], Iterable[tuple[str, str]]], *, model, ec,
                   batch_size: int, block_size: int, rebuild: bool, status: Status, donors: Sequence[Path] = ()) -> tuple[FtsIndex, BlockStore]:
    """Phases 1 to 3 for one corpus: documents, FTS5 index, embeddings. Each phase resumes where it stopped.

    `stream_fn()` yields (doc_id, text) in a fixed order. `corpus_id` names the corpus in the fingerprint.
    `donors` are work directories of corpora that may hold the same documents (see donor_reuse)."""
    work.mkdir(parents=True, exist_ok=True)
    idx = FtsIndex(work / "corpus.sqlite")  # documents and FTS5 do not depend on the model, so --rebuild keeps them
    status("docs", idx.n_docs(), max(idx.n_docs(), 1), "documents stored", force=True)
    n = idx.load_docs(stream_fn(), progress=lambda d: status("docs", d, max(d, 1), "documents stored"))
    status("docs", n, n, "documents stored", force=True)
    idx.build_fts(progress=lambda d, t: status("fts", d, t, "documents indexed"))
    fields = model_fields(ec)
    fp = make_fingerprint(**fields, dataset=corpus_id, ids_sha256=idx.get("ids_sha256"), texts_sha256=idx.get("texts_sha256"))
    stored = idx.get("fingerprint")
    if stored not in (None, fp) and not rebuild:
        raise FingerprintMismatch(f"{work} was built for another configuration. Pass --rebuild, or point --work-dir elsewhere.")
    with idx.db:
        idx._set("fingerprint", fp)
        idx._set("model_fields", fields)
        idx._set("block_size", block_size)
        idx._set("embed_complete", False)
    store = BlockStore(work / "emb", fp, block_size, rebuild=rebuild)
    reuse = None
    for d in donors:
        if d.resolve() != work.resolve():
            reuse = donor_reuse(idx, d, fields)
            if reuse:
                print(f"{status.label} reusing vectors of matching documents from {d}", file=sys.stderr, flush=True)
                break
    total_blocks = store.n_blocks(n)
    status("embed", 0, total_blocks, "blocks", force=True)
    sizes = [min(n, (i + 1) * block_size) - i * block_size for i in range(total_blocks)]
    valid = [store.block_valid(i, sizes[i]) for i in range(total_blocks)]
    guard = EmbedGuard(status.label, n, total_blocks, embed_timeout())
    probe = gpu_probe(model)

    def encode(texts: list[str]) -> np.ndarray:
        vecs = encode_documents(model, texts, batch_size, ec.DOC_PREFIX, ec.postprocess, on_subbatch=lambda d, _t: guard.beat(d))
        guard.beat(block_docs=len(texts), final=True)
        return vecs

    def after_block() -> None:
        free_gpu_cache()
        if probe:
            guard.probe(probe, guard.block)

    with guard:
        guard.start(sum(valid), sum(sz for sz, v in zip(sizes, valid) if v))
        store.encode_missing(
            idx.texts(), encode,
            progress=lambda d, t, rate: status("embed block", d, t, f"blocks, {rate:.0f} docs/s"),
            reuse=reuse, after_block=after_block)
    with idx.db:
        idx._set("embed_complete", True)
    status("embed block", total_blocks, total_blocks, "blocks", force=True)
    return idx, store


def query_log_header(*, fingerprint: str, systems: Sequence[str], limit: int, rerank: dict | None, fusion: str, alpha: float,
                     fill_from_hybrid: bool) -> dict:
    """Everything that decides a logged ranking. A log written under another header is refused.

    A run whose hybrid is not plain RRF, or that holds grid systems, also records the fusion knobs (fusion_params),
    because the normaliser, the missing policy and the rescue knobs change those rankings. A plain RRF run without a
    grid has the header it had before the knobs existed."""
    header = {
        "corpus_fingerprint": fingerprint, "systems": list(systems), "limit": limit, "fusion": fusion,
        "alpha": alpha if fusion == "convex" else None, "stopwords": gestalt_rank.stopwords_on(), "rrf_k": K_RRF,
        "fill_from_hybrid": fill_from_hybrid,
        "rerank": None if not rerank else {"model": rerank["model"], "resolved_model": gestalt_rank.resolve_alias(rerank["model"]),
                                           "depth": rerank["depth"], "maxchars": gestalt_rank.rerank_maxchars(),
                                           "instruction": gestalt_rank.qwen_instruction()},
    }
    if fusion != "rrf" or any(grid_system(s) for s in systems):
        header["fusion_params"] = gestalt_rank.fusion_settings()
    return header
