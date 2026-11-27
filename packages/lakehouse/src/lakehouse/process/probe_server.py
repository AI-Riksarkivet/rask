"""A process's probes, served from a thread and a port of their own.

A probe answered by the loop that serves requests waits behind them. Measured 2026-11-23 on the real catalog under
uvicorn with one CPU ([[XC-204]]): 300 requests arriving at once held ``/livez`` and ``/readyz`` for 2.2 to 2.5 s on
the serving port, past the kubelet's timeouts, while the same probes on a second server in the same process answered
in under 0.3 s. Three such waits in a row restart a replica that is serving correctly.

So the app factory (`lakehouse.service_app.factory`) serves a service's probes here when it is given a port, and a
worker that serves no API serves its own the same way. The probes read what they read on the serving port: the
lifecycle flags and the service's readiness check, which answers from what the process holds and reads nothing. Served
from another loop, ``/livez`` says nothing of the serving loop by itself, so that loop marks a beat each second
(:class:`LoopBeat`) and ``/livez`` fails once the last one is :data:`STALE_SECONDS` old.
"""

import asyncio
import threading
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from time import monotonic
from typing import Final

import uvicorn
from fastapi import FastAPI

from lakehouse.process.correlation import FRAMEWORK_TELEMETRY_OFF
from lakehouse.process.probes import ReadyCheck, make_probes_router


#: How long the probes' server is given to start, and then to stop.
START_SECONDS: Final = 10.0
#: How often a loop marks that it turns, and how old its last mark may be before ``/livez`` fails. Blocking work runs
#: in the threadpool, so a loop that has not turned for this long is wedged.
BEAT_SECONDS: Final = 1.0
STALE_SECONDS: Final = 30.0
_POLL_SECONDS: Final = 0.05


class LoopBeat:
    """Whether a loop still turns: the loop marks a beat each :data:`BEAT_SECONDS`, and any thread may ask."""

    def __init__(self) -> None:
        self._at = monotonic()

    def mark(self) -> None:
        """Mark a beat now."""
        self._at = monotonic()

    async def run(self) -> None:
        """Mark a beat each :data:`BEAT_SECONDS` for as long as the loop runs this."""
        while True:
            self.mark()
            await asyncio.sleep(BEAT_SECONDS)

    def alive(self) -> bool:
        """Whether the last beat is younger than :data:`STALE_SECONDS`."""
        return monotonic() - self._at < STALE_SECONDS


class ProbeServer:
    """One uvicorn server for ``app`` on ``port``, run on a daemon thread with a loop of its own.

    Args:
        app: The app holding the probes; it is served with no lifespan, since the process around it owns what it reads.
        port: The port the kubelet probes.
        name: The thread's name, as a stack dump shows it.
    """

    def __init__(self, app: FastAPI, port: int, *, name: str) -> None:
        self._port = port
        self._server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None, lifespan="off"))
        self._thread = threading.Thread(target=self._server.run, name=name, daemon=True)

    def start(self) -> None:
        """Start the server's thread; :meth:`started` says when it serves."""
        self._thread.start()

    def started(self) -> bool:
        """Whether the server accepts connections.

        Raises:
            RuntimeError: The server's thread ended before it served, as when the port is taken.
        """
        if self._server.started:
            return True
        if not self._thread.is_alive():
            raise RuntimeError(f"the probes did not start on port {self._port}")
        return False

    def ask_to_stop(self) -> None:
        """Ask the server to stop; :meth:`stopped` says when its thread has ended."""
        self._server.should_exit = True

    def stopped(self) -> bool:
        """Whether the server's thread has ended."""
        return not self._thread.is_alive()

    def stop(self) -> None:
        """Ask the server to stop and wait :data:`START_SECONDS` for its thread."""
        self.ask_to_stop()
        self._thread.join(timeout=START_SECONDS)


@contextmanager
def serving(app: FastAPI, port: int, *, name: str) -> Generator[None]:
    """Serve ``app`` on ``port`` from a thread until the block ends, for a process that runs no loop yet.

    Raises:
        RuntimeError: The server did not start within :data:`START_SECONDS`.
    """
    server = ProbeServer(app, port, name=name)
    server.start()
    deadline = monotonic() + START_SECONDS
    try:
        while not server.started():
            if monotonic() >= deadline:
                raise RuntimeError(f"the probes did not start on port {port}")
            threading.Event().wait(_POLL_SECONDS)
        yield
    finally:
        server.stop()


@asynccontextmanager
async def serving_beside(app: FastAPI, ready_check: ReadyCheck, port: int) -> AsyncGenerator[None]:
    """Serve ``app``'s probes on ``port`` from a thread, beside the loop this is entered on, until the block ends.

    The probes read ``app``'s own state, and ``/livez`` follows a beat this loop marks.

    Raises:
        RuntimeError: The server did not start within :data:`START_SECONDS`.
    """
    beat = LoopBeat()
    probes = FastAPI(title=f"{app.title} probes", docs_url=None, redoc_url=None, openapi_url=None, telemetry=FRAMEWORK_TELEMETRY_OFF)
    probes.state = app.state
    probes.include_router(make_probes_router(ready_check, beat.alive))
    server = ProbeServer(probes, port, name="probes")
    beating = asyncio.create_task(beat.run(), name="probes-beat")
    server.start()
    deadline = monotonic() + START_SECONDS
    try:
        while not server.started():
            if monotonic() >= deadline:
                raise RuntimeError(f"the probes did not start on port {port}")
            await asyncio.sleep(_POLL_SECONDS)
        yield
    finally:
        beating.cancel()
        await asyncio.gather(beating, return_exceptions=True)
        # Waited for by polling, as its start is, so the stop holds no thread of any pool.
        server.ask_to_stop()
        deadline = monotonic() + START_SECONDS
        while not server.stopped():
            if monotonic() >= deadline:
                break
            await asyncio.sleep(_POLL_SECONDS)


__all__ = ["BEAT_SECONDS", "STALE_SECONDS", "START_SECONDS", "LoopBeat", "ProbeServer", "serving", "serving_beside"]
