"""SIGTERM still stops the server once the drain is armed on it ([[LH-183]]).

`arm_drain_on_sigterm` takes SIGTERM over from whatever held it. Under uvicorn that is
`Server.handle_exit`, installed with `signal.signal` before the lifespan runs and the only thing that
sets `should_exit`. A handler that flips the flag and stops there leaves the process up and draining
for good: `/readyz` answers 503, `/livez` answers 200, the kubelet waits out the grace period and
SIGKILLs, and the lifespan teardown never runs. The maintenance worker's memory recycle is SIGTERM to
self, so on that lane it never recycles at all.

A REAL uvicorn in a child process, because the defect is the handoff between two handlers and an
in-process double of either one cannot see it (resilience.md §1 measured the process still alive 12 s
after SIGTERM with the flag already flipped).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest


#: uvicorn's own bound on the drain, passed explicitly so "exits within its graceful timeout" has a number.
_GRACEFUL_S = 5
#: How long the test waits for an exit before calling the process a zombie. Margin over the bound above
#: for interpreter teardown on a loaded CI host.
_EXIT_WITHIN_S = _GRACEFUL_S + 5

#: The loop owns SIGTERM's delivery (asyncio's wakeup-fd trampoline, uvloop's own), so the handoff is proven on
#: both: `auto` resolves to uvloop where `uvicorn[standard]` is installed, and the gateway and notifications
#: install plain `uvicorn`, so they run asyncio.
_EVERY_LOOP = pytest.mark.parametrize("loop", ["asyncio", "uvloop"])

_APP = textwrap.dedent(
    """
    import asyncio
    import os
    import signal
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from service_kit.draining import arm_drain_on_sigterm
    from service_kit.lifecycle import is_draining


    @asynccontextmanager
    async def lifespan(app):
        disarm = arm_drain_on_sigterm(app)
        try:
            yield
        finally:
            print(f"TEARDOWN draining={is_draining(app)}", flush=True)
            disarm()


    app = FastAPI(lifespan=lifespan)


    @app.get("/ping")
    async def ping():
        return {"ok": True}


    @app.get("/slow")
    async def slow():
        print("SLOW_STARTED", flush=True)
        await asyncio.sleep(1.5)
        return {"draining": is_draining(app)}


    @app.get("/stuck")
    async def stuck():
        print("STUCK_STARTED", flush=True)
        await asyncio.sleep(120)
        return {"finished": True}


    @app.post("/leave")
    async def leave():
        os.kill(os.getpid(), signal.SIGTERM)
        return {"acked": True}
    """
)


#: A lifespan that arms the drain and then tries to arm it again for a second app in the same process.
_ARMED_TWICE = textwrap.dedent(
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from service_kit.draining import arm_drain_on_sigterm
    from service_kit.lifecycle import is_draining


    @asynccontextmanager
    async def lifespan(app):
        disarm = arm_drain_on_sigterm(app)
        try:
            arm_drain_on_sigterm(FastAPI())
        except RuntimeError as refused:
            print(f"SECOND_ARM_REFUSED {refused}", flush=True)
        try:
            yield
        finally:
            print(f"TEARDOWN draining={is_draining(app)}", flush=True)
            disarm()


    app = FastAPI(lifespan=lifespan)


    @app.get("/ping")
    async def ping():
        return {"ok": True}
    """
)


class _Server:
    """A uvicorn child and the lines it has printed so far."""

    def __init__(self, proc: subprocess.Popen[str], port: int) -> None:
        self.proc = proc
        self.port = port
        self.lines: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def printed(self, marker: str) -> bool:
        return any(marker in line for line in self.lines)

    def wait_for_line(self, marker: str, *, within: float) -> None:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if self.printed(marker):
                return
            time.sleep(0.05)
        raise AssertionError(f"the server never printed {marker!r}: {self.lines}")

    def exited_within(self, seconds: float) -> bool:
        try:
            self.proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return False
        return True


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _uvicorn(tmp_path: Path, loop: str, source: str = _APP) -> Iterator[_Server]:
    (tmp_path / "armed_app.py").write_text(source, encoding="utf-8")
    port = _free_port()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("RASK_", "OTEL_"))}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "armed_app:app",
            "--app-dir",
            str(tmp_path),
            "--port",
            str(port),
            "--loop",
            loop,
            "--timeout-graceful-shutdown",
            str(_GRACEFUL_S),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    server = _Server(proc, port)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(server.url("/ping"), timeout=1)
                break
            except httpx.TransportError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError(f"uvicorn never served: {server.lines}") from None
                time.sleep(0.1)
        yield server
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@_EVERY_LOOP
def test_an_external_SIGTERM_drains_the_in_flight_request_and_then_EXITS(tmp_path: Path, loop: str) -> None:
    """The kubelet's SIGTERM. The request in flight is answered, answered as draining, and the process
    leaves within uvicorn's graceful bound with its lifespan teardown run."""
    with _uvicorn(tmp_path, loop) as server:
        answer: list[dict[str, bool]] = []
        caller = threading.Thread(target=lambda: answer.append(httpx.get(server.url("/slow"), timeout=15).json()))
        caller.start()
        server.wait_for_line("SLOW_STARTED", within=5)

        server.proc.send_signal(signal.SIGTERM)

        exited = server.exited_within(_EXIT_WITHIN_S)
        caller.join(timeout=5)
        assert exited, f"still alive {_EXIT_WITHIN_S}s after SIGTERM — the drain took the signal and never handed it on: {server.lines}"
        assert answer == [{"draining": True}], f"the in-flight request was not answered as draining: {answer}"
        assert server.printed("TEARDOWN draining=True"), f"the lifespan teardown never ran: {server.lines}"


