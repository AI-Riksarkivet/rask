"""The decision door (``/management/v1/decisions``): the authorization questions a check-only service asks ([[LH-442]]).

OpenFGA authenticates its callers and authorizes none of them, so a service holding an OpenFGA token can write any
tuple. The services that only CHECK therefore hold no OpenFGA credential and ask here, so the catalog is the store's
one reader for every service that writes no grant. The wire contract is :mod:`lakehouse.governed.decider`, which the
client and this door share.

A check is answered inside the caller's scope ([[LH-563]]): every object, for a caller holding ``can_decide_access`` on
the estate root (lineage, which serves every tenant); otherwise each object on which it holds that relation, which a
``decider`` grant on a project or a warehouse confers on everything beneath. An object outside the scope is answered
refused without being asked, exactly as an object that does not exist is, so a service scoped to one tenant learns
nothing about another and a table dropped since an event named it never fails the service's whole question. The other
routes open on their own estate rung, checked on the root object: ``can_expand_members`` (list-users),
``can_probe_governance`` (governed, locate), and a caller without it is refused 403 for the whole call.

What may be asked is closed: a check names a ``can_*`` permission or the ``member`` a gate checks, never a role such
as ``owner`` or ``writer`` and never a decision rung; list-users expands only a membership userset; governed
answers only for ``table:`` objects. Anything else is 400.

A check may name tables by name as well as objects by id ([[LH-531]]): every grant is held by a table's uuid (ADR 0173
§8), and a producer's event names its tables only. The catalog is the one resolver of a name, so the door resolves each
through the request's `Resolver`, as every door resolves the name it is addressed by, decides on the uuid, and answers
keyed by the name, with the table's uuid and its namespace's when the subject holds the relation, since the event a
check-only service records keys a table by its uuid (ADR 0173 §10). A name that names no table, one no grant can be held
on and one in a quarantined warehouse are answered refused. A store fault is 503, and no route answers part of what it
was asked. Every call is audited once with what was asked and what was answered, also when it is refused or fails:
asking what a subject may do reads the authorization graph, so the question is itself an access. A caller without the
route's role is audited by the gate itself (`fga_deps.require_relation`).
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidInputError, PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, Resolver, ResolverDep, SettingsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier, require_names
from catalog.services import table_claims
from catalog.services.object_uuids import UnidentifiedObjectError
from lakehouse import location_claims
from lakehouse.base_refs import names_a_location
from lakehouse.governed import fga
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.decider import (
    DECISIONS_PREFIX,
    DecisionCheck,
    DecisionCheckResult,
    DecisionGoverned,
    DecisionGovernedResult,
    DecisionListUsers,
    DecisionListUsersResult,
    DecisionLocate,
    DecisionLocateResult,
    TableDecision,
)
from lakehouse.location_claims import ClaimStore


log = logging.getLogger(__name__)


#: The relation a check's scope is: the caller holds it on the estate root, or on each object it asks about ([[LH-563]]).
_SCOPE: Final = "can_decide_access"

#: The permissions that open the door's routes and grant its scope, which no route may be asked about: a check-only
#: service has no business learning which services may decide, or who may make one.
_DECISION_PERMISSIONS: Final = frozenset({_SCOPE, "can_grant_decider", "can_expand_members", "can_probe_governance"})

#: The relation a check may name besides the `can_*` permissions: the `project#member` a notifier re-checks at
#: delivery. Every other role (`owner`, `writer`, `reader`, `admin`, `operator`, the decision roles) is refused, so a
#: client gates on a lakehouse verb, never a raw relation: a raw relation would let a caller read the grant structure
#: itself.
_DECIDABLE_ROLES: Final = frozenset({"member"})

#: The usersets list-users may expand: the groups a task or a grant names, resolved to the people in them.
_EXPANDABLE_USERSETS: Final = frozenset({("role", "assignee"), ("project", "member")})

#: The object types `governed` answers for: the datasets lineage records.
_PROBEABLE_TYPES: Final = frozenset({"table"})


def _is_decidable(relation: str) -> bool:
    """Whether a check may ask about ``relation``."""
    return (relation.startswith("can_") and relation not in _DECISION_PERMISSIONS) or relation in _DECIDABLE_ROLES


management_router = APIRouter(prefix=DECISIONS_PREFIX, tags=["decisions"])


async def require_a_caller(client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> Caller:
    """The signed-in caller, with authorization wired: 406 with it off, 503 unwired, 401 with no caller.

    The check route's whole gate: what the caller is answered is then its scope's ([[LH-563]]).
    """
    fga_deps.require_fga(settings, client, feature="authorization decisions")
    if token is None:
        raise UnauthenticatedError("authentication required")
    return token


SignedInCaller = Annotated[Caller, Depends(require_a_caller)]


async def _require(client: OpenFgaClient | None, settings: Settings, token: Caller | None, *, permission: str) -> None:
    """Refuse a caller that does not hold ``permission`` on the estate root: 401, 403, 406 or 503."""
    caller = await require_a_caller(client, settings, token)
    await fga_deps.require_relation(client, settings, caller, relation=permission, obj=settings.fga_root_object)


async def require_can_expand_members(client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> None:
    """The list-users route's gate: the caller is a `member_expander`."""
    await _require(client, settings, token, permission="can_expand_members")


