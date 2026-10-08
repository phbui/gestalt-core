"""Discrimination test for the queue-worker progress beat (gestalt, 2026-08-31).

Runs INSIDE the built image, because it exercises the patched queue_service.py and
the exact client libraries that image ships:

  docker run --rm -v "$PWD/graphiti/test-progress-beat.py:/tmp/t.py:ro" \
    --entrypoint /app/mcp/.venv/bin/python gestalt-graphiti-mcp:main /tmp/t.py

Red half asserts the bug: without _gestalt_install_progress_beat(), a completed
outbound HTTP response leaves /tmp/queue-heartbeat untouched, so an episode longer
than the healthcheck's 1200s limit makes the container kill its own PID 1 and
destroy the in-memory queue. Green half asserts the fix.

The httpx2 case is the one that matters and is why this file exists. The image ships
both httpx 0.28.1 and httpx2 2.10.0, and openai 3.2.0 imports httpx2 exclusively. The
first version of this patch hooked only httpx, passed a test written against httpx,
installed without error, and beat nothing during real episodes. A test that exercises
only the library the author assumed is in use proves nothing about the library that
actually is.
"""
import asyncio, os, pathlib, sys
import httpx2, httpx

SRC = pathlib.Path("/app/mcp/src/services/queue_service.py").read_text()
i = SRC.index("def _gestalt_touch_heartbeat")
ns = {}
exec("import pathlib\n" + SRC[i:], ns)
HB = pathlib.Path("/tmp/queue-heartbeat")

async def call(mod):
    t = mod.MockTransport(lambda req: mod.Response(200, json={"ok": True}))
    async with mod.AsyncClient(transport=t) as c:
        await c.get("http://llm.invalid/v1/chat/completions")

def stale():
    HB.touch(); os.utime(HB, (1_000_000, 1_000_000))
    return HB.stat().st_mtime

results = {}
for label, mod in (("httpx2", httpx2), ("httpx", httpx)):
    before = stale(); asyncio.run(call(mod))
    results[f"RED/{label}"] = HB.stat().st_mtime > before

ns["_gestalt_install_progress_beat"]()

for label, mod in (("httpx2", httpx2), ("httpx", httpx)):
    before = stale(); asyncio.run(call(mod))
    results[f"GREEN/{label}"] = HB.stat().st_mtime > before

for k, v in results.items():
    print(f"  {k:14s} heartbeat advanced = {v}")

ok = (results["RED/httpx2"] is False and results["RED/httpx"] is False
      and results["GREEN/httpx2"] is True and results["GREEN/httpx"] is True)
print("DISCRIMINATION:", "PASS — red without the fix on BOTH clients, green with it"
      if ok else "FAIL")
print("KEY: GREEN/httpx2 is the one that matters; openai 3.2.0 imports httpx2, not httpx.")
sys.exit(0 if ok else 1)
