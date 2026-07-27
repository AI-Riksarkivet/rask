"""``GET /v1/projects`` — first-class tenant enumeration, DERIVED from the warehouse registry (no new DB).

A *project* is the FGA model's tenant type (``services/common/auth/model.fga``): warehouses hang off it via
their ``project`` field, and every grant cascades down from it. The estate never stores a project record —
a project EXISTS exactly when a warehouse record claims it — so these endpoints derive the tenant list by
grouping the warehouse registry (``catalog.services.warehouses.list_warehouses``, which already skips a
corrupt/unreadable record with a warning rather than voiding the listing; the grouping here extends that
per-record tolerance to records missing their ``project``/``id`` fields). Creation stays IMPLICIT via
warehouse-create by design: ``POST /v1/warehouses`` (gated on ``project#can_create_warehouse``) is the one
door that mints a tenant, so there is no project-create endpoint to drift from the registry truth.

**Estate-observer gated.** Both routes check ``can_observe_events`` on the fixed root object
(``settings.fga_root_object``) — the estate-OBSERVER action: it gates the estate-wide reads (the
``/v1/events`` feed AND this tenant enumeration), because listing every tenant's name, buckets and admins
is the same whole-estate disclosure as watching every tenant's governance changes. A mere project admin
gets a 403 (authorization scope == data scope, the ``/v1/events`` #12 rationale). ``/v1/projects`` matches
no resource prefix in ``fga_deps.authorize``, so the router-wide gate only sets the 401/503 floor; the
decision is made here explicitly via ``fga_deps.require_relation`` (audited allow/deny/outage, #41).

``admins`` comes from OpenFGA ``list_users`` (the #51 access-review primitive) on
``project:<id>#can_administer`` — EFFECTIVE admins (role assignees, team members, the cascade), not raw
tuples. It degrades to ``[]`` when FGA is off or unavailable (the registry-derived facts still serve;
an authz-lookup outage must not 500 the tenant list — the gate above already failed closed for callers).
"""

from __future__ import annotations

import asyncio
import logging
import re

from common import fga
from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidInputError, ServiceUnavailableError, TableNotFoundError
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.services import warehouses


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["projects"])

# The same DNS-safe id shape the warehouse endpoints enforce (lowercase alnum + hyphen, 3-63 chars) — a
# project id doubles as an FGA object id and a registry field, so a malformed one is rejected up front.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class ProjectWarehouse(BaseModel):
    """One warehouse owned by the project — the registry facts an estate observer needs at a glance."""

    id: str
    bucket: str
    status: str
    # "gold" = the project's gold SERVING warehouse (DECISIONS "Medallion tiers"); None = a work warehouse.
    serving: str | None = None


class ProjectResponse(BaseModel):
    """A derived tenant: its name, the warehouses claiming it, and its effective FGA admins (``[]`` when
    FGA is off/unavailable — degraded, never fabricated)."""

    project: str
    warehouses: list[ProjectWarehouse]
    admins: list[str]


def _group_by_project(records: list[dict[str, str]]) -> dict[str, list[ProjectWarehouse]]:
    """Group warehouse records by their ``project``, sorted for a deterministic response.

    Per-record corruption tolerance, same posture as ``warehouses.list_warehouses``: a record missing its
    ``project`` or ``id`` is SKIPPED with a warning — one tenant's registry corruption must not void the
    whole estate listing. ``status`` defaults to ``"active"`` for pre-lifecycle records (the
    ``warehouse_status`` backward-compat rule).
    """
    grouped: dict[str, list[ProjectWarehouse]] = {}
    for record in records:
        project, warehouse_id = record.get("project"), record.get("id")
        if not project or not warehouse_id:
            log.warning(
                "project_record_skipped",
                extra={"warehouse": warehouse_id, "fields": sorted(record)},
            )
            continue
        grouped.setdefault(str(project), []).append(
            ProjectWarehouse(
                id=str(warehouse_id),
                bucket=str(record.get("bucket") or ""),
                status=str(record.get("status") or "active"),
                serving=str(record["serving"]) if record.get("serving") else None,
            )
        )
    for entries in grouped.values():
        entries.sort(key=lambda w: w.id)
    return grouped


