"""Build-time patch: hold a strong reference to each queue worker task (2026-09-06).

Upstream getzep/graphiti#1574 (open): QueueService starts the per-group worker with a bare
`asyncio.create_task(...)` and keeps no reference to the task, so under the streamable-http
transport the event loop can garbage-collect the worker; `add_memory` then answers "queued" and
the episode is never processed. Python's asyncio docs require the caller to keep a reference for
exactly this reason. On 2026-09-05 three entries were logged as processed with zero nodes in the
graph, the signature of this loss. The patch stores the task in a module-level set and discards it
on completion. Same replace-verify-ast-check contract as patch-queue-heartbeat.py; the path can be
overridden with GESTALT_QS_PATH for the test.
"""
import ast
import os
import pathlib

QS = pathlib.Path(os.environ.get("GESTALT_QS_PATH", "/app/mcp/src/services/queue_service.py"))
src = QS.read_text()

old_spawn = "asyncio.create_task(self._process_episode_queue(group_id))"
new_spawn = ("_gestalt_worker = asyncio.create_task(self._process_episode_queue(group_id))\n"
             "            _gestalt_worker_tasks.add(_gestalt_worker)\n"
             "            _gestalt_worker.add_done_callback(_gestalt_worker_tasks.discard)")
assert src.count(old_spawn) == 1, "worker spawn line not found exactly once; upstream changed, re-derive the patch"
src = src.replace(old_spawn, new_spawn, 1)

old_import = "import asyncio\n"
new_import = ("import asyncio\n"
              "# gestalt patch (graphiti#1574): strong references to worker tasks, see patch-worker-ref.py\n"
              "_gestalt_worker_tasks: set = set()\n")
assert src.count(old_import) == 1, "import asyncio not found exactly once"
src = src.replace(old_import, new_import, 1)

ast.parse(src)
QS.write_text(src)
print("patched", QS)
