"""Multi-vector (ColBERT-style late interaction) retrieval with a sentence-transformers MultiVectorEncoder.

Every document becomes one vector per token. A query scores a document by MaxSim. For each query token take the largest dot product
over the document's token vectors, then sum over the query tokens. Token vectors are unit length, so MaxSim is a sum of cosines.

Indexing keeps documents in corpus.sqlite and writes the token vectors in blocks of `block_size` documents (TokenStore). A block is one
float16 .npy of all its token rows, back to back, and a .done receipt with the per-document row counts. That flat file plus the counts
is the offsets index. A killed run resumes at the first block without a valid receipt.

Search is exact. It scores every query against every token of every document, in batches. No candidate generation runs first. The cost is
(query tokens) x (corpus tokens) x dim multiply-adds per query, so latency grows with the corpus. The scan runs in numpy on the CPU.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import numpy as np
from bench_engine import TEXT_FORMAT, FtsIndex, free_gpu_cache
from resumable import BlockStore, atomic_write_json, make_fingerprint, sha256_hex

from evals.zoo.adapter import describe_fields
from evals.zoo.cost import dir_bytes, param_count

STORE_DTYPE = np.float16
SCORE_TOKENS = 50_000  # corpus token rows scored per matrix product
QUERY_TOKENS = 1_024  # query token rows scored per matrix product


def load_model(model_id: str, revision: str | None, dtype: str, max_doc_tokens: int | None, trust_remote_code: bool):
    """The model card's recipe for sentence-transformers 6: MultiVectorEncoder(model_id). It reads the PyLate files itself, so pylate is not needed."""
    from sentence_transformers import MultiVectorEncoder

    model = MultiVectorEncoder(model_id, revision=revision, trust_remote_code=trust_remote_code, model_kwargs={"dtype": dtype})
    if max_doc_tokens:
        model[0].document_length = max_doc_tokens  # the Transformer module's per-task limit for documents
    return model


def _unit(x) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return np.divide(x, n, out=np.zeros_like(x), where=n > 0)


def maxsim_block(q: np.ndarray, q_lens: np.ndarray, tokens: np.ndarray, lens: np.ndarray) -> np.ndarray:
    """MaxSim of each query against each document. Returns float32 (n_queries, n_docs).

    q holds the query token rows back to back and q_lens their counts. tokens and lens are the same for documents.
    A document with no rows scores -inf. Every query must have at least one row."""
    out = np.full((len(q_lens), len(lens)), -np.inf, dtype=np.float32)
    keep = lens > 0
    if not keep.any():
        return out
    starts = np.concatenate(([0], np.cumsum(lens[keep])[:-1]))
    best = np.maximum.reduceat(q @ tokens.T, starts, axis=1)
    out[:, keep] = np.add.reduceat(best, np.concatenate(([0], np.cumsum(q_lens)[:-1])), axis=0)
    return out


class TokenStore(BlockStore):
    """BlockStore with a variable number of rows per document. It keeps the parent's lock, manifest, fingerprint check and file naming.

    Block i is emb-NNNNN.npy (float16, token rows x dim) and emb-NNNNN.done (docs, per-document row counts, SHA-256 of the array, fingerprint)."""

    def tokens_valid(self, i: int, n_docs: int) -> bool:
        npy, done = self._paths(i)
        try:
            receipt = json.loads(done.read_text())
            arr = np.load(npy, mmap_mode="r")
        except (OSError, ValueError):
            return False
        lens = receipt.get("lens")
        if receipt.get("fingerprint") != self.fingerprint or receipt.get("start") != i * self.block_size or not isinstance(lens, list) or len(lens) != n_docs:
            return False
        if arr.dtype != STORE_DTYPE or arr.ndim != 2 or arr.shape[0] != sum(lens):
            return False
        return sha256_hex(np.ascontiguousarray(arr).data) == receipt.get("sha256")

    def save_tokens(self, i: int, start: int, docs: list[np.ndarray]) -> None:
        npy, done = self._paths(i)
        flat = np.ascontiguousarray(np.concatenate(docs), dtype=STORE_DTYPE)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{npy.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                np.save(fh, flat)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, npy)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        atomic_write_json(done, {"block": i, "start": start, "docs": len(docs), "lens": [len(d) for d in docs], "dim": int(flat.shape[1]),
                                 "sha256": sha256_hex(flat.data), "fingerprint": self.fingerprint})

    def encode_missing_tokens(self, texts, encode_fn, after_block=None) -> int:
        """Encode every block without a valid receipt. encode_fn maps a list of texts to a list of (rows, dim) arrays. Returns documents encoded."""
        n, encoded = len(texts), 0
        for i in range(self.n_blocks(n)):
            start, end = i * self.block_size, min(n, (i + 1) * self.block_size)
            if self.tokens_valid(i, end - start):
                continue
            chunk = list(texts[start:end])
            docs = [np.asarray(d, dtype=np.float32) for d in encode_fn(chunk)]
            if len(docs) != len(chunk) or any(d.ndim != 2 for d in docs):
                raise ValueError(f"encode_fn returned {len(docs)} matrices for {len(chunk)} texts")
            self.save_tokens(i, start, docs)
            encoded += len(chunk)
            if after_block:
                after_block()
        return encoded

    def block(self, i: int) -> tuple[np.ndarray, np.ndarray]:
        """(memory-mapped token rows, per-document row counts) of block i. Call after encode_missing_tokens."""
        npy, done = self._paths(i)
        return np.load(npy, mmap_mode="r"), np.array(json.loads(done.read_text())["lens"], dtype=np.int64)


