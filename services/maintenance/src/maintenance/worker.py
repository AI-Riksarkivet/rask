"""Table maintenance's worker: a process of its own that runs the tasks on the NATS JetStream work queue (ADR 0198).

Every replica runs one worker (:mod:`maintenance.services.tasks`) that takes one task at a time off the queue, so no
table is passed twice at once:

* a **table's pass** expires its superseded versions (``cleanup_old_versions`` behind the refusal ladder that guards
  reclamation), writes its due record when a bring-your-own job should compact or reindex it, and schedules its next
  pass; the catalog's announcements queue one when a table is made and when a commit lands, and its schedule when it
  comes due;
* a **trash record's release** at its deadline;
* the **audit**, once an hour on one replica: it reads OpenFGA's grant census (through the catalog), the control-root
  registries and object storage, REPORTS where they disagree, and queues each table and trash record nothing holds;
* the **scan**, once a day: the audit with the orphan scan, which opens every table, then, ONLY if that report ran
  clean, the purge of expired trash records, off by default.

The worker is owned by this process and not by a web app's lifespan: a pass that hangs ends this replica and nothing
else, and the web pods, which serve the write-event lane, run none. Blocking Lance and S3 IO runs in the threadpool so
the heartbeats stay on the event loop.

The process serves no API. It answers ``/livez`` and ``/readyz`` on ``MAINTENANCE_WORKER_PORT``, for the kubelet and
for its sidecar's health check: a sidecar told to block its shutdown leaves early only when its app turns unhealthy
(measured on daprd 1.19.0-rc.4: 3 s after the app with the check, the whole block without), so the probes are what
lets the sidecar outlive the worker's drain and then leave with it. They are served from a thread of their own: uvicorn
takes SIGTERM for itself on the main thread and would stop answering at the moment the drain begins. Served from
another loop, ``/livez`` would say nothing of the worker's, so the worker's loop marks a beat each second and
``/livez`` fails once the last one is :data:`STALE_SECONDS` old.

Run: ``python -m maintenance.worker``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager, suppress
from time import monotonic

import uvicorn
from fastapi import FastAPI

from lakehouse.governed.signing_key import signing_ready_check
from lakehouse.lance_app import build_lance_service_app
from lakehouse.lifecycle import mark_draining, mark_started
from lakehouse.obs import configure_app_logging
from maintenance.core.config import MaintenanceSettings, get_settings
from maintenance.runtime import collaborators, standing
from maintenance.services.tasks import running_worker


configure_app_logging()
log = logging.getLogger(__name__)

#: How long the probes' server is given to start, and then to stop.
_PROBES_SECONDS = 10.0
#: How often the worker's loop marks that it turns, and how old its last mark may be before ``/livez`` fails. A pass
#: runs in the threadpool, so a loop that has not turned for this long is wedged and its heartbeats have stopped.
_BEAT_SECONDS = 1.0
STALE_SECONDS = 30.0
#: How long the worker may take to start before its process ends, to be started again: a start that waits on the bus or
#: the store for ever is a pod that is alive and never ready. Inside the kubelet's startup allowance (300 s).
START_SECONDS = 240.0
#: The exit code of a worker that did not start within its bound: `EX_SOFTWARE`, as a worker that ends itself uses.
EXIT_UNSTARTED = 70


@asynccontextmanager
async def _serves_no_lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """The probes' app holds nothing of its own: the process around it builds and stops what the worker uses."""
    yield


def _alive() -> bool:
    """Whether the worker's loop has marked a beat within :data:`STALE_SECONDS`."""
    return monotonic() - app.state.beat < STALE_SECONDS


#: The probes, and nothing else: liveness follows the worker's loop, and readiness the lifecycle flags this process sets
#: and its signing key.
app = build_lance_service_app(
    title="Lance table maintenance worker",
    docs_enabled=False,
    audit_enabled=get_settings().audit_enabled,
    lifespan=_serves_no_lifespan,
    log=log,
    ready_check=signing_ready_check(),
    live_check=_alive,
)
app.state.beat = monotonic()


@contextmanager
def serving_probes(port: int) -> Generator[None]:
    """Serve :data:`app` on ``port`` from a thread until the block ends.

    Raises:
        RuntimeError: The server did not start, as when the port is taken.
    """
    app.state.beat = monotonic()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None, lifespan="off"))  # noqa: S104 — the kubelet and the sidecar probe the pod's address
    thread = threading.Thread(target=server.run, name="maintenance-worker-probes", daemon=True)
    thread.start()
    waited = 0.0
    while not server.started:
        if not thread.is_alive() or waited >= _PROBES_SECONDS:
            raise RuntimeError(f"the worker's probes did not start on port {port}")
        thread.join(timeout=0.05)
        waited += 0.05
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=_PROBES_SECONDS)


