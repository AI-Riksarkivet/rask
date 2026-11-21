"""Who may grant or revoke what: the rung a grant's body names, a grant that would elevate its caller, and a role's grant
across tenants."""

from __future__ import annotations

import logging
from functools import lru_cache

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidInputError, PermissionDeniedError, ServiceUnavailableError
from openfga_sdk import OpenFgaClient

from catalog.api.authz.decisions import authorization, require_rung
from catalog.api.authz.objects import rung_on
from catalog.api.authz.request_reads import json_body_or_400
from catalog.api.authz.rungs import TENANT_HELD
from catalog.api.dependencies import Resolver
from catalog.api.security import Caller
from catalog.core.config import CatalogStore
from catalog.services import warehouses
from lakehouse.governed import fga_ids, fga_model, fga_reads
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.settings import FgaSettings


__all__ = [
    "authorize_grant",
    "grant_actions",
    "refuse_a_cross_tenant_role_grant",
    "refuse_self_elevation",
    "require_grant_authority",
]


log = logging.getLogger(__name__)


@lru_cache
def grant_actions(fga_type: str) -> frozenset[str]:
    """Every ``can_grant_*`` the compiled model defines on ``fga_type``.

    Read from ``model.json`` (what the app loads), never a hand-kept list: a relation named here but
    absent there is an OpenFGA 400 that fails CLOSED to a 503 for every caller, which is an outage
    wearing a permission error's clothes."""
    for td in fga_model.load_model()["type_definitions"]:
        if td["type"] == fga_type:
            return frozenset(r for r in (td.get("relations") or {}) if r.startswith("can_grant_"))
    return frozenset()


@lru_cache
def _holding_relation(fga_type: str, rung: str) -> str:
    """The relation that answers "does a subject hold ``rung`` on an object of ``fga_type``".

    A data rung is stored under its bare name, which holds only what was granted on the object, and held through its
    ``_effective`` twin, which adds what implies it and what is inherited ([[LH-222]]). A rung with no twin (``apply``,
    ``manage_grants``) is held as it is stored.
    """
    twin = f"{rung}_effective"
    for td in fga_model.load_model()["type_definitions"]:
        if td["type"] == fga_type and twin in (td.get("relations") or {}):
            return twin
    return rung


