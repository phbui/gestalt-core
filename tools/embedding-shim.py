#!/usr/bin/env python3
"""OpenAI-compatible /v1/embeddings shim backed by local nomic-embed-text-v1.5.

Serves the Graphiti MCP container (gestalt-graphiti) so the temporal memory
layer needs no OpenAI API key. Stdlib HTTP server only — no fastapi/uvicorn.

Bind address defaults to the docker0 bridge (172.17.0.1) so only local
containers and the host can reach it; no auth is implemented, so never bind
this to a routable interface.

Run:  python3 tools/embedding-shim.py [--host 172.17.0.1] [--port 8201]
Unit: ~/.config/systemd/user/gestalt-embed-shim.service
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gestalt_embed_config as ec

MODEL_NAME = ec.MODEL_NAME


def require_nomic() -> None:
    """The shim embeds raw text with no query or document prefix and always L2-normalises. That is the nomic recipe for Graphiti.
    Any other profile would serve vectors from the wrong space, so the shim refuses to start on one."""
    if ec.PROFILE != "nomic":
        raise SystemExit(f"embedding-shim serves the nomic profile only, but GESTALT_EMBED_PROFILE={ec.PROFILE!r}. Unset it and start again.")

_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            # Same pins as the index builder: the weights by revision and the remote modelling code by
            # commit, so the shim can never load code or weights the benchmark did not run.
            _model = SentenceTransformer(ec.MODEL_NAME, revision=ec.MODEL_REVISION, trust_remote_code=ec.TRUST_REMOTE_CODE,
                                         model_kwargs=ec.MODEL_KWARGS, tokenizer_kwargs=ec.TOKENIZER_KWARGS)
        return _model


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/health", "/v1/health"):
            self._send(200, {"status": "ok", "model": MODEL_NAME})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/v1/embeddings", "/embeddings"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))
            inputs = req.get("input", [])
            if isinstance(inputs, str):
                inputs = [inputs]
            # OpenAI clients may send token arrays; only strings are supported here.
            if not all(isinstance(i, str) for i in inputs):
                self._send(400, {"error": "only string inputs supported"})
                return
            model = get_model()
            with _model_lock:
                vecs = model.encode(inputs, normalize_embeddings=True)
            self._send(
                200,
                {
                    "object": "list",
                    "model": req.get("model", MODEL_NAME),
                    "data": [
                        {"object": "embedding", "index": i, "embedding": v.tolist()}
                        for i, v in enumerate(vecs)
                    ],
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                },
            )
        except Exception as e:  # noqa: BLE001 - surface any failure as a 500 body
            self._send(500, {"error": str(e)})

    def log_message(self, format, *args):
        pass  # keep journal quiet; errors surface in response bodies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="172.17.0.1")
    ap.add_argument("--port", type=int, default=8201)
    args = ap.parse_args()
    require_nomic()
    get_model()  # load at startup so the first request isn't slow
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"embedding-shim serving {MODEL_NAME} on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
