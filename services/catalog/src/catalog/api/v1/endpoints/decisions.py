"""The decision door (``/management/v1/decisions``): the authorization questions a check-only service asks ([[LH-442]]).

OpenFGA authenticates its callers and authorizes none of them, so a service holding an OpenFGA token can write any
tuple. The services that only CHECK (lineage, notifications, the viewer and search, flows, compute) therefore hold no
OpenFGA credential and ask here, as Lakekeeper is OpenFGA's only client and its OPA bridge asks Lakekeeper's
``/management/v1/action/batch-check`` (authz/opa-bridge/policies/lakekeeper/check.rego). The wire contract is
:mod:`service_kit.governed.decider`, which the client and this door share.

Each route opens on its own estate rung, checked on the root object, so a service holds only the routes it calls:
``can_decide_access`` (check), ``can_expand_members`` (list-users), ``can_probe_governance`` (governed). A caller
without it is refused 403 for the whole call, as Lakekeeper refuses a check for another identity the actor may not
inspect (``CannotInspectPermissions``, crates/authz-openfga/src/authorizer.rs:1495-1504).

What may be asked is closed: a check names a ``can_*`` permission or the ``reader``/``member`` a gate checks, never a
role such as ``owner`` or ``admin`` and never a decision rung; list-users expands only a membership userset; governed
answers only for ``table:`` objects. Anything else is 400. A store fault is 503, and no route answers part of what it
was asked. Every call is audited once with what was asked and what was answered, also when it is refused or fails, as
Lakekeeper records ``IntrospectPermissions`` (crates/lakekeeper/src/api/management/v1/check.rs:1844-1891); a caller
without the route's role is audited by the gate itself (`fga_deps.require_relation`).
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends
from lance_namespace import InvalidInputError, ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken, Principal
from catalog.core.config import Settings
from service_kit.governed import fga
from service_kit.governed.audit import FAILURE, SUCCESS, audit
from service_kit.governed.decider import (
    DECISIONS_PREFIX,
    DecisionCheck,
    DecisionCheckResult,
    DecisionGoverned,
    DecisionGovernedResult,
    DecisionListUsers,
    DecisionListUsersResult,
)


#: The permissions that open the door's routes, which no route may be asked about: a check-only service has no
#: business learning which services may decide.
_DECISION_PERMISSIONS: Final = frozenset({"can_decide_access", "can_expand_members", "can_probe_governance"})

#: The relations a check may name besides the `can_*` permissions: the estate tiers the fleet gates on (`reader`, which
#: compute and flows check to look; `writer`, which flows checks to run, stop and read a run), and the `project#member`
#: notifications re-checks at delivery. Every other role (`owner`, `admin`,
#: `operator`, the decision roles) is refused: Lakekeeper's door takes typed actions only, never a raw relation
#: (crates/lakekeeper/src/api/management/v1/check.rs:244-280).
_DECIDABLE_ROLES: Final = frozenset({"reader", "writer", "member"})

#: The usersets list-users may expand: the groups a task or a grant names, resolved to the people in them.
_EXPANDABLE_USERSETS: Final = frozenset({("role", "assignee"), ("team", "member"), ("project", "member")})

#: The object types `governed` answers for: the datasets lineage records.
_PROBEABLE_TYPES: Final = frozenset({"table"})


def _is_decidable(relation: str) -> bool:
    """Whether a check may ask about ``relation``."""
    return (relation.startswith("can_") and relation not in _DECISION_PERMISSIONS) or relation in _DECIDABLE_ROLES


#: The audit action every decision call records under.
_ACTION: Final = "authz_decision"

management_router = APIRouter(prefix=DECISIONS_PREFIX, tags=["decisions"])


async def _require(client: OpenFgaClient | None, settings: Settings, token: Principal | None, *, permission: str) -> None:
    """Refuse a caller that does not hold ``permission`` on the estate root: 401, 403, 406 or 503."""
    wired = fga_deps.require_fga(settings, client, feature="authorization decisions")
    if token is None:
        raise UnauthenticatedError("authentication required")
    await fga_deps.require_relation(wired, settings, token, relation=permission, obj=settings.fga_root_object)


async def require_can_decide_access(client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> None:
    """The check route's gate: the caller is a `decider`."""
    await _require(client, settings, token, permission="can_decide_access")


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