async def require_can_probe_governance(client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> None:
    """The governed route's gate: the caller is a `governance_prober`."""
    await _require(client, settings, token, permission="can_probe_governance")


def _wired_client(client: FgaClientDep) -> OpenFgaClient:
    """The client the gate has already proved wired; typed non-optional for the handlers."""
    return fga_deps.require_wired(client)


WiredFgaClient = Annotated[OpenFgaClient, Depends(_wired_client)]


def _relations_of(obj: str) -> frozenset[str]:
    """Every relation the compiled model defines on ``obj``'s type (empty for a type it does not define)."""
    obj_type = obj.partition(":")[0]
    for td in fga.load_model()["type_definitions"]:
        if td["type"] == obj_type:
            return frozenset(td.get("relations") or {})
    return frozenset()


async def _named_table(resolver: Resolver, name: str) -> tuple[str, str | None] | None:
    """The uuid of the table ``name`` names now and its namespace's, or ``None`` where no grant on it can be decided.

    ``None`` for a name that is no table identifier (fewer than two segments, or a segment that is no name), one naming
    no table, one in a quarantined warehouse (which suspends every door on it), and one whose table or namespace records
    no uuid ([[LH-534]]): each names no table a grant is held on.
    """
    segments = parse_identifier(name)
    try:
        require_names(segments)
        if len(segments) < 2:
            return None
        resolved = await resolver.table(segments)
        if resolved.location is None:
            return None
        return resolved.require_uuid(), await resolver.namespace_uuid(segments[:-1])
    except (InvalidInputError, PermissionDeniedError):
        return None
    except UnidentifiedObjectError:
        log.error("decision_left_out_unidentified_object", extra={"table": name})
        return None


def _refused(caller: str, route: str, reason: str, **asked: str) -> InvalidInputError:
    """A question the door does not answer, audited with what was asked before it is refused 400."""
    audit(AuditAction.AUTHZ_DECISION, AuditOutcome.FAILURE, subject=caller, route=route, reason="question_refused", **asked)
    return InvalidInputError(reason)


async def _in_scope(client: OpenFgaClient, settings: Settings, *, caller: Caller, objects: list[str]) -> frozenset[str]:
    """The objects of ``objects`` the caller may be answered about ([[LH-563]]).

    Every one, for a caller holding the scope on the estate root, asked first as one check on the estate root. Otherwise
    each object it holds the scope on, of a type the model gives the scope: a project's or warehouse's ``decider``
    reaches what they hold, and no object of another type is in any but the estate's scope.
    """
    if not objects:
        return frozenset()
    if await fga.check(client, user=caller.subject, relation=_SCOPE, obj=settings.fga_root_object):
        return frozenset(objects)
    scoped = [o for o in objects if fga.is_object_id(o) and _SCOPE in _relations_of(o)]
    held = await fga.batch_check(client, user=caller.subject, relation=_SCOPE, objects=scoped) if scoped else {}
    return frozenset(o for o in scoped if held.get(o))


@management_router.post("/check")
async def decide_check(
    caller: SignedInCaller, client: WiredFgaClient, settings: SettingsDep, resolver: ResolverDep, body: DecisionCheck
) -> DecisionCheckResult:
    """Whether ``user`` holds ``relation`` on each object, and on the table each name names now, inside the caller's scope.

    An object outside the caller's scope is answered False unasked, as is one that is no object id, which no tuple can
    name. A table asked about by name is decided on the uuid the name resolves to now, and answered with that uuid and
    its namespace's when allowed; a name naming no table a grant is held on, or one outside the scope, is answered
    refused. The scope is asked, then the question, each as one round trip.
    \f

    Args:
        caller: The signed-in service asking, resolved first: the door's whole gate.
        client: The OpenFGA client.
        settings: The catalog's settings.
        resolver: The request's name resolver.
        body: The question.

    Returns:
        One verdict per object and one decision per table, keyed as asked.

    Raises:
        InvalidInputError: The question names a relation the door does not answer, or one the model does not define.
        ServiceUnavailableError: OpenFGA did not answer.
    """
    asked = {"grantee": body.user, "relation": body.relation, "objects": ",".join(body.objects), "tables": ",".join(body.tables)}
    subject = caller.subject
    if not _is_decidable(body.relation):
        raise _refused(subject, "check", f"{body.relation!r} is not a relation a decision may ask about", **asked)
    if undefined := sorted(o for o in body.objects if fga.is_object_id(o) and body.relation not in _relations_of(o)):
        raise _refused(subject, "check", f"the model defines no {body.relation!r} on {', '.join(undefined[:5])}", **asked)
    if body.tables and body.relation not in _relations_of("table:"):
        raise _refused(subject, "check", f"the model defines no {body.relation!r} on a table", **asked)
    try:
        resolved = {name: uuids for name in body.tables if (uuids := await _named_table(resolver, name)) is not None}
    except Exception:
        audit(AuditAction.AUTHZ_DECISION, AuditOutcome.FAILURE, subject=subject, route="check", reason="resolution_failed", **asked)
        raise
    objects = list(dict.fromkeys([*body.objects, *(fga.table_object(table) for table, _ in resolved.values())]))
    try:
        in_scope = await _in_scope(client, settings, caller=caller, objects=objects)
        decided_on = [o for o in objects if o in in_scope]
        allowed = await fga.batch_check(client, user=body.user, relation=body.relation, objects=decided_on) if decided_on else {}
    except ServiceUnavailableError:
        audit(AuditAction.AUTHZ_DECISION, AuditOutcome.FAILURE, subject=subject, route="check", reason="authz_unavailable", **asked)
        raise
    decided = {name: TableDecision(allowed=False) for name in body.tables}
    decided |= {
        name: TableDecision(allowed=True, table_uuid=table, namespace_uuid=namespace)
        for name, (table, namespace) in resolved.items()
        if allowed.get(fga.table_object(table))
    }
    granted = [*(o for o in body.objects if allowed.get(o)), *(name for name, decision in decided.items() if decision.allowed)]
    outside = ",".join(o for o in objects if o not in in_scope)
    audit(AuditAction.AUTHZ_DECISION, AuditOutcome.SUCCESS, subject=subject, route="check", allowed=",".join(granted), outside_scope=outside, **asked)
    return DecisionCheckResult(allowed={o: allowed.get(o, False) for o in body.objects}, tables=decided)


@management_router.post("/list-users", dependencies=[Depends(require_can_expand_members)])
async def decide_list_users(client: WiredFgaClient, token: CurrentToken, body: DecisionListUsers) -> DecisionListUsersResult:
    """The people in a membership userset (``role:<id>#assignee``, ``project:<id>#member``)."""
    caller = token.subject if token else "anonymous"
    asked = {"resource": body.object, "relation": body.relation}
    obj_type, sep, obj_id = body.object.partition(":")
    if not (sep and obj_id and fga.is_object_id(body.object)) or (obj_type, body.relation) not in _EXPANDABLE_USERSETS:
        raise _refused(caller, "list-users", f"{body.object}#{body.relation} is not a membership userset a decision may expand", **asked)
    try:
        users = await fga.list_users(client, relation=body.relation, obj=body.object)
    except ServiceUnavailableError:
        audit(AuditAction.AUTHZ_DECISION, AuditOutcome.FAILURE, subject=caller, route="list-users", reason="authz_unavailable", **asked)
        raise
    audit(AuditAction.AUTHZ_DECISION, AuditOutcome.SUCCESS, subject=caller, route="list-users", users=",".join(users), delivered=len(users), **asked)
    return DecisionListUsersResult(users=users)


@management_router.post("/governed", dependencies=[Depends(require_can_probe_governance)])
async def decide_governed(client: WiredFgaClient, token: CurrentToken, body: DecisionGoverned) -> DecisionGovernedResult:
    """Whether any tuple names each ``table:`` object: a refusal on an object nothing governs can never be granted away."""
    caller = token.subject if token else "anonymous"
    asked = {"objects": ",".join(body.objects)}
    if foreign := sorted(o for o in body.objects if o.partition(":")[0] not in _PROBEABLE_TYPES):
        raise _refused(caller, "governed", f"governed answers for {', '.join(sorted(_PROBEABLE_TYPES))} objects only, not {', '.join(foreign[:5])}", **asked)
    try:
        # A name that is no object id holds no tuple, and reading it would fail the read.
        governed = {o: fga.is_object_id(o) and bool(await fga.read_object_tuples(client, o)) for o in body.objects}
    except ServiceUnavailableError:
        audit(AuditAction.AUTHZ_DECISION, AuditOutcome.FAILURE, subject=caller, route="governed", reason="authz_unavailable", **asked)
        raise
    audit(AuditAction.AUTHZ_DECISION, AuditOutcome.SUCCESS, subject=caller, route="governed", governed=",".join(o for o, g in governed.items() if g), **asked)
    return DecisionGovernedResult(governed=governed)


def _table_at(store: ClaimStore, location: str) -> str | None:
    """The uuid of the table holding ``location``, or ``None`` where none does: a spelling that names no location, one no
    claim holds, a released claim, and a data directory's claim, which records no uuid.

    A recoverable drop keeps its table's claim (`lakehouse.location_claims`), so a trashed table's location still
    answers the table: its grants decide what may be recorded about it, as they do by its name.
    """
    if not names_a_location(location):
        return None
    claim = location_claims.holder(store, location)
    return claim.uuid if claim is not None else None


@management_router.post("/locate", dependencies=[Depends(require_can_probe_governance)])
async def decide_locate(token: CurrentToken, settings: SettingsDep, body: DecisionLocate) -> DecisionLocateResult:
    """Which governed table each storage location holds, by the location claims every door that makes a table takes.

    For lineage, which records an outside producer's event naming a dataset by its location (``s3://<bucket>`` and the
    key) as well as by its name ([[LH-553]]): a location a table holds is that table, and its grants decide whether the
    producer may record it, while one no table holds is the outside world. The fact is structural, like ``governed``'s,
    so it answers to the same role. A claim that cannot be read (the store's error, a record that is no claim, a root the
    conditional read does not serve: `lakehouse.records`) is 503, never "holds none", which would exempt it. The
    claims are the answer because the ``dir`` backend keeps no location exclusive (``lance_docs/ns_catalog/catalog/dir``
    § Manifest Table Commits) and the claim store picks one holder (`lakehouse.location_claims`).
    """
    caller = token.subject if token else "anonymous"
    asked = {"locations": ",".join(body.locations)}
    store = table_claims.claim_store(settings)
    try:
        located = {location: await run_in_threadpool(_table_at, store, location) for location in body.locations}
    except (ClientError, OSError, ValueError, NotImplementedError) as exc:
        audit(AuditAction.AUTHZ_DECISION, AuditOutcome.FAILURE, subject=caller, route="locate", reason="claims_unreadable", **asked)
        log.error("decision_locate_unreadable", exc_info=True)
        raise ServiceUnavailableError("the location claims cannot be read") from exc
    audit(
        AuditAction.AUTHZ_DECISION,
        AuditOutcome.SUCCESS,
        subject=caller,
        route="locate",
        located=",".join(loc for loc, uuid in located.items() if uuid),
        **asked,
    )
    return DecisionLocateResult(located=located)


#: Every route here is a management route; the spec prefix carries none.
router = APIRouter()
