"""Which served routes belong to the Lance Namespace spec, as DATA rather than as a runtime read.

`handle_validation_error` has to answer a different status on a spec route than on a rask-only one, and
it cannot ask the spec: **no image carries `lance_docs/`** — verified across every `.docker/*.dockerfile`
and in the running catalog pod, which holds no `spec.yaml` at all (2026-09-14). So the set is committed
here and a test re-derives it from the vendored spec on every run, which is what keeps it from becoming
the hand-maintained tag set that would drift silently.

PATH PARAMETER NAMES ARE COLLAPSED to `{}`, the same normalisation `tests/integration/test_spec_conformance.py`
applies, because the spec and this estate do not have to agree on what a path parameter is CALLED — only
on the shape of the route. `/v1/table/{id}/describe` and `/v1/table/{table_id}/describe` are one route.

It is deliberately NOT the app's own OpenAPI: the catalog serves MORE than the spec (the `/credentials`
vending extension, the warehouse and project registries, health probes), so its own route table cannot
say which of those the spec owns.
"""

from __future__ import annotations

import re
from typing import Final


#: (METHOD, structural path) for every operation in the vendored spec. Re-derived and compared by
#: `test_spec_conformance.py`, so re-vendoring `lance_docs/ns_catalog/spec.yaml` reds the suite unless
#: this moves with it.
SPEC_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", "/v1/namespace/{}/list"),
        ("GET", "/v1/namespace/{}/table/list"),
        ("GET", "/v1/table"),
        ("POST", "/v1/materialized_view/{}/create"),
        ("POST", "/v1/materialized_view/{}/refresh"),
        ("POST", "/v1/namespace/{}/create"),
        ("POST", "/v1/namespace/{}/describe"),
        ("POST", "/v1/namespace/{}/drop"),
        ("POST", "/v1/namespace/{}/exists"),
        ("POST", "/v1/table/batch-commit"),
        ("POST", "/v1/table/version/batch-create"),
        ("POST", "/v1/table/{}/add_columns"),
        ("POST", "/v1/table/{}/alter_columns"),
        ("POST", "/v1/table/{}/analyze_plan"),
        ("POST", "/v1/table/{}/backfill_column"),
        ("POST", "/v1/table/{}/branches/create"),
        ("POST", "/v1/table/{}/branches/delete"),
        ("POST", "/v1/table/{}/branches/list"),
        ("POST", "/v1/table/{}/count_rows"),
        ("POST", "/v1/table/{}/create"),
        ("POST", "/v1/table/{}/create_index"),
        ("POST", "/v1/table/{}/create_scalar_index"),
        ("POST", "/v1/table/{}/declare"),
        ("POST", "/v1/table/{}/delete"),
        ("POST", "/v1/table/{}/deregister"),
        ("POST", "/v1/table/{}/describe"),
        ("POST", "/v1/table/{}/drop"),
        ("POST", "/v1/table/{}/drop_columns"),
        ("POST", "/v1/table/{}/exists"),
        ("POST", "/v1/table/{}/explain_plan"),
        ("POST", "/v1/table/{}/index/list"),
        ("POST", "/v1/table/{}/index/{}/drop"),
        ("POST", "/v1/table/{}/index/{}/stats"),
        ("POST", "/v1/table/{}/insert"),
        ("POST", "/v1/table/{}/merge_insert"),
        ("POST", "/v1/table/{}/query"),
        ("POST", "/v1/table/{}/register"),
        ("POST", "/v1/table/{}/rename"),
        ("POST", "/v1/table/{}/restore"),
        ("POST", "/v1/table/{}/schema_metadata/update"),
        ("POST", "/v1/table/{}/stats"),
        ("POST", "/v1/table/{}/tags/create"),
        ("POST", "/v1/table/{}/tags/delete"),
        ("POST", "/v1/table/{}/tags/list"),
        ("POST", "/v1/table/{}/tags/update"),
        ("POST", "/v1/table/{}/tags/version"),
        ("POST", "/v1/table/{}/update"),
        ("POST", "/v1/table/{}/update_field_metadata"),
        ("POST", "/v1/table/{}/version/create"),
        ("POST", "/v1/table/{}/version/delete"),
        ("POST", "/v1/table/{}/version/describe"),
        ("POST", "/v1/table/{}/version/list"),
        ("POST", "/v1/transaction/{}/alter"),
        ("POST", "/v1/transaction/{}/describe"),
    }
)


