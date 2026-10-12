"""One set of constants for the embedding model (X14, 2026-10-06. Profiles added 2026-10-08).

The index builder, the MCP server and the eval runner must agree on these. The builder writes them to
the `index_meta` table. The server compares that table with this module when it opens the index.

Do not edit a profile's MODEL_NAME, DOC_PREFIX or EMBED_DIM casually. The builder's embedding cache key hashes the
model name and the prefixed text, so a change here re-embeds the whole corpus.

Knobs, all read once at import. Importing this module does not import transformers.

  GESTALT_EMBED_PROFILE  stella-400m (default since 2026-10-11: it scored highest of every candidate on three BEIR sets, see
                         evals/retrieval/BENCHMARKS.md), nomic (the default until then), qwen3-4b, gte-base-v15, denseon.
  GESTALT_TRUST_REMOTE_CODE  auto (default), 1 or 0. Auto uses the library's own nomic class when transformers is 5.5 or later.
  GESTALT_EMBED_DEVICE   device for the model. Default cpu. The builder lets sentence-transformers pick when this is unset.
  GESTALT_EMBED_DIM      Matryoshka width. Default is the profile's full width. Any other width needs the layer-norm, slice, L2 recipe in postprocess().
  GESTALT_SEARCH_DIR     directory that holds gestalt.db. Default is <repo>/.search. Used to build and score a side index.

Precision knobs, read once at import. An unset knob changes nothing. A bad value raises ValueError.

  GESTALT_EMBED_DTYPE    float32, float16 or bfloat16. Unset keeps the profile's own weights: float32 for nomic, bfloat16 for qwen3-4b.
  GESTALT_TF32           0 (default) or 1. apply_tf32() sets torch.backends.cuda.matmul.allow_tf32 and torch.backends.cudnn.allow_tf32 to it. Call it when a model loads.
  GESTALT_ATTN_IMPL      sdpa, eager or flash_attention_2. Unset keeps the library's choice. Passed to the model as attn_implementation.
  GESTALT_EMBED_NORMALIZE  0 or 1. Unset keeps the profile's own: 1 for stella-400m and the other cosine-trained candidates, 0 for nomic and qwen3-4b. At the full width, 1 L2-normalises every vector so sqlite-vec's L2 ranks by cosine. The narrower Matryoshka widths always normalise.
"""
import os
import re
import sys
from pathlib import Path

# The text layout of one chunk. Bump it when chunk_text() changes. The builder stores it in index_meta and needs_rebuild() reads it.
TEXT_FORMAT = "v2-title"

