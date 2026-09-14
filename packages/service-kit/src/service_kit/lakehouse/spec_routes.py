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


def is_spec_route(method: str, path_format: str) -> bool:
    """Whether ``(method, path_format)`` names an operation the Lance Namespace spec defines.

    ``path_format`` is Starlette's route pattern (``request.scope["route"].path_format``), NOT the
    request's concrete URL — the concrete one carries real ids and could never match. A request that
    reached a handler always has a route; one that did not is not a spec operation by definition, so a
    caller with nothing to pass gets ``False`` rather than an exception.
    """
    import re

    if not path_format:
        return False
    return (method.upper(), re.sub(r"\{[^}]+\}", "{}", path_format)) in SPEC_ROUTES
