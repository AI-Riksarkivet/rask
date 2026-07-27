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
from functools import lru_cache

from common import fga
from common.audit import FAILURE, SUCCESS, audit
from fastapi import APIRouter, Request
from lance_namespace import ServiceUnavailableError, UnsupportedOperationError

from catalog.api.dependencies import SettingsDep, get_control_emitter
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
    RelationGrants,
)


table_router = APIRouter(prefix="/v1/table", tags=["access"])
namespace_router = APIRouter(prefix="/v1/namespace", tags=["access"])


# The base rungs an admin may directly assign. The model defines each as ``[user, role#assignee] or …``
# (services/common/auth/model.fga) — a real user/userset grant, unlike the derived ``can_*`` actions or the
# structural ``parent`` edge, neither of which may be hand-granted. Intersected with the model below so a
# renamed rung drops out (and test_fga_model_contract would catch the drift).
_GRANTABLE_BASE: tuple[str, ...] = ("owner", "writer", "reader", "validator")


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
    return AccessListResponse(
        object=obj,
        grants=[RelationGrants(relation=relation, users=task.result()) for relation, task in zip(relations, tasks, strict=True)],
    )


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
            await fga.write_tuples(client, [tup])
        else:
            await fga.delete_tuples(client, [tup])
    except ServiceUnavailableError:
        audit(event, FAILURE, subject=actor, resource=obj, grantee=grantee, relation=body.relation)
        raise
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
    """Grant a base rung on the table to a subject — owner-gated by the router (``can_drop``)."""
    return await _access_mutate(request, settings, token, "table", id, body, grant=True)


@table_router.post("/{id}/access/revoke")
async def revoke_table_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: AccessGrantRequest) -> AccessGrantResponse:
    """Revoke a base rung on the table from a subject — owner-gated by the router (``can_drop``)."""
    return await _access_mutate(request, settings, token, "table", id, body, grant=False)


@namespace_router.post("/{id}/access/grant")
async def grant_namespace_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: AccessGrantRequest) -> AccessGrantResponse:
    """Grant a base rung on the namespace to a subject — owner-gated by the router (``can_delete``)."""
    return await _access_mutate(request, settings, token, "namespace", id, body, grant=True)


@namespace_router.post("/{id}/access/revoke")
async def revoke_namespace_access(id: str, request: Request, settings: SettingsDep, token: CurrentToken, body: AccessGrantRequest) -> AccessGrantResponse:
    """Revoke a base rung on the namespace from a subject — owner-gated by the router (``can_delete``)."""
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


# The v1 aggregator includes one ``router`` per module — the table + namespace routers are stitched here.
router = APIRouter()
router.include_router(table_router)
router.include_router(namespace_router)
