"""``/v1/projects`` — first-class tenants: an explicit registry record, created in ONE operation with its tuples.

A *project* is the FGA model's tenant type (``service_kit/governed/auth/model.fga``): warehouses hang off it
via their ``project`` field, and every grant cascades down from it. A project EXISTS when its registry
record does (``catalog/services/projects.py`` — ``_projects/<id>.json`` on the control root, no DB), and
``POST /v1/projects`` writes that record AND the creator's ``admin`` tuple in one operation, so existence
and permission cannot drift (`open_hierarchy_lifecycle.md`, Decision 1 — the live estate once held three
tenants in authz the catalog had never heard of, seeded FGA-only).

Creation used to be IMPLICIT via warehouse-create, with an estate-admin bootstrap exception for the first
warehouse of a not-yet-existing project. That door is gone: the estate admin creates the PROJECT here,
then that project's admins create warehouses (``require_project_exists`` refuses a warehouse for an
unknown tenant with a 404 that names this route). One way to make a tenant.

The LISTING is the union of the registry and the warehouse-derived view: registry records define the
tenants (a fresh project legitimately has zero warehouses), while a warehouse claiming a project with no
record still surfaces — that is pre-registry legacy the reconciler reports, and hiding it would make the
drift invisible instead of fixable.

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
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidInputError, ServiceUnavailableError, TableNotFoundError
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.control_emit import emit_control
from catalog.services import projects as project_registry
from catalog.services import warehouses
from service_kit.governed import fga
from service_kit.governed.audit import SUCCESS, audit


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
    """The tenant map: PROJECT REGISTRY ∪ warehouse-derived (blocking S3/FS IO → threadpool).

    Registry records define the tenants — a fresh project has zero warehouses and must still list.
    Warehouse-derived entries without a registry record are pre-registry legacy: kept visible (the
    reconciler reports them as drift; a listing that hid them would make the drift undiagnosable)."""
    so = settings.storage_options()
    records = await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so)
    grouped = _group_by_project(records)
    for record in await run_in_threadpool(project_registry.list_projects, settings.registry_root, so):
        grouped.setdefault(str(record["id"]), [])
    return grouped


class CreateProjectRequest(BaseModel):
    """The create payload. Just the id — a tenant is a name, its admins, and what it later holds."""

    id: str


@router.post("", response_model_exclude_none=True)
async def create_project(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    body: CreateProjectRequest,
) -> ProjectResponse:
    """Mint a tenant: registry record + the creator's ``admin`` tuple, in one operation. Estate-admin gated.

    The gate is ``can_observe_events`` on the fixed root object — the estate-admin bar every whole-estate
    surface uses. Idempotent: re-POSTing an existing id carries ``created_at``/``created_by``/``protected``
    forward and re-asserts the admin tuple (a duplicate FGA write is a no-op), so a partial-failure retry
    (record written, tuple write failed) converges instead of erroring. The grant is audited and published
    like any other ACL change, only when it was actually written."""
    project_id = body.id
    if not _ID_RE.match(project_id):
        raise InvalidInputError(f"invalid project id {project_id!r}: must match {_ID_RE.pattern}")
    await fga_deps.require_relation(client, settings, token, relation="can_observe_events", obj=settings.fga_root_object)
    so = settings.storage_options()
    existing = await run_in_threadpool(project_registry.get_project, settings.registry_root, so, project_id)
    actor = f"user:{token.sub}" if token else None
    record = {
        "id": project_id,
        "created_at": (existing.get("created_at") if existing else None) or datetime.now(UTC).isoformat(),
        "created_by": (existing.get("created_by") if existing else None) or (token.sub if token else ""),
        # Deletion protection (Decision 5): mutable lifecycle field, carried forward on re-create —
        # an idempotent re-POST must never silently strip protection, same rule as warehouse `status`.
        "protected": (existing.get("protected") if existing else None) or "false",
    }
    await run_in_threadpool(project_registry.put_project, settings.registry_root, so, record)
    granted = await fga_deps.seed_project_admin(client, settings, token, project=project_id)
    log.info("project_created", extra={"project": project_id, "existing": existing is not None})
    await emit_control(
        control,
        action="project_created",
        object_type="project",
        object_id=f"project:{project_id}",
        actor=actor,
    )
    if granted:
        audit(
            "access_grant",
            SUCCESS,
            subject=actor,
            resource=f"project:{project_id}",
            grantee=actor,
            relation="admin",
            reason="project_created",
        )
        await emit_control(
            control,
            action="grant_added",
            object_type="grant",
            object_id=f"project:{project_id}",
            actor=actor,
            extra={"relation": "admin", "subject": actor, "reason": "project_created"},
        )
    return ProjectResponse(project=project_id, warehouses=[], admins=await _admins_for(client, settings, project_id))


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