#: The status every spec operation may answer besides the ones ``spec.yaml`` lists for it: 406, the spec's status for
#: code 0, Unsupported (`lance_docs/ns_catalog/namespace/operations/errors.md`). The catalog answers it wherever a request
#: names a field or feature the catalog does not serve (a `timeout`, `properties`, a `branch` a door cannot honour, a
#: MemWAL, a feature flag), and the native backend answers it for a method it lacks, while ``spec.yaml`` lists 406 on
#: few operations; the published OpenAPI declares it on each (ADR 0020 row 11).
UNSUPPORTED_STATUS: Final = "406"

#: The error statuses the vendored spec lists for each of its operations, keyed as :data:`SPEC_ROUTES` is, which the
#: published OpenAPI declares on each (``lakehouse.ns_errors``) with the problem body every refusal carries, beside
#: :data:`UNSUPPORTED_STATUS`. Re-derived and compared by ``test_spec_conformance.py``, as :data:`SPEC_ROUTES` is.
SPEC_ERROR_STATUSES: Final[dict[tuple[str, str], frozenset[str]]] = {
    ("POST", "/v1/materialized_view/{}/create"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/materialized_view/{}/refresh"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/namespace/{}/create"): frozenset({"400", "401", "403", "404", "406", "409", "503", "5XX"}),
    ("POST", "/v1/namespace/{}/describe"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/namespace/{}/drop"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/namespace/{}/exists"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("GET", "/v1/namespace/{}/list"): frozenset({"400", "401", "403", "404", "406", "503", "5XX"}),
    ("GET", "/v1/namespace/{}/table/list"): frozenset({"400", "401", "403", "404", "406", "503", "5XX"}),
    ("GET", "/v1/table"): frozenset({"400", "401", "403", "503", "5XX"}),
    ("POST", "/v1/table/batch-commit"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/table/version/batch-create"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/table/{}/add_columns"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/alter_columns"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/analyze_plan"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/backfill_column"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/branches/create"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/table/{}/branches/delete"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/branches/list"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/count_rows"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/create"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/table/{}/create_index"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/create_scalar_index"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/declare"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/table/{}/delete"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/deregister"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/describe"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/drop"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/drop_columns"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/exists"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/explain_plan"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/index/list"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/index/{}/drop"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/index/{}/stats"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/insert"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/merge_insert"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/query"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/register"): frozenset({"400", "401", "403", "404", "406", "409", "503", "5XX"}),
    ("POST", "/v1/table/{}/rename"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/table/{}/restore"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/schema_metadata/update"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/stats"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/tags/create"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/table/{}/tags/delete"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/tags/list"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/tags/update"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/tags/version"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/update"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/update_field_metadata"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/version/create"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/table/{}/version/delete"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/version/describe"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/table/{}/version/list"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
    ("POST", "/v1/transaction/{}/alter"): frozenset({"400", "401", "403", "404", "409", "503", "5XX"}),
    ("POST", "/v1/transaction/{}/describe"): frozenset({"400", "401", "403", "404", "503", "5XX"}),
}


def structural(path_format: str) -> str:
    """``path_format`` with each path parameter's name collapsed to ``{}``, the key :data:`SPEC_ROUTES` uses."""
    return re.sub(r"\{[^}]+\}", "{}", path_format)


def is_spec_route(method: str, path_format: str) -> bool:
    """Whether ``(method, path_format)`` names an operation the Lance Namespace spec defines.

    ``path_format`` is Starlette's route pattern (``request.scope["route"].path_format``), NOT the
    request's concrete URL — the concrete one carries real ids and could never match. A request that
    reached a handler always has a route; one that did not is not a spec operation by definition, so a
    caller with nothing to pass gets ``False`` rather than an exception.
    """
    if not path_format:
        return False
    return (method.upper(), structural(path_format)) in SPEC_ROUTES
