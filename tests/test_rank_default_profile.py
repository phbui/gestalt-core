"""The ranking knobs follow the config module's default profile when GESTALT_EMBED_PROFILE is unset.

Until 2026-10-12 gestalt_rank fell back to the literal nomic, so a process with the variable unset embedded with the shipped default,
stella, and fused and reranked with nomic's knobs. These tests load each module fresh so no cached table hides the fallback."""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
KNOBS = ("GESTALT_EMBED_PROFILE", "GESTALT_FUSION", "GESTALT_FUSION_ALPHA", "GESTALT_FUSION_NORM", "GESTALT_RERANK_MODEL",
         "GESTALT_RERANK_BLEND", "GESTALT_EMBED_NORMALIZE")


def _fresh(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clean(monkeypatch):
    for k in KNOBS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.syspath_prepend(str(REPO / "tools"))


def test_unset_profile_resolves_to_the_config_default(clean):
    gr = _fresh("gr_default_profile", "tools/gestalt_rank.py")
    ec = _fresh("ec_default_profile", "tools/gestalt_embed_config.py")
    assert gr.current_profile() == ec.DEFAULT_PROFILE == ec.PROFILE == "stella-400m"
    assert gr.profile_knob_defaults() == ec.PROFILES[ec.DEFAULT_PROFILE]["KNOB_DEFAULTS"]
    assert (gr.fusion_mode(), gr.fusion_alpha(), gr.rerank_alias(), gr.rerank_blend()) == ("convex", 0.0, "qwen3-0.6b", 0.5)
    assert gr.dense_is_unit() is True and ec.EMBED_NORMALIZE is True


def test_a_named_profile_still_wins(clean, monkeypatch):
    monkeypatch.setenv("GESTALT_EMBED_PROFILE", "nomic")
    gr = _fresh("gr_named_profile", "tools/gestalt_rank.py")
    assert gr.current_profile() == "nomic"
    assert (gr.fusion_mode(), gr.fusion_alpha(), gr.rerank_alias(), gr.rerank_blend()) == ("rrf", 0.5, "bge", 0.0)
    assert gr.dense_is_unit() is False
    assert gr.current_profile("Stella-400M") == "stella-400m"


def test_the_eval_runner_banks_the_profiles_fusion(clean):
    rr = _fresh("rre_default_profile", "evals/retrieval/run_retrieval_evals.py")
    assert rr.default_config()["fusion"] == "convex" and rr.default_config()["alpha"] == 0.0
    assert rr.shipped_bank_configs()["default"]["fusion"] == rr.shipped_bank_configs()["hub"]["fusion"] == "convex"
    cfg = rr.run_config(rr.resolve_opts())
    assert rr.config_diff(cfg, None) == [], "the knobs-off run is the shipped configuration"
    assert rr.config_diff(cfg, {**cfg, "embed_profile": "some-other-profile"}) == ["embed_profile"], "a run under another profile is drift"
    assert rr.config_diff(cfg, {k: v for k, v in cfg.items() if k != "embed_profile"}) == [], "a bank that never recorded a profile cannot say"


def test_the_eval_runner_under_nomic_banks_rrf(clean, monkeypatch):
    monkeypatch.setenv("GESTALT_EMBED_PROFILE", "nomic")
    rr = _fresh("rre_nomic_profile", "evals/retrieval/run_retrieval_evals.py")
    assert rr.default_config()["fusion"] == "rrf" and rr.default_config()["alpha"] is None
    cfg = rr.run_config(rr.resolve_opts())
    assert rr.config_diff(cfg, None) == []
