"""Maintenance's worker is a process of its own (ADR 0198): it serves no API, only its probes, and they follow its life.

The worker's liveness must hold through the whole drain while its readiness says it is stopping: a probe server that
left at SIGTERM would fail the kubelet's liveness check, which kills the container before its draining pass and the
publishes it still makes are done. The probes are served from a thread, so liveness is the worker's own loop marking
that it turns, and a worker asked to stop while it is still starting stops there. Driven over a socket, as the kubelet
reaches it, and through the process's own
``main`` under a real SIGTERM.

Through a read-only window (``MAINTENANCE_READ_ONLY``, rendered from the chart's one ``readOnlyWindow``) the worker takes
no task: it connects to no bus, so every task stays queued on the work queue with its deliveries unspent, and the
replica the window rolls back takes them.
"""

from __future__ import annotations

import asyncio
import signal
import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from tests.secret_store_double import SecretStoreDouble

from lakehouse.governed.signing_key import SIGNING_STATE
from lakehouse.process.lifecycle import mark_draining, mark_started
from maintenance import worker
from maintenance.core.config import MaintenanceSettings, get_settings
from maintenance.services.queue import TaskQueue


#: The bus the deployed settings name, which nothing here answers.
_BUS = "nats://bus:4222"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _answers(port: int) -> tuple[int, int, str]:
    live = httpx.get(f"http://127.0.0.1:{port}/livez", timeout=5)
    ready = httpx.get(f"http://127.0.0.1:{port}/readyz", timeout=5)
    return live.status_code, ready.status_code, ready.json()["status"]


