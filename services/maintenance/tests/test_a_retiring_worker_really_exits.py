"""A retiring worker acks the unit that tripped the mark and then its process EXITS ([[LH-183]]).

`test_a_worker_retires_before_it_is_killed.py` proves what `retire_this_worker` sends, with `os.kill`
replaced — a double that cannot see what happens to the signal next. This runs the real function
inside a request on a real uvicorn whose lifespan arms the drain the way `maintenance.service` does,
because the recycle is only a recycle if that SIGTERM reaches uvicorn's own handler and the process
leaves for Kubernetes to replace.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import suppress
from pathlib import Path

import httpx


_APP = textwrap.dedent(
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from maintenance.services.rewrite_slot import retire_this_worker
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


    @app.post("/unit")
    async def unit():
        retire_this_worker(passes=0, reason="memory")
        return {"status": "SUCCESS"}
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_a_memory_recycle_acks_its_unit_and_the_process_LEAVES(tmp_path: Path) -> None:
    (tmp_path / "retiring_app.py").write_text(_APP, encoding="utf-8")
    port = _free_port()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("RASK_", "OTEL_", "MAINTENANCE_"))}
    # uvloop, named: maintenance installs `uvicorn[standard]`, so `auto` resolves to it in the image. The asyncio
    # handoff is proven by service-kit's `test_sigterm_still_stops_the_server.py`.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "retiring_app:app",
            "--app-dir",
            str(tmp_path),
            "--port",
            str(port),
            "--loop",
            "uvloop",
            "--timeout-graceful-shutdown",
            "5",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    stdout = proc.stdout
    threading.Thread(target=lambda: lines.extend(line.rstrip("\n") for line in stdout), daemon=True).start()
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(f"http://127.0.0.1:{port}/ping", timeout=1)
                break
            except httpx.TransportError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError(f"uvicorn never served: {lines}") from None
                time.sleep(0.1)

        answer = httpx.post(f"http://127.0.0.1:{port}/unit", timeout=10).json()
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)

        assert answer == {"status": "SUCCESS"}, f"the unit that tripped the mark was not acked: {answer}"
        assert proc.poll() is not None, f"the worker retired and did not exit — up, NotReady and parking every unit it is sent: {lines}"
        time.sleep(0.2)
        assert any("maintenance_worker_retiring" in line for line in lines), f"the real retirement never ran: {lines}"
        assert any("TEARDOWN draining=True" in line for line in lines), f"the lifespan teardown never ran: {lines}"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