PROFILES = {
    "nomic": {
        "MODEL_NAME": "nomic-ai/nomic-embed-text-v1.5",
        # Snapshot hash the hub already has cached (read from ~/.cache/huggingface/hub on the hub, 2026-10-06).
        # To refresh: HfApi().model_info(MODEL_NAME).sha, or `ls ~/.cache/huggingface/hub/models--nomic-ai--nomic-embed-text-v1.5/snapshots/`.
        # TRUST_REMOTE_CODE below is what an OLD library needs. _resolve_trust_remote_code() turns it off when transformers ships
        # the nomic_bert class itself. Pinning the revision is what makes remote code tolerable where it still runs:
        # the weights and their config are fixed, so a Hub push cannot change what loads.
        # The executed modelling code lives in a second repo, nomic-ai/nomic-bert-2048, snapshot
        # 7710840340a098cfb869c4f65e87cf2b1b70caca on the hub. It is pinned through MODEL_KWARGS:
        # MODEL_KWARGS passes that snapshot as code_revision, and the loaded module path carries the hash (checked 2026-10-08, transformers 5.16).
        "MODEL_REVISION": "e9b6763023c676ca8431644204f50c2b100d9aab",
        "CODE_REPO": "nomic-ai/nomic-bert-2048",
        "CODE_REVISION": "7710840340a098cfb869c4f65e87cf2b1b70caca",
        "FULL_DIM": 768,
        "MRL_DIMS": (768, 512, 256, 128, 64),
        # The model card recipe for a narrower vector: layer-norm, slice, L2 normalise.
        "MRL_LAYER_NORM": True,
        "DOC_PREFIX": "search_document: ",
        "QUERY_PREFIX": "search_query: ",
        "TRUST_REMOTE_CODE": True,
        "NATIVE_TYPE": "nomic_bert",
        "MODEL_KWARGS": {"code_revision": "7710840340a098cfb869c4f65e87cf2b1b70caca"},
        "TOKENIZER_KWARGS": {},
    },
    "qwen3-4b": {
        "MODEL_NAME": "Qwen/Qwen3-Embedding-4B",
        # Snapshot the hub has cached (ls ~/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-4B/snapshots/ on the hub, 2026-10-08, 7.6 GB).
        "MODEL_REVISION": "5cf2132abc99cad020ac570b19d031efec650f2b",
        "CODE_REPO": None,
        "CODE_REVISION": None,
        "FULL_DIM": 2560,
        # The card allows any width from 32 to 2560. Qwen3 needs no layer-norm: slice and L2 normalise.
        "MRL_DIMS": tuple(range(32, 2561)),
        "MRL_LAYER_NORM": False,
        "DOC_PREFIX": "",
        # The card's prompt_name="query" expands to "Instruct: {task}\nQuery:{query}". Writing it as a prefix keeps
        # the server's `QUERY_PREFIX + query` the one query-side rule for every profile.
        "QUERY_PREFIX": "Instruct: Given a question about a personal engineering knowledge base, retrieve the note section that answers it\nQuery:",
        "TRUST_REMOTE_CODE": False,
        "MODEL_KWARGS": {"torch_dtype": "bfloat16"},
        "TOKENIZER_KWARGS": {"padding_side": "left"},
    },
    # Candidates from evals/zoo/zoo-candidates.yaml (2026-10-09). Revisions and prefixes were read from each model card and
    # huggingface.co/api/models/<id>. A profile is a candidate until the paired comparison in the zoo passes the decision rule in the
    # benchmarks entry ^embedder-candidates-2026-10-09. All three were trained for cosine, so they default to unit-length vectors.
    "gte-base-v15": {
        "MODEL_NAME": "Alibaba-NLP/gte-base-en-v1.5",
        "MODEL_REVISION": "a829fd0e060bb84554da0dfd354d0de0f7712b7f",
        # The modelling code (BERT with RoPE and GLU, model_type "new") is not in transformers and not in the model repo. The model's
        # auto_map points at the repository Alibaba-NLP/new-impl, which is pinned here and passed as code_revision.
        "CODE_REPO": "Alibaba-NLP/new-impl",
        "CODE_REVISION": "40ced75c3017eb27626c9d4ea981bde21a2662f4",
        "FULL_DIM": 768,
        "MRL_DIMS": (768,),  # the card does not offer Matryoshka widths
        "MRL_LAYER_NORM": False,
        "DOC_PREFIX": "",
        "QUERY_PREFIX": "",
        "TRUST_REMOTE_CODE": True,
        "NATIVE_TYPE": None,  # no transformers release ships this architecture, so remote code always runs
        "NORMALIZE": True,
        "MODEL_KWARGS": {"code_revision": "40ced75c3017eb27626c9d4ea981bde21a2662f4"},
        "TOKENIZER_KWARGS": {},
    },
    "stella-400m": {
        "MODEL_NAME": "NovaSearch/stella_en_400M_v5",
        "MODEL_REVISION": "ffeb2b7ee715c226d4ffe5e4619f7dbb48624c20",
        # The modelling code (configuration.py, modeling.py) is in the model repo, so MODEL_REVISION pins it too.
        "CODE_REPO": None,
        "CODE_REVISION": None,
        "FULL_DIM": 1024,
        "MRL_DIMS": (1024,),  # the card's narrower widths use separate trained dense heads, not a slice of this vector
        "MRL_LAYER_NORM": False,
        "DOC_PREFIX": "",
        # The card's prompt_name="s2p_query" expands to this text. The card recommends 512 tokens.
        "QUERY_PREFIX": "Instruct: Given a web search query, retrieve relevant passages that answer the query.\nQuery: ",
        "TRUST_REMOTE_CODE": True,
        "NATIVE_TYPE": None,
        "NORMALIZE": True,
        "MODEL_KWARGS": {},
        "TOKENIZER_KWARGS": {},
        # Defaults for the gestalt_rank knobs under this profile. A set environment variable wins. On BEIR the BM25-RRF plus
        # Qwen3-Reranker-0.6B pipeline lowered stella (FiQA 0.5534 alone, 0.4842 through it). Dropping BM25 and blending the
        # reranker 0.5 with stella's order raised it on all three sets (2026-10-11: SciFact 0.7920, NFCorpus 0.4191, FiQA 0.5736).
        # Convex fusion at alpha 0 with the tmin floor keeps stella's order and score shape, and puts a lexical-only hit below
        # every dense hit. GESTALT_RERANK stays auto, so a leaf without the hub's GPU serves stella alone.
        # The checkpoint asks for xformers memory-efficient attention and unpadding; without xformers both must be off (model card).
        "CONFIG_KWARGS": {"use_memory_efficient_attention": False, "unpad_inputs": False},
        "KNOB_DEFAULTS": {"GESTALT_FUSION": "convex", "GESTALT_FUSION_ALPHA": "0", "GESTALT_FUSION_NORM": "tmin",
                          "GESTALT_RERANK_MODEL": "qwen3-0.6b", "GESTALT_RERANK_BLEND": "0.5"},
    },
    "denseon": {
        "MODEL_NAME": "lightonai/DenseOn",
        "MODEL_REVISION": "cb9947ebccb33862d24e3c7ca2edb25e51acd887",
        "CODE_REPO": None,
        "CODE_REVISION": None,
        "FULL_DIM": 768,
        "MRL_DIMS": (768,),
        "MRL_LAYER_NORM": False,
        "DOC_PREFIX": "document: ",
        "QUERY_PREFIX": "query: ",
        "TRUST_REMOTE_CODE": False,  # ModernBERT is native in transformers
        "NATIVE_TYPE": None,
        "NORMALIZE": True,
        "MODEL_KWARGS": {},
        "TOKENIZER_KWARGS": {},
    },
}


