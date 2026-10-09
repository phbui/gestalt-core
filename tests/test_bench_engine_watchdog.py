"""bench_engine.py embed phase: progress lines, the stall watchdog, the between-block GPU probe and GESTALT_EVAL_EMBED_TIMEOUT.

No GPU. A bag-of-words stub stands in for the model and a fake `torch` stands in for CUDA.
"""
from __future__ import annotations

import functools
import subprocess
import sys
import textwrap
import time
import types
import zlib
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals" / "retrieval"))
sys.path.insert(0, str(REPO / "tools"))
pytest.importorskip("sqlite_vec")
import bench_engine as E  # noqa: E402

DOCS = [(f"d{i}", f"alpha{i % 9} beta{i % 4} gamma") for i in range(21)]


class Ec:
    MODEL_NAME, MODEL_REVISION, PROFILE, EMBED_DIM, DOC_PREFIX, QUERY_PREFIX = "stub", "r1", "nomic", 16, "d: ", "q: "

    @staticmethod
    def postprocess(a):
        return a


class Model:
    """crc32 bag of words. `pause` seconds per encode call, slept in short steps so an interrupt can land."""
    device = "cpu"

    def __init__(self, pause: float = 0.0):
        self.pause = pause

    def encode(self, texts, batch_size=64, convert_to_numpy=True, show_progress_bar=False):
        end = time.monotonic() + self.pause
        while time.monotonic() < end:
            time.sleep(0.02)
        out = np.zeros((len(texts), 16), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in t.split():
                out[i, zlib.crc32(w.encode()) % 16] += 1.0
        return out


def prepare(tmp_path, model, name="c", batch_size=2):
    return E.prepare_corpus(tmp_path / name, name, lambda: iter(DOCS), model=model, ec=Ec, batch_size=batch_size, block_size=7,
                            rebuild=False, status=E.Status(None, "beir_bench: tiny", every_s=1e9))


def fake_torch(ones):
    cuda = types.SimpleNamespace(is_available=lambda: False)
    return types.SimpleNamespace(ones=ones, cuda=cuda)


class Tensor:
    def sum(self):
        return self

    def item(self):
        return 1.0


def test_progress_lines_appear_inside_a_block(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(E, "EmbedGuard", functools.partial(E.EmbedGuard, every=2))
    prepare(tmp_path, Model())
    lines = [ln for ln in capsys.readouterr().err.splitlines() if " embed block " in ln and ", docs " in ln]
    # 7 documents per block at batch 2 is 4 sub-batches, so a line after sub-batch 2 and 4 and one at the end of each block
    assert any(ln.startswith("beir_bench: tiny embed block 1/3, docs 4/21, ") for ln in lines)
    assert any(ln.startswith("beir_bench: tiny embed block 3/3, docs 21/21, ") and "s elapsed" in ln for ln in lines)
    assert len(lines) > 3


def test_a_healthy_run_writes_the_same_files_with_and_without_the_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTALT_EVAL_EMBED_TIMEOUT", "0")
    _, off = prepare(tmp_path / "off", Model())
    monkeypatch.delenv("GESTALT_EVAL_EMBED_TIMEOUT")
    _, on = prepare(tmp_path / "on", Model())
    off_files = {p.name: p.read_bytes() for p in sorted((tmp_path / "off" / "c" / "emb").iterdir())}
    on_files = {p.name: p.read_bytes() for p in sorted((tmp_path / "on" / "c" / "emb").iterdir())}
    assert off_files.keys() == on_files.keys() and off_files
    assert off_files == on_files
    got = np.concatenate([np.asarray(b) for _, b in on.iter_blocks(len(DOCS))])
    np.testing.assert_array_equal(got, Model().encode(["d: " + t for _, t in DOCS]))


@pytest.mark.parametrize("raw", ["abc", "-1", "nan"])
def test_a_bad_embed_timeout_is_a_value_error(monkeypatch, raw):
    monkeypatch.setenv("GESTALT_EVAL_EMBED_TIMEOUT", raw)
    with pytest.raises(ValueError, match="GESTALT_EVAL_EMBED_TIMEOUT"):
        E.embed_timeout()


def test_embed_timeout_defaults_to_600_and_takes_zero(monkeypatch):
    monkeypatch.delenv("GESTALT_EVAL_EMBED_TIMEOUT", raising=False)
    assert E.embed_timeout() == 600.0
    monkeypatch.setenv("GESTALT_EVAL_EMBED_TIMEOUT", "0")
    assert E.embed_timeout() == 0.0


def test_the_exit_code_matches_the_query_stall_code():
    import run_retrieval_evals as harness
    assert E.EXIT_STALL == harness.EXIT_QUERY_STALL == 4


def test_a_sub_batch_past_the_timeout_exits_4_with_the_stall_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GESTALT_EVAL_EMBED_TIMEOUT", "1")
    monkeypatch.setattr(E, "EmbedGuard", functools.partial(E.EmbedGuard, poll=0.1, grace=60.0, exit_fn=lambda code: pytest.fail("forced exit")))
    with pytest.raises(SystemExit) as stop:
        prepare(tmp_path, Model(pause=3.0))
    assert stop.value.code == 4
    err = capsys.readouterr().err
    assert "embed stall: no sub-batch finished in 1s, last block 0, docs 0" in err


def test_a_hung_call_is_ended_by_os_exit_after_the_grace(tmp_path):
    """The model call never returns to Python, so only the watchdog thread's os._exit can end the process."""
    script = tmp_path / "hang.py"
    script.write_text(textwrap.dedent(f"""
        import functools, sys, time
        sys.path[:0] = [{str(REPO / "evals" / "retrieval")!r}, {str(REPO / "tools")!r}, {str(REPO / "tests")!r}]
        import bench_engine as E
        from test_bench_engine_watchdog import Ec, Model, DOCS
        from pathlib import Path
        E.EmbedGuard = functools.partial(E.EmbedGuard, poll=0.1, grace=1.0)
        class Hung(Model):
            def encode(self, texts, **kw):
                time.sleep(120)
        E.prepare_corpus(Path({str(tmp_path / "w")!r}), "c", lambda: iter(DOCS), model=Hung(), ec=Ec, batch_size=2, block_size=7,
                         rebuild=False, status=E.Status(None, "beir_bench: tiny", every_s=1e9))
    """))
    t0 = time.monotonic()
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60,
                          env={"GESTALT_EVAL_EMBED_TIMEOUT": "1", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert done.returncode == 4, done.stderr
    assert "embed stall: no sub-batch finished in" in done.stderr
    assert time.monotonic() - t0 < 30


def cuda_model():
    m = Model()
    m.device = "cuda:0"
    return m


def test_the_probe_after_a_block_exits_4_when_cuda_is_gone(tmp_path, monkeypatch, capsys):
    def dead(*_a, **_k):
        raise RuntimeError("CUDA error: unspecified launch failure")
    monkeypatch.setitem(sys.modules, "torch", fake_torch(dead))
    monkeypatch.setattr(E, "EmbedGuard", functools.partial(E.EmbedGuard, grace=60.0, exit_fn=lambda code: pytest.fail("forced exit")))
    with pytest.raises(SystemExit) as stop:
        prepare(tmp_path, cuda_model())
    assert stop.value.code == 4
    assert "GPU lost after block 1: CUDA error: unspecified launch failure" in capsys.readouterr().err


def test_a_probe_that_hangs_exits_4(tmp_path, monkeypatch, capsys):
    def slow(*_a, **_k):
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            time.sleep(0.02)
        return Tensor()
    monkeypatch.setitem(sys.modules, "torch", fake_torch(slow))
    monkeypatch.setattr(E, "PROBE_TIMEOUT", 0.5)
    monkeypatch.setattr(E, "EmbedGuard", functools.partial(E.EmbedGuard, poll=0.1, grace=60.0, exit_fn=lambda code: pytest.fail("forced exit")))
    with pytest.raises(SystemExit) as stop:
        prepare(tmp_path, cuda_model())
    assert stop.value.code == 4
    assert "GPU lost after block 1" in capsys.readouterr().err


def test_a_healthy_probe_changes_nothing_and_an_unrelated_runtime_error_passes_through(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "torch", fake_torch(lambda *a, **k: calls.append(k) or Tensor()))
    _, store = prepare(tmp_path, cuda_model())
    assert len(calls) == 3 and calls[0] == {"device": "cuda:0"}
    got = np.concatenate([np.asarray(b) for _, b in store.iter_blocks(len(DOCS))])
    np.testing.assert_array_equal(got, Model().encode(["d: " + t for _, t in DOCS]))

    def odd(*_a, **_k):
        raise RuntimeError("something else")
    monkeypatch.setitem(sys.modules, "torch", fake_torch(odd))
    with pytest.raises(RuntimeError, match="something else"):
        prepare(tmp_path, cuda_model(), "d")


def test_a_cpu_model_is_never_probed(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", fake_torch(lambda *a, **k: pytest.fail("probed")))
    prepare(tmp_path, Model())
