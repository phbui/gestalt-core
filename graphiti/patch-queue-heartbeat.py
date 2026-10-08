"""Build-time patch: queue-worker heartbeat for self-healing (2026-08-30).

The episode queue worker is an in-process asyncio task; when it dies (observed
2026-08-29: Ollama wedges made it exit silently) the container stays "healthy"
because /health only tests the HTTP layer, and episodes queue forever into a
dead consumer. This patch makes the worker touch /tmp/queue-heartbeat every
loop iteration AND once per idle minute, so the compose healthcheck can treat
a stale heartbeat as container-unhealthy and kill PID 1 — docker's restart
policy then revives the whole server with fresh workers. Applied in the
Dockerfile with the same replace-verify-ast-check contract as the host patch.

2026-08-31: added a third beat, on every completed outbound HTTP response, after
the container killed itself mid-episode and destroyed its own queue. The two
original beats only fire when the queue is IDLE, so a legitimately slow episode
read as a wedge. See _gestalt_install_progress_beat for the full failure trace
and why a progress beat rather than an asyncio ticker is the correct signal.
"""
import ast
import pathlib

QS = pathlib.Path("/app/mcp/src/services/queue_service.py")
src = QS.read_text()

old_get = """                # Get the next episode processing function from the queue
                # This will wait if the queue is empty
                process_func = await self._episode_queues[group_id].get()"""
new_get = """                # Get the next episode processing function from the queue
                # This will wait if the queue is empty.
                # gestalt heartbeat patch: touch the beat file each iteration and
                # once per idle minute, so a dead worker is detectable from outside
                # the process (compose healthcheck) while an idle-empty queue is not
                # mistaken for a dead one.
                while True:
                    try:
                        process_func = await asyncio.wait_for(
                            self._episode_queues[group_id].get(), timeout=55
                        )
                        break
                    except asyncio.TimeoutError:
                        _gestalt_touch_heartbeat()
                _gestalt_touch_heartbeat()"""
assert old_get in src, "queue get block not found — upstream changed, re-derive the patch"
src = src.replace(old_get, new_get, 1)

old_init = """        self._graphiti_client = graphiti_client
        logger.info('Queue service initialized with graphiti client')"""
new_init = """        self._graphiti_client = graphiti_client
        logger.info('Queue service initialized with graphiti client')
        # gestalt heartbeat patch: idle beat. Workers only exist after the first
        # episode, so a worker-less (or genuinely idle) service must still beat or
        # the healthcheck would kill a healthy container (observed crash-loop,
        # 2026-08-30). Touch ONLY when every queue is empty AND has no unfinished
        # task: a stuck episode leaves unfinished_tasks > 0 with qsize 0, which
        # must NOT beat — that is exactly the wedge the healthcheck exists to
        # catch. _unfinished_tasks is private but this image is version-pinned.
        asyncio.create_task(self._gestalt_idle_beat())
        _gestalt_install_progress_beat()

    async def _gestalt_idle_beat(self) -> None:
        while True:
            try:
                idle = all(
                    q.qsize() == 0 and getattr(q, '_unfinished_tasks', 0) == 0
                    for q in self._episode_queues.values()
                )
                if idle:
                    _gestalt_touch_heartbeat()
            except Exception:
                pass
            await asyncio.sleep(60)"""
assert old_init in src, "initialize() body not found — upstream changed, re-derive"
src = src.replace(old_init, new_init, 1)

helper = '''

def _gestalt_touch_heartbeat() -> None:
    """Best-effort liveness beat for the compose healthcheck (gestalt patch)."""
    try:
        pathlib.Path("/tmp/queue-heartbeat").touch()
    except OSError:
        pass


def _gestalt_install_progress_beat() -> None:
    """Beat on every completed outbound HTTP response (gestalt patch, 2026-08-31).

    The idle beat above deliberately refuses to beat while an episode is in
    flight, so that a wedged episode goes stale and the healthcheck kills PID 1.
    That makes a merely SLOW episode indistinguishable from a stuck one. On
    2026-08-31 an episode started at 04:37:14 UTC was still running at 04:57:28;
    the beat was 1214s stale against the healthcheck's 1200s limit, so the
    container SIGKILLed its own PID 1 and destroyed both that episode and the
    entire in-memory queue behind it. Episodes here legitimately run 150-712s and
    the long ones exceed 1200s, so this was unwinnable for large entries.

    Beating on a completed response measures FORWARD PROGRESS rather than mere
    liveness, which is the distinction the watchdog actually needs. A slow episode
    issues many LLM and embedder calls and keeps beating. A wedge (hung call, dead
    worker, stalled Ollama) produces no responses, goes stale, and is still killed.
    A plain asyncio ticker would NOT preserve that: it keeps beating while awaiting
    a hung call, silently disarming the watchdog.

    Only LLM and embedder traffic flows through httpx here; FalkorDB uses the redis
    protocol and the healthcheck uses curl, so this cannot beat on unrelated I/O.
    """
    hooked = []
    # This image ships BOTH httpx 0.28.1 and httpx2 2.10.0, and openai 3.2.0 imports
    # httpx2 exclusively (its "HTTP Request:" log lines carry the httpx2 logger name).
    # Hooking only httpx attaches to a library the LLM path never touches and beats
    # nothing, silently — the first build of this patch did exactly that and looked
    # fine until the heartbeat was watched during a live episode. Hook every client
    # present, and log which, so a future SDK swap is visible instead of silent.
    for _name in ("httpx2", "httpx"):
        try:
            _mod = __import__(_name)
        except Exception:
            continue
        _cls = getattr(_mod, "AsyncClient", None)
        if _cls is None or getattr(_cls, "_gestalt_beat_installed", False):
            continue

        def _wrap(_orig):
            async def _send(self, *args, **kwargs):
                response = await _orig(self, *args, **kwargs)
                _gestalt_touch_heartbeat()
                return response
            return _send

        _cls.send = _wrap(_cls.send)
        _cls._gestalt_beat_installed = True
        hooked.append(_name)
    try:
        import logging
        logging.getLogger("graphiti_mcp_server").info(
            "gestalt progress beat installed on: %s", ", ".join(hooked) or "NOTHING")
    except Exception:
        pass
'''
assert "_gestalt_install_progress_beat()" in src, "progress beat not installed in initialize()"
assert "import asyncio" in src
if "import pathlib" not in src:
    src = src.replace("import asyncio", "import asyncio\nimport pathlib", 1)
src += helper
QS.write_text(src)
ast.parse(QS.read_text())
print("queue_service.py patched + parses")
