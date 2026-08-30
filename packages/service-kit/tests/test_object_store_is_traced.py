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

import pathlib
from typing import TYPE_CHECKING, cast

import pytest


if TYPE_CHECKING:
    from lance_namespace import LanceNamespace

REPO = pathlib.Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("package", ["botocore", "urllib3"])
def test_the_instrumentor_is_a_declared_dependency(package: str) -> None:
    """A `with suppress(ImportError)` guard degrades silently, so the dep must be declared or the
    instrumentation is permanently inert while looking wired."""
    pyproject = (REPO / "packages/service-kit/pyproject.toml").read_text()
    assert f"opentelemetry-instrumentation-{package}" in pyproject, (
        f"opentelemetry-instrumentation-{package} is not a service-kit dependency, so the suppress(ImportError) guard around it would never find it"
    )


@pytest.mark.parametrize("package", ["botocore", "urllib3"])
def test_setup_otel_instruments_the_leg(package: str) -> None:
    source = (REPO / "packages/service-kit/src/service_kit/otel.py").read_text()
    assert f"opentelemetry.instrumentation.{package}" in source, (
        f"setup_otel never instruments {package} — every S3/k8s call is invisible in a trace, and the "
        f"instrumented httpx legs beside them make the trace misleading rather than merely incomplete"
    )


def test_the_instrumentors_actually_load() -> None:
    """The guard means a missing package degrades to silence. Prove these two are really importable,
    or both tests above pass against instrumentation that never runs."""
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor

    assert BotocoreInstrumentor is not None
    assert URLLib3Instrumentor is not None


def test_an_s3_call_emits_a_span() -> None:
    """End to end, against a real in-memory exporter: instrument, make a boto3 call, see a span.

    This is the assertion that cannot pass by accident. Both tests above are source greps — they would
    stay green if the instrumentor were installed but never took effect (wrong provider, called before
    the SDK exists, patched symbol renamed upstream).
    """
    import boto3
    from botocore.exceptions import ClientError
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    BotocoreInstrumentor().uninstrument()
    BotocoreInstrumentor().instrument(tracer_provider=provider)
    try:
        client = boto3.client(
            "s3",
            endpoint_url="http://127.0.0.1:1",  # nothing listens; the span is emitted regardless
            aws_access_key_id="k",
            aws_secret_access_key="s",
            region_name="us-east-1",
        )
        with pytest.raises((ClientError, Exception)):
            client.list_buckets()
    finally:
        BotocoreInstrumentor().uninstrument()

    names = [span.name for span in exporter.get_finished_spans()]
    assert names, "a boto3 S3 call produced no span even with the instrumentor active"


#: Catalog operations whose work is Lance/pyarrow — C++, and so unreachable from any Python
#: instrumentor. These are the ones a trace could never show.
_CATALOG_OPERATIONS = ("drop_table", "register_table", "undrop_table", "deregister_table")


def test_the_catalog_native_seam_opens_a_span_per_operation() -> None:
    """One span per catalog operation, at the seam rather than at four hand-picked routes.

    THE FINDING ASKS FOR FOUR SPANS; this is one seam that yields all of them and every sibling
    operation besides. `catalog.services.native.call` is the single choke point through which every
    spec operation reaches the backend — `method(*args)` IS the Lance/pyarrow/S3 work — so naming the
    span there covers all 54 operations at once, and a new one is covered on the day it is added
    rather than when somebody remembers to decorate it.

    It also stays on the right side of the reference's anti-pattern table. "Wrapping the whole route
    body" is listed there as duplicating the auto-created HTTP span; the instruction is to wrap the
    BUSINESS OPERATION inside the route, which is precisely what this seam is. Wrapping `drop_table`
    the function would have been the anti-pattern, and its body is 100 lines of load-bearing comments
    that re-indenting for a diagnostic improvement would put at risk for no extra coverage.
    """
    import inspect

    from catalog.services import native

    source = inspect.getsource(native.call)
    assert "start_as_current_span" in source, (
        "catalog.services.native.call opens no span — every catalog operation's Lance/S3 work is "
        "invisible, and pyarrow's S3FileSystem is C++ so no instrumentor can ever reach it"
    )


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


@pytest.mark.parametrize("operation", _CATALOG_OPERATIONS)
def test_each_named_operation_reaches_the_instrumented_seam(operation: str) -> None:
    """The finding names four operations; prove each one actually goes through the seam, or the
    coverage claim above is theory."""
    import ast

    source_path = REPO / "services/catalog/src/catalog/api/v1/endpoints/tables.py"
    tree = ast.parse(source_path.read_text())
    func = next(
        (node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == operation),
        None,
    )
    assert func is not None, f"{operation} no longer exists in tables.py — this gate needs re-anchoring"

    dumped = ast.dump(func)
    assert "native" in dumped and "call" in dumped, (
        f"{operation} does not reach the backend through `native.call`, so the seam's span does not cover it — it needs its own"
    )
