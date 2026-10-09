"""GESTALT_GPU_DUTY: a duty cycle that pauses after every model call so the average GPU draw stays under the clamp ceiling."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import gestalt_rank  # noqa: E402


class Model:
    def __init__(self):
        self.calls = 0

    def predict(self, pairs, **kw):
        self.calls += 1
        time.sleep(0.02)
        return [0.0] * len(pairs)


def test_default_duty_is_one_and_adds_no_pause(monkeypatch):
    monkeypatch.delenv("GESTALT_GPU_DUTY", raising=False)
    assert gestalt_rank.gpu_duty() == 1.0
    assert gestalt_rank.duty_sleep(5.0) == 0.0
    m = Model()
    gestalt_rank.throttle_calls(m, "predict")
    assert not getattr(m.predict, "_gestalt_throttled", False)


def test_duty_below_one_pauses_in_proportion(monkeypatch):
    monkeypatch.setenv("GESTALT_GPU_DUTY", "0.5")
    pauses = []
    monkeypatch.setattr(gestalt_rank, "duty_sleep", lambda busy, duty=None: pauses.append(busy) or 0.0)
    m = Model()
    gestalt_rank.throttle_calls(m, "predict")
    gestalt_rank.throttle_calls(m, "predict")  # idempotent: one wrapper, not two
    out = m.predict([("q", "d")] * 3)
    assert out == [0.0, 0.0, 0.0] and m.calls == 1
    assert len(pauses) == 1 and pauses[0] >= 0.02  # the pause is computed from the measured busy time


def test_the_pause_is_the_busy_time_scaled_by_the_duty(monkeypatch):
    monkeypatch.setenv("GESTALT_GPU_DUTY", "0.5")
    slept = []
    monkeypatch.setattr(gestalt_rank.time, "sleep", lambda s: slept.append(s))
    assert abs(gestalt_rank.duty_sleep(2.0) - 2.0) < 1e-9  # duty 0.5: pause equals the busy time
    assert abs(gestalt_rank.duty_sleep(1.0, duty=0.25) - 3.0) < 1e-9
    assert slept == [2.0, 3.0]


def test_duty_is_clamped_and_bad_values_fall_back(monkeypatch):
    monkeypatch.setenv("GESTALT_GPU_DUTY", "0.001")
    assert gestalt_rank.gpu_duty() == 0.05
    monkeypatch.setenv("GESTALT_GPU_DUTY", "7")
    assert gestalt_rank.gpu_duty() == 1.0
    monkeypatch.setenv("GESTALT_GPU_DUTY", "nan")
    assert gestalt_rank.gpu_duty() == 1.0


def test_index_builder_paces_its_embedding_model_under_a_duty_cycle(monkeypatch):
    """The builder's model.encode is wrapped by throttle_calls when GESTALT_GPU_DUTY is under 1, and left alone at 1.0."""
    import importlib.util
    import pathlib
    import sys

    tools = pathlib.Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools))
    spec = importlib.util.spec_from_file_location("gib_duty_test", tools / "gestalt-index-builder.py")
    gib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gib)

    class FakeModel:
        def __init__(self, *a, **kw):
            pass

        def encode(self, texts, **kw):
            return [[0.0] * 4 for _ in texts]

    monkeypatch.setenv("GESTALT_GPU_DUTY", "0.5")
    m = gib._load_embedding_model(FakeModel, 3)
    assert getattr(m.encode, "_gestalt_throttled", False) is True
    monkeypatch.setenv("GESTALT_GPU_DUTY", "1.0")
    m = gib._load_embedding_model(FakeModel, 3)
    assert getattr(m.encode, "_gestalt_throttled", False) is False


def test_index_builder_slices_the_corpus_encode_under_a_duty_cycle(monkeypatch):
    """One encode call at duty 1.0, one call per GESTALT_EMBED_CALL_DOCS slice under a duty cycle, same rows either way."""
    import importlib.util
    import pathlib
    import sys

    import numpy as np

    tools = pathlib.Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools))
    spec = importlib.util.spec_from_file_location("gib_duty_test2", tools / "gestalt-index-builder.py")
    gib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gib)
    calls = []

    class FakeModel:
        def encode(self, texts, **kw):
            calls.append(len(texts))
            return np.array([[float(len(t)), 1.0] for t in texts], dtype="float32")

    monkeypatch.setattr(gib._ec, "postprocess", lambda a: a)
    monkeypatch.setattr(gib, "EMBED_BATCH_SIZE", 2)
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    monkeypatch.setenv("GESTALT_GPU_DUTY", "1.0")
    one = gib._encode_paced(FakeModel(), texts)
    assert calls == [5]
    calls.clear()
    monkeypatch.setenv("GESTALT_GPU_DUTY", "0.5")
    monkeypatch.setenv("GESTALT_EMBED_CALL_DOCS", "2")
    sliced = gib._encode_paced(FakeModel(), texts)
    assert calls == [2, 2, 1]
    assert np.array_equal(one, sliced)
