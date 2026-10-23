"""Materialized view endpoints (delegated to the native backend)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateMaterializedViewRequest,
    CreateMaterializedViewResponse,
    RefreshMaterializedViewRequest,
    RefreshMaterializedViewResponse,
)

from catalog.api.dependencies import DataNamespaceDep, NamespaceDep
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import native


router = APIRouter(prefix="/v1/materialized_view", tags=["materialized_view"])


# The spec's success status, declared on the decorator. All three answer a spec-correct 501 today
# (the `dir` backend stubs them), so nothing exercises the success path — which is exactly why a
# missing `status_code` would go unnoticed until a backend arrived and silently answered 200.
@router.post("/{id}/create", response_model_exclude_none=True, status_code=201)
async def create_materialized_view(
    id: str,
    body: CreateMaterializedViewRequest,
    ns: NamespaceDep,
) -> CreateMaterializedViewResponse:
    """Create a materialized view via the native backend's ``create_materialized_view``.

    Nothing is seeded: the ``dir`` backend answers 406 for every create, so no view exists to own, and the catalog
    mints no uuid for one (ADR 0173 §3 keys tables and namespaces). The backend that serves views gets its seed with
    its uuid.

    No OpenLineage is emitted here (unlike the cascade): the MV's source arrives ONLY as an opaque
    ``source_query`` blob the namespace server stores without interpreting, so there is no structured list
    of source tables to name in a lineage event. Emitting MV lineage is a decision-gate (WONTFIX until an
    MV consumer needs it) — it requires either a query parser or a structured ``source_tables`` request
    field; do NOT fabricate an edge from the view's own output schema. See docs/adr/0017-38b-mv-lineage-is-wontfix-no-source-tables.md #38b.
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    response: CreateMaterializedViewResponse = await run_in_threadpool(native.call, ns, "create_materialized_view", body)
    return response


# The spec's success status, declared on the decorator. All three answer a spec-correct 501 today
# (the `dir` backend stubs them), so nothing exercises the success path — which is exactly why a
# missing `status_code` would go unnoticed until a backend arrived and silently answered 200.
@router.post("/{id}/refresh", response_model_exclude_none=True, status_code=202)
def refresh_materialized_view(id: str, data_ns: DataNamespaceDep, body: RefreshMaterializedViewRequest | None = None) -> RefreshMaterializedViewResponse:
    """Rematerialize a materialized view via the native backend's ``refresh_materialized_view``."""
    req = body or RefreshMaterializedViewRequest()
    req.id = reconcile_body_id(parse_identifier(id), req.id)
    return native.call(data_ns, "refresh_materialized_view", req)
