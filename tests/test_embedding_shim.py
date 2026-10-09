"""In-process tests for the HTTP handler logic in tools/embedding-shim.py.

BaseHTTPRequestHandler normally requires a real socket connection to construct
(its __init__ calls handle() immediately). This builds a bare instance with
object.__new__ instead and fills in only the attributes do_GET/do_POST/_send
actually touch: self.path, self.headers (a real email.message.Message so
.get() works), self.rfile (io.BytesIO), self.wfile (io.BytesIO), and the three
response methods. No socket, no server, no model loaded: get_model() is
monkeypatched per test rather than importing sentence_transformers.
"""
from __future__ import annotations

import io
import json
import sys
from email.message import Message
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import _load


@pytest.fixture(scope="module")
def shim():
    return _load("embedding_shim", "tools/embedding-shim.py")


def _fake_handler(shim, path, body: bytes = b""):
    inst = object.__new__(shim.Handler)
    inst.path = path
    headers = Message()
    headers["Content-Length"] = str(len(body))
    inst.headers = headers
    inst.rfile = io.BytesIO(body)
    inst.wfile = io.BytesIO()
    inst._responses = []
    inst.send_response = lambda code: inst._responses.append(("status", code))
    inst.send_header = lambda k, v: inst._responses.append(("header", k, v))
    inst.end_headers = lambda: inst._responses.append(("end",))
    return inst


def _sent_json(inst):
    return json.loads(inst.wfile.getvalue().decode())


def _sent_status(inst):
    return next(v for kind, v in ((r[0], r[1]) for r in inst._responses) if kind == "status")


# ---------------------------------------------------------------------------
# _send: the shared response writer
# ---------------------------------------------------------------------------


def test_send_writes_status_and_json_body(shim):
    inst = _fake_handler(shim, "/whatever")
    shim.Handler._send(inst, 200, {"a": 1})
    assert _sent_status(inst) == 200
    assert _sent_json(inst) == {"a": 1}
    assert ("header", "Content-Type", "application/json") in inst._responses


# ---------------------------------------------------------------------------
# do_GET
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/v1/health"])
def test_do_get_health_returns_ok_and_model_name(shim, path):
    inst = _fake_handler(shim, path)
    inst.do_GET()
    assert _sent_status(inst) == 200
    body = _sent_json(inst)
    assert body["status"] == "ok"
    assert body["model"] == shim.MODEL_NAME


def test_do_get_unknown_path_returns_404(shim):
    inst = _fake_handler(shim, "/nope")
    inst.do_GET()
    assert _sent_status(inst) == 404


# ---------------------------------------------------------------------------
# do_POST
# ---------------------------------------------------------------------------


def test_do_post_unknown_path_returns_404(shim):
    inst = _fake_handler(shim, "/wrong", b"{}")
    inst.do_POST()
    assert _sent_status(inst) == 404


def test_do_post_rejects_non_string_inputs(shim):
    body = json.dumps({"input": [1, 2, 3]}).encode()
    inst = _fake_handler(shim, "/v1/embeddings", body)
    inst.do_POST()
    assert _sent_status(inst) == 400
    assert "only string inputs" in _sent_json(inst)["error"]


class _FakeVec:
    def __init__(self, values):
        self._values = values

    def tolist(self):
        return self._values


class _FakeModel:
    def __init__(self):
        self.calls = []

    def encode(self, inputs, normalize_embeddings=True):
        self.calls.append((list(inputs), normalize_embeddings))
        return [_FakeVec([float(len(s)), 0.0]) for s in inputs]


def test_do_post_embeds_a_single_string_input(shim, monkeypatch):
    model = _FakeModel()
    monkeypatch.setattr(shim, "get_model", lambda: model)
    body = json.dumps({"input": "hello"}).encode()
    inst = _fake_handler(shim, "/v1/embeddings", body)
    inst.do_POST()
    assert _sent_status(inst) == 200
    payload = _sent_json(inst)
    assert payload["object"] == "list"
    assert payload["model"] == shim.MODEL_NAME
    assert len(payload["data"]) == 1
    assert payload["data"][0]["embedding"] == [5.0, 0.0]
    assert model.calls == [(["hello"], True)]


def test_do_post_embeds_a_list_of_strings_and_echoes_model_name(shim, monkeypatch):
    model = _FakeModel()
    monkeypatch.setattr(shim, "get_model", lambda: model)
    body = json.dumps({"input": ["ab", "abcd"], "model": "custom-name"}).encode()
    inst = _fake_handler(shim, "/embeddings", body)
    inst.do_POST()
    payload = _sent_json(inst)
    assert payload["model"] == "custom-name"
    assert [d["index"] for d in payload["data"]] == [0, 1]
    assert [d["embedding"] for d in payload["data"]] == [[2.0, 0.0], [4.0, 0.0]]


def test_do_post_returns_500_on_unexpected_exception(shim, monkeypatch):
    def boom():
        raise RuntimeError("model load failed")

    monkeypatch.setattr(shim, "get_model", boom)
    body = json.dumps({"input": "x"}).encode()
    inst = _fake_handler(shim, "/v1/embeddings", body)
    inst.do_POST()
    assert _sent_status(inst) == 500
    assert "model load failed" in _sent_json(inst)["error"]


def test_the_shim_refuses_a_profile_it_cannot_serve(shim, monkeypatch):
    monkeypatch.setattr(shim.ec, "PROFILE", "qwen3-4b")
    with pytest.raises(SystemExit) as e:
        shim.require_nomic()
    assert "nomic" in str(e.value)


def test_the_shim_accepts_the_nomic_profile(shim, monkeypatch):
    monkeypatch.setattr(shim.ec, "PROFILE", "nomic")
    shim.require_nomic()


def test_the_shim_reports_the_configured_model(shim):
    assert shim.MODEL_NAME == shim.ec.MODEL_NAME