def _strict_choice(name: str, choices: tuple[str, ...]) -> str | None:
    """The value of an env var that must be one of `choices`. Unset or empty gives None. Anything else raises ValueError."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return None
    if raw not in choices:
        raise ValueError(f"{name}={raw!r} is not valid. Use one of: {', '.join(choices)}, or leave it unset.")
    return raw


def _warn(msg: str) -> None:
    print(f"gestalt-embed-config: {msg}", file=sys.stderr)


DEFAULT_PROFILE = "stella-400m"
PROFILE = (os.environ.get("GESTALT_EMBED_PROFILE") or DEFAULT_PROFILE).strip().lower()
if PROFILE not in PROFILES:
    _warn(f"unknown GESTALT_EMBED_PROFILE={PROFILE!r}, using {DEFAULT_PROFILE} (known: {', '.join(PROFILES)})")
    PROFILE = DEFAULT_PROFILE
_P = PROFILES[PROFILE]

MODEL_NAME = _P["MODEL_NAME"]
MODEL_REVISION = _P["MODEL_REVISION"]
CODE_REPO = _P["CODE_REPO"]
CODE_REVISION = _P["CODE_REVISION"]
DOC_PREFIX = _P["DOC_PREFIX"]
QUERY_PREFIX = _P["QUERY_PREFIX"]
# transformers ships the nomic_bert class from this release on (per the model card).
_NATIVE_NOMIC_FROM = (5, 5)


def _native_class_available(model_type: str = "nomic_bert") -> bool:
    """True when the installed transformers ships the model class itself, so nothing has to be fetched and executed from the Hub.

    The answer comes from the package metadata. Importing transformers or probing its submodules costs over a second, and every server
    that only answers FTS queries would pay it at import time. A transformers that is not installed gives False."""
    from importlib.metadata import PackageNotFoundError, version

    if model_type != "nomic_bert":
        return False
    try:
        parts = re.match(r"(\d+)\.(\d+)", version("transformers"))
    except PackageNotFoundError:
        return False
    return bool(parts) and (int(parts[1]), int(parts[2])) >= _NATIVE_NOMIC_FROM


def _resolve_trust_remote_code(profile_default: bool, native_type: str | None = "nomic_bert") -> bool:
    """GESTALT_TRUST_REMOTE_CODE = auto (default), 1 or 0. Only a profile that needs remote code at all (nomic) has anything to resolve.

    auto: use the library's own nomic_bert class when transformers has it (5.5 and later per the model card, 5.16 checked here), so the
    index build executes no code from the Hub. Older libraries fall back to the remote code pinned by CODE_REVISION. Tested 2026-10-08
    on 26 texts, transformers 5.16.1, CPU: native and remote-code vectors are identical, maximum absolute difference 0.0, and the
    native class is immune to the get_extended_attention_mask break that stops the remote code on transformers 5.19."""
    if not profile_default:
        return False
    mode = (os.environ.get("GESTALT_TRUST_REMOTE_CODE") or "auto").strip().lower()
    if mode in ("1", "true", "on", "yes"):
        return True
    if mode in ("0", "false", "off", "no"):
        return False
    if mode != "auto":
        _warn(f"unknown GESTALT_TRUST_REMOTE_CODE={mode!r}, using auto")
    return not (native_type and _native_class_available(native_type))


TRUST_REMOTE_CODE = _resolve_trust_remote_code(_P["TRUST_REMOTE_CODE"], _P.get("NATIVE_TYPE"))
# How the model loads: "remote-code" executes the pinned modelling code from the Hub, "native" uses the class transformers ships.
# A profile that never needs remote code (qwen3-4b) always loads natively. The index builder can record this next to the model name.
LOAD_PATH = "remote-code" if TRUST_REMOTE_CODE else "native"
# The pinned code revision only matters when remote code runs. A native load passes nothing extra to the constructor.
MODEL_KWARGS = dict(_P["MODEL_KWARGS"]) if (TRUST_REMOTE_CODE or not _P["TRUST_REMOTE_CODE"]) else {}
TOKENIZER_KWARGS = dict(_P["TOKENIZER_KWARGS"])
CONFIG_KWARGS = dict(_P.get("CONFIG_KWARGS") or {})  # passed to SentenceTransformer(config_kwargs=...), overriding the checkpoint config


def shim_extended_attention_mask_device() -> bool:
    """Let remote code call get_extended_attention_mask(..., device=...) on transformers 5, which dropped that argument.

    stella_en_400M_v5's modelling code passes device=self.device; transformers 5.15's signature is (attention_mask, input_shape,
    dtype=None), so the call raised TypeError (2026-10-09). The argument only placed the mask. True when the shim was installed."""
    import inspect

    from transformers.modeling_utils import ModuleUtilsMixin

    orig = ModuleUtilsMixin.get_extended_attention_mask
    if getattr(orig, "_gestalt_device_shim", False) or "device" in inspect.signature(orig).parameters:
        return False

    def patched(self, attention_mask, input_shape, dtype=None, device=None):  # device is accepted and ignored
        return orig(self, attention_mask, input_shape, dtype) if dtype is not None else orig(self, attention_mask, input_shape)

    patched._gestalt_device_shim = True
    ModuleUtilsMixin.get_extended_attention_mask = patched
    return True


def rebuild_nonpersistent_buffers(model) -> int:
    """Recompute the rotary cache and position_ids that remote modelling code builds in __init__ and never saves.

    transformers 5 builds a model on the meta device and loads only the checkpoint's tensors, so these buffers arrive as uninitialised
    memory and rope_cos[position_ids] indexes with garbage (gte-v1.5, 2026-10-09; Alibaba-NLP/new-impl discussion 14). Returns the
    number of modules touched. Harmless on a model without them."""
    import torch

    touched = 0
    for module in model.modules():
        if hasattr(module, "_set_cos_sin_cache") and hasattr(module, "inv_freq"):
            device = next((p.device for p in module.parameters()), None) or module.inv_freq.device
            dim, base = int(module.dim), float(module.base)
            module.inv_freq = 1.0 / (base ** (torch.arange(0, dim, 2, device=device).float() / dim))
            seq_len = int(module.max_position_embeddings * getattr(module, "scaling_factor", 1.0))
            module._set_cos_sin_cache(seq_len=seq_len, device=device, dtype=torch.get_default_dtype())
            touched += 1
        buf = getattr(module, "position_ids", None)
        if isinstance(buf, torch.Tensor) and buf.dim() == 1 and "position_ids" in dict(module.named_buffers(recurse=False)):
            module.position_ids = torch.arange(buf.numel(), device=buf.device)
            touched += 1
    return touched


def before_load() -> None:
    """Call before SentenceTransformer(...) for this profile: installs the transformers 5 shim that remote code needs."""
    if TRUST_REMOTE_CODE:
        shim_extended_attention_mask_device()


def after_load(model):
    """Call on every freshly loaded embedding model: repairs what transformers 5 leaves uninitialised in remote code. Returns the model."""
    if TRUST_REMOTE_CODE and hasattr(model, "modules"):  # a stand-in encoder in a test has no torch modules to repair
        rebuild_nonpersistent_buffers(model)
    return model

# Precision knobs. Each changes MODEL_KWARGS only when it is set, so the defaults stay as they were.
DTYPE_CHOICES = ("float32", "float16", "bfloat16")
_dtype_env = _strict_choice("GESTALT_EMBED_DTYPE", DTYPE_CHOICES)
ATTN_IMPL = _strict_choice("GESTALT_ATTN_IMPL", ("sdpa", "eager", "flash_attention_2"))
TF32 = _strict_choice("GESTALT_TF32", ("0", "1")) == "1"
_normalize_env = _strict_choice("GESTALT_EMBED_NORMALIZE", ("0", "1"))
# Unset keeps each profile's own default: off for nomic and qwen3-4b as before, on for the cosine-trained candidates.
EMBED_NORMALIZE = (_normalize_env == "1") if _normalize_env is not None else bool(_P.get("NORMALIZE", False))
if _dtype_env:
    MODEL_KWARGS["torch_dtype"] = _dtype_env
if ATTN_IMPL:
    MODEL_KWARGS["attn_implementation"] = ATTN_IMPL
# The weights' dtype as it resolves: the knob, else the profile's own, else sentence-transformers' float32 default.
EMBED_DTYPE = MODEL_KWARGS.get("torch_dtype", "float32")


def apply_tf32() -> None:
    """Set the two TF32 switches in torch to GESTALT_TF32. Call it once when the model loads."""
    import torch

    torch.backends.cuda.matmul.allow_tf32 = TF32
    torch.backends.cudnn.allow_tf32 = TF32


FULL_DIM = _P["FULL_DIM"]
MRL_DIMS = _P["MRL_DIMS"]
_LAYER_NORM = _P["MRL_LAYER_NORM"]


def _resolve_dim() -> int:
    raw = (os.environ.get("GESTALT_EMBED_DIM") or "").strip()
    if not raw:
        return FULL_DIM
    try:
        dim = int(raw)
    except ValueError:
        dim = -1
    if dim not in MRL_DIMS:
        _warn(f"GESTALT_EMBED_DIM={raw!r} is not a Matryoshka width for {PROFILE}, using {FULL_DIM}")
        return FULL_DIM
    return dim


EMBED_DIM = _resolve_dim()
EMBED_DEVICE = (os.environ.get("GESTALT_EMBED_DEVICE") or "cpu").strip()
SEARCH_DIR = Path(os.environ["GESTALT_SEARCH_DIR"]).expanduser() if os.environ.get("GESTALT_SEARCH_DIR") else None


def resolve_db_path(default: Path) -> Path:
    """The index file to open: gestalt.db under GESTALT_SEARCH_DIR when it is set, else `default`."""
    return SEARCH_DIR / "gestalt.db" if SEARCH_DIR is not None else default


def postprocess(vec):
    """Apply the Matryoshka width to one vector or a batch. At the full width the input comes back untouched.

    A narrower width runs the model card recipe: layer-norm, slice to EMBED_DIM, L2 normalise. Every encode site
    must call this, documents and queries alike, or the two sides of the index live in different spaces.
    """
    import numpy as np

    arr = np.asarray(vec)
    if EMBED_DIM == FULL_DIM:
        if not EMBED_NORMALIZE:
            return arr
        arr = arr.astype(np.float32, copy=False)
        norm = np.linalg.norm(arr, axis=-1, keepdims=True)
        return arr / np.where(norm == 0, 1.0, norm)
    arr = arr.astype(np.float32, copy=False)
    if arr.shape[-1] < EMBED_DIM:
        raise ValueError(f"vector width {arr.shape[-1]} is below GESTALT_EMBED_DIM={EMBED_DIM}")
    if _LAYER_NORM:
        arr = (arr - arr.mean(axis=-1, keepdims=True)) / np.sqrt(arr.var(axis=-1, keepdims=True) + 1e-5)
    arr = arr[..., :EMBED_DIM]
    norm = np.linalg.norm(arr, axis=-1, keepdims=True)
    return np.ascontiguousarray((arr / np.maximum(norm, 1e-12)).astype(np.float32))


def chunk_text(slug: str, title: str, heading: str, content: str, prefix: str | None = None) -> str:
    """The text the builder embeds for one chunk (TEXT_FORMAT v2-title).

    A titled chunk reads "<title> (<slug words>) — <heading>", then a blank line, then the content. An untitled
    chunk keeps the old "<slug words> — <heading>" header. `prefix` defaults to DOC_PREFIX. Pass "" to get the
    body alone, which late chunking joins into one document.
    """
    head = f"{slug.replace('-', ' ')}"
    if title:
        head = f"{title} ({head})"
    return (DOC_PREFIX if prefix is None else prefix) + f"{head} — {heading}\n\n{content}"