async def authorize_grant(
    request: Request,
    client: OpenFgaClient,
    resolver: Resolver,
    *,
    user: str,
    fga_type: str,
    segments: list[str],
    revoking: bool,
) -> None:
    """Authorize grant/revoke on the RUNG in the body — ``can_grant_<relation>``, not a blanket bar.

    This route used to gate on the owner tier (``can_drop`` / ``can_delete``), which welded handing
    out access to owning the data: the only way to let someone administer access was to give them
    everything. The model now separates the two (``manage_grants`` / ``pass_grants``), and the gate
    has to read the body to use it — the path says *that* a grant is happening, only the body says
    *which rung*. Same shape as ``authorize_batch`` above, and ``request.json()`` is cached by
    Starlette so the endpoint re-parses the same bytes.

    Fails CLOSED on anything unexpected. The body is client-controlled and this runs BEFORE Pydantic,
    so a non-dict, a non-string relation, or a rung the model has no ``can_grant_*`` for is a 403 —
    never a 500, and never a check on a phantom relation.

    **REVOKE IS NOT GATED IDENTICALLY TO GRANT, and used to be.** The old rule said "taking a rung
    away is the same authority as handing it out, and a weaker revoke bar would let a delegate strip
    the owner who delegated to them" — true of ``owner``, and false of everything at or below the
    delegate's own rung. ``can_grant_reader`` is ``manage_grants or (reader and pass_grants)``, so
    kevin (``writer`` + ``pass_grants``, the fixture's "delegate and no more") could DELETE carol's
    reader tuple, and every other reader/writer tuple on the object including the owner's.
    Revoke goes to ``can_revoke_grant``, which every type the door serves defines stricter than its grants:
    ``manage_grants`` alone on a table or a namespace, ``curator`` alone on a classification label.

    SQL's WITH GRANT OPTION — which this axis is modelled on — lets a delegate revoke the grants THEY
    made. That needs the granting actor recorded ON the tuple, which this store does not carry, so
    the conservative bar is the honest one until it does.

    **AND A GRANT MAY NOT BE SELF-DIRECTED UNLESS IT IS A NO-OP.** ``can_grant_owner`` is
    ``manage_grants``, so the C2 headline persona — "administers access, cannot read the data" — was
    a state its holder could leave in one authorized call: judy POSTs herself ``owner`` and now holds
    ``can_read_data`` on data she was deliberately never given, leaving one audit row identical to a
    legitimate delegation. OpenFGA cannot express "grantee is not the caller" — there is no such
    construct — so this separation of duties belongs HERE, at the only layer that knows both. A
    caller who ALREADY holds the rung is unaffected (re-granting yourself what you have changes
    nothing), which is what keeps an owner's ordinary self-referential grant working.

    The userset form is covered too, and has to be: refusing only a literal ``user:<principal>`` would leave
    ``role:x#assignee`` as the same escalation one indirection away, for any userset the caller is
    already inside.

    Raises:
        InvalidInputError: The body is not JSON.
        PermissionDeniedError: The body is not an access request, or the caller may not grant or revoke its rung.
    """
    body = await json_body_or_400(request)
    if not isinstance(body, dict):
        raise PermissionDeniedError("malformed access request body")
    relation = body.get("relation")
    if not isinstance(relation, str) or not relation:
        raise PermissionDeniedError("malformed access request body: 'relation' must be a string")
    action = "can_revoke_grant" if revoking else f"can_grant_{relation}"
    # Shape-checked against the GRANT actions for a revoke too: the set of rungs that may be taken away is the set
    # that may be handed out. Only the AUTHORITY differs.
    if f"can_grant_{relation}" not in grant_actions(fga_type):
        raise PermissionDeniedError(f"{relation!r} is not a grantable rung on {fga_type}")
    obj = await require_rung(client, resolver, user=user, rung=rung_on(action, fga_type, segments))
    if not revoking:
        await refuse_self_elevation(client, user=user, grantee=body.get("user"), relation=relation, obj=obj)


async def require_grant_authority(
    request: Request,
    client: OpenFgaClient | None,
    settings: FgaSettings,
    *,
    token: Caller | None,
    resolver: Resolver,
    fga_type: str,
    segments: list[str],
    revoking: bool,
) -> None:
    """Gate a grant or revoke door the router leaves to its handler exactly as :func:`authorize` gates the table and
    namespace ones, through :func:`authorize_grant`: ``can_grant_<rung>`` from the body, or ``can_revoke_grant``, and no
    grant that raises the caller's own access ([[LH-555]]). A no-op with FGA off; with it on, no client is 503 and no
    caller 401 (:func:`authorization`), since nothing before the handler decided."""
    if (authz := authorization(settings, client, token)) is None:
        return
    await authorize_grant(request, authz.client, resolver, user=authz.subject, fga_type=fga_type, segments=segments, revoking=revoking)


