#!/usr/bin/env python3
"""patch-worker-ref.py discriminates: applied to the upstream spawn shape it keeps a strong reference;
applied to a file without that shape it refuses rather than silently doing nothing."""
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
PATCH = HERE / "patch-worker-ref.py"

UPSTREAM_SHAPE = '''import asyncio
import logging

class QueueService:
    def __init__(self):
        self._queue_workers = {}
        self._episode_queues = {}

    async def _process_episode_queue(self, group_id: str) -> None:
        pass

    async def add(self, group_id):
        if not self._queue_workers.get(group_id, False):
            asyncio.create_task(self._process_episode_queue(group_id))

        return self._episode_queues[group_id].qsize()
'''


def run(text: str):
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "queue_service.py"
        p.write_text(text)
        r = subprocess.run([sys.executable, str(PATCH)], env=dict(os.environ, GESTALT_QS_PATH=str(p)), capture_output=True, text=True)
        return r.returncode, p.read_text(), r.stderr


rc, out, err = run(UPSTREAM_SHAPE)
assert rc == 0, err
assert "_gestalt_worker_tasks: set = set()" in out
assert "_gestalt_worker = asyncio.create_task(self._process_episode_queue(group_id))" in out
assert "_gestalt_worker.add_done_callback(_gestalt_worker_tasks.discard)" in out
assert "asyncio.create_task(self._process_episode_queue(group_id))\n\n        return" not in out
compile(out, "queue_service.py", "exec")

rc, out, err = run(UPSTREAM_SHAPE.replace("asyncio.create_task(self._process_episode_queue(group_id))", "self._spawn(group_id)"))
assert rc != 0 and "not found" in err, "patch must refuse when the upstream shape is gone"
print("ok: patch-worker-ref applies to the upstream shape and refuses any other")
