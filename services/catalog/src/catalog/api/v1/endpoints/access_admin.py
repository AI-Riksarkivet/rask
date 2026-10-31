"""Estate-admin FGA administration API (``/management/v1/access``): raw tuples, the model text, and a check.

The RAW-tuple counterpart of the per-object ``…/access/*`` surfaces (#51/#68/#72/#81): where those answer
questions about ONE table/namespace at the owner bar, this browses and edits the authorization store
itself — every tuple, any type — so it is gated exactly like ``/management/v1/events``: the surface is estate-wide,
therefore observing (or mutating) it is a PLATFORM privilege, ``can_observe_events`` on the fixed root
object (``settings.fga_root_object``), decided explicitly per handler because ``/management/v1/access`` matches no
resource prefix in ``fga_deps.authorize`` (the router gate only sets the 401/503 floor).

Fail-closed and model-true throughout: FGA off → 406 Unsupported (nothing to administer), an unwired client → 503,
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

from fastapi import APIRouter, Depends, Path, Query
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    InvalidInputError,
    ServiceUnavailableError,
    TableNotFoundError,
)
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, SettingsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings
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
    PrincipalDeleted,
    PrincipalRecord,
    TupleCondition,
)
from catalog.services import principals, project_admins
from service_kit.control_emit import ControlEmitter, emit_control
from service_kit.governed import fga
from service_kit.governed.audit import AuditAction, AuditOutcome, audit
from service_kit.governed.principal import is_principal


log = logging.getLogger(__name__)


#: OpenFGA's Read page-size ceiling — a larger request is a server-side 400, so cap it at the API edge.
_MAX_PAGE_SIZE = 100
#: The warehouse relation only the chart's bootstrap hook and the catalog's own maintainer seed write ([[LH-473]]).
_MAINTENANCE = "maintenance"


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
def _types_granting_principals() -> tuple[str, ...]:
    """The types with a relation a principal can hold directly (``[user]``, never only ``user:*``): where a tuple naming
    one can sit, and so every type a Read by user is asked about."""
    granting = []
    for td in fga.load_model()["type_definitions"]:
        meta = (td.get("metadata") or {}).get("relations") or {}
        if any(
            t.get("type") == "user" and not t.get("relation") and not t.get("wildcard")
            for m in meta.values()
            for t in m.get("directly_related_user_types") or []
        ):
            granting.append(td["type"])
    return tuple(sorted(granting))


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
def _model_dsl() -> str:
    """The checked-in ``model.fga`` DSL text, read ONCE per process (catalog-api-18).

    ``fga.load_model_dsl()`` is an uncached blocking ``Path.read_text()`` of a file BAKED INTO THE
    IMAGE — it cannot change while the process runs, so re-reading it per request bought nothing and
    cost a filesystem round trip inside an ``async def``. Cached here alongside ``_model_types`` /
    ``_model_conditions``, which already read the compiled twin exactly this way. The route still hands
    the first (cold) call to a threadpool, so even the read that does happen never blocks the loop.
    """
    return fga.load_model_dsl()


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
    """Resolve to a FULL subject (`fga.grantee_object`): a subject is the user it is; a qualified object or userset
    (``team:acme#member``, ``role:admin#assignee``) passes through verbatim — the same resolution as the
    per-object access/check + access/grant surfaces (double-prefixing would falsely deny every check)."""
    return fga.grantee_object(user)


def _tuple_user(user: str, *, evaluated: bool) -> str:
    """The user a caller-named tuple names: a principal, as :func:`_qualified_subject` resolves it, with one exception.

    A delete names a stored ``user:<id>`` that is no principal exactly as the store holds it (a raw IdP subject the
    store already held, [[LH-063]]). A delete only removes access, and refusing it would leave such a grant irrevocable
    through the body the Inspector sends back, as :func:`_validated_write_tuple` keeps a clock-pinned grant revocable.
    A write, a bare value and a stored id that is a principal are resolved as before, so a revoke never misses the
    encoded tuple it names.
    """
    kind, typed, stored = user.partition(":")
    if not evaluated and typed and kind == "user" and not is_principal(fga.subject_of(stored)):
        return user
    return _qualified_subject(user)


def _require_fga_client(client: FgaClientDep) -> OpenFgaClient:
    """The wired client, or the same 503 the gate raises.

    Duplicated with `estate_gate` deliberately and minimally: FastAPI resolves dependencies
    independently, so this cannot read the gate's result. Both go through the SAME body
    (`fga_deps.require_wired`), the gate runs first, so the refusal here is unreachable in practice —
    it exists so the TYPE is honest.
    """
    return fga_deps.require_wired(client)


#: The raw OpenFGA client, for the handlers that operate on the tuple store directly.
#:
#: SEPARATE FROM THE GATE on purpose. `estate_gate` used to return this, so authorization and injection
#: were one call and every handler had to make it. Splitting them lets the gate live on the router —
#: where a new route inherits it — while a handler that needs the client says so in its own signature.
#:
#: Non-optional: `estate_gate` has already refused an unwired client with a 503 before any handler runs,
#: so `None` is unreachable here and typing it optional would push a check into nine places that the
#: door has already made once.
EstateFgaClient = Annotated[OpenFgaClient, Depends(_require_fga_client)]


async def estate_gate(client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> None:
    """The estate-admin door every ``/management/v1/access`` route clears (mirrors ``/management/v1/events``).

    ON THE ROUTER, not called nine times by hand. This used to be `_estate_gate`, opened explicitly at
    the top of all nine handlers — every one of them correct, which is exactly the hazard: the tenth
    route is the one that forgets, and nothing would have said so. `authn.md` gives the rule directly —
    "apply at the router level when every route in the group needs the same check — cheaper to read and
    harder to forget" — and this is the estate's highest-privilege router, reading and WRITING the raw
    tuple store that is the authorization state of everything.

    It returns None now rather than the client. Handlers that need the client object take `FgaClientDep`
    instead, so the gate does one job and the injection does the other.

    FGA off → 406 Unsupported (there is no store to administer, unlike the events feed which still serves its buffer);
    enabled-but-unwired → 503 fail-closed; then ``can_observe_events`` on the fixed root object — a
    platform privilege, NOT per-project, because the tuple store is the whole estate's authorization state
    (authz scope == data scope, the #12 events fix). The gate itself audits the allow/deny.
    """
    wired = fga_deps.require_fga(settings, client, feature="FGA administration")
    await fga_deps.require_relation(wired, settings, token, relation="can_observe_events", obj=settings.fga_root_object)


#: ONE DOOR for the whole router. See `estate_gate` — this replaces nine hand-written calls, and is
#: declared here rather than at the top of the file only because it must follow the function it names.
router = APIRouter(prefix="/management/v1/access", tags=["access"], dependencies=[Depends(estate_gate)])


@router.get("/tuples")
async def read_access_tuples(
    client: EstateFgaClient,
    token: CurrentToken,
    object_type: Annotated[str | None, Query(description="with user=, that subject's tuples on the objects of this type (e.g. 'table')")] = None,
    user: Annotated[str | None, Query(description="filter by subject (bare id = user:<id>)")] = None,
    object: Annotated[str | None, Query(description="filter by one full object ('table:<uuid>', as OpenFGA holds a table)")] = None,
    page_size: Annotated[int | None, Query(ge=1, le=_MAX_PAGE_SIZE)] = None,
    continuation: Annotated[str | None, Query(description="the previous page's continuation token")] = None,
) -> AccessTuplesPage:
    """One page of the raw tuple store, estate-admin gated.

    Each filter is one OpenFGA's Read answers: ``object`` alone (one object's tuples), ``user`` with ``object`` or
    ``object_type`` (that subject's tuples on the object, or on the objects of the type), or no filter (the whole store,
    paginated). Read refuses a filter naming no object type, and one naming a type with neither an object id nor a user
    (OpenFGA v1.18.3), and OpenFGA's refusal would reach the caller as a 503, so ``user`` alone and ``object_type`` alone
    are a 400 here, naming the half to add.
    """
    if object is not None:
        obj_filter: str | None = _validated_object(object)
    elif object_type is not None:
        if object_type not in _model_types():
            raise InvalidInputError(f"{object_type!r} is not a type the authorization model defines")
        if user is None:
            raise InvalidInputError(
                "a type-only filter is not supported: OpenFGA's Read API needs an object id or a user beside an "
                "object type — add user= (that subject's tuples of this type) or object= (one object's tuples)"
            )
        obj_filter = f"{object_type}:"
    elif user is not None:
        raise InvalidInputError(
            "a user-only filter is not supported: OpenFGA's Read API requires an object type whenever a "
            "tuple filter is sent — add object_type= (that subject's tuples of one type) or object= (one object's tuples)"
        )
    else:
        obj_filter = None  # no filter at all → the whole store, paginated
    subject = token.subject if token else "anonymous"
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
        audit(AuditAction.ACCESS_TUPLES_READ, AuditOutcome.FAILURE, subject=subject, resource=resource, reason="authz_unavailable")
        raise
    # #41: the raw-tuple listing is an estate-wide ACL disclosure — audit it distinctly from the gate's
    # allow, exactly like access_review on the per-object surface.
    audit(AuditAction.ACCESS_TUPLES_READ, AuditOutcome.SUCCESS, subject=subject, resource=resource, delivered=len(tuples))
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


def _validated_write_tuple(body: AccessTuple, *, evaluated: bool) -> fga.ClientTuple:
    """Validate a caller-named tuple against the compiled model before it can reach OpenFGA.

    ``evaluated`` is True when OpenFGA will evaluate the tuple (a write, a simulated hypothesis) and False
    for a delete, which OpenFGA resolves by key alone. Only an evaluated tuple is refused the check's
    clock: the read door echoes a stored condition verbatim, so a delete refusing it would leave a
    clock-pinned grant irrevocable through the body the Inspector sends back. The refusal is
    ``fga.reject_stored_clock``, which ``fga.write_tuples`` and ``fga.check`` also run; calling it here
    answers before a simulation's baseline check has been sent.
    """
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
        required = set(declared) - {fga.CLOCK_PARAMETER}
        if missing := required - supplied:
            raise InvalidInputError(f"condition {body.condition.name!r} needs {', '.join(sorted(missing))}")
        if unknown := supplied - set(declared):
            raise InvalidInputError(f"condition {body.condition.name!r} has no parameter {', '.join(sorted(unknown))}")
        condition = fga.RelationshipCondition(name=body.condition.name, context=dict(body.condition.context or {}))

    tup = fga.ClientTuple(user=_tuple_user(body.user, evaluated=evaluated), relation=body.relation, object=obj, condition=condition)
    if evaluated:
        fga.reject_stored_clock([tup])
    return tup


async def _mutate_tuple(client: OpenFgaClient, control: ControlEmitter, token: Caller | None, body: AccessTuple, *, write: bool) -> AccessTuple:
    tup = _validated_write_tuple(body, evaluated=write)
    actor = token.subject if token else "anonymous"
    event = AuditAction.ACCESS_TUPLE_WRITE if write else AuditAction.ACCESS_TUPLE_DELETE
    try:
        if write:
            await fga.write_tuples(client, [tup], actor=actor, origin="admin_api")  # noqa: TID251
        else:
            await fga.delete_tuples(client, [tup], actor=actor, origin="admin_api")  # noqa: TID251
    except ServiceUnavailableError:
        # Only the FAILURE row is emitted here. The SUCCESS row now comes from inside
        # `fga.write_tuples`/`delete_tuples`, so every write site gets one — and emitting it here too
        # would give this one surface two rows per write while the other six still had none.
        audit(event, AuditOutcome.FAILURE, subject=actor, resource=tup.object, grantee=tup.user, relation=tup.relation)
        raise
    # The control-plane feed documents that GRANTS tick the cursor — and the per-object grant surface
    # emits, so a raw-tuple write that stayed silent made this the one grant path live surfaces could
    # not see. Best-effort AFTER the store mutation (the emitter swallows bus failures), like access.py.
    condition = getattr(tup, "condition", None)
    await emit_control(
        control,
        action="grant_added" if write else "grant_revoked",
        object_type="grant",
        object_id=tup.object,
        actor=f"user:{token.subject}" if token else None,
        extra={"relation": tup.relation, "subject": tup.user} | ({"condition": condition.name} if condition is not None else {}),
    )
    return _as_access_tuple(tup)


@router.post("/tuples")
async def write_access_tuple(client: EstateFgaClient, control: ControlEmitterDep, token: CurrentToken, body: AccessTuple) -> AccessTuple:
    """Write ONE raw tuple (idempotent — a duplicate write is success), echoing the tuple as stored.

    Only a directly-assignable relation the compiled model defines on the object's type is accepted (a
    derived ``can_*`` action or an unknown type/relation is a 400) — the raw-tuple analog of the
    per-object grant surface's grantable-rung guard, without its table/namespace restriction.
    """
    return await _mutate_tuple(client, control, token, body, write=True)


@router.delete("/tuples")
async def delete_access_tuple(client: EstateFgaClient, control: ControlEmitterDep, token: CurrentToken, body: AccessTuple) -> AccessTuple:
    """Delete ONE raw tuple (idempotent — an already-absent tuple is success), echoing the tuple removed."""
    return await _mutate_tuple(client, control, token, body, write=False)


@router.get("/model")
async def get_access_model(client: EstateFgaClient, settings: SettingsDep) -> AccessModelResponse:
    """The authorization model — the checked-in ``model.fga`` DSL text plus the pinned model id.

    Read-only and estate-admin gated: the model reveals the whole privilege topology (which rungs derive
    which actions), the same platform-tier disclosure as the tuple listing.
    """
    model_id = client.get_authorization_model_id() or settings.fga_model_id or ""
    return AccessModelResponse(
        # OFF THE LOOP: the first call still touches the filesystem, and this route is `async def`.
        dsl=await run_in_threadpool(_model_dsl),
        authorization_model_id=model_id,
        conditions={name: dict(params) for name, params in _model_conditions().items()},
    )


@router.post("/check")
async def check_access(client: EstateFgaClient, token: CurrentToken, body: AccessCheckBody) -> AccessCheckResult:
    """One ``(user, relation, object)`` OpenFGA Check over ANY model type — the estate-wide extension of
    the per-object ``…/access/check`` playground (#68), which stays as-is for table/namespace owners.

    Any relation the compiled model defines on the type may be probed (base rungs included — an admin
    debugging a cascade asks about ``writer``, not just ``can_write_data``); an unknown one is a clean 400.
    ``checked`` echoes the resolved tuple so the verdict is unambiguous.

    ``context`` supplies the runtime values a CONDITION needs — ``current_time`` for the model's
    ``non_expired_grant``. An omitted ``current_time`` defaults to the server's now; an explicit one
    wins, which is how the explorer asks "was this true at 14:00?".
    """
    obj = _validated_object(body.object)
    fga_type = obj.partition(":")[0]
    if body.relation not in _defined_relations(fga_type):
        raise InvalidInputError(f"{body.relation!r} is not a relation the model defines on {fga_type}")
    user = _qualified_subject(body.user)
    subject = token.subject if token else "anonymous"
    try:
        allowed = await fga.check(client, user=user, relation=body.relation, obj=obj, qualify=False, context=body.context or None)
    except ServiceUnavailableError:
        audit(AuditAction.ACCESS_SIMULATE, AuditOutcome.FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    # Same disclosure event as the per-object simulator — one queryable stream for "who probed the graph".
    audit(AuditAction.ACCESS_SIMULATE, AuditOutcome.SUCCESS, subject=subject, resource=obj)
    checked = AccessTuple(user=user, relation=body.relation, object=obj)
    return AccessCheckResult(allowed=allowed, checked=checked)


def _validated_relation(obj_or_type: str, relation: str) -> str:
    """Require ``relation`` to be one the compiled model defines on the type (base rungs included)."""
    fga_type = obj_or_type.partition(":")[0]
    if relation not in _defined_relations(fga_type):
        raise InvalidInputError(f"{relation!r} is not a relation the model defines on {fga_type}")
    return relation


@router.post("/list-objects")
async def list_access_objects(client: EstateFgaClient, token: CurrentToken, body: AccessListObjectsRequest) -> AccessListObjectsResponse:
    """ "What can this subject reach?" — every ``type`` object on which ``user`` holds ``relation``.

    The forward half of the explorer, and the answer ``GET /tuples`` structurally cannot give: Read needs
    an object type per call and returns only STORED tuples, so enumerating a subject meant one audited
    read per model type and still missed everything derived. ListObjects expands the model, so a table
    reached through team → project → warehouse → namespace shows up here and nowhere else.

    The subject may be a userset (``team:eng#member``, ``role:validators#assignee``), which matters
    because this model deliberately refuses ``team#member`` on resource rungs — a team reaches data
    through a role, and only the userset question shows that.
    """
    if body.type not in _model_types():
        raise InvalidInputError(f"{body.type!r} is not a type the authorization model defines")
    relation = _validated_relation(body.type, body.relation)
    user = _qualified_subject(body.user)
    subject = token.subject if token else "anonymous"
    try:
        objects = (await fga.list_objects(client, user=user, relation=relation, object_type=body.type, qualify=False)).objects
    except ServiceUnavailableError:
        audit(AuditAction.ACCESS_LIST_OBJECTS, AuditOutcome.FAILURE, subject=subject, resource=f"{body.type}:", reason="authz_unavailable")
        raise
    # Enumerating one subject's whole reach is an estate-wide ACL disclosure, same tier as access_tuples_read.
    audit(
        AuditAction.ACCESS_LIST_OBJECTS,
        AuditOutcome.SUCCESS,
        subject=subject,
        resource=f"{body.type}:",
        grantee=user,
        relation=relation,
        delivered=len(objects),
    )
    return AccessListObjectsResponse(objects=objects, user=user, relation=relation, type=body.type)


@router.post("/list-users")
async def list_access_users(client: EstateFgaClient, token: CurrentToken, body: AccessListUsersRequest) -> AccessListUsersResponse:
    """ "Who can do this?" — every subject holding ``relation`` on ``object``, effective access included.

    The inverse of list-objects and the primitive a revoke needs BEFORE it writes: a tuple sitting high in
    the hierarchy can revoke access for many principals at once, and "are you sure?" is not an answer to
    "how many". Subjects come back fully qualified so a userset is never mistaken for a user.
    """
    obj = _validated_object(body.object)
    relation = _validated_relation(obj, body.relation)
    if body.user_type not in _model_types():
        raise InvalidInputError(f"{body.user_type!r} is not a type the authorization model defines")
    if body.user_relation is not None and body.user_relation not in _defined_relations(body.user_type):
        raise InvalidInputError(f"{body.user_relation!r} is not a relation the model defines on {body.user_type}")
    subject = token.subject if token else "anonymous"
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
        audit(AuditAction.ACCESS_LIST_USERS, AuditOutcome.FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    audit(AuditAction.ACCESS_LIST_USERS, AuditOutcome.SUCCESS, subject=subject, resource=obj, relation=relation, delivered=len(users))
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
async def expand_access(client: EstateFgaClient, token: CurrentToken, body: AccessExpandRequest) -> AccessExpandResponse:
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
    obj = _validated_object(body.object)
    relation = _validated_relation(obj, body.relation)
    depth = max(1, min(body.depth, fga.MAX_EXPAND_TREE_DEPTH))
    subject = token.subject if token else "anonymous"
    try:
        tree = await fga.expand_tree(client, relation=relation, obj=obj, max_depth=depth)
    except ServiceUnavailableError:
        audit(AuditAction.ACCESS_EXPAND, AuditOutcome.FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    audit(AuditAction.ACCESS_EXPAND, AuditOutcome.SUCCESS, subject=subject, resource=obj, relation=relation, depth=depth)
    # An empty tree ({}) means the relation resolves to nothing here — a real answer, distinct from the
    # 503 an outage raises above. Keep them distinguishable on the wire: null tree, never a silent {}.
    return AccessExpandResponse(
        tree=AccessExpandNode.model_validate(tree) if tree else None,
        object=obj,
        relation=relation,
        depth=depth,
    )


@router.post("/simulate")
async def simulate_access(client: EstateFgaClient, token: CurrentToken, body: AccessSimulateRequest) -> AccessSimulateResult:
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
    obj = _validated_object(body.object)
    relation = _validated_relation(obj, body.relation)
    user = _qualified_subject(body.user)
    # Validated, not merely parsed — same gate a real write clears.
    hypothetical = [_validated_write_tuple(t, evaluated=True) for t in body.hypothetical]
    subject = token.subject if token else "anonymous"
    try:
        # Two checks, one round trip each. The baseline cannot be skipped when `hypothetical` is empty
        # either: the caller still asked for a delta, and answering with allowed == baseline is the
        # honest shape rather than a special case that returns a different meaning.
        baseline = await fga.check(client, user=user, relation=relation, obj=obj, qualify=False)
        allowed = (
            baseline if not hypothetical else await fga.check(client, user=user, relation=relation, obj=obj, qualify=False, contextual_tuples=hypothetical)
        )
    except ServiceUnavailableError:
        audit(AuditAction.ACCESS_SIMULATE_HYPOTHETICAL, AuditOutcome.FAILURE, subject=subject, resource=obj, reason="authz_unavailable")
        raise
    # Probing the graph IS disclosing it, hypothetically or not — same tier as access_simulate, but a
    # distinct action so a compliance query can tell a what-if apart from a live check.
    audit(AuditAction.ACCESS_SIMULATE_HYPOTHETICAL, AuditOutcome.SUCCESS, subject=subject, resource=obj, relation=relation, hypotheticals=len(hypothetical))
    return AccessSimulateResult(
        allowed=allowed,
        baseline=baseline,
        checked=AccessTuple(user=user, relation=relation, object=obj),
        hypothetical=[_as_access_tuple(t) for t in hypothetical],
    )


def _principal_key(principal: str) -> str:
    """``principal`` when it names an identity provider and a subject (``<idp-id>~<subject>``), else a 400."""
    if not is_principal(principal):
        raise InvalidInputError(f"{principal[:128]!r} is not a principal: one is '<idp-id>~<subject>'")
    return principal


async def _tuples_naming(client: OpenFgaClient, user: str) -> list[fga.ClientTuple]:
    """Every direct tuple naming ``user``, read type by type and page by page, since Read takes a user only with a type."""
    found: list[fga.ClientTuple] = []
    for fga_type in _types_granting_principals():
        continuation: str | None = None
        while True:
            page, continuation = await fga.read_tuples(client, user=user, obj=f"{fga_type}:", page_size=_MAX_PAGE_SIZE, continuation_token=continuation)
            found.extend(page)
            if not continuation:
                break
    return found


#: A principal key in a path: the identity provider's id and the subject it vouched for.
PrincipalPath = Annotated[str, Path(description="the principal, '<idp-id>~<subject>' (e.g. 'dex~alice')")]


def _refuse_what_git_declares(key: str, naming: list[fga.ClientTuple], settings: Settings) -> None:
    """Refuse (400, code 13) a principal the next upgrade or boot would grant again, naming where it is declared.

    Only the chart's bootstrap hook writes a tuple on the estate root, and only it and the catalog's own seed write a
    warehouse ``maintenance`` tuple; the catalog grants the accounts its settings name at boot. Judging the tuples rather
    than a list the chart renders keeps the catalog naming no client the hook grants a decision role.
    """
    if key in {*settings.fga_maintainers, *settings.provenance_writers, *settings.provenance_maintainers}:
        raise InvalidInputError(f"{key} is an account the catalog grants from its settings at boot: remove it from the values that name it")
    if authorities := sorted(f"{t.relation} on {t.object}" for t in naming if t.object == settings.fga_root_object or t.relation == _MAINTENANCE):
        raise InvalidInputError(
            f"{key} holds an estate authority the chart's bootstrap hook grants ({', '.join(authorities)}): remove it from the "
            "values that declare it (auth.bootstrapAdmin, the services the hook grants), or revoke those tuples first"
        )


async def _refuse_stranding(client: OpenFgaClient, key: str, user: str, naming: list[fga.ClientTuple]) -> list[str]:
    """The projects ``user`` holds a direct ``admin`` on; refused (409, code 14) while it is any one's only direct admin."""
    administered = sorted({t.object for t in naming if t.relation == project_admins.ADMIN and t.object.startswith("project:")})
    if stranded := [obj for obj in administered if project_admins.sole_admin(await fga.read_object_tuples(client, obj), obj, user)]:
        raise ConcurrentModificationError(f"{key} holds the only direct admin of {', '.join(stranded)} — grant another admin first; nothing was revoked")
    return administered


async def _revoke_all(client: OpenFgaClient, user: str, naming: list[fga.ClientTuple], administered: list[str], *, actor: str) -> int:
    """Revoke every tuple in ``naming``, each project ``admin`` through the store-arbitrated revoke; how many this removed."""
    revoked = 0
    for obj in administered:
        removed_admin, _ = await project_admins.revoke_admin(client, obj, user, actor=actor, origin="principal_delete")
        revoked += int(removed_admin)
    rest = [
        fga.ClientTuple(user=t.user, relation=t.relation, object=t.object) for t in naming if t.object not in administered or t.relation != project_admins.ADMIN
    ]
    if rest:
        revoked += len(await fga.delete_tuples(client, rest, actor=actor, origin="principal_delete"))  # noqa: TID251
    return revoked


@router.delete("/principals/{principal:path}")
async def delete_principal(
    principal: PrincipalPath, client: EstateFgaClient, control: ControlEmitterDep, settings: SettingsDep, token: CurrentToken
) -> PrincipalDeleted:
    """Offboard one principal: revoke every tuple naming it, record it deleted, announce ``principal_deleted`` ([[LH-233]]).

    A grant is a tuple naming the principal on an object of any type, written by whichever door made it, so the door
    reads them type by type rather than trusting any one door's list. It never strands an administration: a principal
    holding the only direct ``admin`` of a project is refused before anything is revoked (:func:`_refuse_stranding`), and
    each project ``admin`` goes through the members door's store-arbitrated revoke, which refuses again should another
    revoke leave the principal a project's last admin meanwhile; what was revoked before that stays revoked, and calling
    the door again finishes. The estate admin cannot offboard themselves, since the rung this door asks would go with
    them, and a principal whose grants git declares is refused (:func:`_refuse_what_git_declares`). Re-running the door
    revokes whatever was granted since, keeps the first deletion's record and announces again, so a lost announcement is
    recovered by calling it again. ``revoked`` counts the tuples this call removed.
    """
    key = _principal_key(principal)
    actor = token.subject if token else "anonymous"
    if token is not None and token.subject == key:
        raise InvalidInputError("an estate admin cannot offboard themselves: the rung this door asks would go with them")
    user = fga.user_object(key)
    naming = await _tuples_naming(client, user)
    _refuse_what_git_declares(key, naming, settings)
    administered = await _refuse_stranding(client, key, user, naming)
    revoked = await _revoke_all(client, user, naming, administered, actor=actor)
    record = await run_in_threadpool(principals.record_deletion, settings.registry_root, settings.storage_options(), key, deleted_by=actor)
    audit(AuditAction.PRINCIPAL_DELETE, AuditOutcome.SUCCESS, subject=actor, resource=user, revoked=revoked)
    await emit_control(
        control,
        action="principal_deleted",
        object_type="principal",
        object_id=user,
        actor=f"user:{token.subject}" if token else None,
        extra={"subject": user, "revoked": revoked},
    )
    return PrincipalDeleted(record=record, revoked=revoked)


@router.get("/principals/{principal:path}")
async def read_principal(principal: PrincipalPath, settings: SettingsDep) -> PrincipalRecord:
    """The deletion record of an offboarded principal; 404 for one the door never removed."""
    record = await run_in_threadpool(principals.read_record, settings.registry_root, settings.storage_options(), _principal_key(principal))
    if record is None:
        raise TableNotFoundError(f"no principal {principal[:128]!r} was offboarded")
    return record