@pytest.fixture
def store_pair(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The store pair a process with no projected token signs with, so a start reaches the step under test."""
    for name in ("MAINTENANCE_S3_ACCESS_KEY_ID", "MAINTENANCE_S3_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_workers_probes_follow_its_life_and_liveness_holds_through_the_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _free_port()

    # No worker runs here, so the wire the worker's start publishes is set as one that signs nothing sets it.
    monkeypatch.setattr(worker.app.state, SIGNING_STATE, None, raising=False)
    with worker.serving_probes(port):
        starting = _answers(port)
        mark_started(worker.app)
        running = _answers(port)
        mark_draining(worker.app)
        draining = _answers(port)

    assert (starting, running, draining) == ((200, 503, "starting"), (200, 200, "ready"), (200, 503, "shutting_down"))
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{port}/livez", timeout=2)


@pytest.mark.usefixtures("store_pair")
def test_a_worker_with_no_bus_to_take_tasks_from_does_not_start() -> None:
    settings = MaintenanceSettings.model_validate({})

    with pytest.raises(RuntimeError, match="names no bus"):
        asyncio.run(asyncio.wait_for(worker.run(settings, asyncio.Event()), timeout=20))


def test_a_worker_in_a_read_only_window_takes_no_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moto_url: str, secret_store: SecretStoreDouble) -> None:
    """It starts, answers ready and holds until it is stopped, dialling no bus: the bus here never answers, so a worker
    that reached for it would still be starting when stopped. Its settings are the deployed service's: a projected token
    exchanged at moto's STS, and a secret store it never logs in to, since a worker that dials no bus reads no credential.
    Ready because it declares that it signs nothing: a process that declared no signing at all is held out of rotation."""
    dialled: list[str] = []
    # `worker.app` is the process's one app: start from a worker no other test has declared a signer on.
    monkeypatch.delattr(worker.app.state, SIGNING_STATE, raising=False)
    port = _free_port()
    # The kubelet's probe reaches the worker's own socket, which the store's double does not answer for.
    secret_store.router.route(host="127.0.0.1").pass_through()

    async def _bus(url: str, credentials: str, *, name: str) -> TaskQueue:
        dialled.append(url)
        await asyncio.Event().wait()
        raise AssertionError(f"{name} connected with {len(credentials)} bytes of credential")

    monkeypatch.setattr(TaskQueue, "connect", _bus)
    (token := tmp_path / "token").write_text("a projected token")
    monkeypatch.setenv("MAINTENANCE_S3_WEB_IDENTITY_TOKEN_FILE", str(token))
    monkeypatch.setenv("MAINTENANCE_S3_STS_ENDPOINT", moto_url)
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b", "nats_url": _BUS, "read_only": True})

    async def _run() -> tuple[bool, tuple[int, int, str]]:
        stop = asyncio.Event()
        running = asyncio.create_task(asyncio.wait_for(worker.run(settings, stop), timeout=20))
        await asyncio.sleep(0.5)
        held = await asyncio.to_thread(_answers, port)
        stop.set()
        return await running, held

    with worker.serving_probes(port):
        ran, held = asyncio.run(_run())

    assert (ran, held, dialled, secret_store.login.call_count) == (True, (200, 200, "ready"), [], 0)


async def _never_starts(_settings: MaintenanceSettings, _stop: asyncio.Event, _started: asyncio.Event) -> None:
    """A start that waits for ever, as the first connection to a bus that is down does (nats-py retries it without end)."""
    await asyncio.Event().wait()


@pytest.mark.parametrize(
    ("stop_after", "allowed", "ends"),
    [
        pytest.param(0.5, 30.0, None, id="asked-to-stop-it-stops-there"),
        pytest.param(None, 0.5, TimeoutError, id="left-alone-it-ends-at-its-bound"),
    ],
)
def test_a_worker_whose_start_never_finishes_does_not_wait_for_it(
    monkeypatch: pytest.MonkeyPatch, stop_after: float | None, allowed: float, ends: type[Exception] | None
) -> None:
    """A stop that waited for the start would hold the pod in Terminating for its whole grace, and a start nobody
    bounded would leave a pod that is alive and never ready."""
    monkeypatch.setattr(worker, "_work", _never_starts)
    monkeypatch.setattr(worker, "START_SECONDS", allowed)

    async def _run() -> float:
        stop = asyncio.Event()
        if stop_after is not None:
            asyncio.get_running_loop().call_later(stop_after, stop.set)
        began = time.monotonic()
        await asyncio.wait_for(worker.run(MaintenanceSettings.model_construct(), stop), timeout=20)
        return time.monotonic() - began

    if ends is None:
        assert asyncio.run(_run()) < 5
    else:
        with pytest.raises(ends, match="did not start"):
            asyncio.run(_run())


def _polled(port: int, wanted: tuple[int, int, str], process: subprocess.Popen[str], seconds: float = 30.0) -> tuple[int, int, str] | None:
    """What the probes answer once they answer ``wanted``, or the last answer when the process ends or the time is up."""
    deadline, last = time.monotonic() + seconds, None
    while process.poll() is None and time.monotonic() < deadline:
        try:
            last = _answers(port)
        except httpx.TransportError:
            last = None
        if last == wanted:
            return last
        time.sleep(0.05)
    return last


@pytest.mark.parametrize(
    ("started", "code"),
    [
        pytest.param(True, 0, id="a-running-worker-drains-with-its-probes-up"),
        pytest.param(False, 0, id="one-stopped-while-starting-ends-without-waiting-for-its-start"),
    ],
)
def test_the_process_under_sigterm_keeps_its_probes_up_until_it_is_done_and_exits_clean(tmp_path: Path, started: bool, code: int) -> None:
    """``main`` under a real SIGTERM, with the work stood in for. A running worker drains until the test lets it go: the
    signal reaches the worker's loop and not the probes' server, which answers until the drain is done. A worker stopped
    while a start step it cannot cancel still runs in a thread ends at once, without waiting for that thread."""
    port, gate, entered = _free_port(), tmp_path / "let-go", tmp_path / "entered"
    script = tmp_path / "drains.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import asyncio, os, pathlib, threading, time
            os.environ["MAINTENANCE_WORKER_PORT"] = "{port}"
            from lakehouse.governed.signing_key import attach_signing
            from lakehouse.process.lifecycle import mark_draining, mark_started
            from maintenance import worker

            async def drains(settings, stop):
                pathlib.Path({str(entered)!r}).touch()
                attach_signing(worker.app, None)
                mark_started(worker.app)
                await stop.wait()
                mark_draining(worker.app)
                while not pathlib.Path({str(gate)!r}).exists():
                    await asyncio.sleep(0.05)
                return True

            async def stopped_while_starting(settings, stop):
                threading.Thread(target=time.sleep, args=(120,)).start()
                pathlib.Path({str(entered)!r}).touch()
                await stop.wait()
                return False

            worker.run = drains if {started!r} else stopped_while_starting
            worker.main()
            """
        )
    )
    process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # noqa: S603 — this test's own script
    try:
        up = _polled(port, (200, 200, "ready") if started else (200, 503, "starting"), process)
        # The probes answer before the worker's loop takes SIGTERM for itself: signalled then, the process would die of
        # the signal, which is no claim of this test.
        deadline = time.monotonic() + 30
        while not entered.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        process.send_signal(signal.SIGTERM)
        draining = _polled(port, (200, 503, "shutting_down"), process) if started else None
        gate.touch()
        ended = process.wait(timeout=20)
    finally:
        process.kill()

    assert (up, draining, ended) == ((200, 200, "ready"), (200, 503, "shutting_down"), code) if started else (up, ended) == ((200, 503, "starting"), code)
