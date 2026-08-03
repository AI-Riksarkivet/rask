"""Estate-admin FGA administration API (``/v1/access``): raw tuples, the model text, and a check.

The RAW-tuple counterpart of the per-object ``…/access/*`` surfaces (#51/#68/#72/#81): where those answer
questions about ONE table/namespace at the owner bar, this browses and edits the authorization store
itself — every tuple, any type — so it is gated exactly like ``/v1/events``: the surface is estate-wide,
therefore observing (or mutating) it is a PLATFORM privilege, ``can_observe_events`` on the fixed root
object (``settings.fga_root_object``), decided explicitly per handler because ``/v1/access`` matches no
resource prefix in ``fga_deps.authorize`` (the router gate only sets the 401/503 floor).

Fail-closed and model-true throughout: FGA off → 501 (nothing to administer), an unwired client → 503,
and every relation/type named by a caller is validated against the COMPILED model first, so a phantom
relation or a non-assignable (derived ``can_*``) tuple write is a clean 400 here — never an OpenFGA 400
that fails closed to a 503, and never a write the idempotent-duplicate handling could swallow as success.
Tuple writes/deletes are additionally audited (``access_tuple_write`` / ``access_tuple_delete``, the
``access_grant`` pattern) on top of the gate's allow/deny record; reads audit the disclosure
(``access_tuples_read``), mirroring ``access_review``.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Query, Request
from lance_namespace import (
    InvalidInputError,
    ServiceUnavailableError,
    UnsupportedOperationError,
)
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps
from catalog.api.dependencies import SettingsDep, get_control_emitter
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.control_emit import emit_control
from catalog.schemas import (
    AccessCheckBody,
    AccessCheckResult,
    AccessExpandNode,
    AccessExpandRequest,
    AccessExpandResponse,
    AccessListObjectsRequest,
    AccessListObjectsResponse,
    AccessListUsersRequest,
    AccessListUsersResponse,
    AccessModelResponse,
    AccessSimulateRequest,
    AccessSimulateResult,
    AccessTuple,
    AccessTuplesPage,
    TupleCondition,
)
from service_kit.governed import fga
from service_kit.governed.audit import FAILURE, SUCCESS, audit


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/access", tags=["access"])

#: OpenFGA's Read page-size ceiling — a larger request is a server-side 400, so cap it at the API edge.
_MAX_PAGE_SIZE = 100


@lru_cache
def _model_types() -> frozenset[str]:
    """Every type the compiled model defines — an object naming any other type is a clean 400."""
    return frozenset(td["type"] for td in fga.load_model()["type_definitions"])


@lru_cache
def _defined_relations(fga_type: str) -> frozenset[str]:
    """Every relation the compiled model defines on ``fga_type`` (base rungs, ``can_*`` actions, and the
    structural edges) — what a CHECK may probe. Read from ``model.json`` (what the app loads) so a phantom
    relation can never reach OpenFGA as a 400 that fails closed to a 503."""
    for td in fga.load_model()["type_definitions"]:
        if td["type"] == fga_type:
            return frozenset(td.get("relations") or {})
    return frozenset()


@lru_cache
def _assignable_relations(fga_type: str) -> frozenset[str]:
    """The relations on ``fga_type`` that accept a DIRECT tuple (non-empty ``directly_related_user_types``
    in the model metadata) — what a tuple WRITE/DELETE may name. A derived ``can_*`` action is defined but
    not assignable: OpenFGA rejects such a write with the same 400 class the idempotent-duplicate handling
    swallows, so without this pre-check a no-op write could report 200."""
    for td in fga.load_model()["type_definitions"]:
        if td["type"] == fga_type:
            meta = (td.get("metadata") or {}).get("relations") or {}
            return frozenset(r for r, m in meta.items() if m.get("directly_related_user_types"))
    return frozenset()


@lru_cache
def _model_conditions() -> dict[str, dict[str, str]]:
    """``{condition: {parameter: type}}`` from the compiled model, types stripped to their short form
    (``TYPE_NAME_TIMESTAMP`` → ``timestamp``).

    Read from the model rather than hand-listed for the usual reason — a UI offering a parameter the
    condition does not take, or missing one it requires, produces an OpenFGA 400 that this API's
    fail-closed handling turns into a 503 for everyone.
    """
    out: dict[str, dict[str, str]] = {}
    for name, spec in (fga.load_model().get("conditions") or {}).items():
        params = (spec or {}).get("parameters") or {}
        out[name] = {p: str(v.get("type_name", "")).removeprefix("TYPE_NAME_").lower() for p, v in params.items()}
    return out


@lru_cache
def _conditions_for(fga_type: str, relation: str) -> frozenset[str]:
    """The conditions this relation may be granted WITH on this type.

    A rung accepts both forms in this model (`[user, …, user with non_expired_grant, …]`), so an empty
    set here means the rung is permanent-only — and a caller naming a condition on it is a clean 400
    rather than an OpenFGA rejection that fails closed to 503."""
    for td in fga.load_model()["type_definitions"]:
        if td["type"] != fga_type:
            continue
        meta = ((td.get("metadata") or {}).get("relations") or {}).get(relation) or {}
        return frozenset(t["condition"] for t in meta.get("directly_related_user_types") or [] if t.get("condition"))
    return frozenset()


def _validated_object(obj: str) -> str:
    """Require ``<type>:<id>`` with a model-defined type and a non-empty id."""
    obj_type, sep, obj_id = obj.partition(":")
    if not sep or not obj_id:
        raise InvalidInputError(f"object must be '<type>:<id>', got {obj!r}")
    if obj_type not in _model_types():
        raise InvalidInputError(f"{obj_type!r} is not a type the authorization model defines")
    return obj


def _qualified_subject(user: str) -> str:
    """Resolve to a FULL subject: a bare id is a user (``user:<id>``); a qualified subject or userset
    (``team:acme#member``, ``role:admin#assignee``) passes through verbatim — the same resolution as the
    per-object access/check + access/grant surfaces (double-prefixing would falsely deny every check)."""
    return user if ":" in user else f"user:{user}"


async def _estate_gate(request: Request, settings: Settings, token: CurrentToken) -> OpenFgaClient:
    """The estate-admin door every ``/v1/access`` handler clears (mirrors ``/v1/events``).

    FGA off → 501 (there is no store to administer, unlike the events feed which still serves its buffer);
    enabled-but-unwired → 503 fail-closed; then ``can_observe_events`` on the fixed root object — a
    platform privilege, NOT per-project, because the tuple store is the whole estate's authorization state
    (authz scope == data scope, the #12 events fix). The gate itself audits the allow/deny.
    """
    if not settings.fga_enabled:
        raise UnsupportedOperationError("FGA administration requires OpenFGA (this stack runs auth-off)")
    client = getattr(request.app.state, "fga", None)
    if client is None:
        raise ServiceUnavailableError("authorization service is not available")
    await fga_deps.require_relation(client, settings, token, relation="can_observe_events", obj=settings.fga_root_object)
    return client


@router.get("/tuples")
async def read_access_tuples(
    request: Request,
    settings: SettingsDep,
    token: CurrentToken,
    object_type: Annotated[str | None, Query(description="scan one whole type (e.g. 'table')")] = None,
    user: Annotated[str | None, Query(description="filter by subject (bare id = user:<id>)")] = None,
    object: Annotated[str | None, Query(description="filter by one full object ('table:db1$t')")] = None,
    page_size: Annotated[int | None, Query(ge=1, le=_MAX_PAGE_SIZE)] = None,
    continuation: Annotated[str | None, Query(description="the previous page's continuation token")] = None,
) -> AccessTuplesPage:
    """One page of the raw tuple store, estate-admin gated.

    Filter combos follow OpenFGA Read's contract — the object side must carry at least a TYPE whenever any
    filter is sent: ``object_type`` alone (whole-type scan), ``object`` alone (one object's tuples),
    ``user`` combined with either (that subject's tuples there). A user-only filter is a 400 — OpenFGA's
    Read API cannot answer it (add ``object_type`` or ``object``; per-subject enumeration across all types
    is a ListObjects question, not a Read). No filter reads the whole store, paginated.
    """
    client = await _estate_gate(request, settings, token)
    if object is not None:
        obj_filter: str | None = _validated_object(object)
    elif object_type is not None:
        if object_type not in _model_types():
            raise InvalidInputError(f"{object_type!r} is not a type the authorization model defines")
        obj_filter = f"{object_type}:"
    elif user is not None:
        raise InvalidInputError(
            "a user-only filter is not supported: OpenFGA's Read API requires an object type whenever a "
            "tuple filter is sent — add object_type= (whole-type scan) or object= (one object)"
        )
    else:
        obj_filter = None  # no filter at all → the whole store, paginated
    subject = token.sub if token else "anonymous"
    resource = obj_filter or "*"
    try:
        tuples, next_token = await fga.read_tuples(
            client,
            user=_qualified_subject(user) if user else None,
            obj=obj_filter,
            page_size=page_size,
            continuation_token=continuation,
        )
    except ServiceUnavailableError:
        audit("access_tuples_read", FAILURE, subject=subject, resource=resource, reason="authz_unavailable")
        raise
    # #41: the raw-tuple listing is an estate-wide ACL disclosure — audit it distinctly from the gate's
    # allow, exactly like access_review on the per-object surface.
    audit("access_tuples_read", SUCCESS, subject=subject, resource=resource, delivered=len(tuples))
    return AccessTuplesPage(
        tuples=[_as_access_tuple(t) for t in tuples],
        continuation=next_token,
    )


def _as_access_tuple(t: fga.ClientTuple) -> AccessTuple:
    """Map a stored tuple out to the wire, CONDITION INCLUDED.

    Dropping the condition here would make every time-boxed grant read back as permanent — the UI
    would show no expiry, and a reviewer auditing "who has writer on bronze" would see a name that is
    in fact about to lapse (or has already). The distinction is the entire feature."""
    condition = None
    if (c := getattr(t, "condition", None)) is not None and getattr(c, "name", None):
        condition = TupleCondition(name=c.name, context=dict(getattr(c, "context", None) or {}))
    return AccessTuple(user=t.user, relation=t.relation, object=t.object, condition=condition)


def _validated_write_tuple(body: AccessTuple) -> fga.ClientTuple:
    """Validate a caller-named tuple against the compiled model before it can reach OpenFGA."""
    obj = _validated_object(body.object)
    fga_type = obj.partition(":")[0]
    assignable = _assignable_relations(fga_type)
    if body.relation not in assignable:
        raise InvalidInputError(f"{body.relation!r} is not a directly-assignable relation on {fga_type} (one of {', '.join(sorted(assignable))})")

    condition = None
    if body.condition is not None:
        allowed = _conditions_for(fga_type, body.relation)
        if body.condition.name not in allowed:
            detail = f"one of {', '.join(sorted(allowed))}" if allowed else f"{fga_type}#{body.relation} accepts no condition"
            raise InvalidInputError(f"{body.condition.name!r} is not a condition on {fga_type}#{body.relation} ({detail})")
        # Every declared parameter must be supplied. A CEL expression missing an operand does not
        # evaluate to false — it ERRORS, which OpenFGA returns as a 400 and this API fails closed to a
        # 503. Catching it here makes an incomplete grant a clean rejection at the moment it is written,
        # rather than an outage the first time anyone checks the object.
        declared = _model_conditions().get(body.condition.name, {})
        supplied = set(body.condition.context or {})
        # `current_time` is the CALLER's clock, supplied per check — it must NOT ride the tuple.
        required = {p for p in declared if p != "current_time"}
        if missing := required - supplied:
            raise InvalidInputError(f"condition {body.condition.name!r} needs {', '.join(sorted(missing))}")
        if unknown := supplied - set(declared):
            raise InvalidInputError(f"condition {body.condition.name!r} has no parameter {', '.join(sorted(unknown))}")
        condition = fga.RelationshipCondition(name=body.condition.name, context=dict(body.condition.context or {}))

    return fga.ClientTuple(user=_qualified_subject(body.user), relation=body.relation, object=obj, condition=condition)


async def _mutate_tuple(request: Request, settings: Settings, token: CurrentToken, body: AccessTuple, *, write: bool) -> AccessTuple:
    client = await _estate_gate(request, settings, token)
    tup = _validated_write_tuple(body)
    actor = token.sub if token else "anonymous"
    event = "access_tuple_write" if write else "access_tuple_delete"
    try:
        if write:
            await fga.write_tuples(client, [tup], actor=actor, origin="admin_api")
        else:
            await fga.delete_tuples(client, [tup], actor=actor, origin="admin_api")
    except ServiceUnavailableError:
        # Only the FAILURE row is emitted here. The SUCCESS row now comes from inside
        # `fga.write_tuples`/`delete_tuples`, so every write site gets one — and emitting it here too
        # would give this one surface two rows per write while the other six still had none.
        audit(event, FAILURE, subject=actor, resource=tup.object, grantee=tup.user, relation=tup.relation)
        raise
    # The control-plane feed documents that GRANTS tick the cursor — and the per-object grant surface
    # emits, so a raw-tuple write that stayed silent made this the one grant path live surfaces could
    # not see. Best-effort AFTER the store mutation (the emitter swallows bus failures), like access.py.
    condition = getattr(tup, "condition", None)
    await emit_control(
        get_control_emitter(request),
        action="grant_added" if write else "grant_revoked",
        object_type="grant",
        object_id=tup.object,
        actor=f"user:{token.sub}" if token else None,
        extra={"relation": tup.relation, "subject": tup.user} | ({"condition": condition.name} if condition is not None else {}),
    )
    return _as_access_tuple(tup)


@router.post("/tuples")
async def write_access_tuple(request: Request, settings: SettingsDep, token: CurrentToken, body: AccessTuple) -> AccessTuple:
    """Write ONE raw tuple (idempotent — a duplicate write is success), echoing the tuple as stored.

    Only a directly-assignable relation the compiled model defines on the object's type is accepted (a
    derived ``can_*`` action or an unknown type/relation is a 400) — the raw-tuple analog of the
    per-object grant surface's grantable-rung guard, without its table/namespace restriction.
    """
    return await _mutate_tuple(request, settings, token, body, write=True)


@router.delete("/tuples")
async def delete_access_tuple(request: Request, settings: SettingsDep, token: CurrentToken, body: AccessTuple) -> AccessTuple:
    """Delete ONE raw tuple (idempotent — an already-absent tuple is success), echoing the tuple removed."""
    return await _mutate_tuple(request, settings, token, body, write=False)


@router.get("/model")
async def get_access_model(request: Request, settings: SettingsDep, token: CurrentToken) -> AccessModelResponse:
    """The authorization model — the checked-in ``model.fga`` DSL text plus the pinned model id.

    Read-only and estate-admin gated: the model reveals the whole privilege topology (which rungs derive
    which actions), the same platform-tier disclosure as the tuple listing.
    """
    client = await _estate_gate(request, settings, token)
    model_id = client.get_authorization_model_id() or settings.fga_model_id or ""
    return AccessModelResponse(
        dsl=fga.load_model_dsl(),
        authorization_model_id=model_id,
        conditions={name: dict(params) for name, params in _model_conditions().items()},
    )


@router.post("/check")
async def check_access(request: Request, settings: SettingsDep, token: CurrentToken, body: AccessCheckBody) -> AccessCheckResult:
    """One ``(user, relation, object)`` OpenFGA Check over ANY model type — the estate-wide extension of
    the per-object ``…/access/check`` playground (#68), which stays as-is for table/namespace owners.

    Any relation the compiled model defines on the type may be probed (base rungs included — an admin
    debugging a cascade asks about ``writer``, not just ``can_write_data``); an unknown one is a clean 400.
    ``checked`` echoes the resolved tuple so the verdict is unambiguous.

    ``context`` supplies the runtime values a CONDITION needs — ``current_time`` for the model's
    ``non_expired_grant``. Omitting it against a time-boxed grant is a DENY, not a neutral answer:
    OpenFGA cannot evaluate the CEL expression without its operands. The server does NOT default the
    clock, deliberately — a check that silently substituted "now" would answer a question the caller
    did not ask, and the whole point of the explorer is asking "was this true at 14:00?".
    """
    client = await _estate_gate(request, settings, token)
    obj = _validated_object(body.object)
    fga_type = obj.partition(":")[0]
    if body.relation not in _defined_relations(fga_type):
        raise InvalidInputError(f"{body.relation!r} is not a relation the model defines on {fga_type}")
    user = _qualified_subject(body.user)
    subject = token.sub if token else "anonymous"
    try:
        allowed = await fga.check(client, user=user, relation=body.relation, obj=obj, qualify=False, context=body.context or None)
    except ServiceUnavailableError:
        audit("access_simulate", FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    # Same disclosure event as the per-object simulator — one queryable stream for "who probed the graph".
    audit("access_simulate", SUCCESS, subject=subject, resource=obj)
    checked = AccessTuple(user=user, relation=body.relation, object=obj)
    return AccessCheckResult(allowed=allowed, checked=checked)


def _validated_relation(obj_or_type: str, relation: str) -> str:
    """Require ``relation`` to be one the compiled model defines on the type (base rungs included)."""
    fga_type = obj_or_type.partition(":")[0]
    if relation not in _defined_relations(fga_type):
        raise InvalidInputError(f"{relation!r} is not a relation the model defines on {fga_type}")
    return relation


@router.post("/list-objects")
async def list_access_objects(request: Request, settings: SettingsDep, token: CurrentToken, body: AccessListObjectsRequest) -> AccessListObjectsResponse:
    """ "What can this subject reach?" — every ``type`` object on which ``user`` holds ``relation``.

    The forward half of the explorer, and the answer ``GET /tuples`` structurally cannot give: Read needs
    an object type per call and returns only STORED tuples, so enumerating a subject meant one audited
    read per model type and still missed everything derived. ListObjects expands the model, so a table
    reached through team → project → warehouse → namespace shows up here and nowhere else.

    The subject may be a userset (``team:eng#member``, ``role:validators#assignee``), which matters
    because this model deliberately refuses ``team#member`` on resource rungs — a team reaches data
    through a role, and only the userset question shows that.
    """
    client = await _estate_gate(request, settings, token)
    if body.type not in _model_types():
        raise InvalidInputError(f"{body.type!r} is not a type the authorization model defines")
    relation = _validated_relation(body.type, body.relation)
    user = _qualified_subject(body.user)
    subject = token.sub if token else "anonymous"
    try:
        objects = await fga.list_objects(client, user=user, relation=relation, object_type=body.type, qualify=False)
    except ServiceUnavailableError:
        audit("access_list_objects", FAILURE, subject=subject, resource=f"{body.type}:", reason="authz_unavailable")
        raise
    # Enumerating one subject's whole reach is an estate-wide ACL disclosure, same tier as access_tuples_read.
    audit("access_list_objects", SUCCESS, subject=subject, resource=f"{body.type}:", grantee=user, relation=relation, delivered=len(objects))
    return AccessListObjectsResponse(objects=objects, user=user, relation=relation, type=body.type)


@router.post("/list-users")
async def list_access_users(request: Request, settings: SettingsDep, token: CurrentToken, body: AccessListUsersRequest) -> AccessListUsersResponse:
    """ "Who can do this?" — every subject holding ``relation`` on ``object``, effective access included.

    The inverse of list-objects and the primitive a revoke needs BEFORE it writes: a tuple sitting high in
    the hierarchy can revoke access for many principals at once, and "are you sure?" is not an answer to
    "how many". Subjects come back fully qualified so a userset is never mistaken for a user.
    """
    client = await _estate_gate(request, settings, token)
    obj = _validated_object(body.object)
    relation = _validated_relation(obj, body.relation)
    if body.user_type not in _model_types():
        raise InvalidInputError(f"{body.user_type!r} is not a type the authorization model defines")
    if body.user_relation is not None and body.user_relation not in _defined_relations(body.user_type):
        raise InvalidInputError(f"{body.user_relation!r} is not a relation the model defines on {body.user_type}")
    subject = token.sub if token else "anonymous"
    try:
        users = await fga.list_users(
            client,
            relation=relation,
            obj=obj,
            user_type=body.user_type,
            user_relation=body.user_relation,
            qualified=True,
        )
    except ServiceUnavailableError:
        audit("access_list_users", FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    audit("access_list_users", SUCCESS, subject=subject, resource=obj, relation=relation, delivered=len(users))
    # ListUsers has no pagination and the server truncates silently at listUsersMaxResults — an
    # under-reported access review must never render as a complete one, so the ceiling rides the wire.
    return AccessListUsersResponse(
        users=users,
        object=obj,
        relation=relation,
        user_type=body.user_type,
        user_relation=body.user_relation,
        truncated=len(users) >= fga.LIST_USERS_SERVER_CAP,
    )


@router.post("/expand")
async def expand_access(request: Request, settings: SettingsDep, token: CurrentToken, body: AccessExpandRequest) -> AccessExpandResponse:
    """WHY a relation resolves — the userset tree, optionally followed through the cascade.

    Check answers yes/no and list-users answers who; neither says through WHICH rung or WHICH parent hop,
    so a permission derived over three objects is indistinguishable from a direct grant — and they have
    very different blast radii. This returns the derivation itself, keeping OpenFGA's own operators
    (``union`` / ``intersection`` / ``difference``) rather than flattening them away: the operator is
    frequently the whole explanation for a surprising grant.

    ``depth`` is server-side on purpose. Walking the chain client-side costs one estate-gated, audited
    round trip per hop under the browser client's fixed timeout; here it is one gate, one audit row, and
    one response.
    """
    client = await _estate_gate(request, settings, token)
    obj = _validated_object(body.object)
    relation = _validated_relation(obj, body.relation)
    depth = max(1, min(body.depth, fga.MAX_EXPAND_TREE_DEPTH))
    subject = token.sub if token else "anonymous"
    try:
        tree = await fga.expand_tree(client, relation=relation, obj=obj, max_depth=depth)
    except ServiceUnavailableError:
        audit("access_expand", FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    audit("access_expand", SUCCESS, subject=subject, resource=obj, relation=relation, depth=depth)
    # An empty tree ({}) means the relation resolves to nothing here — a real answer, distinct from the
    # 503 an outage raises above. Keep them distinguishable on the wire: null tree, never a silent {}.
    return AccessExpandResponse(
        tree=AccessExpandNode.model_validate(tree) if tree else None,
        object=obj,
        relation=relation,
        depth=depth,
    )


@router.post("/simulate")
async def simulate_access(request: Request, settings: SettingsDep, token: CurrentToken, body: AccessSimulateRequest) -> AccessSimulateResult:
    """ "Would this grant do what I think?" — a Check evaluated as if ``hypothetical`` existed.

    Nothing is written. OpenFGA's contextual tuples are per-request, so the store is untouched and the
    simulation owes no cleanup. This exists because the model is CONCENTRIC: a grant three levels up
    cascades to every child, and predicting that by reading the DSL is exactly the reasoning humans get
    wrong. The only way to find out today is to write the tuple and look — which on an authorization
    store is a change you then have to remember to undo.

    Returns the DELTA. ``allowed`` alone cannot be acted on: a grant that changes nothing and a grant
    that unlocks access both answer true. ``baseline`` is the same Check WITHOUT the hypothesis, so the
    caller can distinguish "this is what changes the answer" from "this grant is a no-op" — and the
    second is the more common finding.

    Every hypothetical tuple is validated exactly like a real write (``_validated_write_tuple``): a
    derived ``can_*`` or an unknown type is a clean 400 here rather than a confusing OpenFGA rejection,
    and — more importantly — a hypothesis this API would refuse to write must not be simulatable, or
    the answer describes a grant that cannot exist.
    """
    client = await _estate_gate(request, settings, token)
    obj = _validated_object(body.object)
    relation = _validated_relation(obj, body.relation)
    user = _qualified_subject(body.user)
    # Validated, not merely parsed — same gate a real write clears.
    hypothetical = [_validated_write_tuple(t) for t in body.hypothetical]
    subject = token.sub if token else "anonymous"
    try:
        # Two checks, one round trip each. The baseline cannot be skipped when `hypothetical` is empty
        # either: the caller still asked for a delta, and answering with allowed == baseline is the
        # honest shape rather than a special case that returns a different meaning.
        baseline = await fga.check(client, user=user, relation=relation, obj=obj, qualify=False)
        allowed = (
            baseline if not hypothetical else await fga.check(client, user=user, relation=relation, obj=obj, qualify=False, contextual_tuples=hypothetical)
        )
    except ServiceUnavailableError:
        audit("access_simulate_hypothetical", FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    # Probing the graph IS disclosing it, hypothetically or not — same tier as access_simulate, but a
    # distinct action so a compliance query can tell a what-if apart from a live check.
    audit("access_simulate_hypothetical", SUCCESS, subject=subject, resource=obj, relation=relation, hypotheticals=len(hypothetical))
    return AccessSimulateResult(
        allowed=allowed,
        baseline=baseline,
        checked=AccessTuple(user=user, relation=relation, object=obj),
        hypothetical=[_as_access_tuple(t) for t in hypothetical],
    )
