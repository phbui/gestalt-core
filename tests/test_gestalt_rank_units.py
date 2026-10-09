"""Unit tests for the small pure helpers in tools/gestalt_rank.py: knob parsing, min-max scaling, rerank text and the idle unload."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import gestalt_rank as gr  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(gr, "_warned", set())


def test_float_env_returns_the_default_when_unset_or_blank(monkeypatch):
    monkeypatch.delenv("X_KNOB", raising=False)
    assert gr._float_env("X_KNOB", 0.25, 0.0, 1.0) == 0.25
    monkeypatch.setenv("X_KNOB", "   ")
    assert gr._float_env("X_KNOB", 0.25, 0.0, 1.0) == 0.25


@pytest.mark.parametrize("raw,want", [("0.7", 0.7), (" 0.7 ", 0.7), ("7", 1.0), ("-3", 0.0), ("0", 0.0)])
def test_float_env_parses_and_clamps_into_the_range(monkeypatch, raw, want):
    monkeypatch.setenv("X_KNOB", raw)
    assert gr._float_env("X_KNOB", 0.25, 0.0, 1.0) == want


@pytest.mark.parametrize("raw", ["abc", "nan", "inf", "-inf", "1,5"])
def test_float_env_falls_back_to_the_default_on_an_invalid_value_and_warns_once(monkeypatch, capsys, raw):
    monkeypatch.setenv("X_KNOB", raw)
    assert gr._float_env("X_KNOB", 0.25, 0.0, 1.0) == 0.25
    assert gr._float_env("X_KNOB", 0.25, 0.0, 1.0) == 0.25
    assert capsys.readouterr().err.count("X_KNOB") == 1


def test_int_env_returns_the_default_when_unset(monkeypatch):
    monkeypatch.delenv("X_KNOB", raising=False)
    assert gr._int_env("X_KNOB", 40, 1, 200) == 40


@pytest.mark.parametrize("raw,want", [("12", 12), (" 12 ", 12), ("0", 1), ("-5", 1), ("9999", 200)])
def test_int_env_parses_and_clamps_into_the_range(monkeypatch, raw, want):
    monkeypatch.setenv("X_KNOB", raw)
    assert gr._int_env("X_KNOB", 40, 1, 200) == want


@pytest.mark.parametrize("raw", ["forty", "4.5", "1e3", "0x10"])
def test_int_env_falls_back_to_the_default_on_an_invalid_value_and_warns_once(monkeypatch, capsys, raw):
    monkeypatch.setenv("X_KNOB", raw)
    assert gr._int_env("X_KNOB", 40, 1, 200) == 40
    assert gr._int_env("X_KNOB", 40, 1, 200) == 40
    assert capsys.readouterr().err.count("X_KNOB") == 1


def test_the_named_knobs_default_when_nothing_is_set(monkeypatch):
    for name in ("GESTALT_FUSION", "GESTALT_FUSION_ALPHA", "GESTALT_RERANK", "GESTALT_RERANK_MODEL", "GESTALT_FTS_STOPWORDS"):
        monkeypatch.delenv(name, raising=False)
    assert (gr.fusion_mode(), gr.fusion_alpha(), gr.rerank_mode(), gr.rerank_alias(), gr.stopwords_on()) == ("rrf", 0.5, "auto", "bge", False)


def test_the_named_knobs_read_their_variables(monkeypatch):
    monkeypatch.setenv("GESTALT_FUSION", "CONVEX")
    monkeypatch.setenv("GESTALT_FUSION_ALPHA", "0.3")
    monkeypatch.setenv("GESTALT_RERANK", "On")
    monkeypatch.setenv("GESTALT_RERANK_MODEL", "  qwen3-0.6b ")
    assert (gr.fusion_mode(), gr.fusion_alpha(), gr.rerank_mode(), gr.rerank_alias()) == ("convex", 0.3, "on", "qwen3-0.6b")


def test_a_blank_rerank_model_means_the_default_alias(monkeypatch):
    monkeypatch.setenv("GESTALT_RERANK_MODEL", "   ")
    assert gr.rerank_alias() == "bge"


@pytest.mark.parametrize("raw,want", [("on", True), ("1", True), ("TRUE", True), ("yes", True), ("off", False), ("0", False), ("false", False), ("no", False), ("", False)])
def test_stopwords_on_accepts_the_documented_spellings(monkeypatch, raw, want):
    monkeypatch.setenv("GESTALT_FTS_STOPWORDS", raw)
    assert gr.stopwords_on() is want


def test_stopwords_on_treats_an_unknown_value_as_off_and_says_so(monkeypatch, capsys):
    monkeypatch.setenv("GESTALT_FTS_STOPWORDS", "perhaps")
    assert gr.stopwords_on() is False
    assert "GESTALT_FTS_STOPWORDS" in capsys.readouterr().err


@pytest.mark.parametrize("raw,want", [("maybe", "auto"), ("off", "off"), ("ON", "on")])
def test_rerank_mode_keeps_only_the_three_modes(monkeypatch, raw, want):
    monkeypatch.setenv("GESTALT_RERANK", raw)
    assert gr.rerank_mode() == want


def test_fusion_mode_rejects_an_unknown_name(monkeypatch):
    monkeypatch.setenv("GESTALT_FUSION", "rff")
    assert gr.fusion_mode() == "rrf"


# --- _minmax -------------------------------------------------------------------------------------


def test_minmax_scales_the_smallest_to_zero_and_the_largest_to_one():
    assert gr._minmax([2.0, 4.0, 3.0]) == [0.0, 1.0, 0.5]


def test_minmax_handles_negative_values_and_keeps_order():
    got = gr._minmax([-10.0, -5.0, 0.0])
    assert got == [0.0, 0.5, 1.0]


def test_minmax_of_equal_values_is_one_half_not_full_credit_and_not_a_division_by_zero():
    """A pool with one distinct score says nothing about relevance, so every item gets 0.5 (fusion.py's degenerate policy)."""
    assert gr._minmax([3.0, 3.0, 3.0]) == [0.5, 0.5, 0.5]
    assert gr._minmax([7.0]) == [0.5]


# --- rerank_text ---------------------------------------------------------------------------------


def test_rerank_text_is_title_heading_newline_content():
    row = {"title": "Deploy notes", "slug": "deploy", "heading": "Steps", "content": "run the script"}
    assert gr.rerank_text(row, 100) == "Deploy notes — Steps\nrun the script"


def test_rerank_text_falls_back_to_the_slug_when_the_title_is_empty_or_null():
    assert gr.rerank_text({"title": "", "slug": "deploy", "heading": "H", "content": "c"}, 100).startswith("deploy — H")
    assert gr.rerank_text({"title": None, "slug": "deploy", "heading": "H", "content": "c"}, 100).startswith("deploy — H")


def test_rerank_text_cuts_the_content_but_not_the_title_line():
    out = gr.rerank_text({"title": "T", "heading": "H", "content": "abcdefghij"}, 4)
    assert out == "T — H\nabcd"


def test_rerank_text_survives_missing_columns():
    assert gr.rerank_text({"content": "body"}, 100) == " — \nbody"
    assert gr.rerank_text({}, 100) == " — \n"


# --- unload_reranker -----------------------------------------------------------------------------


class _Clock:
    def __init__(self, now):
        self.now = now

    def monotonic(self):
        return self.now


@pytest.fixture
def loaded(monkeypatch):
    models = {"bge": object(), "qwen3-0.6b": object()}
    monkeypatch.setattr(gr, "_rerankers", models)
    monkeypatch.setattr(gr, "_rerank_last_used", 100.0)
    clock = _Clock(130.0)
    monkeypatch.setattr(gr.time, "monotonic", clock.monotonic)
    return clock


def test_unload_with_nothing_loaded_does_nothing(monkeypatch):
    monkeypatch.setattr(gr, "_rerankers", {})
    assert gr.unload_reranker(force=True) is False


def test_unload_without_an_idle_limit_or_force_keeps_the_models(loaded):
    assert gr.unload_reranker() is False and gr.rerank_loaded()


def test_unload_keeps_the_models_until_they_have_been_idle_long_enough(loaded):
    assert gr.unload_reranker(idle_s=60) is False and gr.rerank_loaded()
    loaded.now = 161.0
    assert gr.unload_reranker(idle_s=60) is True
    assert not gr.rerank_loaded()


def test_force_drops_every_model_at_once(loaded):
    assert gr.unload_reranker(force=True) is True
    assert not gr.rerank_loaded()
    assert gr.unload_reranker(force=True) is False


def test_is_hub_reads_the_fleet_env_and_names_no_private_host(monkeypatch):
    """The file is hashed into every result and ships as is, so the fallback hub name is the neutral word hub."""
    import socket

    import gestalt_rank as gr

    monkeypatch.setattr(socket, "gethostname", lambda: "box.example")
    monkeypatch.delenv("FLEET_HUB_NAME", raising=False)
    monkeypatch.setenv("FLEET_HUB", "box.example")
    assert gr.is_hub() is True
    monkeypatch.setenv("FLEET_HUB", "other.example")
    assert gr.is_hub() is False
    monkeypatch.delenv("FLEET_HUB", raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "hub")
    assert gr.is_hub() is True
    import inspect

    assert "hub" not in inspect.getsource(gr.is_hub)