class MultiVector:
    needs_work_dir = True
    parts = ()

    def __init__(self, name: str, model: str, work_dir: str | Path, revision: str | None = None, dtype: str = "float32",
                 max_doc_tokens: int | None = None, batch_size: int = 32, block_size: int = 2_000, trust_remote_code: bool = False,
                 licence: str | None = None):
        self.name, self.model_id, self.revision, self.dtype = name, model, revision, dtype
        self.dir, self.max_doc_tokens, self.batch_size, self.block_size = Path(work_dir), max_doc_tokens, batch_size, block_size
        self.trust_remote_code, self.licence = trust_remote_code, licence
        self.indexed = False
        self.params = self.vectors_per_doc_mean = None
        self.idx = self.store = self.model = None

    def _encode_docs(self, texts: list[str]) -> list[np.ndarray]:
        out = self.model.encode_document(texts, batch_size=self.batch_size, convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False)
        return [_unit(d) for d in out]

    def index(self, docs) -> None:
        self.idx = FtsIndex(self.dir / "corpus.sqlite")
        n = self.idx.load_docs(docs)
        self.model = load_model(self.model_id, self.revision, self.dtype, self.max_doc_tokens, self.trust_remote_code)
        fp = make_fingerprint(kind="multivector-v1", model=self.model_id, revision=self.revision, dtype=self.dtype, max_doc_tokens=self.max_doc_tokens,
                              store_dtype=np.dtype(STORE_DTYPE).name, text_format=TEXT_FORMAT, ids_sha256=self.idx.get("ids_sha256"),
                              texts_sha256=self.idx.get("texts_sha256"))
        self.store = TokenStore(self.dir / "tok", fp, self.block_size)
        try:
            self.store.encode_missing_tokens(self.idx.texts(), self._encode_docs, after_block=free_gpu_cache)
        except BaseException:
            self.store.close()  # a failed run must not keep the work-directory lock
            raise
        self.n_docs = n
        self.n_blocks = self.store.n_blocks(n)
        self.vectors_per_doc_mean = round(sum(int(self.store.block(i)[1].sum()) for i in range(self.n_blocks)) / n, 2) if n else None
        self.params = param_count(self.model)
        self.indexed = True

    def search(self, queries, k):
        if not queries:
            return {}
        enc = self.model.encode_query([t for _, t in queries], batch_size=self.batch_size, convert_to_numpy=True, normalize_embeddings=True,
                                      show_progress_bar=False)
        out = {qid: [] for qid, _ in queries}
        group: list = []
        for (qid, _), m in zip(queries, enc):
            m = _unit(m)
            if not len(m):
                continue  # a query with no token vectors has no hits
            if group and sum(len(g) for _, g in group) + len(m) > QUERY_TOKENS:
                self._score_group(group, k, out)
                group = []
            group.append((qid, m))
        if group:
            self._score_group(group, k, out)
        return out

    def _score_group(self, group, k, out) -> None:
        q = np.concatenate([m for _, m in group])
        q_lens = np.array([len(m) for _, m in group], dtype=np.int64)
        scores = np.empty((len(group), self.n_docs), dtype=np.float32)
        pos = 0
        for i in range(self.n_blocks):
            tokens, lens = self.store.block(i)
            d0 = t0 = 0
            while d0 < len(lens):
                d1, rows = d0, 0
                while d1 < len(lens) and (d1 == d0 or rows + lens[d1] <= SCORE_TOKENS):
                    rows += int(lens[d1])
                    d1 += 1
                scores[:, pos + d0:pos + d1] = maxsim_block(q, q_lens, np.asarray(tokens[t0:t0 + rows], dtype=np.float32), lens[d0:d1])
                d0, t0 = d1, t0 + rows
            pos += len(lens)
        top = min(k, self.n_docs)
        for j, (qid, _) in enumerate(group):
            row = scores[j]
            if top <= 0:
                continue
            cand = np.argpartition(-row, top - 1)[:top] if top < len(row) else np.arange(len(row))
            cand = cand[np.lexsort((cand, -row[cand]))]  # best first, ties by document order
            cand = cand[np.isfinite(row[cand])]
            out[qid] = list(zip(self.idx.doc_ids(cand.tolist()), row[cand].tolist()))

    def close(self) -> None:
        if self.store:
            self.store.close()
        if self.idx:
            self.idx.close()
        self.model = None
        free_gpu_cache()

    def describe(self) -> dict:
        d = describe_fields(model_id=self.model_id, revision=self.revision, dtype=self.dtype, parameters=self.params, licence=self.licence,
                            index_bytes=dir_bytes(self.dir) if self.dir.exists() else None)
        return d | {"vectors_per_doc_mean": self.vectors_per_doc_mean}