@pytest.mark.parametrize("loop", ["uvloop"])
def test_SIGTERM_TO_SELF_acks_the_request_that_sent_it_and_then_EXITS(tmp_path: Path, loop: str) -> None:
    """The recycle's shape: a handler signals its own process. The response carrying the ack must
    arrive, and then the process must actually leave for Kubernetes to replace it."""
    with _uvicorn(tmp_path, loop) as server:
        answer = httpx.post(server.url("/leave"), timeout=10).json()

        exited = server.exited_within(_EXIT_WITHIN_S)
        assert answer == {"acked": True}, answer
        assert exited, f"a self-SIGTERM left the process up and draining — the recycle cannot recycle: {server.lines}"
        assert server.printed("TEARDOWN draining=True"), f"the lifespan teardown never ran: {server.lines}"


@pytest.mark.parametrize("loop", ["uvloop"])
def test_a_SECOND_arm_in_one_process_is_REFUSED_and_SIGTERM_still_stops_the_server(tmp_path: Path, loop: str) -> None:
    """A process has one SIGTERM disposition. A second arm would displace the first with a handler whose
    `previous` is the loop's own no-op trampoline, so SIGTERM would be handed to nothing: swallowed, and the
    process up and draining until SIGKILL. The second arm is refused, and the first still hands SIGTERM on."""
    with _uvicorn(tmp_path, loop, _ARMED_TWICE) as server:
        server.proc.send_signal(signal.SIGTERM)

        exited = server.exited_within(_EXIT_WITHIN_S)
        assert exited, f"still alive {_EXIT_WITHIN_S}s after SIGTERM — a second arm swallowed it: {server.lines}"
        assert server.printed("SECOND_ARM_REFUSED"), f"the second arm was not refused: {server.lines}"
        assert server.printed("TEARDOWN draining=True"), f"the lifespan teardown never ran: {server.lines}"


def test_an_ASYNC_request_that_OUTLIVES_the_bound_is_cancelled_and_the_teardown_runs(tmp_path: Path) -> None:
    """The chart renders `--timeout-graceful-shutdown` so the lifespan teardown runs before the kubelet's
    SIGKILL (`tests/unit/test_uvicorn_drains_inside_its_pods_grace_period.py`). That holds only if uvicorn
    still applies its bound once the drain has handed it SIGTERM: at the bound it cancels the async request
    still running, and then runs the lifespan teardown.

    The bound reaches async work only. A request running in a thread (Starlette's `run_in_threadpool`) is
    not interrupted by it and ends by returning or by the kubelet's SIGKILL, so when the PROCESS leaves is
    not this test's claim: here it leaves by uvicorn's closing re-raise of SIGTERM, which a PID 1 does not
    get (`chart/values.yaml` `lifecycle`)."""
    # One loop: the bound and its cancel are uvicorn's own, and the loop-dependent handoff is proven on both above.
    with _uvicorn(tmp_path, "uvloop") as server:
        outcome: list[str] = []

        def _call() -> None:
            try:
                outcome.append(f"answered {httpx.get(server.url('/stuck'), timeout=30).status_code}")
            except httpx.TransportError as exc:
                outcome.append(f"cut off: {type(exc).__name__}")

        caller = threading.Thread(target=_call)
        caller.start()
        server.wait_for_line("STUCK_STARTED", within=5)

        server.proc.send_signal(signal.SIGTERM)

        server.wait_for_line("TEARDOWN draining=True", within=_EXIT_WITHIN_S)
        caller.join(timeout=5)
        cancelled = [index for index, line in enumerate(server.lines) if "timeout graceful shutdown exceeded" in line]
        torn_down = next(index for index, line in enumerate(server.lines) if "TEARDOWN draining=True" in line)
        assert cancelled and cancelled[0] < torn_down, f"uvicorn did not cancel the request at its bound before the teardown: {server.lines}"
        assert outcome and not outcome[0].startswith("answered 200"), f"the stuck request finished instead of being cancelled: {outcome}"


_NO_SERVER = textwrap.dedent(
    """
    import asyncio
    import signal

    from fastapi import FastAPI

    from service_kit.draining import arm_drain_on_sigterm


    async def main():
        arm_drain_on_sigterm(FastAPI())
        signal.raise_signal(signal.SIGTERM)
        await asyncio.sleep(3)
        print("SURVIVED_SIGTERM", flush=True)


    asyncio.run(main())
    """
)


def test_with_NO_handler_to_hand_on_to_SIGTERM_keeps_its_default_action() -> None:
    """Nothing installed a Python handler, so SIGTERM's disposition was the default: terminate. Arming
    the drain must not turn that into "ignore"."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("RASK_", "OTEL_"))}
    done = subprocess.run([sys.executable, "-c", _NO_SERVER], capture_output=True, text=True, env=env, timeout=30, check=False)

    assert "SURVIVED_SIGTERM" not in done.stdout, f"an armed process with the default disposition outlived SIGTERM: {done.stdout}{done.stderr}"
    assert done.returncode == -signal.SIGTERM, f"expected death by SIGTERM, got {done.returncode}: {done.stderr}"