# Per-project budget for the display-only admins lookup. The BFF gives the whole request ~8s; a slow (not
# down) OpenFGA must never spend that N-projects-over: one attempt (no retries — the wrapper's default
# bounded-retry ladder alone can exceed the budget) under a tight deadline, degrading to [] on expiry.
_ADMINS_TIMEOUT_SECONDS = 2.0


async def _admins_for(client: OpenFgaClient | None, settings: Settings, project: str) -> list[str]:
    """The project's effective ``can_administer`` subjects via ``fga.list_users`` (#51), or ``[]``.

    Degrades — never 500s: FGA off/unwired yields ``[]`` outright; an OpenFGA outage
    (``ServiceUnavailableError``) or a lookup blowing the per-call ``_ADMINS_TIMEOUT_SECONDS`` budget is
    logged and yields ``[]`` too, so the registry-derived facts still serve while the authz lookup is
    down or slow. The estate gate already ran fail-closed before this, so a degraded ``admins`` is a
    display gap, not an authz gap.
    """
    if not (settings.fga_enabled and client is not None):
        return []
    try:
        async with asyncio.timeout(_ADMINS_TIMEOUT_SECONDS):
            return await fga.list_users(client, relation="can_administer", obj=f"project:{project}", retry_attempts=1)
    except (ServiceUnavailableError, TimeoutError):
        log.warning("project_admins_unavailable", extra={"project": project})
        return []


async def _load_grouped(settings: Settings) -> dict[str, list[ProjectWarehouse]]:
    """Read the registry (blocking S3/FS IO → threadpool) and group it by project."""
    records = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, settings.storage_options())
    return _group_by_project(records)


@router.get("")
async def list_projects(settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> list[ProjectResponse]:
    """Enumerate the estate's tenants, derived from the warehouse registry. Estate-observer gated.

    Fail-closed via FGA on the read: a non-estate-observer gets 403, an OpenFGA outage 503, and the gate
    is a no-op only when FGA is off (parity with every ``require_*`` site). ``admins`` degrades to ``[]``
    when FGA is off or the ``list_users`` lookup is unavailable — the registry facts still serve."""
    # Estate gate: can_observe_events on the fixed root object — the estate-OBSERVER action shared with
    # /v1/events (whole-estate disclosure ⇒ whole-estate privilege; no caller-supplied object id).
    await fga_deps.require_relation(client, settings, token, relation="can_observe_events", obj=settings.fga_root_object)
    grouped = await _load_grouped(settings)
    # The per-project admins lookups run CONCURRENTLY: sequential awaits would stack N × the per-call
    # budget on a slow OpenFGA (a brownout, not an outage) and blow past the BFF's request deadline;
    # gathered, the worst case is ONE _ADMINS_TIMEOUT_SECONDS regardless of tenant count (each call
    # still degrades to [] on its own expiry — see _admins_for).
    projects = sorted(grouped)
    admins = await asyncio.gather(*(_admins_for(client, settings, project) for project in projects))
    return [
        ProjectResponse(project=project, warehouses=grouped[project], admins=project_admins) for project, project_admins in zip(projects, admins, strict=True)
    ]


@router.get("/{project_id}")
async def get_project(project_id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> ProjectResponse:
    """One derived tenant — the same shape as the list. Estate-observer gated; unknown project → 404."""
    if not _ID_RE.match(project_id):
        raise InvalidInputError(f"invalid project id {project_id!r}: must match {_ID_RE.pattern}")
    await fga_deps.require_relation(client, settings, token, relation="can_observe_events", obj=settings.fga_root_object)
    grouped = await _load_grouped(settings)
    entries = grouped.get(project_id)
    if entries is None:
        raise TableNotFoundError(f"project not found: {project_id}")
    return ProjectResponse(project=project_id, warehouses=entries, admins=await _admins_for(client, settings, project_id))