async def refuse_self_elevation(client: OpenFgaClient, *, user: str, grantee: object, relation: str, obj: str) -> None:
    """Refuse a grant that would hand the CALLER a rung they do not already hold.

    Separation of duties, enforced at the only layer that can see both sides. See
    :func:`authorize_grant` for why it cannot live in the model.

    Reads the grantee the same way :func:`~catalog.api.v1.endpoints.access._access_mutate` does
    (:func:`fga_ids.grantee_object`) — because a guard that resolved it
    DIFFERENTLY from the code that writes the tuple would be guarding a grant nobody makes.

    Order matters: this runs AFTER the ``can_grant_*`` check, so a caller who may not grant the rung
    at all is refused for that reason and never learns whether the self-grant rule would also have
    caught them.
    """
    if not isinstance(grantee, str) or not grantee:
        return  # not a grant this guard can reason about; the endpoint's own validation owns the shape
    subject = fga_ids.user_object(user)
    resolved = fga_ids.grantee_object(grantee)
    if resolved != subject:
        if "#" not in resolved:
            return  # a plain grant to somebody else
        # A USERSET. `role:stewards#assignee` names everyone holding `assignee` on that role, so a
        # caller already inside it is granting themselves by indirection. One check, and only on the
        # userset path — the common case costs nothing.
        userset_object, _, userset_relation = resolved.partition("#")
        try:
            inside = await fga_reads.check(client, user=subject, relation=userset_relation, obj=userset_object, qualify=False)
        except ServiceUnavailableError:  # authz layer down mid-decision — fail closed, like every other door
            audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, relation="can_grant_self", subject=user, resource=obj, reason="authz_unavailable")
            raise
        if not inside:
            return
    # The caller is the grantee. Allowed only when it changes nothing they do not already have.
    try:
        already = await fga_reads.check(client, user=subject, relation=_holding_relation(obj.partition(":")[0], relation), obj=obj, qualify=False)
    except ServiceUnavailableError:
        audit(AuditAction.AUTHZ, AuditOutcome.FAILURE, relation="can_grant_self", subject=user, resource=obj, reason="authz_unavailable")
        raise
    if already:
        return
    audit(AuditAction.AUTHZ, AuditOutcome.DENY, relation="can_grant_self", subject=user, resource=obj, grantee=resolved, granted_relation=relation)
    log.info("self_elevation_refused", extra={"relation": relation, "object": obj})
    raise PermissionDeniedError(f"a grant may not raise your own access: you do not hold {relation} on {obj}")


async def refuse_a_cross_tenant_role_grant(client: OpenFgaClient, grantee: str, *, fga_type: str, segments: list[str], settings: CatalogStore) -> None:
    """Refuse granting a role from one tenant a rung on another tenant's object: a namespace's, a table's or a view's
    tenant is the project of the warehouse its name begins with ([[LH-593]]), a warehouse's is named by its own record
    ([[LH-555]]), a project's is its own id, and a role's the project its own edge names ([[LH-557]]).

    A role name is estate-global unless the model says otherwise, so `role:analyst#assignee` minted in
    one project could be granted on any other project's namespace and every layer below would honour
    it: the tuple is well-formed, the rung is real, and the grantor holds the owner rung on the object
    they are granting. Nothing downstream can tell the role came from somewhere else.

    FAIL-OPEN FOR AN UNSCOPED ROLE, DELIBERATELY. A role carrying no `project` edge is estate-wide and
    keeps exactly the reach it has today, so this refuses something that was previously possible only
    where the model now carries the fact to refuse it on. The alternative — refusing every role whose
    tenant is unknown — would break every existing grant on the day the edge landed, which is how a
    security control gets reverted rather than adopted.

    A SCOPED ROLE IS REFUSED ON AN OBJECT NO PROJECT OWNS ([[LH-572]]): the estate's own warehouse, a warehouse with no
    record and every name in either belong to no tenant, so a grant there would reach the object, and everything beneath
    it, for one project's assignees. A registry read that fails raises as what it is, never as an
    object with no tenant.

    InvalidInput (13 -> 400), matching the bad-rung refusal above and lance-ns's own vocabulary: the
    grantor may well hold every rung this needs, so it is a malformed request rather than a denial.
    """
    if not grantee.startswith("role:") or fga_type not in TENANT_HELD or not segments:
        return
    role_object = grantee.split("#", 1)[0]
    owned_by = {
        tup.user.removeprefix("project:")
        for tup in await fga_reads.read_object_tuples(client, role_object)
        if tup.relation == "project" and tup.user.startswith("project:")
    }
    if not owned_by:
        return
    target = await run_in_threadpool(_tenant_of, settings, fga_type, segments[0])
    if target in owned_by:
        return
    held_by = f"project {target!r}" if target is not None else "no project"
    raise InvalidInputError(f"{grantee!r} belongs to project {min(owned_by)!r} and cannot be granted on {segments[0]!r}, which belongs to {held_by}")


def _tenant_of(settings: CatalogStore, fga_type: str, first_segment: str) -> str | None:
    """The project an object of ``fga_type`` belongs to, from its id's ``first_segment``: a project's own id, else the
    project of the warehouse it names, which is a warehouse's own id and the first segment of every name in one
    ([[LH-593]]). ``None`` when the registry names none."""
    if fga_type == "project":
        return first_segment
    return warehouses.project_of(settings.registry_root, settings.storage_options(), first_segment)
