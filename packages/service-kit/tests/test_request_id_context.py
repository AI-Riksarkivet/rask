"""The minted request id must reach a log record, or it correlates nothing.

open_fastapi-audit — "RequestIDMiddleware mints a correlation id that nothing reads: no ContextVar,
no logging middleware, no log field".

The middleware set `request.state.request_id` and echoed `X-Request-ID` back, and no module read
either. So a user could quote the id from a failed request and an operator had nothing to grep — two
lines of `BaseHTTPMiddleware` writing a value nothing consumes.

WHAT WAS NOT BROKEN, because the audit is careful and this gate should not overstate it: correlation
was never ABSENT. `LoggingInstrumentor` already injects `otelTraceID`/`otelSpanID` into every record
and ships them to GreptimeDB. The residual was a DEAD id, not an observability outage, which is why
this is low — and why finishing the chain is cheap rather than urgent.

`production-patterns.md` gives the shape exactly: a `ContextVar` set in the middleware, reset in a
`finally`, and read anywhere downstream "without plumbing". The reset is the half the reference marks
critical — without it an id leaks into the NEXT request on a reused worker, which is worse than no id
at all: it correlates the wrong things while looking correct.

A `logging.Filter` rather than a `LoggingMiddleware`, deliberately: the filter reaches every module in
every service through `setup_logging`'s single formatter, while a middleware only ever sees the
records it writes itself.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.middleware import RequestIDMiddleware


def test_the_context_var_exists_and_defaults_safely() -> None:
    from service_kit.context import current_request_id, request_id_ctx

    assert request_id_ctx.get() is not None
    assert current_request_id() == request_id_ctx.get()


def test_the_middleware_publishes_the_id_to_the_context() -> None:
    from service_kit.context import current_request_id

    seen: dict[str, str] = {}
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        # Deliberately reads it WITHOUT taking `request` — that is the whole point of the ContextVar.
        seen["id"] = current_request_id()
        return {"ok": "yes"}

    response = TestClient(app).get("/thing", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"
    assert seen["id"] == "abc123", "the handler could not see the id the middleware minted"


def test_the_id_does_NOT_leak_into_the_next_request() -> None:
    """The `finally: reset(token)` the reference marks critical.

    Without it the value survives on a reused worker and correlates the NEXT request to the previous
    caller's id — worse than no id, because it looks right.
    """
    from service_kit.context import current_request_id

    seen: list[str] = []
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        seen.append(current_request_id())
        return {"ok": "yes"}

    client = TestClient(app)
    client.get("/thing", headers={"X-Request-ID": "first"})
    client.get("/thing", headers={"X-Request-ID": "second"})

    assert seen == ["first", "second"]
    assert current_request_id() != "second", "the id survived the response and will label the next request"


def test_a_log_record_carries_the_request_id() -> None:
    """The point of the whole chain: an operator can grep for what the caller quoted."""
    from service_kit.context import RequestIdFilter, request_id_ctx

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    token = request_id_ctx.set("deadbeef")
    try:
        assert RequestIdFilter().filter(record) is True
    finally:
        request_id_ctx.reset(token)

    assert record.request_id == "deadbeef"  # ty: ignore[unresolved-attribute]


def test_setup_logging_installs_the_filter() -> None:
    """A filter nobody installs is the same dead end this finding is about."""
    import inspect

    from service_kit import setup_logging

    assert "RequestIdFilter" in inspect.getsource(setup_logging)
