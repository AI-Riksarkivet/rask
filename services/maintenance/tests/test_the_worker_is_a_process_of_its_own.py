"""Maintenance's worker is a process of its own (ADR 0198): it serves no API, only its probes, and they follow its life.

The pod's sidecar is told to block its shutdown and leaves early only when its app turns unhealthy, so the worker's
liveness must hold through the whole drain while its readiness says it is stopping; a probe server that left at
SIGTERM would take the sidecar, and the publishes a draining pass still makes, with it. The probes are served from a
thread, so liveness is the worker's own loop marking that it turns, and a worker asked to stop while it is still
starting stops there. Driven over a socket, as the kubelet and the sidecar reach it, and through the process's own
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
import respx

from lakehouse.lifecycle import mark_draining, mark_started
from maintenance import service, worker
from maintenance.core.config import MaintenanceSettings, get_settings
from maintenance.services.queue import TaskQueue


#: The bus the deployed settings name, which nothing here answers.
_BUS = "nats://bus:4222"
_SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"


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


def test_the_workers_probes_follow_its_life_and_liveness_holds_through_the_drain() -> None:
    port = _free_port()

    with worker.serving_probes(port):
        starting = _answers(port)
        mark_started(worker.app)
        running = _answers(port)
        mark_draining(worker.app)
        draining = _answers(port)

    assert (starting, running, draining) == ((200, 503, "starting"), (200, 200, "ready"), (200, 503, "shutting_down"))
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{port}/livez", timeout=2)


def test_a_worker_whose_loop_stopped_turning_is_not_live() -> None:
    port = _free_port()

    with worker.serving_probes(port):
        worker.app.state.beat = time.monotonic() - worker.STALE_SECONDS - 1
        wedged = httpx.get(f"http://127.0.0.1:{port}/livez", timeout=5).status_code

    assert wedged == 503


@pytest.mark.usefixtures("store_pair")
def test_a_worker_with_no_bus_to_take_tasks_from_does_not_start() -> None:
    settings = MaintenanceSettings.model_validate({})

    with pytest.raises(RuntimeError, match="names no bus"):
        asyncio.run(asyncio.wait_for(worker.run(settings, asyncio.Event()), timeout=20))


def test_a_worker_in_a_read_only_window_takes_no_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moto_url: str, respx_allows_unused_routes: None) -> None:
    """It starts, answers ready and holds until it is stopped, dialling no bus: the bus here never answers, so a worker
    that reached for it would still be starting when stopped. Its settings are the deployed service's: a projected token
    exchanged at moto's STS, and its bus credential read from the store, which a worker that never dials never reads."""
    dialled: list[str] = []

    async def _bus(url: str, credentials: str, *, name: str) -> TaskQueue:
        dialled.append(url)
        await asyncio.Event().wait()
        raise AssertionError(f"{name} connected with {len(credentials)} bytes of credential")

    monkeypatch.setattr(TaskQueue, "connect", _bus)
    (token := tmp_path / "token").write_text("a projected token")
    monkeypatch.setenv("MAINTENANCE_S3_WEB_IDENTITY_TOKEN_FILE", str(token))
    monkeypatch.setenv("MAINTENANCE_S3_STS_ENDPOINT", moto_url)
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b", "nats_url": _BUS, "secrets_from_dapr": True, "read_only": True})

    async def _run() -> bool:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(1.0, stop.set)
        return await asyncio.wait_for(worker.run(settings, stop), timeout=20)

    with respx.mock as sidecar:
        sidecar.get(f"{_SECRETS}/nats-user-maintenance-worker").mock(return_value=httpx.Response(200, json={"jwt": "eyJ.jwt", "seed": "SUASEED"}))
        assert (asyncio.run(_run()), dialled) == (True, [])


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
            from lakehouse.lifecycle import mark_draining, mark_started
            from maintenance import worker

            async def drains(settings, stop):
                pathlib.Path({str(entered)!r}).touch()
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


def test_a_web_process_whose_start_fails_leaves_its_drain_disarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A start that fails must not leave the SIGTERM drain armed: the next start in the process would be refused for
    it, and the failure that mattered would be hidden behind that one."""
    for name in ("MAINTENANCE_S3_ACCESS_KEY_ID", "MAINTENANCE_S3_SECRET_ACCESS_KEY", "AWS_WEB_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    async def _starts() -> None:
        async with service.lifespan(service.app):
            pass

    failures = []
    for _ in range(2):
        with pytest.raises(ValueError) as failed:  # noqa: PT011 — the same refusal twice is the claim
            asyncio.run(_starts())
        failures.append(str(failed.value))
    get_settings.cache_clear()

    assert failures[0] == failures[1] and "already armed" not in failures[1]
