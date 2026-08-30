"""The request id the estate hands the caller must reach the trace.

open_fastapi-audit — "No `server_request_hook` anywhere: `X-Request-ID` is minted, echoed to the
client, and joined to no span and no log".

`RequestIDMiddleware` mints an id, stores it on `request.state` and echoes it in the response header;
without a hook on the instrumentation that id reaches no span, and a caller holding an `X-Request-ID`
from a failed call has a value that correlates with nothing. The hook is the join, and it belongs in
`setup_otel` rather than in another middleware: it runs INSIDE the instrumentation and buffers
nothing, so it is safe on the `/api/explorer` Range streaming that 206 video seeking depends on, where
a `BaseHTTPMiddleware` layer is refused outright (`service_kit.media.middleware`).

THE HOOK'S REACH IS THE `setup_otel` FAMILY, not the estate. It is a Python callable passed to
`FastAPIInstrumentor.instrument_app`, so only an app that wires the SDK in process can install it —
the five `make_service_app` services and the gateway. The eight the chart launches under
`opentelemetry-instrument` carry the id in their logs and on no span; which family takes which OTel
path is pinned by `tests/unit/test_the_app_roster_has_no_fifth_shape.py`.

PII STAYS OFF THE SPAN. The reference is explicit: "Don't put PII (email, raw user id, auth tokens) in
span attributes — those go to the trace backend forever." The estate is clean there today and this
must not be the change that stops it, so the hook is pinned to the header set it may read.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING, cast

import pytest


if TYPE_CHECKING:
    from opentelemetry.trace import Span

REPO = pathlib.Path(__file__).resolve().parents[3]


def test_setup_otel_installs_a_server_request_hook() -> None:
    source = (REPO / "packages/service-kit/src/service_kit/otel.py").read_text()
    assert "server_request_hook" in source, (
        "setup_otel installs the FastAPI instrumentor with no hook, so the X-Request-ID the estate "
        "mints and echoes reaches no span — the caller's id correlates with nothing"
    )


def test_the_hook_puts_the_request_id_on_the_span() -> None:
    """End to end against a real exporter: send the header, read the attribute off the span."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from service_kit.otel import server_request_hook

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = FastAPI()

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, server_request_hook=server_request_hook)
    try:
        TestClient(app).get("/thing", headers={"X-Request-ID": "abc123"})
    finally:
        FastAPIInstrumentor.uninstrument_app(app)

    attributes = [dict(span.attributes or {}) for span in exporter.get_finished_spans()]
    assert any(attrs.get("request.id") == "abc123" for attrs in attributes), f"no span carried request.id; got {attributes}"


def test_the_hook_survives_a_request_with_no_id() -> None:
    """A missing header must not raise — the hook runs on every request in every app."""
    from service_kit.otel import server_request_hook

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attrs[key] = value

    span = _Span()
    server_request_hook(cast("Span", span), {"type": "http", "headers": []})
    assert span.attrs == {}


def test_the_hook_is_a_no_op_on_a_non_recording_span() -> None:
    """The reference's own guard: `if not (span and span.is_recording()): return`."""
    from service_kit.otel import server_request_hook

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}

        def is_recording(self) -> bool:
            return False

        def set_attribute(self, key: str, value: object) -> None:  # pragma: no cover - must not run
            self.attrs[key] = value

    span = _Span()
    server_request_hook(cast("Span", span), {"type": "http", "headers": [(b"x-request-id", b"abc")]})
    assert span.attrs == {}
    server_request_hook(None, {"type": "http", "headers": [(b"x-request-id", b"abc")]})


@pytest.mark.parametrize("header", ["authorization", "cookie", "x-user-email", "x-user"])
def test_the_hook_reads_no_PII_header(header: str) -> None:
    """ "Don't put PII (email, raw user id, auth tokens) in span attributes — those go to the trace
    backend forever." The estate is clean today; this must not be the change that ends that."""
    from service_kit.otel import server_request_hook

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attrs[key] = value

    span = _Span()
    server_request_hook(cast("Span", span), {"type": "http", "headers": [(header.encode(), b"secret-value")]})
    assert "secret-value" not in str(span.attrs), f"the hook copied a {header} header onto the span"


def test_the_gateway_mints_a_request_id_for_the_whole_trace() -> None:
    """One value across every hop, minted at the front door.

    The Fix asks for this specifically: "Mint the id at the gateway so one value spans the whole
    trace." The gateway builds its own FastAPI and runs neither `register_middleware` nor the media
    one, so it minted nothing — every downstream service invented its own id for the same request, and
    the caller's echoed value matched none of them. An id that differs per hop correlates nothing,
    which is the same failure as having none.

    `X-Request-ID` is not hop-by-hop, so once the gateway sets it the proxy forwards it unchanged and
    every downstream service echoes rather than re-mints it — one value across every hop's logs, and
    across the spans of the apps that wire OTel in process (see the module docstring).
    """
    import importlib

    from fastapi.testclient import TestClient

    import gateway

    gw = importlib.reload(gateway)
    with TestClient(gw.app) as client:
        response = client.get("/healthz")
    assert response.headers.get("X-Request-ID"), (
        "the gateway echoes no X-Request-ID, so each downstream service mints its own for the same request and the caller's id matches nothing"
    )

    with TestClient(gw.app) as client:
        echoed = client.get("/healthz", headers={"X-Request-ID": "caller-supplied"})
    assert echoed.headers.get("X-Request-ID") == "caller-supplied", (
        "the gateway replaced a caller-supplied id — a client correlating its own logs across the boundary loses the join"
    )
