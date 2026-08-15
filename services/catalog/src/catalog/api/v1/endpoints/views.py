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

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, NamespaceDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import native


router = APIRouter(prefix="/v1/materialized_view", tags=["materialized_view"])


@router.post("/{id}/create", response_model_exclude_none=True)
async def create_materialized_view(
    id: str,
    body: CreateMaterializedViewRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
) -> CreateMaterializedViewResponse:
    """Create a materialized view via the native backend's ``create_materialized_view``.

    Then seeds FGA ownership on the ``materialized_view`` type so the creator keeps refresh/read rights.

    No OpenLineage is emitted here (unlike the cascade): the MV's source arrives ONLY as an opaque
    ``source_query`` blob the namespace server stores without interpreting, so there is no structured list
    of source tables to name in a lineage event. Emitting MV lineage is a decision-gate (WONTFIX until an
    MV consumer needs it) — it requires either a query parser or a structured ``source_tables`` request
    field; do NOT fabricate an edge from the view's own output schema. See docs/DECISIONS.md #38b.
    """
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    response: CreateMaterializedViewResponse = await run_in_threadpool(native.call, ns, "create_materialized_view", body)
    # Create is gated on the parent namespace (can_create_materialized_view); seed owner + the parent edge
    # on the ``materialized_view`` object so the creator keeps can_refresh/can_read on their own view and a
    # namespace writer inherits refresh rights via the cascade. Without it the creator would be locked out.
    # undo=None because there is nothing to call: this backend exposes no `drop_materialized_view`
    # (grep the tree — create and refresh are the only two view ops wired). So a failed seed still
    # strands the view, and routing through the compensating seam rather than bare `seed_ownership`
    # is the point: the day a drop op appears, the fix is one argument here, and until then this line
    # states the gap instead of hiding it behind a call that looks identical to the covered doors.
    await fga_deps.seed_ownership_or_compensate(client, settings, token, resource="materialized_view", segments=segments, undo=None)
    return response


@router.post("/{id}/refresh", response_model_exclude_none=True)
def refresh_materialized_view(
    id: str, ns: NamespaceDep, settings: SettingsDep, body: RefreshMaterializedViewRequest | None = None
) -> RefreshMaterializedViewResponse:
    """Rematerialize a materialized view via the native backend's ``refresh_materialized_view``."""
    req = body or RefreshMaterializedViewRequest()
    req.id = reconcile_body_id(parse_identifier(id, settings.delimiter), req.id)
    return native.call(ns, "refresh_materialized_view", req)
