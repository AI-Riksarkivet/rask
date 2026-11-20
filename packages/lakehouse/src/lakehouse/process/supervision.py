"""What a lakehouse process runs beside its work: background loops it supervises, and its own end when one of them ends.

A background loop that ends while its process serves leaves a pod that is Ready and does nothing: a pylance panic is a
BaseException, so it ends a loop that catches Exception with no log line and the probes green, and the task the bus
delivers again ends the next replica's loop the same way. So every loop a process starts runs under a
:class:`Supervisor`, which ends the process when one ends before the process stops, whether the loop returned, raised,
or a native library panicked; Kubernetes then starts the container again, the only repair a dead loop has (ADR 0203).

:func:`end_process` is that end: the critical line logged, what the process holds of its logs, metrics and traces given
:data:`FLUSH_SECONDS` at most to leave, then ``os._exit``. The flush is load-bearing, not tidiness: the Collector drops a
lakehouse pod's stdout copy, since the pod ships its records over OTLP, so a record still in the exporter's batch when
the process ends never reaches the store.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Coroutine
from time import monotonic
from typing import Any, Final, NoReturn, Self

from opentelemetry import metrics, trace
from opentelemetry._logs import get_logger_provider


log = logging.getLogger(__name__)

#: `EX_SOFTWARE`: the exit code of a process that ended itself because something in it stopped working.
EX_SOFTWARE: Final = 70
#: How long the end of a process waits at most for its logs, metrics and traces to leave it, all three together.
FLUSH_SECONDS: Final = 3.0
#: What a BaseException that is not an Exception still means: the process, or the task it ends, is asked to stop.
_STOPS: Final = (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError)

type Work = Callable[[], Coroutine[Any, Any, None]]
#: How a process ends itself: :func:`end_process`, or a recorder in a test. Its keywords are open, since every end passes
#: the fields of its log line as keywords beside ``code``, ``cause`` and ``flush_seconds``, so no narrower signature can
#: type both.
type EndProcess = Callable[..., NoReturn]


def is_panic(exc: BaseException) -> bool:
    """Whether ``exc`` is a native library's panic: a BaseException that is neither an Exception nor a request to stop.

    pyo3 raises a Rust panic as ``pyo3_runtime.PanicException``, which derives from BaseException and which no module
    exports (pyo3 synthesises ``pyo3_runtime`` lazily, and each extension has its own copy of the class), so a panic is
    told by what it is not. A group is a panic when it holds no request to stop: being no Exception, it holds a leaf that
    is not one either.
    """
    if isinstance(exc, Exception):
        return False
    if isinstance(exc, BaseExceptionGroup):
        return exc.subgroup(_STOPS) is None
    return not isinstance(exc, _STOPS)


def flush_telemetry(seconds: float) -> None:
    """Flush the process's stream log handlers, then give its OpenTelemetry logs, traces and metrics ``seconds`` at most.

    The OpenTelemetry flush runs on a thread of its own, waited for ``seconds`` at most, and the process's end ends it
    where it stands: the SDK's batch processors export in the calling thread and ignore the timeout they are handed, so a
    flush asked for one second against a Collector that takes the connection and never answers took 16 (measured
    2026-11-12 on opentelemetry-sdk 1.45.2, its OTLP/HTTP exporter's timeout 8 s). A provider the SDK did not install (no
    ``opentelemetry-instrument``, as in a test) has nothing to flush. A flush that fails is the end of a process that is
    ending anyway, so it is passed over: the process's last line is already out.
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.flush()
    if seconds <= 0:
        return
    flushing = threading.Thread(target=_flush_providers, args=(monotonic() + seconds,), name="telemetry-flush", daemon=True)
    flushing.start()
    flushing.join(seconds)


def _flush_providers(deadline: float) -> None:
    for provider in (get_logger_provider(), trace.get_tracer_provider(), metrics.get_meter_provider()):
        force_flush = getattr(provider, "force_flush", None)
        left = deadline - monotonic()
        if force_flush is None or left <= 0:
            continue
        try:
            force_flush(timeout_millis=int(left * 1000))
        except Exception:  # noqa: S112 — a failed flush cannot stop a process from ending
            continue


def end_process(event: str, *, code: int = EX_SOFTWARE, cause: BaseException | None = None, flush_seconds: float = FLUSH_SECONDS, **extra: object) -> NoReturn:
    """End this process with ``code``, logging ``event`` (and ``cause``'s traceback) and giving its telemetry
    ``flush_seconds`` at most to leave first: less where what the process holds runs out sooner, as a worker's lease on
    its task does.

    ``os._exit``, never ``sys.exit``: a thread nothing can stop (a pass running in the threadpool, a read hung on a
    socket) would hold the interpreter's exit for as long as it runs, and the pod would stay in Terminating doing nothing.
    """
    log.critical(event, extra=extra, exc_info=cause)
    flush_telemetry(flush_seconds)
    os._exit(code)


class Supervisor:
    """The named background work of one process, each piece started here; one that ends unasked ends the process.

    Two kinds. A LOOP (:meth:`keep`) runs until the process stops, so its end in any way before then is a failure: one
    that returned would leave the work it does undone as surely as one that raised. A TASK (:meth:`start`) runs until its
    work is done, so its return is its end and only its failure ends the process. A piece cancelled by its owner is
    stopped, never failed, so a component cancels its own loop when its user stops it. Once :meth:`wind_down` is called
    every end is the process stopping: a worker that drains its loops lets them return.

    ``end`` is :func:`end_process` outside a test.
    """

    def __init__(self, *, end: EndProcess = end_process) -> None:
        self._end = end
        self._running: dict[asyncio.Task[None], tuple[str, bool]] = {}
        self._winding_down = False

    def keep(self, name: str, loop: Work) -> asyncio.Task[None]:
        """Run ``loop`` as the process's background loop ``name``, until the process stops; the task that runs it."""
        return self._spawn(name, loop, kept=True)

    def start(self, name: str, work: Work) -> asyncio.Task[None]:
        """Run ``work`` as the process's background task ``name``, until it is done; the task that runs it."""
        return self._spawn(name, work, kept=False)

    def wind_down(self) -> None:
        """From now on, every end is the process stopping: nothing that ends ends the process."""
        self._winding_down = True

    @property
    def running(self) -> list[asyncio.Task[None]]:
        """The work still running, oldest first."""
        return list(self._running)

    async def stop(self) -> None:
        """Wind down, cancel what still runs, and wait for each piece to end, so nothing outlives the process's own stop."""
        self.wind_down()
        running = list(self._running)
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def _spawn(self, name: str, work: Work, *, kept: bool) -> asyncio.Task[None]:
        task = asyncio.create_task(work(), name=name)
        self._running[task] = (name, kept)
        task.add_done_callback(self._ended)
        return task

    def _ended(self, task: asyncio.Task[None]) -> None:
        name, kept = self._running.pop(task)
        if self._winding_down or task.cancelled():
            return
        cause = task.exception()
        if cause is None and not kept:
            return
        self._end("background_work_ended", loop=name, cause=cause)


__all__ = ["EX_SOFTWARE", "FLUSH_SECONDS", "EndProcess", "Supervisor", "Work", "end_process", "flush_telemetry", "is_panic"]
