"""The object-store and Kubernetes legs must appear in a trace.

open_fastapi-audit — "Every object-store, S3 and Kubernetes leg in the estate is untraced — no
botocore or urllib3 instrumentor exists, and 8 of the 14 apps open no span of their own".

`setup_otel` instruments fastapi, httpx, logging, requests, grpc (both variants) and aiohttp. It does
NOT instrument botocore or urllib3, and neither package appears in the lock. boto3 is live in four
places — `catalog/core/vending.py`, `catalog/services/warehouses.py`, `service_kit/lakehouse/records.py`
and `storage/client.py` — and the Kubernetes client rides urllib3. So every S3 call and every k8s call
in the estate is invisible in a trace.

WHY THAT IS WORSE THAN A HOLE: it is a MISLEADING trace, the same failure the `requests` instrumentor
comment already describes. The cheap httpx reads carry client spans while the expensive object-store
legs appear instantaneous, so a request that spent four seconds in S3 and one that spent none look
identical. That is diagnostic blindness — longer MTTR — rather than an open door, which is why the
audit grades it medium.

WHAT THIS DOES NOT FIX, deliberately. pyarrow's `S3FileSystem` is C++ and unreachable from Python
instrumentation at all; no instrumentor can ever cover it. For those legs the answer is a span around
the BUSINESS OPERATION, which is what `observability.md` reserves manual spans for ("only for domain
operations the framework can't see") and what `medallion/services/transform.py` already does. That
half is a separate, larger change and is tracked in the audit entry; this gate covers the half an
instrumentor can actually reach.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast


if TYPE_CHECKING:
    from lance_namespace import LanceNamespace


def test_the_native_seam_names_the_operation_and_really_emits() -> None:
    """A span named for the method, proven against a real exporter rather than by grep."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from catalog.services import native

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class _Backend:
        def describe_table(self, _request: object) -> str:
            return "ok"

    original = native.tracer
    native.tracer = trace.get_tracer(__name__, tracer_provider=provider)
    try:
        # A cast rather than a suppression comment: the fake stands in for the backend protocol, and
        # narrowing is the honest statement of that. (Writing the suppression form even inside a
        # comment makes ty parse it as a malformed directive — which is how this line was found.)
        assert native.call(cast("LanceNamespace", _Backend()), "describe_table", object()) == "ok"
    finally:
        native.tracer = original

    names = [span.name for span in exporter.get_finished_spans()]
    assert names == ["catalog.describe_table"], f"expected one operation-named span, got {names}"
