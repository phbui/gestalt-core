"""The precision knobs: GESTALT_EMBED_DTYPE, GESTALT_RERANK_DTYPE, GESTALT_TF32 and GESTALT_ATTN_IMPL.

No GPU and no model. A fake torch module records what the TF32 switches were set to.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
import gestalt_rank  # noqa: E402

KNOBS = ("GESTALT_EMBED_PROFILE", "GESTALT_EMBED_DTYPE", "GESTALT_RERANK_DTYPE", "GESTALT_TF32", "GESTALT_ATTN_IMPL", "GESTALT_RERANK_DEVICE",
         "GESTALT_TRUST_REMOTE_CODE")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in KNOBS:
        monkeypatch.delenv(k, raising=False)


def fresh(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("gestalt_embed_config_precision", REPO / "tools" / "gestalt_embed_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_torch(monkeypatch):
    t = types.SimpleNamespace(backends=types.SimpleNamespace(cuda=types.SimpleNamespace(matmul=types.SimpleNamespace(allow_tf32="unset")),
                                                              cudnn=types.SimpleNamespace(allow_tf32="unset")))
    monkeypatch.setitem(sys.modules, "torch", t)
    return t


def test_embed_defaults_are_unchanged(monkeypatch):
    nomic = fresh(monkeypatch)
    assert "torch_dtype" not in nomic.MODEL_KWARGS and "attn_implementation" not in nomic.MODEL_KWARGS
    assert nomic.EMBED_DTYPE == "float32" and nomic.TF32 is False and nomic.ATTN_IMPL is None
    qwen = fresh(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b")
    assert qwen.MODEL_KWARGS == {"torch_dtype": "bfloat16"} and qwen.EMBED_DTYPE == "bfloat16"


@pytest.mark.parametrize("value", ["float32", "float16", "bfloat16"])
def test_embed_dtype_resolves_through_model_kwargs(monkeypatch, value):
    ec = fresh(monkeypatch, GESTALT_EMBED_DTYPE=value)
    assert ec.MODEL_KWARGS["torch_dtype"] == value and ec.EMBED_DTYPE == value


def test_embed_dtype_overrides_the_qwen_profile_default(monkeypatch):
    ec = fresh(monkeypatch, GESTALT_EMBED_PROFILE="qwen3-4b", GESTALT_EMBED_DTYPE="float16")
    assert ec.MODEL_KWARGS == {"torch_dtype": "float16"}


def test_attn_impl_is_passed_as_attn_implementation(monkeypatch):
    ec = fresh(monkeypatch, GESTALT_ATTN_IMPL="eager")
    assert ec.MODEL_KWARGS["attn_implementation"] == "eager" and ec.ATTN_IMPL == "eager"


@pytest.mark.parametrize("name,value", [("GESTALT_EMBED_DTYPE", "half"), ("GESTALT_TF32", "yes"), ("GESTALT_ATTN_IMPL", "flash")])
def test_embed_knobs_reject_bad_values(monkeypatch, name, value):
    with pytest.raises(ValueError, match=name):
        fresh(monkeypatch, **{name: value})


def test_tf32_defaults_off_and_is_applied_to_both_switches(monkeypatch):
    t = fake_torch(monkeypatch)
    fresh(monkeypatch).apply_tf32()
    assert t.backends.cuda.matmul.allow_tf32 is False and t.backends.cudnn.allow_tf32 is False
    fresh(monkeypatch, GESTALT_TF32="1").apply_tf32()
    assert t.backends.cuda.matmul.allow_tf32 is True and t.backends.cudnn.allow_tf32 is True


def test_rerank_defaults_are_unchanged(monkeypatch):
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: False)
    assert gestalt_rank.rerank_device_kwargs() == ("cpu", {})
    assert gestalt_rank.rerank_dtype() == "default" and gestalt_rank.tf32_on() is False
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: True)
    assert gestalt_rank.rerank_device_kwargs() == ("cuda", {"model_kwargs": {"torch_dtype": "float16"}})
    assert gestalt_rank.rerank_dtype() == "float16"


@pytest.mark.parametrize("value", ["float16", "bfloat16", "float32"])
def test_rerank_dtype_resolves(monkeypatch, value):
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: True)
    monkeypatch.setenv("GESTALT_RERANK_DTYPE", value)
    assert gestalt_rank.rerank_device_kwargs()[1]["model_kwargs"]["torch_dtype"] == value
    assert gestalt_rank.rerank_dtype() == value


def test_rerank_attn_impl_joins_the_model_kwargs(monkeypatch):
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: True)
    monkeypatch.setenv("GESTALT_ATTN_IMPL", "sdpa")
    assert gestalt_rank.rerank_device_kwargs()[1]["model_kwargs"] == {"torch_dtype": "float16", "attn_implementation": "sdpa"}


@pytest.mark.parametrize("name,value", [("GESTALT_RERANK_DTYPE", "int8"), ("GESTALT_TF32", "2"), ("GESTALT_ATTN_IMPL", "flash")])
def test_rerank_knobs_reject_bad_values(monkeypatch, name, value):
    monkeypatch.setattr(gestalt_rank, "cuda_available", lambda: True)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        if name == "GESTALT_TF32":
            gestalt_rank.tf32_on()
        else:
            gestalt_rank.rerank_device_kwargs()


def test_rerank_tf32_is_applied(monkeypatch):
    t = fake_torch(monkeypatch)
    gestalt_rank.apply_tf32()
    assert t.backends.cuda.matmul.allow_tf32 is False
    monkeypatch.setenv("GESTALT_TF32", "1")
    gestalt_rank.apply_tf32()
    assert t.backends.cuda.matmul.allow_tf32 is True and t.backends.cudnn.allow_tf32 is True


def test_environment_records_the_precision_keys(monkeypatch):
    pytest.importorskip("sqlite_vec")
    pytest.importorskip("torch")
    pytest.importorskip("sentence_transformers")
    from conftest import HashEncoder

    import bench_stats as bs

    monkeypatch.setenv("GESTALT_RERANK_DTYPE", "bfloat16")
    model = HashEncoder()
    model.device = "cpu"
    env = bs.environment(model, None, batch_size=16)
    import gestalt_embed_config as ec

    assert env["embed_dtype"] == ec.EMBED_DTYPE and env["rerank_dtype"] == "bfloat16"
    assert env["tf32"] is ec.TF32 and env["attn_impl"] == ec.ATTN_IMPL
    assert env["embed_batch_size"] == 16 and env["embed_sort_order"] == "length-sorted"
    assert env["embed_profile"] == ec.PROFILE and env["embed_dim"] == ec.EMBED_DIM, "the existing keys stay"
    assert bs.environment(model, None)["embed_batch_size"] is None



def test_embed_normalize_knob_unit_normalises_at_full_width_and_defaults_off(monkeypatch):
    """GESTALT_EMBED_NORMALIZE=1 makes postprocess return unit vectors at the full width. Off, vectors come back untouched."""
    import importlib
    import sys

    import numpy as np

    monkeypatch.delenv("GESTALT_EMBED_DIM", raising=False)
    monkeypatch.delenv("GESTALT_EMBED_NORMALIZE", raising=False)
    sys.modules.pop("gestalt_embed_config", None)
    ec = importlib.import_module("gestalt_embed_config")
    assert ec.EMBED_NORMALIZE is False
    v = np.array([[3.0, 4.0] + [0.0] * (ec.FULL_DIM - 2)], dtype=np.float32)
    assert np.allclose(ec.postprocess(v), v)
    monkeypatch.setenv("GESTALT_EMBED_NORMALIZE", "1")
    sys.modules.pop("gestalt_embed_config", None)
    ec2 = importlib.import_module("gestalt_embed_config")
    out = ec2.postprocess(v)
    assert np.allclose(np.linalg.norm(out, axis=-1), 1.0) and np.allclose(out[0, :2], [0.6, 0.8])
    monkeypatch.setenv("GESTALT_EMBED_NORMALIZE", "yes")
    sys.modules.pop("gestalt_embed_config", None)
    with pytest.raises(ValueError):
        importlib.import_module("gestalt_embed_config")
    sys.modules.pop("gestalt_embed_config", None)
