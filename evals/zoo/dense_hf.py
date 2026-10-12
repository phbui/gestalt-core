"""Dense retrieval with any sentence-transformers model on the Hugging Face Hub.

Documents are stored in corpus.sqlite and encoded in blocks (resumable.BlockStore), so a killed run resumes at the first block without a
valid receipt. Search is exact (bench_engine.ExactDense). Its score is the negative L2 distance.

Vectors are used as the model returns them, as beir_bench does. nomic-embed-text-v1.5 has no Normalize module, so its raw vectors are
not unit length and L2 ranks them differently from cosine. Pass normalize=True to rescale every vector to unit length first.
"""
from __future__ import annotations

from pathlib import Path

import gestalt_embed_config as _gec  # the transformers 5 fixes live with the profiles, so the product and the zoo share them
import numpy as np
from bench_engine import (
    TEXT_FORMAT,
    EmbedGuard,
    ExactDense,
    FtsIndex,
    embed_timeout,
    encode_documents,
    encode_queries,
    free_gpu_cache,
    gpu_probe,
)
from resumable import DEFAULT_BLOCK_SIZE, BlockStore, make_fingerprint

from evals.zoo.adapter import describe_fields
from evals.zoo.cost import dir_bytes, param_count

rebuild_nonpersistent_buffers = _gec.rebuild_nonpersistent_buffers
shim_extended_attention_mask_device = _gec.shim_extended_attention_mask_device


def load_model(model_id: str, revision: str | None, dtype: str, max_seq_length: int | None, trust_remote_code: bool, code_revision: str | None = None,
               config_kwargs: dict | None = None):
    from sentence_transformers import SentenceTransformer

    kwargs = {"dtype": dtype}
    if code_revision:  # pins the repository that holds the modelling code, as the gestalt embed profiles do for nomic
        kwargs["code_revision"] = code_revision
    # config_kwargs overrides the checkpoint's config, e.g. stella's use_memory_efficient_attention (needs xformers) and unpad_inputs.
    model = SentenceTransformer(model_id, revision=revision, trust_remote_code=trust_remote_code, model_kwargs=kwargs, config_kwargs=config_kwargs or None)
    if trust_remote_code:
        shim_extended_attention_mask_device()
        rebuild_nonpersistent_buffers(model)
    if max_seq_length:
        model.max_seq_length = max_seq_length
    return model


def _unit(x) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    bad = ~np.isfinite(x).all(axis=-1)
    if bad.any():  # a silent zero row ranks nowhere; DenseOn at float16 produced 628 of them on SciFact (2026-10-09) and scored 0.38
        raise RuntimeError(f"{int(bad.sum())} of {len(bad)} embeddings are not finite; the model overflowed at this dtype, use bfloat16 or float32")
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return np.divide(x, n, out=np.zeros_like(x), where=n > 0)


def _raw(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float32)


class DenseHF:
    needs_work_dir = True
    parts = ()

    def __init__(self, name: str, model: str, work_dir: str | Path, revision: str | None = None, dtype: str = "float32",
                 doc_prefix: str = "", query_prefix: str = "", max_seq_length: int | None = None, batch_size: int = 64,
                 block_size: int = DEFAULT_BLOCK_SIZE, trust_remote_code: bool = False, normalize: bool = False, licence: str | None = None, code_revision: str | None = None,
                 config_kwargs: dict | None = None):
        self.name, self.model_id, self.revision, self.dtype = name, model, revision, dtype
        self.dir, self.doc_prefix, self.query_prefix = Path(work_dir), doc_prefix, query_prefix
        self.max_seq_length, self.batch_size, self.block_size = max_seq_length, batch_size, block_size
        self.trust_remote_code, self.licence, self.code_revision = trust_remote_code, licence, code_revision
        self.config_kwargs = dict(config_kwargs or {})
        self.post = _unit if normalize else _raw
        self.normalize = normalize
        self.indexed = False
        self.params = None
        self.idx = self.store = self.model = None

    def index(self, docs) -> None:
        self.idx = FtsIndex(self.dir / "corpus.sqlite")
        n = self.idx.load_docs(docs)
        self.model = load_model(self.model_id, self.revision, self.dtype, self.max_seq_length, self.trust_remote_code, self.code_revision, self.config_kwargs)
        fp = make_fingerprint(model=self.model_id, revision=self.revision, dtype=self.dtype, doc_prefix=self.doc_prefix,
                              max_seq_length=self.max_seq_length, normalize=self.normalize, text_format=TEXT_FORMAT,
                              ids_sha256=self.idx.get("ids_sha256"), texts_sha256=self.idx.get("texts_sha256"),
                              **({"code_revision": self.code_revision} if self.code_revision else {}),
                              **({"config_kwargs": sorted(self.config_kwargs.items())} if self.config_kwargs else {}))
        self.store = BlockStore(self.dir / "emb", fp, self.block_size)
        # The same progress lines and stall watchdog as the bench (bench_engine.EmbedGuard, GESTALT_EVAL_EMBED_TIMEOUT). On
        # 2026-10-09 a zoo run sat 23 minutes at full CPU with the GPU idle after a model loaded, and nothing ended it.
        total_blocks = self.store.n_blocks(n)
        sizes = [min(n, (i + 1) * self.block_size) - i * self.block_size for i in range(total_blocks)]
        valid = [self.store.block_valid(i, sizes[i]) for i in range(total_blocks)]
        guard = EmbedGuard(f"zoo {self.name}", n, total_blocks, embed_timeout())
        probe = gpu_probe(self.model)

        def encode(texts: list[str]) -> np.ndarray:
            vecs = encode_documents(self.model, texts, self.batch_size, self.doc_prefix, self.post, on_subbatch=lambda d, _t: guard.beat(d))
            guard.beat(block_docs=len(texts), final=True)
            return vecs

        def after_block() -> None:
            free_gpu_cache()
            if probe:
                guard.probe(probe, guard.block)

        try:
            with guard:
                guard.start(sum(valid), sum(sz for sz, v in zip(sizes, valid) if v))
                self.store.encode_missing(self.idx.texts(), encode, after_block=after_block)
        except BaseException:
            self.store.close()  # a failed run must not keep the work-directory lock
            raise
        self.dense = ExactDense(self.store, n)
        self.params = param_count(self.model)
        self.indexed = True

    def search(self, queries, k):
        if not queries:
            return {}
        qv = encode_queries(self.model, [t for _, t in queries], self.query_prefix, self.post)
        ids, dist = self.dense.topk(qv, k)
        return {qid: list(zip(self.idx.doc_ids(ids[j].tolist()), (-dist[j]).tolist())) for j, (qid, _) in enumerate(queries)}

    def close(self) -> None:
        if self.store:
            self.store.close()
        if self.idx:
            self.idx.close()
        self.model = None
        free_gpu_cache()

    def describe(self) -> dict:
        return describe_fields(model_id=self.model_id, revision=self.revision, dtype=self.dtype, parameters=self.params, licence=self.licence,
                               index_bytes=dir_bytes(self.dir) if self.dir.exists() else None)