def _refused(caller: str, route: str, reason: str, **asked: str) -> InvalidInputError:
    """A question the door does not answer, audited with what was asked before it is refused 400."""
    audit(_ACTION, FAILURE, subject=caller, route=route, reason="question_refused", **asked)
    return InvalidInputError(reason)


@management_router.post("/check", dependencies=[Depends(require_can_decide_access)])
async def decide_check(client: WiredFgaClient, token: CurrentToken, body: DecisionCheck) -> DecisionCheckResult:
    """Whether ``user`` holds ``relation`` on each object, in one round trip.

    An object that is no object id is answered False unasked, as `fga.batch_check` answers it: no tuple can name it.
    """
    caller = token.sub if token else "anonymous"
    asked = {"grantee": body.user, "relation": body.relation, "objects": ",".join(body.objects)}
    if not _is_decidable(body.relation):
        raise _refused(caller, "check", f"{body.relation!r} is not a relation a decision may ask about", **asked)
    if undefined := sorted(o for o in body.objects if fga.is_object_id(o) and body.relation not in _relations_of(o)):
        raise _refused(caller, "check", f"the model defines no {body.relation!r} on {', '.join(undefined[:5])}", **asked)
    try:
        allowed = await fga.batch_check(client, user=body.user, relation=body.relation, objects=body.objects)
    except ServiceUnavailableError:
        audit(_ACTION, FAILURE, subject=caller, route="check", reason="authz_unavailable", **asked)
        raise
    audit(_ACTION, SUCCESS, subject=caller, route="check", allowed=",".join(o for o in body.objects if allowed.get(o)), **asked)
    return DecisionCheckResult(allowed={o: allowed.get(o, False) for o in body.objects})


@management_router.post("/list-users", dependencies=[Depends(require_can_expand_members)])
async def decide_list_users(client: WiredFgaClient, token: CurrentToken, body: DecisionListUsers) -> DecisionListUsersResult:
    """The people in a membership userset (``role:<id>#assignee``, ``team:<id>#member``, ``project:<id>#member``)."""
    caller = token.sub if token else "anonymous"
    asked = {"resource": body.object, "relation": body.relation}
    obj_type, sep, obj_id = body.object.partition(":")
    if not (sep and obj_id and fga.is_object_id(body.object)) or (obj_type, body.relation) not in _EXPANDABLE_USERSETS:
        raise _refused(caller, "list-users", f"{body.object}#{body.relation} is not a membership userset a decision may expand", **asked)
    try:
        users = await fga.list_users(client, relation=body.relation, obj=body.object)
    except ServiceUnavailableError:
        audit(_ACTION, FAILURE, subject=caller, route="list-users", reason="authz_unavailable", **asked)
        raise
    audit(_ACTION, SUCCESS, subject=caller, route="list-users", users=",".join(users), delivered=len(users), **asked)
    return DecisionListUsersResult(users=users)


@management_router.post("/governed", dependencies=[Depends(require_can_probe_governance)])
async def decide_governed(client: WiredFgaClient, token: CurrentToken, body: DecisionGoverned) -> DecisionGovernedResult:
    """Whether any tuple names each ``table:`` object: a refusal on an object nothing governs can never be granted away."""
    caller = token.sub if token else "anonymous"
    asked = {"objects": ",".join(body.objects)}
    if foreign := sorted(o for o in body.objects if o.partition(":")[0] not in _PROBEABLE_TYPES):
        raise _refused(caller, "governed", f"governed answers for {', '.join(sorted(_PROBEABLE_TYPES))} objects only, not {', '.join(foreign[:5])}", **asked)
    try:
        # A name that is no object id holds no tuple, and reading it would fail the read.
        governed = {o: fga.is_object_id(o) and bool(await fga.read_object_tuples(client, o)) for o in body.objects}
    except ServiceUnavailableError:
        audit(_ACTION, FAILURE, subject=caller, route="governed", reason="authz_unavailable", **asked)
        raise
    audit(_ACTION, SUCCESS, subject=caller, route="governed", governed=",".join(o for o, g in governed.items() if g), **asked)
    return DecisionGovernedResult(governed=governed)


#: Every route here is a management route; the spec prefix carries none.
router = APIRouter()
