"""Delegate an operation to the native ``LanceNamespace`` backend.

Thin pass-through used by endpoints for every operation the native backend
implements. Absent methods and backend stubs are translated to
``UnsupportedOperationError`` (HTTP 501).
"""

from __future__ import annotations

from typing import Any

from lance_namespace import LanceNamespace, UnsupportedOperationError
from opentelemetry import trace

from service_kit.lakehouse.ns_errors import as_unsupported_if_stub


# These three native ``DirectoryNamespace`` methods are typed ``request: dict`` and forward to Rust WITHOUT
# calling ``request.model_dump()`` themselves — unlike every sibling, which takes the pydantic model and
# dumps it internally. Passing the pydantic model to them raises ``TypeError: ... is not an instance of
# 'Mapping'`` (which a too-broad stub hint previously laundered into a fake 501 — they are actually backed).
# So ``call`` dumps pydantic args to a dict for exactly these. (Confirmed against lance_namespace; the
# audit traced the masked 501s here.)
_DICT_REQUEST_METHODS = frozenset({"create_table_version", "describe_table_version", "batch_delete_table_versions"})


def _as_dict(arg: object) -> object:
    """Marshal a pydantic request model to a plain dict (pass non-models through unchanged)."""
    dump = getattr(arg, "model_dump", None)
    return dump() if callable(dump) else arg


#: Module-level and REBINDABLE: a test points this at an in-memory exporter to prove the span is
#: really emitted rather than merely written.
tracer = trace.get_tracer(__name__)


def call(ns: LanceNamespace, method_name: str, *args: object) -> Any:
    """Invoke ``method_name`` on the backend, mapping absent/stub methods to 501.

    Returns the backend's response model verbatim (typed ``Any`` so endpoints can
    annotate the concrete response model for OpenAPI + serialization).
    """
    method = getattr(ns, method_name, None)
    if method is None:
        raise UnsupportedOperationError(f"Not supported: {method_name}")
    if method_name in _DICT_REQUEST_METHODS:
        # Marshal the pydantic request to a dict — these methods expect a Mapping, not the model.
        args = tuple(_as_dict(a) for a in args)
    # THE ONE PLACE THE CATALOG'S REAL WORK IS VISIBLE. `method(*args)` is the Lance/pyarrow/S3 leg,
    # and pyarrow's `S3FileSystem` is C++ — unreachable from any Python instrumentor, ever. So a trace
    # showed one flat HTTP span for a drop that touched the object store, the Lance manifest and the
    # namespace registry, and a four-second drop looked exactly like an instant one.
    #
    # AT THE SEAM, not at four hand-picked routes. Every spec operation reaches the backend through
    # here, so one span definition covers all 54 and a new operation is covered on the day it is added
    # rather than when somebody remembers to decorate it. It is also the BUSINESS operation rather than
    # the route body — the reference lists wrapping a whole route as an anti-pattern, because that just
    # duplicates the span FastAPIInstrumentor already emits.
    with tracer.start_as_current_span(f"catalog.{method_name}") as span:
        span.set_attribute("lance.catalog.operation", method_name)
        try:
            return method(*args)
        except Exception as exc:
            translated = as_unsupported_if_stub(exc)
            if translated is exc:
                raise
            raise translated from exc
