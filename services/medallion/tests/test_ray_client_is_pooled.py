"""A durable worker's Ray client must have the worker's lifetime, not the call's.

open_fastapi-audit — "The Ray dashboard client is rebuilt per durable-workflow activity while the
catalog client beside it is pooled, in the module family whose own comment cites the rule".

Every Ray submit opened `httpx.AsyncClient(...)` in an `async with` and tore it down on the way out —
one TCP connect, one TLS handshake and one pool teardown per submit, on a durable workflow that runs
these activities repeatedly. `production-patterns.md`: "One engine, one HTTP client, per process.
Created in lifespan, stashed on `app.state`."

THE HONEST CAVEAT, which the finding makes and this gate keeps: a workflow ACTIVITY has no `Request`
and no reachable `app.state`, so the reference's literal prescription does not apply. That is why the
answer here is a module-level client with the WORKER's lifetime, closed when the runtime shuts down —
not a lifespan-owned one it has no way to reach.

AND THE CLOSE MATTERS AS MUCH AS THE POOL. A module-level client that nothing closes trades a
per-call teardown for a permanent leak plus a "Unclosed client session" on every shutdown. The test
below pins both halves, because the pooling half alone is the easier and worse fix.

`mover.py` already pools its catalog client, so this was MED-008 partially applied — the workflow
activities were its unfixed remainder.
"""

from __future__ import annotations

import contextlib
import inspect
from typing import Any

import pytest

from medallion.services import ray_submit


def test_the_module_offers_one_pooled_client() -> None:
    assert hasattr(ray_submit, "ray_client"), (
        "ray_submit builds a fresh httpx.AsyncClient per submit — one connect, handshake and pool "
        "teardown per activity on a durable workflow that runs them repeatedly"
    )


def test_no_submit_path_builds_its_own_client() -> None:
    """The accessor existing is not the property; the call sites using it is."""
    for name in ("submit_stage_job", "submit_train_job"):
        fn = getattr(ray_submit, name, None)
        if fn is None:
            continue
        source = inspect.getsource(fn)
        code = "\n".join(line for line in source.split("\n") if not line.strip().startswith("#"))
        assert "AsyncClient(" not in code, f"{name} still constructs its own client"


@pytest.mark.asyncio
async def test_the_client_is_reused_across_calls() -> None:
    first = await ray_submit.ray_client()
    second = await ray_submit.ray_client()
    assert first is second, "each call got a new client — the pool is per-call again"
    await ray_submit.close_ray_client()


@pytest.mark.asyncio
async def test_it_can_be_closed_and_rebuilt() -> None:
    """A module-level client nothing closes is a leak plus an 'Unclosed client session' on every
    shutdown — the easier half of this fix, and the worse one on its own."""
    first = await ray_submit.ray_client()
    await ray_submit.close_ray_client()
    assert first.is_closed

    second = await ray_submit.ray_client()
    assert second is not first, "after close, the next caller must get a working client"
    await ray_submit.close_ray_client()


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# AND THE POOL IS ONLY REAL IF THE LOOP IS. Everything above is true of the CLIENT and was false of
# the estate, because `workflow._run_async` gave every activity its own `asyncio.run` — a fresh event
# loop, closed on the way out. A pooled keep-alive connection belongs to the loop that opened it, so
# the next activity inherited a connection bound to a dead one.
#
# Measured on the live estate 2026-08-30: EVERY stage dispatch logged
# `Activity execution failed - task_id: 1, error: Event loop is closed`, then succeeded ~2 s later on
# the retry — because the failed attempt evicted the dead connection and the third attempt opened a
# fresh one. 28 of them in 24 h on `silver-to-gold` alone. The cascade completed every time, which is
# exactly why it survived this long: the defect paid for itself in one retry per stage and a warning
# that read like Dapr's own noise.
#
# So the two halves must agree. Either the client is per-call (and the pool above is a lie) or the
# WORKER HAS ONE LOOP — and since this file's whole subject is that the client has the worker's
# lifetime, the loop must have it too.


class _KeepAliveServer:
    """A real HTTP/1.1 server, because this defect only exists when a connection is actually POOLED.

    `http.server` speaks HTTP/1.0 unless told otherwise, and a 1.0 response closes the connection —
    which silently makes the pool empty and the bug unreproducible. That is not a detail of the test;
    it is the mechanism.
    """

    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the base class names it
                """Silence the per-request line so pytest's output stays about the test."""
                return

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.address = f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> _KeepAliveServer:
        import threading

        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_a_pooled_connection_survives_the_NEXT_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two activities, one pooled connection. The second must not inherit a closed loop.

    This drives the production bridge (`workflow._run_async`) rather than asserting on its shape, so
    it stays true of whatever mechanism keeps the loop alive.
    """
    from types import SimpleNamespace

    from medallion.workflow import _run_async
    from service_kit.activity_loop import stop_worker_loop

    with _KeepAliveServer() as server:
        # Stub the settings rather than the model: `ray_address` is derived, and the only two fields
        # `ray_client` reads are these.
        monkeypatch.setattr(ray_submit, "get_settings", lambda: SimpleNamespace(ray_address=server.address, ray_request_timeout_seconds=5.0))

        async def _fetch() -> int:
            client = await ray_submit.ray_client()
            response = await client.get("/api/version")
            return response.status_code

        try:
            assert _run_async(_fetch()) == 200, "the first activity must reach the server at all"
            # The second is the whole test: it reuses the keep-alive connection the first one pooled.
            assert _run_async(_fetch()) == 200, (
                "the pooled connection belonged to the first activity's event loop, which "
                "`asyncio.run` closed — this is the `Event loop is closed` every stage dispatch logged"
            )
        finally:
            # Close the client ON THE LOOP THAT OWNS IT — the whole subject of this test.
            with contextlib.suppress(Exception):
                _run_async(ray_submit.close_ray_client())
            stop_worker_loop()
