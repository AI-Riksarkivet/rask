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

**Estate-observer gated.** The create/list/get routes check ``can_observe_events`` on the fixed root object
(``settings.fga_root_object``) — the estate-OBSERVER action: it gates the estate-wide reads (the
``/v1/events`` feed AND this tenant enumeration), because listing every tenant's name, buckets and admins
is the same whole-estate disclosure as watching every tenant's governance changes. A mere project admin
gets a 403 (authorization scope == data scope, the ``/v1/events`` #12 rationale). ``/v1/projects`` matches
no resource prefix in ``fga_deps.authorize``, so the router-wide gate only sets the 401/503 floor; the
decision is made here explicitly via ``fga_deps.require_relation`` (audited allow/deny/outage, #41).

**DELETE is gated differently, on purpose: ``project:<id>#can_administer``.** Retiring a tenant is an act
INSIDE it, so the tenant's own admins must be able to do it without holding estate-wide privilege — and an
estate OBSERVER, whose bar is "may watch the whole estate", must not be able to delete a tenant they do not
administer. The delete itself is bottom-up (`open_hierarchy_lifecycle.md` Decision 3): it refuses 409 while
the project still holds warehouses, naming them, and carries no ``cascade`` at all (see ``delete_project``).

``admins`` comes from OpenFGA ``list_users`` (the #51 access-review primitive) on
``project:<id>#can_administer`` — EFFECTIVE admins (role assignees, team members, the cascade), not raw
tuples. It degrades to ``[]`` when FGA is off or unavailable (the registry-derived facts still serve;
an authz-lookup outage must not 500 the tenant list — the gate above already failed closed for callers).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    InvalidInputError,
    NamespaceNotEmptyError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.control_emit import emit_control
from catalog.core.identifiers import CONTROL_ID_RE
from catalog.services import projects as project_registry
from catalog.services import warehouses
from service_kit.governed import fga
from service_kit.governed.audit import SUCCESS, audit
from service_kit.lakehouse.records import RecordExistsError, RecordMissingError


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["projects"])

# The DNS-safe id shape the whole control plane enforces — a project id doubles as an FGA object id and a
# registry filename, so a malformed one is rejected up front. SHARED (`catalog.core.identifiers`): this
# `\Z` anchor was fixed here first and the two other copies kept the `$` that lets "acme\n" through.
_ID_RE = CONTROL_ID_RE


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


async def _converge_project(settings: Settings, so: dict[str, str], record: dict[str, str]) -> dict[str, str]:
    """The conditional re-POST write (diff2 F1 leg c) — shared by the sequential and lost-race paths.

    Both used to end in an unconditional `put_project` of a record assembled from a read taken earlier
    in the handler, so a `/protection` call landing in that window was silently reverted. The merge now
    happens inside the write, against the record as it actually stands.
    """
    try:
        return await run_in_threadpool(project_registry.upsert_project, settings.registry_root, so, record)
    except RecordMissingError:
        # Raced a concurrent tenant delete: retryable, never a blind re-create — code 14 -> 409, so
        # the caller retries into a clean mint rather than resurrecting a project just deleted.
        raise ConcurrentModificationError(f"project {record['id']!r} write raced a concurrent delete; retry") from None


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
    if existing is None:
        # The tenant MINT is conditional (F1): two concurrent creates of one id both read "absent"
        # above; the store refuses the second, which then converges on the winner's identity fields
        # exactly like the sequential idempotent re-POST.
        try:
            await run_in_threadpool(project_registry.create_project_record, settings.registry_root, so, record)
        except RecordExistsError:
            # Lost the mint race — converge on the WINNER, conditionally (diff2 F1 leg c). The merge
            # happens inside the write, against the record as it actually stands, so a protection
            # change landing in this window is preserved instead of clobbered.
            record = await _converge_project(settings, so, record)
    else:
        record = await _converge_project(settings, so, record)
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


class DeleteProjectResponse(BaseModel):
    """What the delete actually removed: the retired tenant and how many FGA tuples went with it.

    The count is reported rather than assumed — an operator retiring a tenant needs to see that the authz
    side really happened (``0`` on an FGA-off stack is a fact, not a silent success)."""

    project: str
    tuples_revoked: int


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    force: bool = False,
) -> DeleteProjectResponse:
    """Retire a tenant: refuse while it still holds warehouses, else revoke its tuples and drop its record.

    Gated on ``project:<id>#can_administer`` — the tenant's OWN admin bar, not the estate-observer gate the
    read routes use (module docstring). Order, fail-closed and each step earning the next:
    id shape (400) → existence (404) → authz (403) → deletion protection (409) → emptiness (409) →
    revoke tuples → delete record → audit + event.

    **Emptiness is bottom-up** (`open_hierarchy_lifecycle.md` Decision 3): a project holding warehouses
    refuses 409 and NAMES them — a refusal that does not say what blocks it just moves the search to the
    user. **There is deliberately no ``cascade`` parameter on this route at all.** A project cascade would
    reach warehouses, and a warehouse's own delete can purge a bucket; one request must never be able to
    destroy a tenant's storage transitively. Emptying goes one rung at a time through
    ``DELETE /v1/warehouses/{id}``, where the purge is its own separate opt-in.

    ``force=true`` overrides deletion PROTECTION only (Decision 5) — the FGA gate above ran first and runs
    identically with or without it, so forcing cannot delete a project the caller may not administer."""
    if not _ID_RE.match(project_id):
        raise InvalidInputError(f"invalid project id {project_id!r}: must match {_ID_RE.pattern}")
    so = settings.storage_options()
    # The record is read first because the gate needs the object it belongs to. Both outcomes below then
    # answer 404: a missing tenant and a caller who may not administer one are made INDISTINGUISHABLE.
    #
    # NO EXISTENCE ORACLE (audit #4 — the rule `_set_warehouse_status` established and
    # `delete_warehouse` follows): without the collapse, any authenticated caller could enumerate the
    # estate's tenants by the 404-vs-403 difference alone, and `GET /v1/projects` is estate-observer
    # gated precisely so they cannot. A create door legitimately answers 404 ("make the tenant first")
    # because that is a fix the caller needs; a DESTRUCTIVE door has no such teaching to do.
    record = await run_in_threadpool(project_registry.get_project, settings.registry_root, so, project_id)
    if record is None:
        raise TableNotFoundError(f"project not found: {project_id}")
    try:
        await fga_deps.require_relation(client, settings, token, relation="can_administer", obj=f"project:{project_id}")
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"project not found: {project_id}") from exc
    # Protection state and the warehouse listing below are both tenant DISCLOSURE, so they run only after
    # the gate above has proven the caller administers this tenant.
    fga_deps.require_not_protected(record, kind="project", obj_id=project_id, force=force)

    # A project's contents ARE its warehouse records — the same registry the listing derives tenants from,
    # so what the 409 names is exactly what GET /v1/projects/{id} shows.
    held = sorted(str(r["id"]) for r in await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so) if r.get("project") == project_id)
    if held:
        raise NamespaceNotEmptyError(
            f"project '{project_id}' still holds {len(held)} warehouse(s): {', '.join(held)}. "
            f"Delete each one first — DELETE /v1/warehouses/{{id}} (its own bucket purge is a separate "
            f"opt-in) — then delete the project. There is deliberately no cascade here: retiring a tenant "
            f"must never be able to destroy its buckets transitively in one request."
        )

    removed = 0
    revoked = False
    # Tuples FIRST, record second. A revoke that fails (OpenFGA outage → 503) leaves the tenant fully
    # described and re-deletable; the opposite order would strand grants on a project no API can name any
    # more — privilege that nothing in the product can see, let alone clear.
    if settings.fga_enabled and client is not None:
        removed = await fga.revoke_object_tuples(
            client,
            f"project:{project_id}",
            # The caller who retired the tenant. `system:catalog` is the honest fallback for an auth-off
            # stack, where there genuinely is no principal — never a stand-in for one we failed to thread.
            actor=token.sub if token is not None else "system:catalog",
            origin="lifecycle_delete",
        )
        revoked = True
    # That call removes every tuple whose OBJECT is `project:<id>` — the admin/member grants and the
    # `team` edge. Tuples where the project is the USER are a different set, and the one that would matter
    # is `warehouse:X#project@project:<id>`: none can survive here, because the emptiness refusal above
    # proves no warehouse record claims this project, and a warehouse's delete revokes its own tuples (that
    # edge among them) before its record goes. The annotator plane's `annotation_project:X#tenant@project:
    # <id>` edge is NOT covered by that check — it lives outside the warehouse registry. It confers nothing
    # once the project itself holds no admin/member tuples (there is no principal left to cascade from), and
    # a dangling cross-store edge is precisely the drift Decision 4's reconciler reports rather than guesses at.
    await run_in_threadpool(project_registry.delete_project_record, settings.registry_root, so, project_id)

    actor = f"user:{token.sub}" if token else None
    log.info("project_deleted", extra={"project": project_id, "tuples_revoked": removed, "forced": force})
    if revoked:
        # Mirrors create_project's grant audit, and only when the revoke really ran: an FGA-off stack must
        # not fabricate a compliance record for a revocation that never happened. The revoke is
        # subject-agnostic by design (it deletes EVERY tuple on the object), so the grantee is the wildcard
        # and `removed` carries the magnitude.
        audit(
            "access_revoke",
            SUCCESS,
            subject=actor,
            resource=f"project:{project_id}",
            grantee="*",
            relation="*",
            removed=removed,
            reason="project_deleted",
        )
    await emit_control(
        control,
        action="project_deleted",
        object_type="project",
        object_id=f"project:{project_id}",
        actor=actor,
    )
    return DeleteProjectResponse(project=project_id, tuples_revoked=removed)
