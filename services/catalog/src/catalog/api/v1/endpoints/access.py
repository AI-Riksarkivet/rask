"""Access-review endpoints (#51): who holds which ``can_*`` action on a table or namespace.

``POST …/access/list`` answers the standing-access question the #41 audit trail cannot ("who *can*
read this?", not "who *did*"): for every ``can_*`` action the compiled model defines on the type, it
asks OpenFGA ListUsers — which expands role assignees, team members, and the parent cascade, so the
answer is effective access, not just direct tuples. The relation set comes from the model the app
actually loads (never a hand-kept list), so a model edit is reflected here automatically and
``test_fga_model_contract`` proves every queried pair exists.

Owner-tier gated by the router-level ``authorize`` (``access/list`` maps to ``can_drop`` /
``can_delete`` — an enumeration reveals principals, so it clears the same bar as destroying the
object); the gate audits the authz decision, and the endpoint additionally emits a dedicated
``access_review`` audit event (the disclosure itself, distinguishable from an actual drop — the same
two-layer pattern as credential vending). Fail-closed: an OpenFGA outage is a 503, never an empty
grant list that reads as "nobody has access".

Two properties of "effective access" are accepted by design and worth knowing when reading a review:
the expansion follows the parent cascade, so a leaf-table owner sees individual grantees who hold
access only via namespace/warehouse/project team or role grants (within-tenant upward visibility —
the cascade never crosses projects); and OpenFGA's ListUsers has no pagination, so a relation held
by more subjects than the server's ``listUsersMaxResults`` cap returns a partial list (the wrapper
logs a warning when a result looks capped).
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from fastapi import APIRouter, Request
from lance_namespace import ServiceUnavailableError, UnauthenticatedError, UnsupportedOperationError

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, SettingsDep, get_control_emitter
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.control_emit import emit_control
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
from service_kit.governed import fga
from service_kit.governed.audit import FAILURE, SUCCESS, audit


log = logging.getLogger(__name__)

table_router = APIRouter(prefix="/v1/table", tags=["access"])
namespace_router = APIRouter(prefix="/v1/namespace", tags=["access"])
# Warehouse is NOT in fga_deps._RESOURCES, so `authorize` returns early for these paths — every route
# mounted here must gate itself explicitly (see `set_warehouse_managed_access`).
warehouse_router = APIRouter(prefix="/v1/warehouse", tags=["access"])
# Same rule as the warehouse router above: `/v1/projects/…` is outside `fga_deps._RESOURCES`, so
# `authorize` returns early and every route here gates itself. Its own router rather than a route on
# the projects module so the access surface stays in one file — the place someone looks when asking
# "what is gated, and how".
project_router = APIRouter(prefix="/v1/projects", tags=["access"])


# The base rungs an admin may directly assign. The model defines each as ``[user, role#assignee] or …``
# (service_kit/governed/auth/model.fga) — a real user/userset grant, unlike the derived ``can_*`` actions or the
# structural ``parent``/``child`` edges, none of which may be hand-granted. Intersected with the model below so a
# renamed rung drops out (and test_fga_model_contract would catch the drift).
#
# ``manage_grants`` and ``pass_grants`` are here because grant power is now its own axis rather than a
# side-effect of ``owner``: leaving them out would define the delegation the model describes and provide
# no way to confer it. They are not more dangerous than ``owner`` — both are reachable only through
# ``can_grant_manage_grants`` / ``can_grant_pass_grants``, which are ``manage_grants``-only, so a
# grant-option delegate can neither mint further delegates nor promote themselves.
_GRANTABLE_BASE: tuple[str, ...] = ("owner", "writer", "reader", "validator", "manage_grants", "pass_grants")


@lru_cache
def _grantable_relations(fga_type: str) -> tuple[str, ...]:
    """The base rungs grantable on ``fga_type`` — ``_GRANTABLE_BASE`` restricted to what the model defines."""
    for td in fga.load_model()["type_definitions"]:
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
    for td in fga.load_model()["type_definitions"]:
        if td["type"] == fga_type:
            return tuple(sorted(r for r in (td.get("relations") or {}) if r.startswith("can_")))
    return ()


async def _access_list(request: Request, settings: Settings, token: CurrentToken, fga_type: str, id: str) -> AccessListResponse:
    if not settings.fga_enabled:
        raise UnsupportedOperationError("access review requires OpenFGA (this stack runs auth-off)")
    client = getattr(request.app.state, "fga", None)
    if client is None:  # the router gate already 503s this; kept so the endpoint is safe standalone
        raise ServiceUnavailableError("authorization service is not available")
    segments = parse_identifier(id, settings.delimiter)
    obj = f"{fga_type}:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    relations = _can_relations(fga_type)
    subject = token.sub if token else "anonymous"
    try:
        # TaskGroup, not gather: one failed relation cancels the siblings, so a degraded OpenFGA is
        # never hammered by up-to-nine orphaned retry loops after the request has already 503'd.
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(fga.list_users(client, relation=r, obj=obj)) for r in relations]
    except* ServiceUnavailableError as outage:
        # #41: the review FAILED mid-enumeration — without this, the gate's earlier allow would be
        # the only trace, indistinguishable from a completed disclosure.
        audit("access_review", FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise outage.exceptions[0] from None
    # #41 audit the actual ACL disclosure (who reviewed what) — the gate's can_drop/can_delete allow
    # alone would be byte-identical to a pending destructive op.
    audit("access_review", SUCCESS, subject=subject, resource=obj)
    # TRUNCATION IS REPORTED, not just logged (diff2 F10 item 8). `list_users` stops at OpenFGA's
    # `listUsersMaxResults` and warned into the log; the response looked complete. On this surface that
    # is the dangerous direction to be wrong in — a reviewer reading 1000 of 1500 holders concludes the
    # other 500 hold nothing, which is the opposite of the truth and is exactly the answer an access
    # review exists to prevent. Inferred from the length against the cap, the same way the admin door
    # (`AccessListUsersResponse.truncated`) has always done it.
    grants = [
        RelationGrants(relation=relation, users=(users := task.result()), truncated=len(users) >= fga.LIST_USERS_SERVER_CAP)
        for relation, task in zip(relations, tasks, strict=True)
    ]
    if any(g.truncated for g in grants):
        log.warning("access_review_truncated", extra={"object": obj, "relations": [g.relation for g in grants if g.truncated]})
    return AccessListResponse(object=obj, grants=grants, truncated=any(g.truncated for g in grants))


@table_router.post("/{id}/access/list")
async def list_table_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken) -> AccessListResponse:
    """Effective access on the table, per ``can_*`` action — owner-gated by the router (``can_drop``)."""
    return await _access_list(request, settings, token, "table", id)


@namespace_router.post("/{id}/access/list")
async def list_namespace_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken) -> AccessListResponse:
    """Effective access on the namespace, per ``can_*`` action — owner-gated by the router
    (``can_delete``)."""
    return await _access_list(request, settings, token, "namespace", id)


async def _access_check(
    request: Request,
    settings: Settings,
    token: CurrentToken,
    fga_type: str,
    id: str,
    body: AccessCheckRequest,
) -> AccessCheckResponse:
    """The #68 playground's check primitive — a single ``(user, relation, object)`` OpenFGA Check,
    owner-gated identically to ``access/list`` (probing the graph is the same disclosure as enumerating
    it). Only relations the compiled model defines on ``fga_type`` may be probed, so an unknown relation
    is a clean 4xx here rather than a 400 that fails closed to a 503 for the caller."""
    if not settings.fga_enabled:
        raise UnsupportedOperationError("access simulation requires OpenFGA (this stack runs auth-off)")
    client = getattr(request.app.state, "fga", None)
    if client is None:  # the router gate already 503s this; kept so the endpoint is safe standalone
        raise ServiceUnavailableError("authorization service is not available")
    if body.relation not in _can_relations(fga_type):
        raise UnsupportedOperationError(f"{body.relation!r} is not a can_* relation on {fga_type}")
    segments = parse_identifier(id, settings.delimiter)
    obj = f"{fga_type}:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    subject = token.sub if token else "anonymous"
    # Resolve to a FULL subject: a bare id is a user (``user:<id>``); a qualified userset
    # (``role:…#member`` / ``team:…#member``) is passed through as-is. Then check with qualify=False so
    # fga.check sends it verbatim — otherwise its default ``user:`` prefix would double to ``user:user:…``
    # and every simulated Check would falsely deny (audit 2026-07-20 caught exactly this).
    user = body.user if ":" in body.user else f"user:{body.user}"
    try:
        allowed = await fga.check(client, user=user, relation=body.relation, obj=obj, qualify=False)
    except ServiceUnavailableError:
        audit("access_simulate", FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    # #41: the simulation IS an authz-graph disclosure (who probed what) — audit it distinctly from the
    # gate's owner allow, exactly like access_review / credential vending.
    audit("access_simulate", SUCCESS, subject=subject, resource=obj)
    return AccessCheckResponse(object=obj, user=user, relation=body.relation, allowed=allowed)


@table_router.post("/{id}/access/check")
async def check_table_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: AccessCheckRequest) -> AccessCheckResponse:
    """Simulate 'does <user> hold <relation> on this table?' — owner-gated by the router (``can_drop``)."""
    return await _access_check(request, settings, token, "table", id, body)


@namespace_router.post("/{id}/access/check")
async def check_namespace_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: AccessCheckRequest) -> AccessCheckResponse:
    """Simulate 'does <user> hold <relation> on this namespace?' — owner-gated (``can_delete``)."""
    return await _access_check(request, settings, token, "namespace", id, body)


async def _my_permissions(request: Request, settings: Settings, token: CurrentToken, fga_type: str, id: str) -> MyPermissionsResponse:
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
    if not settings.fga_enabled:
        raise UnsupportedOperationError("permission self-view requires OpenFGA (this stack runs auth-off)")
    client = getattr(request.app.state, "fga", None)
    if client is None:  # the router gate already 503s this; kept so the endpoint is safe standalone
        raise ServiceUnavailableError("authorization service is not available")
    # `fga_enabled` implies `oidc_enabled` (the pair is refused at boot) and OIDC 401s a bearer-less
    # request before any handler runs, so a token is guaranteed once the gate above has passed. Stated
    # rather than assumed: `CurrentToken` is `IDToken | None`, and the alternative is `token.sub`
    # raising AttributeError on a branch that only configuration makes unreachable. The siblings can
    # fall back to "anonymous" because they use the subject for an AUDIT row; here it IS the subject
    # of every Check, and "anonymous" would silently answer the wrong question.
    if token is None:
        raise UnauthenticatedError("the permission self-view needs an authenticated caller")
    segments = parse_identifier(id, settings.delimiter)
    obj = f"{fga_type}:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    # An OIDC ``sub`` is opaque and MAY already carry a type prefix. Prefixing unconditionally yields
    # `user:user:<sub>`, a subject that matches no tuple, so every relation answers a correct-looking
    # `false` and the page renders "you may do nothing" for an owner. Same rule the settings gate uses.
    subject = token.sub if ":" in token.sub else f"user:{token.sub}"
    relations = _can_relations(fga_type)
    # TaskGroup, not gather — same reason as the review path: one failure cancels its siblings rather
    # than leaving orphaned retry loops hammering a degraded OpenFGA after the request has 503'd.
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(fga.check(client, user=subject, relation=r, obj=obj, qualify=False)) for r in relations]
    except* ServiceUnavailableError as outage:
        # A TaskGroup raises an ExceptionGroup, which no handler in the problem-body map recognises —
        # it would surface as a 500 and the caller would read "the catalog is broken" instead of
        # "authorization is unavailable, fail closed". Unwrapped exactly as `_access_list` does.
        raise outage.exceptions[0] from None
    return MyPermissionsResponse(object=obj, subject=subject, permissions={r: t.result() for r, t in zip(relations, tasks, strict=True)})


@table_router.post("/{id}/access/my-permissions")
async def my_table_permissions(id: str, request: Request, settings: SettingsDep, token: CurrentToken) -> MyPermissionsResponse:
    """What the caller may do on this table — reader-gated by the router (``can_get_metadata``)."""
    return await _my_permissions(request, settings, token, "table", id)


@namespace_router.post("/{id}/access/my-permissions")
async def my_namespace_permissions(id: str, request: Request, settings: SettingsDep, token: CurrentToken) -> MyPermissionsResponse:
    """What the caller may do on this namespace — reader-gated by the router (``can_get_metadata``)."""
    return await _my_permissions(request, settings, token, "namespace", id)


@warehouse_router.post("/{id}/access/my-permissions")
async def my_warehouse_permissions(id: str, request: Request, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> MyPermissionsResponse:
    """What the caller may do on this warehouse — the self-view the UI needs to render a DISABLED
    action with its reason instead of a button that 403s on click.

    GATED EXPLICITLY, unlike its table/namespace siblings. Warehouse is not in `fga_deps._RESOURCES`
    (`namespace`, `table`, `materialized_view`, `transaction`), so the router-level `authorize`
    returns early for every `/v1/warehouse/…` path and a route mounted here that forgets its own check
    is simply ungated — the hazard this module's own header comment names.

    Reader tier (`can_get_metadata`), matching `_my_permissions`' rule rather than the owner bar its
    two siblings on this router use: "what may I do here" discloses nothing about any other principal,
    and gating it at the owner bar would mean only the people who already know the answer could ask.
    """
    await fga_deps.require_relation(client, settings, token, relation="can_get_metadata", obj=f"warehouse:{id}")
    return await _my_permissions(request, settings, token, "warehouse", id)


@project_router.post("/{id}/access/my-permissions")
async def my_project_permissions(id: str, request: Request, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> MyPermissionsResponse:
    """What the caller may do on this project — same self-view, same explicit gate as the warehouse
    rung (``/v1/projects/…`` is likewise outside ``_RESOURCES``).

    The project rung is where the estate's most irreversible action lives (`DELETE /v1/projects/{id}`
    has no cascade at all), so this is the one whose absence forced the UI to show a delete button and
    discover the answer from a 403.

    GATED ON `member`, A BASE RUNG, and that is a deliberate departure worth reading. Every other
    surface here gates on a `can_*` action, but the `project` type defines NO reader-tier action — its
    whole action surface is `can_administer` / `can_create_warehouse` / `can_create_annotation_project`
    / `can_grant_*` / `can_read_assignments`, all admin or member tier. I reached for
    `can_get_metadata` by analogy with table and namespace; it does not exist on this type, and
    `test_every_fga_relation_in_code_exists_in_the_compiled_model` caught it — OpenFGA rejects an
    undefined relation with a 400 that fails closed to a 503 for every caller, so the analogy would
    have made this endpoint permanently unavailable.

    `member` is the reader-equivalent for this type: it is the tier ordinary tenant work already sits
    at (`can_create_annotation_project: member`), and a caller with no relationship to the project
    still gets a 403 rather than learning the tenant exists. The alternative — adding
    `can_get_metadata: member` to the model for symmetry — is a MODEL change with `.fga.yaml` updates
    and a migration story behind it, which is not something a UI-gating endpoint should drag in.
    """
    await fga_deps.require_relation(client, settings, token, relation="member", obj=f"project:{id}")
    return await _my_permissions(request, settings, token, "project", id)


async def _access_mutate(
    request: Request,
    settings: Settings,
    token: CurrentToken,
    fga_type: str,
    id: str,
    body: AccessGrantRequest,
    *,
    grant: bool,
) -> AccessGrantResponse:
    """Grant or revoke one base rung (owner/writer/reader/validator) to a subject on a table/namespace.

    The MUTATE half of the #68 governance surface — the write counterpart of ``access/list``.
    Owner-tier gated by the router (``access/grant`` / ``access/revoke`` → ``can_drop`` / ``can_delete`` in
    fga_deps) — managing an object's ACL is an owner-privileged act, the same bar as reviewing it.
    Fail-closed: only a grantable base rung the model defines is accepted (a ``can_*`` action or ``parent``
    is a 4xx, not a silent junk tuple); an OpenFGA outage is a 503, never a silent grant/revoke no-op. Both
    directions are idempotent (``write_tuples`` swallows a duplicate, ``delete_tuples`` an absent tuple) and
    audited distinctly (``access_grant`` / ``access_revoke``), carrying the grantee + rung."""
    if not settings.fga_enabled:
        raise UnsupportedOperationError("access mutation requires OpenFGA (this stack runs auth-off)")
    client = getattr(request.app.state, "fga", None)
    if client is None:  # the router gate already 503s this; kept so the endpoint is safe standalone
        raise ServiceUnavailableError("authorization service is not available")
    grantable = _grantable_relations(fga_type)
    if body.relation not in grantable:
        raise UnsupportedOperationError(f"{body.relation!r} is not a grantable rung on {fga_type} (one of {', '.join(grantable)})")
    segments = parse_identifier(id, settings.delimiter)
    obj = f"{fga_type}:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    actor = token.sub if token else "anonymous"
    # Resolve the grantee to a FULL subject (qualify=False semantics, mirroring access/check): a bare id is a
    # user; a qualified userset (``role:…#assignee`` / ``team:…#member``) is passed through verbatim.
    grantee = body.user if ":" in body.user else f"user:{body.user}"
    tup = fga.ClientTuple(user=grantee, relation=body.relation, object=obj)
    event = "access_grant" if grant else "access_revoke"
    try:
        if grant:
            await fga.write_tuples(client, [tup], actor=actor, origin="grant_api")
        else:
            await fga.delete_tuples(client, [tup], actor=actor, origin="grant_api")
    except ServiceUnavailableError:
        audit(event, FAILURE, subject=actor, resource=obj, grantee=grantee, relation=body.relation)
        raise
    # The per-tuple SUCCESS row is emitted by the library (one row per write site, structurally). This
    # keeps only the surface-specific vocabulary — `access_grant`/`access_revoke` is the compliance
    # verb for the per-object grant API, and dropping it would break the existing audit queries.
    audit(event, SUCCESS, subject=actor, resource=obj, grantee=grantee, relation=body.relation)
    await emit_control(
        get_control_emitter(request),
        action="grant_added" if grant else "grant_revoked",
        object_type="grant",
        object_id=obj,
        actor=f"user:{token.sub}" if token else None,
        extra={"relation": body.relation, "subject": grantee},
    )
    return AccessGrantResponse(object=obj, user=grantee, relation=body.relation, granted=grant)


@table_router.post("/{id}/access/grant")
async def grant_table_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: AccessGrantRequest) -> AccessGrantResponse:
    """Grant a base rung on the table to a subject — gated PER RUNG by the router
    (``can_grant_<relation>``, read from the body). Granting is its own axis now: a `manage_grants`
    holder may hand out access without holding the data, and a `pass_grants` delegate may hand on only
    what they already hold."""
    return await _access_mutate(request, settings, token, "table", id, body, grant=True)


@table_router.post("/{id}/access/revoke")
async def revoke_table_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: AccessGrantRequest) -> AccessGrantResponse:
    """Revoke a base rung on the table from a subject — gated per rung, identically to grant: taking a
    rung away is the same authority as handing it out."""
    return await _access_mutate(request, settings, token, "table", id, body, grant=False)


@namespace_router.post("/{id}/access/grant")
async def grant_namespace_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: AccessGrantRequest) -> AccessGrantResponse:
    """Grant a base rung on the namespace to a subject — gated per rung (``can_grant_<relation>``)."""
    return await _access_mutate(request, settings, token, "namespace", id, body, grant=True)


@namespace_router.post("/{id}/access/revoke")
async def revoke_namespace_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: AccessGrantRequest) -> AccessGrantResponse:
    """Revoke a base rung on the namespace from a subject — gated per rung, identically to grant."""
    return await _access_mutate(request, settings, token, "namespace", id, body, grant=False)


def _graph_node(node_id: str) -> GraphNode:
    """Split ``<type>:<rest>`` into a typed, labelled node (a userset like ``role:admin#assignee`` keeps
    its full id, labelled without the leading type)."""
    fga_type, _, rest = node_id.partition(":")
    return GraphNode(id=node_id, type=fga_type or "unknown", label=rest or node_id)


async def _access_graph(request: Request, settings: Settings, token: CurrentToken, fga_type: str, id: str) -> AccessGraphResponse:
    """The #81 authorization-graph primitive — one hop of the relationship graph around an object: the
    object, every subject directly granted a rung on it, and its ``parent``/``project`` container edge.

    Built from ``read_object_tuples`` (the direct tuples on the object — grants + the parent edge), so a
    grant appears as ``subject → object`` labelled with the rung and the container as ``object → parent``.
    The frontend expands the cascade by calling this again on a parent node. Owner-tier gated by the router
    (same disclosure bar as ``access/list``: the graph reveals principals), audited (``access_graph``), and
    fail-closed — an OpenFGA outage is a 503, never a partial graph that reads as 'nobody has access'."""
    if not settings.fga_enabled:
        raise UnsupportedOperationError("access graph requires OpenFGA (this stack runs auth-off)")
    client = getattr(request.app.state, "fga", None)
    if client is None:  # the router gate already 503s this; kept so the endpoint is safe standalone
        raise ServiceUnavailableError("authorization service is not available")
    segments = parse_identifier(id, settings.delimiter)
    obj = f"{fga_type}:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    subject = token.sub if token else "anonymous"
    try:
        tuples = await fga.read_object_tuples(client, obj)
    except ServiceUnavailableError:
        audit("access_graph", FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
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
    audit("access_graph", SUCCESS, subject=subject, resource=obj)
    return AccessGraphResponse(object=obj, nodes=list(nodes.values()), edges=edges)


@table_router.post("/{id}/access/graph")
async def graph_table_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken) -> AccessGraphResponse:
    """One hop of the authorization graph around the table — owner-gated by the router (``can_drop``)."""
    return await _access_graph(request, settings, token, "table", id)


@namespace_router.post("/{id}/access/graph")
async def graph_namespace_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken) -> AccessGraphResponse:
    """One hop of the authorization graph around the namespace — owner-gated (``can_delete``)."""
    return await _access_graph(request, settings, token, "namespace", id)


# --------------------------------------------------------------------------- #
# Managed access — centralize granting for a whole container
# --------------------------------------------------------------------------- #

#: The wildcard subject the flag is stored as. ``[user:*]`` is a TYPE RESTRICTION used as a switch,
#: not a public grant: no rule reads it as "everyone", only ``managed_access_inheritance`` reads it at
#: all. Written as one canonical tuple so the flag has exactly one representation to set, clear and
#: audit — a second spelling (``role:*``) would be a second thing to check when asking "is this on?".
_MANAGED_ACCESS_SUBJECT = "user:*"


async def _read_managed_access(request: Request, settings: Settings, fga_type: str, id: str) -> ManagedAccessResponse:
    """Is this container managed? The READ half, without which the flag is unusable in a UI.

    Reader-tier (``can_get_metadata``, via the suffix map), not the grant bar that SETS it. That is
    deliberate: the flag is the reason someone's grant controls are absent, so the person who needs
    the answer most is precisely the one who may not change it. Hiding the state from them turns a
    stated policy into an unexplained missing button.

    Answers ``false`` on an auth-off stack rather than refusing: no tuples means no flag, and a page
    asking "is this managed?" wants an answer it can render.
    """
    segments = parse_identifier(id, settings.delimiter)
    obj = f"{fga_type}:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    client = getattr(request.app.state, "fga", None)
    if not settings.fga_enabled or client is None:
        return ManagedAccessResponse(object=obj, managed_access=False)
    tuples = await fga.read_object_tuples(client, obj)
    return ManagedAccessResponse(object=obj, managed_access=any(t.object == obj and t.relation == "managed_access" for t in tuples))


@namespace_router.post("/{id}/managed-access/describe")
async def get_namespace_managed_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken) -> ManagedAccessResponse:
    """Whether granting on this namespace is centralized — reader-gated (``can_get_metadata``)."""
    return await _read_managed_access(request, settings, "namespace", id)


@warehouse_router.post("/{id}/managed-access/describe")
async def get_warehouse_managed_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken) -> ManagedAccessResponse:
    """The same for a warehouse. Gated EXPLICITLY — `warehouse` is not in ``_RESOURCES``, so
    ``authorize`` returns early and a route added here without this call is ungated."""
    segments = parse_identifier(id, settings.delimiter)
    obj = f"warehouse:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    await fga_deps.require_relation(getattr(request.app.state, "fga", None), settings, token, relation="can_get_metadata", obj=obj)
    return await _read_managed_access(request, settings, "warehouse", id)


async def _set_managed_access(request: Request, settings: Settings, token: CurrentToken, fga_type: str, id: str, enabled: bool) -> ManagedAccessResponse:
    """Set or clear the flag, idempotently.

    Reads before writing for the same reason the membership API does: an OpenFGA Write is
    transactional and REJECTS a tuple that already exists, so "turn it on when it is already on"
    would 400 rather than being the no-op a caller reasonably expects.
    """
    if not settings.fga_enabled:
        raise UnsupportedOperationError("managed access requires OpenFGA (this stack runs auth-off)")
    client = getattr(request.app.state, "fga", None)
    if client is None:  # the router gate already 503s this; kept so the endpoint is safe standalone
        raise ServiceUnavailableError("authorization service is not available")
    segments = parse_identifier(id, settings.delimiter)
    obj = f"{fga_type}:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    actor = token.sub if token else "system:catalog"
    tup = fga.ClientTuple(user=_MANAGED_ACCESS_SUBJECT, relation="managed_access", object=obj)

    already = any(t.object == obj and t.relation == "managed_access" for t in await fga.read_object_tuples(client, obj))
    if enabled and not already:
        await fga.write_tuples(client, [tup], actor=actor, origin="grant_api")
    elif not enabled and already:
        await fga.delete_tuples(client, [tup], actor=actor, origin="grant_api")
    # Audited distinctly from a grant: this does not move anyone's access, it changes WHO MAY move it —
    # a governance act whose blast radius is every object beneath, and one an auditor will look for by
    # name rather than by inferring it from the absence of later grants.
    audit("managed_access_set", SUCCESS, subject=actor, resource=obj, enabled=enabled)
    await emit_control(
        get_control_emitter(request),
        action="grant_added" if enabled else "grant_revoked",
        object_type="grant",
        object_id=obj,
        actor=f"user:{token.sub}" if token else None,
        extra={"relation": "managed_access", "subject": _MANAGED_ACCESS_SUBJECT},
    )
    return ManagedAccessResponse(object=obj, managed_access=enabled)


@namespace_router.post("/{id}/managed-access/set")
async def set_namespace_managed_access(
    id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: ManagedAccessRequest
) -> ManagedAccessResponse:
    """Centralize granting for this namespace and everything beneath it — gated on
    ``can_set_managed_access`` (which derives from ``manage_grants``).

    With it on, owners BELOW keep every data power and lose only the ability to hand access out; a
    grant-manager at or above this namespace keeps it. Note the consequence for clearing it: inside an
    already-managed scope the owner's ``manage_grants`` is withdrawn, so they cannot switch it off —
    which is what makes it a policy rather than a suggestion.
    """
    return await _set_managed_access(request, settings, token, "namespace", id, body.enabled)


@warehouse_router.post("/{id}/managed-access/set")
async def set_warehouse_managed_access(
    id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: ManagedAccessRequest
) -> ManagedAccessResponse:
    """The same, for a whole warehouse — the scope root, so this governs every stage and table in it.

    Gated EXPLICITLY rather than through the router's suffix map: ``warehouse`` is not in
    ``_RESOURCES``, so ``authorize`` returns early for these paths (the same reason ``warehouses.py``
    and ``projects.py`` call ``require_relation`` by hand). A route added here without that call would
    be authenticated and ungated.
    """
    segments = parse_identifier(id, settings.delimiter)
    obj = f"warehouse:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
    await fga_deps.require_relation(getattr(request.app.state, "fga", None), settings, token, relation="can_set_managed_access", obj=obj)
    return await _set_managed_access(request, settings, token, "warehouse", id, body.enabled)


# The v1 aggregator includes one ``router`` per module — the table + namespace routers are stitched here.
router = APIRouter()
router.include_router(table_router)
router.include_router(namespace_router)
router.include_router(warehouse_router)
router.include_router(project_router)