async def _beating() -> None:
    """Mark, each :data:`_BEAT_SECONDS`, that this loop turns."""
    while True:
        app.state.beat = monotonic()
        await asyncio.sleep(_BEAT_SECONDS)


async def _work(settings: MaintenanceSettings, stop: asyncio.Event, started: asyncio.Event) -> None:
    """Start what the worker stands on and the worker, run it until ``stop``, then let its running task finish; through
    a read-only window, hold instead (:func:`_held`)."""
    if settings.read_only:
        await _held(stop, started)
        return
    async with (
        standing(app, settings),
        collaborators(app, settings) as held,
        running_worker(settings, ready=lambda: held.ready() and not stop.is_set(), emitter=held.lineage, control=held.control) as queue,
    ):
        if queue is None:
            raise RuntimeError("MAINTENANCE_NATS_URL names no bus: the worker has no queue to take a task from")
        mark_started(app)
        started.set()
        log.info("maintenance_worker_started")
        await stop.wait()
        mark_draining(app)
        log.info("maintenance_worker_stopping", extra={"drain_seconds": settings.drain_seconds})


async def _held(stop: asyncio.Event, started: asyncio.Event) -> None:
    """Hold through the lakehouse's read-only window (``MAINTENANCE_READ_ONLY``) until ``stop``, taking no task.

    The worker connects to no bus and opens no store: every task stays queued on the work queue with its deliveries
    unspent, nothing acked or given back, and each fired schedule waits on its stream. A task is kept for the work
    queue's age, a day (the chart's stream Job declares ``MAINTENANCE_TASKS`` with ``max_age`` 86400 s); one a longer
    window ages out is queued again by the first audit after it, whose backstop queues each table nothing holds. The
    flag is rendered into the pod from git, so the window ends with a rolled pod that takes the tasks.
    """
    mark_started(app)
    started.set()
    log.info("maintenance_worker_held_for_read_only_window")
    await stop.wait()
    mark_draining(app)


async def run(settings: MaintenanceSettings, stop: asyncio.Event) -> bool:
    """Run the worker until ``stop`` is set, then let the task it is running finish and stop what it used; whether the
    worker had started when it was asked to stop.

    A worker asked to stop while it is still starting is stopped there: its start may be waiting on a bus that is down,
    and a process that kept waiting would hold its pod in Terminating for the whole grace. The worker takes no task
    while its signing key is unresolved, since what a pass emits could not be signed, none once it is asked to stop, and
    none through a read-only window (:func:`_held`).

    Raises:
        RuntimeError: No bus is named, so there is no queue to take a task from; or the store holds no bus credential.
        TimeoutError: The worker did not start within :data:`START_SECONDS`.
    """
    started = asyncio.Event()
    beat = asyncio.create_task(_beating(), name="maintenance-worker-beat")
    work = asyncio.create_task(_work(settings, stop, started), name="maintenance-worker")
    asked = asyncio.create_task(stop.wait(), name="maintenance-worker-stop")
    begun = asyncio.create_task(started.wait(), name="maintenance-worker-started")
    try:
        await asyncio.wait({work, asked, begun}, timeout=START_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        if not started.is_set() and not work.done():
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work
            if not stop.is_set():
                raise TimeoutError(f"the worker did not start within {START_SECONDS:.0f} s")
            log.info("maintenance_worker_stopped", extra={"started": False})
            return False
        await work
        log.info("maintenance_worker_stopped", extra={"started": True})
        return True
    finally:
        for task in (asked, begun, beat):
            task.cancel()
        await asyncio.gather(asked, begun, beat, return_exceptions=True)


async def _until_signalled(settings: MaintenanceSettings) -> bool:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    return await run(settings, stop)


def _end_unstarted(code: int) -> None:
    """End a process whose start was abandoned, without waiting for it. A start step that runs in a thread (a secret
    read retried for minutes while the sidecar is down) cannot be cancelled, and the interpreter waits for its thread
    at exit: the pod would stay in Terminating until the read gave up. Nothing was started that needs stopping."""
    logging.shutdown()
    os._exit(code)


def main() -> None:
    """The worker process: its probes, then the worker until SIGTERM or SIGINT."""
    settings = get_settings()
    with serving_probes(settings.worker_port):
        try:
            started = asyncio.run(_until_signalled(settings))
        except TimeoutError:
            log.exception("maintenance_worker_start_outrun")
            _end_unstarted(EXIT_UNSTARTED)
        else:
            if not started:
                _end_unstarted(0)


if __name__ == "__main__":
    main()
