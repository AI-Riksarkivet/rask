"""Access-review endpoints (#51): who holds which ``can_*`` action on a table or namespace.

``POST …/access/list`` answers the standing-access question the #41 audit trail cannot ("who *can*
read this?", not "who *did*"): for every ``can_*`` action the compiled model defines on the type, it
asks OpenFGA ListUsers — which expands role assignees, nested roles, and the parent cascade, so the
answer is effective access, not just direct tuples. The relation set comes from the model the app
actually loads (never a hand-kept list), so a model edit is reflected here automatically and
``test_fga_model_contract`` proves every queried pair exists.

Gated by the router-level ``authorize`` on ``can_review_access``, the owner's and the grant manager's (an enumeration
reveals principals, so a modifier, who holds the drop's rung and grants nothing, does not pass it, [[LH-570]]); the
gate audits the authz decision, and the endpoint additionally emits a dedicated
``access_review`` audit event (the disclosure itself, distinguishable from an actual drop — the same
two-layer pattern as credential vending). Fail-closed: an OpenFGA outage is a 503, never an empty
grant list that reads as "nobody has access".

Two properties of "effective access" are accepted by design and worth knowing when reading a review:
the expansion follows the parent cascade, so a leaf-table owner sees individual grantees who hold
access only via namespace/warehouse/project role grants (within-tenant upward visibility —
the cascade never crosses projects); and OpenFGA's ListUsers has no pagination, so a relation held
by more subjects than the server's ``listUsersMaxResults`` cap returns a partial list (the wrapper
logs a warning when a result looks capped).
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, Depends, Request
from lance_namespace import InvalidInputError, NamespaceNotFoundError, ServiceUnavailableError, TableNotFoundError, UnauthenticatedError
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from catalog.api import announcing
from catalog.api.announcing import Announcer, AnnouncerDep
from catalog.api.authz import decisions, door_gates, grant_authority
from catalog.api.authz.context import AuthzContext, AuthzContextDep
from catalog.api.dependencies import FgaClientDep, Resolver, ResolverDep, SettingsDep
from catalog.core.identifiers import parse_identifier
from catalog.schemas import (
    AccessCheckRequest,
    AccessCheckResponse,
    AccessGrantRequest,
    AccessGrantResponse,
    AccessGraphResponse,
    AccessListResponse,
    GraphEdge,
    GraphNode,
    ManagedAccessRequest,
    ManagedAccessResponse,
    MyPermissionsResponse,
    RelationGrants,
)
from lakehouse.governed import fga_ids, fga_model, fga_reads, fga_writes
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.settings import FgaSettings


log = logging.getLogger(__name__)

#: THE MANAGEMENT SURFACE ([[LH-021]]). rask's authorization model is not in the Lance namespace spec
#: at ANY point — grants, policies and managed access are entirely rask's own — so a spec client
#: discovering `/v1/table/{id}/access/grant` meets a verb the document never defines. These mount at
#: `/management/v1` beside the rest of the estate's governance, and inherit the same three rungs
#: from `api/v1/router.py` as the spec routers do.
table_router = APIRouter(prefix="/management/v1/table", tags=["access"])
namespace_router = APIRouter(prefix="/management/v1/namespace", tags=["access"])
# Warehouse is NOT in rungs._RESOURCES, so `authorize` returns early for these paths — every route
# mounted here must gate itself explicitly (see `set_warehouse_managed_access`).
warehouse_router = APIRouter(prefix="/management/v1/warehouse", tags=["access"])
# Same rule as the warehouse router above: `/management/v1/projects/…` is outside `rungs._RESOURCES`, so
# `authorize` returns early and every route here gates itself. Its own router rather than a route on
# the projects module so the access surface stays in one file — the place someone looks when asking
# "what is gated, and how".
project_router = APIRouter(prefix="/management/v1/projects", tags=["access"])
#: The classification VOCABULARY ([[LH-055]]). Its own router because a classification is not a data
#: object: it has no rows, no parent warehouse and no `id` to parse — the path segment IS the label.
classification_router = APIRouter(prefix="/management/v1/classification", tags=["access"])


# The base rungs an admin may directly assign. The model defines each as directly assignable
# (lakehouse/governed/auth/model.fga) — a real user/userset grant, unlike the derived ``can_*`` actions or the
# structural ``parent``/``child`` edges, none of which may be hand-granted. Intersected with the model below so a
# renamed rung drops out (and test_fga_model_contract would catch the drift).
#
# ``manage_grants`` and ``pass_grants`` are here because grant power is now its own axis rather than a
# side-effect of ``owner``: leaving them out would define the delegation the model describes and provide
# no way to confer it. They are not more dangerous than ``owner`` — both are reachable only through
# ``can_grant_manage_grants`` / ``can_grant_pass_grants``, which are ``manage_grants``-only, so a
# grant-option delegate can neither mint further delegates nor promote themselves.
#: The rungs this surface may hand out: every one the model gives a `can_grant_<rung>`, other than the tenancy rungs
#: the members door serves. A service's own identity is granted here like a person's ([[LH-563]]): `maintainer` for a
#: bring-your-own compaction job, `decider` on a project or a warehouse for a service asking the decision door there.
#: `tagger` and the warehouse's `maintenance` have no grant action in the model, so this surface hands out neither.
#:
#: IT MUST COVER EVERY `can_grant_<rung>` THE MODEL DECLARES, and did not — `classifier` shipped with
#: its grant action and the argument for it written into `model.fga`, while this hand-kept tuple
#: refused the rung 400 at the only door that could delegate it. `grant_authority.grant_actions` reads the
#: model and this does not, so the two drifted in silence.
#: `services/catalog/tests/test_a_grantable_rung_has_a_door.py` compares them now.
_GRANTABLE_BASE: tuple[str, ...] = (
    "owner",
    "modifier",
    "writer",
    "reader",
    "describer",
    "maintainer",
    "manage_grants",
    "pass_grants",
    "classifier",
    "apply",
    "decider",
)


async def _target(resolver: Resolver, fga_type: str, segments: list[str]) -> tuple[str, str | None]:
    """The FGA object the door's id names and its name: a table's or namespace's uuid object (``table:<uuid>``) beside
    the name the door was addressed by ([[LH-524]]); a classification label, a warehouse or a project by its own id.

    Raises:
        TableNotFoundError: No table has the name.
        NamespaceNotFoundError: No namespace has the name.
    """
    canonical = fga_ids.canonical_object_id(segments)
    if fga_type not in ("table", "namespace"):
        return f"{fga_type}:{canonical}", None
    obj = await resolver.object(fga_type, segments)
    if obj is None:
        raise (TableNotFoundError if fga_type == "table" else NamespaceNotFoundError)(f"{fga_type} not found: {canonical}")
    return obj, canonical


@lru_cache
def _grantable_relations(fga_type: str) -> tuple[str, ...]:
    """The base rungs grantable on ``fga_type`` — ``_GRANTABLE_BASE`` restricted to what the model defines."""
    for td in fga_model.load_model()["type_definitions"]:
        if td["type"] == fga_type:
            defined = td.get("relations") or {}
            return tuple(r for r in _GRANTABLE_BASE if r in defined)
    return ()


@lru_cache
def _can_relations(fga_type: str) -> tuple[str, ...]:
    """Every ``can_*`` action the compiled model defines on ``fga_type``, sorted.

    Read from ``model.json`` (what the app loads) so the enumeration can never drift into a phantom
    relation — OpenFGA answers those with a 400 that fails closed to a 503 for every caller.
    """
    for td in fga_model.load_model()["type_definitions"]:
        if td["type"] == fga_type:
            return tuple(sorted(r for r in (td.get("relations") or {}) if r.startswith("can_")))
    return ()


async def _access_list(context: AuthzContext, *, resolver: Resolver, fga_type: str, id: str) -> AccessListResponse:
    client = decisions.require_fga(context.settings, context.client, feature="access review")
    segments = parse_identifier(id)
    obj, name = await _target(resolver, fga_type, segments)
    relations = _can_relations(fga_type)
    subject = context.token.subject if context.token else "anonymous"
    try:
        # TaskGroup, not gather: one failed relation cancels the siblings, so a degraded OpenFGA is
        # never hammered by up-to-nine orphaned retry loops after the request has already 503'd.
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(fga_reads.list_users(client, relation=r, obj=obj)) for r in relations]
    except* ServiceUnavailableError as outage:
        # #41: the review FAILED mid-enumeration — without this, the gate's earlier allow would be
        # the only trace, indistinguishable from a completed disclosure.
        audit(AuditAction.ACCESS_REVIEW, AuditOutcome.FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise outage.exceptions[0] from None
    # #41 audit the actual ACL disclosure (who reviewed what) — the gate's can_review_access allow
    # alone would be byte-identical to a pending destructive op.
    audit(AuditAction.ACCESS_REVIEW, AuditOutcome.SUCCESS, subject=subject, resource=obj)
    # TRUNCATION IS REPORTED, not just logged (diff2 F10 item 8). `list_users` stops at OpenFGA's
    # `listUsersMaxResults` and warned into the log; the response looked complete. On this surface that
    # is the dangerous direction to be wrong in — a reviewer reading 1000 of 1500 holders concludes the
    # other 500 hold nothing, which is the opposite of the truth and is exactly the answer an access
    # review exists to prevent. Inferred from the length against the cap, the same way the admin door
    # (`AccessListUsersResponse.truncated`) has always done it.
    grants = [
        RelationGrants(relation=relation, users=(users := task.result()), truncated=len(users) >= fga_reads.LIST_USERS_SERVER_CAP)
        for relation, task in zip(relations, tasks, strict=True)
    ]
    if any(g.truncated for g in grants):
        log.warning("access_review_truncated", extra={"object": obj, "relations": [g.relation for g in grants if g.truncated]})
    return AccessListResponse(object=obj, name=name, grants=grants, truncated=any(g.truncated for g in grants))


@table_router.post("/{id}/access/list")
async def list_table_access(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> AccessListResponse:
    """Effective access on the table, per ``can_*`` action — the router asks ``can_review_access``, the owner's and the grant manager's."""
    return await _access_list(context, resolver=resolver, fga_type="table", id=id)


@namespace_router.post("/{id}/access/list")
async def list_namespace_access(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> AccessListResponse:
    """Effective access on the namespace, per ``can_*`` action — the router asks ``can_review_access``."""
    return await _access_list(context, resolver=resolver, fga_type="namespace", id=id)


async def require_warehouse_review(id: str, context: AuthzContextDep) -> None:
    """Gate a warehouse's access listing on ``can_read_assignments``, the model's rung for seeing who holds what
    (``manage_grants``, which a project's admin holds through ``can_administer from project``) ([[LH-555]]). A route
    dependency, since the router leaves every warehouse route to its handler."""
    await door_gates.require_relation(context, relation="can_read_assignments", obj=f"warehouse:{id}")


async def require_warehouse_grant(id: str, *, request: Request, context: AuthzContextDep, resolver: ResolverDep) -> None:
    """Gate a warehouse grant as the router gates a table's or a namespace's: ``can_grant_<rung>`` read from the body, and
    no grant that raises the caller's own access ([[LH-555]])."""
    await grant_authority.require_grant_authority(request, context, resolver=resolver, fga_type="warehouse", segments=parse_identifier(id))


async def require_warehouse_revoke(id: str, *, request: Request, context: AuthzContextDep, resolver: ResolverDep) -> None:
    """Gate a warehouse revoke on ``can_revoke_grant``, stricter than any grant ([[LH-555]])."""
    await grant_authority.require_revoke_authority(request, context, resolver=resolver, fga_type="warehouse", segments=parse_identifier(id))


async def require_project_review(id: str, context: AuthzContextDep) -> None:
    """Gate a project's access listing on ``can_read_assignments``, which its security admin holds ([[LH-563]]). A route
    dependency, since the router leaves every project route to its handler."""
    await door_gates.require_relation(context, relation="can_read_assignments", obj=f"project:{id}")


async def require_project_grant(id: str, *, request: Request, context: AuthzContextDep, resolver: ResolverDep) -> None:
    """Gate a project grant as a warehouse's is gated: ``can_grant_<rung>`` read from the body, and no grant that raises
    the caller's own access ([[LH-563]])."""
    await grant_authority.require_grant_authority(request, context, resolver=resolver, fga_type="project", segments=parse_identifier(id))


async def require_project_revoke(id: str, *, request: Request, context: AuthzContextDep, resolver: ResolverDep) -> None:
    """Gate a project revoke on ``can_revoke_grant``, its security admin's ([[LH-563]])."""
    await grant_authority.require_revoke_authority(request, context, resolver=resolver, fga_type="project", segments=parse_identifier(id))


@warehouse_router.post("/{id}/access/list", dependencies=[Depends(require_warehouse_review)])
async def list_warehouse_access(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> AccessListResponse:
    """Effective access on the warehouse, per ``can_*`` action, to who may manage its grants ([[LH-555]])."""
    return await _access_list(context, resolver=resolver, fga_type="warehouse", id=id)


@project_router.post("/{id}/access/list", dependencies=[Depends(require_project_review)])
async def list_project_access(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> AccessListResponse:
    """Effective access on the project, per ``can_*`` action, to who may see its grants: its security admins ([[LH-563]]).

    Lists who may ask the decision door about the project's objects (``can_decide_access``) beside its other actions.
    \f

    Args:
        id: The project.
        context: The request's authorization context: the OpenFGA client, the settings and the caller.
        resolver: The request's name resolver.

    Returns:
        Each ``can_*`` action the model defines on a project, with the subjects holding it.

    Raises:
        ServiceUnavailableError: OpenFGA did not answer.
    """
    return await _access_list(context, resolver=resolver, fga_type="project", id=id)


async def _access_check(
    context: AuthzContext,
    *,
    resolver: Resolver,
    fga_type: str,
    id: str,
    body: AccessCheckRequest,
) -> AccessCheckResponse:
    """The #68 playground's check primitive — a single ``(user, relation, object)`` OpenFGA Check,
    gated identically to ``access/list`` (probing the graph is the same disclosure as enumerating
    it). Only relations the compiled model defines on ``fga_type`` may be probed, so an unknown relation
    is a clean 400 here — the caller's error, the same code the estate-admin door gives the identical
    mistake — rather than an OpenFGA 400 that fails closed to a 503 for the caller."""
    client = decisions.require_fga(context.settings, context.client, feature="access simulation")
    if body.relation not in _can_relations(fga_type):
        # The CLIENT's error (400), never UnsupportedOperation (406): the deployment supports the check
        # fine, the body names a relation the model does not define — access_admin.py's parity.
        raise InvalidInputError(f"{body.relation!r} is not a can_* relation on {fga_type}")
    segments = parse_identifier(id)
    obj, name = await _target(resolver, fga_type, segments)
    subject = context.token.subject if context.token else "anonymous"
    # Resolve to a FULL subject (`fga_ids.grantee_object`): a subject is the user it is; a qualified userset
    # (``role:…#assignee``) is passed through as-is. Then `fga_reads.check_fga_user` sends it verbatim —
    # `fga_reads.check` would qualify it again, to ``user:user:…``
    # and every simulated Check would falsely deny (audit 2026-07-20 caught exactly this).
    user = fga_ids.grantee_object(body.user)
    try:
        allowed = await fga_reads.check_fga_user(client, fga_user=user, relation=body.relation, obj=obj)
    except ServiceUnavailableError:
        audit(AuditAction.ACCESS_SIMULATE, AuditOutcome.FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    # #41: the simulation IS an authz-graph disclosure (who probed what) — audit it distinctly from the
    # gate's owner allow, exactly like access_review / credential vending.
    audit(AuditAction.ACCESS_SIMULATE, AuditOutcome.SUCCESS, subject=subject, resource=obj)
    return AccessCheckResponse(object=obj, name=name, user=user, relation=body.relation, allowed=allowed)


@table_router.post("/{id}/access/check")
async def check_table_access(*, id: str, context: AuthzContextDep, resolver: ResolverDep, body: AccessCheckRequest) -> AccessCheckResponse:
    """Simulate 'does <user> hold <relation> on this table?' — the router asks ``can_review_access``."""
    return await _access_check(context, resolver=resolver, fga_type="table", id=id, body=body)


@namespace_router.post("/{id}/access/check")
async def check_namespace_access(*, id: str, context: AuthzContextDep, resolver: ResolverDep, body: AccessCheckRequest) -> AccessCheckResponse:
    """Simulate 'does <user> hold <relation> on this namespace?' — the router asks ``can_review_access``."""
    return await _access_check(context, resolver=resolver, fga_type="namespace", id=id, body=body)


async def _my_permissions(context: AuthzContext, *, resolver: Resolver, fga_type: str, id: str) -> MyPermissionsResponse:
    """Answer every ``can_*`` on this object for the CALLER — the self-view.

    Reader-gated (``can_get_metadata``), NOT owner-gated like its two siblings, and the distinction is
    the whole point: ``access/list`` enumerates who holds what and ``access/check`` probes an arbitrary
    subject, so both disclose principals and clear the owner bar. "What may I do here" discloses
    nothing about anyone else — gating it at the owner bar would mean only the people who already know
    the answer could ask the question, which is why no surface over these primitives existed.

    Takes no ``user``: a self-view that accepts a subject is the enumeration question renamed. The
    subject comes from the bearer and nowhere else.

    Not audited. ``access_review`` and ``access_simulate`` are audited because each is an authz-graph
    DISCLOSURE about third parties; a caller learning their own effective permissions reveals nothing
    they could not obtain by attempting the operations, and one audit row per page render would bury
    the rows that matter.
    """
    client = decisions.require_fga(context.settings, context.client, feature="permission self-view")
    # `fga_enabled` implies `oidc_enabled` (the pair is refused at boot) and OIDC 401s a bearer-less
    # request before any handler runs, so a token is guaranteed once the gate above has passed. Stated
    # rather than assumed: `CurrentToken` is `Caller | None`, and the alternative is `token.subject`
    # raising AttributeError on a branch that only configuration makes unreachable. The siblings can
    # fall back to "anonymous" because they use the subject for an AUDIT row; here it IS the subject
    # of every Check, and "anonymous" would silently answer the wrong question.
    if context.token is None:
        raise UnauthenticatedError("the permission self-view needs an authenticated caller")
    segments = parse_identifier(id)
    obj, name = await _target(resolver, fga_type, segments)
    # The caller's subject is the user it is (`fga_ids.user_object`), a service account's included.
    subject = fga_ids.user_object(context.token.subject)
    relations = _can_relations(fga_type)
    # TaskGroup, not gather — same reason as the review path: one failure cancels its siblings rather
    # than leaving orphaned retry loops hammering a degraded OpenFGA after the request has 503'd.
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(fga_reads.check_fga_user(client, fga_user=subject, relation=r, obj=obj)) for r in relations]
    except* ServiceUnavailableError as outage:
        # A TaskGroup raises an ExceptionGroup, which no handler in the problem-body map recognises —
        # it would surface as a 500 and the caller would read "the catalog is broken" instead of
        # "authorization is unavailable, fail closed". Unwrapped exactly as `_access_list` does.
        raise outage.exceptions[0] from None
    return MyPermissionsResponse(object=obj, name=name, subject=subject, permissions={r: t.result() for r, t in zip(relations, tasks, strict=True)})


@table_router.post("/{id}/access/my-permissions")
async def my_table_permissions(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> MyPermissionsResponse:
    """What the caller may do on this table — reader-gated by the router (``can_get_metadata``)."""
    return await _my_permissions(context, resolver=resolver, fga_type="table", id=id)


@namespace_router.post("/{id}/access/my-permissions")
async def my_namespace_permissions(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> MyPermissionsResponse:
    """What the caller may do on this namespace — reader-gated by the router (``can_get_metadata``)."""
    return await _my_permissions(context, resolver=resolver, fga_type="namespace", id=id)


@warehouse_router.post("/{id}/access/my-permissions")
async def my_warehouse_permissions(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> MyPermissionsResponse:
    """What the caller may do on this warehouse — the self-view the UI needs to render a DISABLED
    action with its reason instead of a button that 403s on click.

    GATED EXPLICITLY, unlike its table/namespace siblings. Warehouse is not in `rungs._RESOURCES`
    (`namespace`, `table`, `materialized_view`, `transaction`), so the router-level `authorize`
    returns early for every `/management/v1/warehouse/…` path and a route mounted here that forgets its own check
    is simply ungated — the hazard this module's own header comment names.

    Reader tier (`can_get_metadata`), matching `_my_permissions`' rule rather than the owner bar its
    two siblings on this router use: "what may I do here" discloses nothing about any other principal,
    and gating it at the owner bar would mean only the people who already know the answer could ask.
    """
    await door_gates.require_relation(context, relation="can_get_metadata", obj=f"warehouse:{id}")
    return await _my_permissions(context, resolver=resolver, fga_type="warehouse", id=id)


@project_router.post("/{id}/access/my-permissions")
async def my_project_permissions(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> MyPermissionsResponse:
    """What the caller may do on this project — same self-view, same explicit gate as the warehouse
    rung (``/management/v1/projects/…`` is likewise outside ``_RESOURCES``).

    The project rung is where the estate's most irreversible action lives (`DELETE /management/v1/projects/{id}`
    has no cascade at all), so this is the one whose absence forced the UI to show a delete button and
    discover the answer from a 403.

    GATED ON `member`, A BASE RUNG, and that is a deliberate departure worth reading. Every other
    surface here gates on a `can_*` action, but the `project` type defines NO reader-tier action — its
    whole action surface is `can_administer` / `can_create_warehouse` / `can_grant_*` /
    `can_read_assignments`, all admin tier. `can_get_metadata`, the analogy with table and namespace,
    does not exist on this type, and OpenFGA rejects an undefined relation with a 400 that fails closed
    to a 503 for every caller, so gating on it would make this endpoint permanently unavailable.

    `member` is the reader-equivalent for this type: it is the tier ordinary tenant work sits at, and a
    caller with no relationship to the project still gets a 403 rather than learning the tenant exists.
    So a 200 here is how any client learns, on a person's own token, that the person is a member of the
    tenant, and `can_administer` that they administer it (ADR 0171). The alternative — adding
    `can_get_metadata: member` to the model for symmetry — is a MODEL change with `.fga.yaml` updates
    and a migration story behind it, which is not something a UI-gating endpoint should drag in.
    """
    await door_gates.require_relation(context, relation="member", obj=f"project:{id}")
    return await _my_permissions(context, resolver=resolver, fga_type="project", id=id)


class _AccessChange(BaseModel):
    """What a grant or a revoke door does to a rung, and how it audits, announces and answers it."""

    model_config = ConfigDict(frozen=True)

    #: Whether the rung is handed out: a grant also refuses a role crossing tenants, and answers ``granted``.
    granted: bool
    event: AuditAction
    action: Literal["grant_added", "grant_revoked"]
    door: str


_GRANT = _AccessChange(granted=True, event=AuditAction.ACCESS_GRANT, action="grant_added", door="access_grant")
_REVOKE = _AccessChange(granted=False, event=AuditAction.ACCESS_REVOKE, action="grant_revoked", door="access_revoke")


class _AccessTarget(BaseModel):
    """The rung a grant or revoke door changes: the object's type and id, and the subject and rung the body names."""

    model_config = ConfigDict(frozen=True)

    fga_type: str
    id: str
    body: AccessGrantRequest


async def _grant_access(context: AuthzContext, *, announcer: Announcer, resolver: Resolver, target: _AccessTarget) -> AccessGrantResponse:
    """Grant one grantable rung (:func:`_change_access`)."""
    return await _change_access(context, announcer=announcer, resolver=resolver, target=target, change=_GRANT, store=fga_writes.write_tuples)  # noqa: TID251


async def _revoke_access(context: AuthzContext, *, announcer: Announcer, resolver: Resolver, target: _AccessTarget) -> AccessGrantResponse:
    """Revoke one grantable rung (:func:`_change_access`)."""
    return await _change_access(context, announcer=announcer, resolver=resolver, target=target, change=_REVOKE, store=fga_writes.delete_tuples)  # noqa: TID251


async def _change_access(
    context: AuthzContext,
    *,
    announcer: Announcer,
    resolver: Resolver,
    target: _AccessTarget,
    change: _AccessChange,
    store: fga_writes.TupleStore,
) -> AccessGrantResponse:
    """Grant or revoke one grantable rung to a subject on a table, a namespace, a warehouse, a project or a classification label.

    The MUTATE half of the #68 governance surface — the write counterpart of ``access/list``.

    GATED PER RUNG FROM THE BODY, not by the router's owner-tier suffix map: neither suffix appears in
    ``OWNER_SUFFIX_RELATION``, and the router's guard reads the rung out of the body
    (:func:`~catalog.api.authz.grant_authority.authorize_grant`, :func:`~catalog.api.authz.grant_authority.authorize_revoke`)
    because the path says only THAT a grant is happening. Grant needs ``can_grant_<relation>``; revoke
    needs ``can_revoke_grant``, which each served type defines stricter than its grants (``manage_grants``
    alone on a table or a namespace, ``curator`` alone on a classification label — a delegate must not be
    able to strip the owner who delegated to them). Reviewing (``access/list``/``access/check``) is
    owner-tier.
    Fail-closed: only a grantable base rung the model defines is accepted (a ``can_*`` action or ``parent``
    is a 4xx, not a silent junk tuple); an OpenFGA outage is a 503, never a silent grant/revoke no-op. Both
    directions are idempotent (``write_tuples`` swallows a duplicate, ``delete_tuples`` an absent tuple) and
    audited distinctly (``access_grant`` / ``access_revoke``), carrying the grantee + rung. The announcement is filed
    before the write, so a door that fails after it is still announced, once ([[LH-576]])."""
    fga_type, body = target.fga_type, target.body
    client = decisions.require_fga(context.settings, context.client, feature="access mutation")
    grantable = _grantable_relations(fga_type)
    if body.relation not in grantable:
        # 400, not 406 — same rule as the check door above: a bad rung NAME is client input, and the
        # sibling estate-admin surface already answers this class of mistake with InvalidInput.
        raise InvalidInputError(f"{body.relation!r} is not a grantable rung on {fga_type} (one of {', '.join(grantable)})")
    segments = parse_identifier(target.id)
    obj, name = await _target(resolver, fga_type, segments)
    actor = context.token.subject if context.token else "anonymous"
    # Resolve the grantee to a FULL subject (as access/check does for `check_fga_user`): a subject is the
    # user it is; a qualified userset (``role:…#assignee``) is passed through verbatim.
    grantee = fga_ids.grantee_object(body.user)
    # The store refuses a tuple its model cannot hold, and that refusal reaches here as an outage, so the model is asked
    # first: a decision rung (`[user]`) names one account, never a role's assignees ([[LH-563]]).
    if not fga_model.admits(fga_type, body.relation, grantee):
        admitted = ", ".join(sorted(fga_model.type_restrictions(fga_type, body.relation)))
        raise InvalidInputError(f"{body.relation!r} on {fga_type} cannot name {grantee}: the model lets it name {admitted}")
    if change.granted:
        await grant_authority.refuse_a_cross_tenant_role_grant(client, grantee, fga_type=fga_type, segments=segments, settings=context.settings)
    tup = fga_writes.ClientTuple(user=grantee, relation=body.relation, object=obj)
    event = change.event
    told = announcing.grant_announcement(tup, action=change.action, actor=f"user:{context.token.subject}" if context.token else None, object_name=name)
    async with announcer.announcing(door=change.door, target=obj, announcements=[told]) as filing:
        try:
            await store(client, [tup], actor=actor, origin="grant_api")
        except ServiceUnavailableError:
            audit(event, AuditOutcome.FAILURE, subject=actor, resource=obj, grantee=grantee, relation=body.relation)
            raise
        # The per-tuple SUCCESS row is emitted by the library (one row per write site, structurally). This
        # keeps only the surface-specific vocabulary — `access_grant`/`access_revoke` is the compliance
        # verb for the per-object grant API, and dropping it would break the existing audit queries.
        audit(event, AuditOutcome.SUCCESS, subject=actor, resource=obj, grantee=grantee, relation=body.relation)
        await filing.send(told)
    return AccessGrantResponse(object=obj, name=name, user=grantee, relation=body.relation, granted=change.granted)


@table_router.post("/{id}/access/grant")
async def grant_table_access(
    *, id: str, context: AuthzContextDep, announcer: AnnouncerDep, resolver: ResolverDep, body: AccessGrantRequest
) -> AccessGrantResponse:
    """Grant a base rung on the table to a subject — gated PER RUNG by the router
    (``can_grant_<relation>``, read from the body). Granting is its own axis now: a `manage_grants`
    holder may hand out access without holding the data, and a `pass_grants` delegate may hand on only
    what they already hold."""
    return await _grant_access(context, announcer=announcer, resolver=resolver, target=_AccessTarget(fga_type="table", id=id, body=body))


@table_router.post("/{id}/access/revoke")
async def revoke_table_access(
    *, id: str, context: AuthzContextDep, announcer: AnnouncerDep, resolver: ResolverDep, body: AccessGrantRequest
) -> AccessGrantResponse:
    """Revoke a base rung on the table from a subject — gated per rung, identically to grant: taking a
    rung away is the same authority as handing it out."""
    return await _revoke_access(context, announcer=announcer, resolver=resolver, target=_AccessTarget(fga_type="table", id=id, body=body))


@namespace_router.post("/{id}/access/grant")
async def grant_namespace_access(
    *, id: str, context: AuthzContextDep, announcer: AnnouncerDep, resolver: ResolverDep, body: AccessGrantRequest
) -> AccessGrantResponse:
    """Grant a base rung on the namespace to a subject — gated per rung (``can_grant_<relation>``)."""
    return await _grant_access(context, announcer=announcer, resolver=resolver, target=_AccessTarget(fga_type="namespace", id=id, body=body))


@warehouse_router.post("/{id}/access/grant", dependencies=[Depends(require_warehouse_grant)])
async def grant_warehouse_access(
    *,
    id: str,
    context: AuthzContextDep,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
    body: AccessGrantRequest,
) -> AccessGrantResponse:
    """Grant a base rung on the warehouse to a subject, gated by :func:`require_warehouse_grant` ([[LH-555]])."""
    return await _grant_access(context, announcer=announcer, resolver=resolver, target=_AccessTarget(fga_type="warehouse", id=id, body=body))


@warehouse_router.post("/{id}/access/revoke", dependencies=[Depends(require_warehouse_revoke)])
async def revoke_warehouse_access(
    *,
    id: str,
    context: AuthzContextDep,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
    body: AccessGrantRequest,
) -> AccessGrantResponse:
    """Revoke a base rung on the warehouse from a subject, gated by :func:`require_warehouse_revoke` ([[LH-555]])."""
    return await _revoke_access(context, announcer=announcer, resolver=resolver, target=_AccessTarget(fga_type="warehouse", id=id, body=body))


@project_router.post("/{id}/access/grant", dependencies=[Depends(require_project_grant)])
async def grant_project_access(
    *,
    id: str,
    context: AuthzContextDep,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
    body: AccessGrantRequest,
) -> AccessGrantResponse:
    """Grant a rung on the project to a subject, gated by :func:`require_project_grant` ([[LH-563]]).

    The rung is ``decider`` (a service's own identity may then ask the decision door about the project's objects, and
    nothing beyond them), or one of the project's two data rungs, ``modifier`` and ``describer``, which reach every
    warehouse of the project ([[LH-570]]). Who belongs to the project is the members door's.
    \f

    Args:
        id: The project.
        context: The request's authorization context: the OpenFGA client, the settings and the caller.
        announcer: Where the grant is filed and announced.
        resolver: The request's name resolver.
        body: The subject and the rung.

    Returns:
        The tuple granted.

    Raises:
        InvalidInputError: The rung is no rung this door grants on a project.
        ServiceUnavailableError: OpenFGA did not answer.
    """
    return await _grant_access(context, announcer=announcer, resolver=resolver, target=_AccessTarget(fga_type="project", id=id, body=body))


@project_router.post("/{id}/access/revoke", dependencies=[Depends(require_project_revoke)])
async def revoke_project_access(
    *,
    id: str,
    context: AuthzContextDep,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
    body: AccessGrantRequest,
) -> AccessGrantResponse:
    """Revoke a rung on the project from a subject, gated by :func:`require_project_revoke` ([[LH-563]]).
    \f

    Args:
        id: The project.
        context: The request's authorization context: the OpenFGA client, the settings and the caller.
        announcer: Where the revoke is filed and announced.
        resolver: The request's name resolver.
        body: The subject and the rung.

    Returns:
        The tuple revoked.

    Raises:
        InvalidInputError: The rung is no rung this door grants on a project.
        ServiceUnavailableError: OpenFGA did not answer.
    """
    return await _revoke_access(context, announcer=announcer, resolver=resolver, target=_AccessTarget(fga_type="project", id=id, body=body))


@namespace_router.post("/{id}/access/revoke")
async def revoke_namespace_access(
    *, id: str, context: AuthzContextDep, announcer: AnnouncerDep, resolver: ResolverDep, body: AccessGrantRequest
) -> AccessGrantResponse:
    """Revoke a base rung on the namespace from a subject — gated per rung, identically to grant."""
    return await _revoke_access(context, announcer=announcer, resolver=resolver, target=_AccessTarget(fga_type="namespace", id=id, body=body))


@classification_router.post("/{id}/access/grant")
async def grant_classification_access(
    *, id: str, context: AuthzContextDep, announcer: AnnouncerDep, resolver: ResolverDep, body: AccessGrantRequest
) -> AccessGrantResponse:
    """Delegate a classification label to a subject — gated per rung by ``can_grant_apply``.

    THE DOOR THE RUNG WAS POINTLESS WITHOUT. `apply` exists so a data-protection officer may be trusted
    with `pii` and not with `restricted`; without somewhere to confer it, the vocabulary is estate-admin
    only and the separation is decorative. `model.fga` makes the same argument for `classifier` one
    level along — "a person's grant belongs on the grant door rather than in a deploy".
    """
    return await _grant_access(context, announcer=announcer, resolver=resolver, target=_AccessTarget(fga_type="classification", id=id, body=body))


@classification_router.post("/{id}/access/revoke")
async def revoke_classification_access(
    *, id: str, context: AuthzContextDep, announcer: AnnouncerDep, resolver: ResolverDep, body: AccessGrantRequest
) -> AccessGrantResponse:
    """Withdraw a classification delegation — ``can_revoke_grant``, stricter than the grant, exactly as
    on table and namespace: taking a rung back is administration, never delegation."""
    return await _revoke_access(context, announcer=announcer, resolver=resolver, target=_AccessTarget(fga_type="classification", id=id, body=body))


@classification_router.post("/{id}/access/my-permissions")
async def my_classification_permissions(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> MyPermissionsResponse:
    """Which classification actions the caller holds on this label — the same self-service read the
    data types carry, and the only way a holder can discover a delegation made to them."""
    return await _my_permissions(context, resolver=resolver, fga_type="classification", id=id)


def _graph_node(node_id: str) -> GraphNode:
    """Split ``<type>:<rest>`` into a typed, labelled node (a userset like ``role:admin#assignee`` keeps
    its full id, labelled without the leading type)."""
    fga_type, _, rest = node_id.partition(":")
    return GraphNode(id=node_id, type=fga_type or "unknown", label=rest or node_id)


async def _access_graph(context: AuthzContext, *, resolver: Resolver, fga_type: str, id: str) -> AccessGraphResponse:
    """The #81 authorization-graph primitive — one hop of the relationship graph around an object: the
    object, every subject directly granted a rung on it, and its ``parent``/``project`` container edge.

    Built from ``read_object_tuples`` (the direct tuples on the object — grants + the parent edge), so a
    grant appears as ``subject → object`` labelled with the rung and the container as ``object → parent``.
    The frontend expands the cascade by calling this again on a parent node. Owner-tier gated by the router
    (same disclosure bar as ``access/list``: the graph reveals principals), audited (``access_graph``), and
    fail-closed — an OpenFGA outage is a 503, never a partial graph that reads as 'nobody has access'."""
    client = decisions.require_fga(context.settings, context.client, feature="access graph")
    segments = parse_identifier(id)
    obj, name = await _target(resolver, fga_type, segments)
    subject = context.token.subject if context.token else "anonymous"
    try:
        tuples = await fga_reads.read_object_tuples(client, obj)
    except ServiceUnavailableError:
        audit(AuditAction.ACCESS_GRAPH, AuditOutcome.FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    nodes: dict[str, GraphNode] = {obj: _graph_node(obj)}
    edges: list[GraphEdge] = []
    for t in tuples:
        nodes.setdefault(t.user, _graph_node(t.user))
        # A parent/project tuple is written (parent_object, parent, obj) — the object's container edge, so
        # it points obj → parent. Every other tuple is a grant of a rung to a subject → obj.
        if t.relation in ("parent", "project"):
            edges.append(GraphEdge(source=obj, target=t.user, relation=t.relation))
        else:
            edges.append(GraphEdge(source=t.user, target=obj, relation=t.relation))
    audit(AuditAction.ACCESS_GRAPH, AuditOutcome.SUCCESS, subject=subject, resource=obj)
    return AccessGraphResponse(object=obj, name=name, nodes=list(nodes.values()), edges=edges)


@table_router.post("/{id}/access/graph")
async def graph_table_access(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> AccessGraphResponse:
    """One hop of the authorization graph around the table — the router asks ``can_review_access``."""
    return await _access_graph(context, resolver=resolver, fga_type="table", id=id)


@namespace_router.post("/{id}/access/graph")
async def graph_namespace_access(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> AccessGraphResponse:
    """One hop of the authorization graph around the namespace — the router asks ``can_review_access``."""
    return await _access_graph(context, resolver=resolver, fga_type="namespace", id=id)


# --------------------------------------------------------------------------- #
# Managed access — centralize granting for a whole container
# --------------------------------------------------------------------------- #

#: The wildcard subject the flag is stored as. ``[user:*]`` is a TYPE RESTRICTION used as a switch,
#: not a public grant: no rule reads it as "everyone", only ``managed_access_inheritance`` reads it at
#: all. Written as one canonical tuple so the flag has exactly one representation to set, clear and
#: audit — a second spelling (``role:*``) would be a second thing to check when asking "is this on?".
_MANAGED_ACCESS_SUBJECT = "user:*"


async def _read_managed_access(client: OpenFgaClient | None, settings: FgaSettings, resolver: Resolver, *, fga_type: str, id: str) -> ManagedAccessResponse:
    """Is this container managed? The READ half, without which the flag is unusable in a UI.

    Reader-tier (``can_get_metadata``, via the suffix map), not the grant bar that SETS it. That is
    deliberate: the flag is the reason someone's grant controls are absent, so the person who needs
    the answer most is precisely the one who may not change it. Hiding the state from them turns a
    stated policy into an unexplained missing button.

    Answers ``false`` on an auth-off stack rather than refusing: no tuples means no flag, and a page
    asking "is this managed?" wants an answer it can render.
    """
    segments = parse_identifier(id)
    if (client := decisions.wired(settings, client)) is None:
        return ManagedAccessResponse(object=f"{fga_type}:{fga_ids.canonical_object_id(segments)}", managed_access=False)
    obj, name = await _target(resolver, fga_type, segments)
    tuples = await fga_reads.read_object_tuples(client, obj)
    return ManagedAccessResponse(object=obj, name=name, managed_access=any(t.object == obj and t.relation == "managed_access" for t in tuples))


@namespace_router.post("/{id}/managed-access/describe")
async def get_namespace_managed_access(*, id: str, client: FgaClientDep, settings: SettingsDep, resolver: ResolverDep) -> ManagedAccessResponse:
    """Whether granting on this namespace is centralized — reader-gated (``can_get_metadata``)."""
    return await _read_managed_access(client, settings, resolver, fga_type="namespace", id=id)


@warehouse_router.post("/{id}/managed-access/describe")
async def get_warehouse_managed_access(
    *,
    id: str,
    context: AuthzContextDep,
    resolver: ResolverDep,
) -> ManagedAccessResponse:
    """The same for a warehouse. Gated EXPLICITLY — `warehouse` is not in ``_RESOURCES``, so
    ``authorize`` returns early and a route added here without this call is ungated."""
    segments = parse_identifier(id)
    obj = f"warehouse:{fga_ids.canonical_object_id(segments)}"
    await door_gates.require_relation(context, relation="can_get_metadata", obj=obj)
    return await _read_managed_access(context.client, context.settings, resolver, fga_type="warehouse", id=id)


async def _set_managed_access(
    context: AuthzContext,
    *,
    announcer: Announcer,
    resolver: Resolver,
    fga_type: str,
    id: str,
    enabled: bool,
) -> ManagedAccessResponse:
    """Set or clear the flag, idempotently.

    Reads before writing for the same reason the membership API does: an OpenFGA Write is
    transactional and REJECTS a tuple that already exists, so "turn it on when it is already on"
    would 400 rather than being the no-op a caller reasonably expects. The announcement is filed before the read, as a
    grant's is ([[LH-576]]).
    """
    client = decisions.require_fga(context.settings, context.client, feature="managed access")
    segments = parse_identifier(id)
    obj, name = await _target(resolver, fga_type, segments)
    actor = context.token.subject if context.token else "system:catalog"
    tup = fga_writes.ClientTuple(user=_MANAGED_ACCESS_SUBJECT, relation="managed_access", object=obj)

    told = announcing.grant_announcement(
        tup, action="grant_added" if enabled else "grant_revoked", actor=f"user:{context.token.subject}" if context.token else None, object_name=name
    )
    async with announcer.announcing(door="managed_access_set", target=obj, announcements=[told]) as filing:
        already = any(t.object == obj and t.relation == "managed_access" for t in await fga_reads.read_object_tuples(client, obj))
        if enabled and not already:
            await fga_writes.write_tuples(client, [tup], actor=actor, origin="grant_api")  # noqa: TID251
        elif not enabled and already:
            await fga_writes.delete_tuples(client, [tup], actor=actor, origin="grant_api")  # noqa: TID251
        # Audited distinctly from a grant: this does not move anyone's access, it changes WHO MAY move it —
        # a governance act whose blast radius is every object beneath, and one an auditor will look for by
        # name rather than by inferring it from the absence of later grants.
        audit(AuditAction.MANAGED_ACCESS_SET, AuditOutcome.SUCCESS, subject=actor, resource=obj, enabled=enabled)
        await filing.send(told)
    return ManagedAccessResponse(object=obj, name=name, managed_access=enabled)


@namespace_router.post("/{id}/managed-access/set")
async def set_namespace_managed_access(
    *, id: str, context: AuthzContextDep, announcer: AnnouncerDep, resolver: ResolverDep, body: ManagedAccessRequest
) -> ManagedAccessResponse:
    """Centralize granting for this namespace and everything beneath it — gated on
    ``can_set_managed_access`` (which derives from ``manage_grants``).

    With it on, owners BELOW keep every data power and lose only the ability to hand access out; a
    grant-manager at or above this namespace keeps it. Note the consequence for clearing it: inside an
    already-managed scope the owner's ``manage_grants`` is withdrawn, so they cannot switch it off —
    which is what makes it a policy rather than a suggestion.
    """
    return await _set_managed_access(context, announcer=announcer, resolver=resolver, fga_type="namespace", id=id, enabled=body.enabled)


@warehouse_router.post("/{id}/managed-access/set")
async def set_warehouse_managed_access(
    *, id: str, context: AuthzContextDep, announcer: AnnouncerDep, resolver: ResolverDep, body: ManagedAccessRequest
) -> ManagedAccessResponse:
    """The same, for a whole warehouse — the scope root, so this governs every stage and table in it,
    **and the warehouse itself**.

    That last clause was not true until 2026-08-16. ``warehouse#manage_grants`` was the one rung
    missing the ``but not managed_access_inheritance`` subtraction, so turning this on locked down
    every namespace and table below and left the warehouse OWNER still able to grant on the warehouse —
    the widest grant there is, since ``reader from parent`` cascades from here to everything beneath.

    Who can still grant here once it is on: an explicitly appointed ``manage_grants`` holder (the
    records manager this exists for), and the PROJECT admin above. The project admin is not a new
    power — they already reached it through ``owner: [...] or admin from project`` — but the clause is
    load-bearing: ``can_set_managed_access`` derives from ``manage_grants``, so without a path from
    above, the flag would withdraw the only relation able to clear it and freeze this bucket's access
    administration permanently. Unlike a namespace, a warehouse has no managed parent scope to
    unlock it from.

    Gated EXPLICITLY rather than through the router's suffix map: ``warehouse`` is not in
    ``_RESOURCES``, so ``authorize`` returns early for these paths (the same reason ``warehouses.py``
    and ``projects.py`` call ``require_relation`` by hand). A route added here without that call would
    be authenticated and ungated.
    """
    segments = parse_identifier(id)
    obj = f"warehouse:{fga_ids.canonical_object_id(segments)}"
    await door_gates.require_relation(context, relation="can_set_managed_access", obj=obj)
    return await _set_managed_access(context, announcer=announcer, resolver=resolver, fga_type="warehouse", id=id, enabled=body.enabled)


# The v1 aggregator includes one ``router`` per module — the table + namespace routers are stitched here.
