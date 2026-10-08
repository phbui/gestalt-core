"""One set of constants for the embedding model (X14, 2026-10-06).

The index builder, the MCP server and the eval runner must agree on these. The builder writes them to
the `index_meta` table. The server compares that table with this module when it opens the index.

Do not edit MODEL_NAME, DOC_PREFIX or EMBED_DIM casually. The builder's embedding cache key hashes the
model name and the prefixed text, so a change here re-embeds the whole corpus.
"""

MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
# Snapshot hash the hub already has cached (read from ~/.cache/huggingface/hub on hub, 2026-10-06).
# To refresh: HfApi().model_info(MODEL_NAME).sha, or `ls ~/.cache/huggingface/hub/models--nomic-ai--nomic-embed-text-v1.5/snapshots/`.
# trust_remote_code stays on because the model needs it. Pinning the revision is what makes that tolerable:
# the weights and their config are fixed, so a Hub push cannot change what loads.
# The executed modelling code lives in a second repo, nomic-ai/nomic-bert-2048, snapshot
# 7710840340a098cfb869c4f65e87cf2b1b70caca on the hub. It is NOT pinned yet: whether
# model_kwargs={"code_revision": ...} reaches that lookup is untested (RC-ai-research section 4).
MODEL_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"
CODE_REPO = "nomic-ai/nomic-bert-2048"
CODE_REVISION = "7710840340a098cfb869c4f65e87cf2b1b70caca"
EMBED_DIM = 768
DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "
