"""The router's guard, :func:`authorize`: the caller's permission for every guarded route, decided before its handler
runs. A no-op when FGA is disabled.

It maps the request (resource + operation) to the ``can_*`` ACTION relation its rung declares (``rungs``) and checks
the authenticated caller against OpenFGA. Batch routes read the (Starlette-cached) body and check each named table, and
a transaction door is authorized on the table its id names.
"""

import logging

from fastapi import Request
from lance_namespace import InternalError, InvalidInputError, PermissionDeniedError, UnauthenticatedError
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from catalog.api.authz.context import AuthzContext, AuthzContextDep
from catalog.api.authz.decisions import record_undeclared, refused_read_as_absent, require_audited, require_rung, require_trashed, require_wired
from catalog.api.authz.grant_authority import authorize_grant, authorize_revoke
from catalog.api.authz.objects import addressed_object, resolve_rung, rung_label, rung_on
from catalog.api.authz.request_reads import batch_ids, json_body_or_400
from catalog.api.authz.rungs import (
    BATCH_OWNER_OPS,
    BATCH_PATHS,
    CREATE_ON_PARENT_SUFFIXES,
    FGA_TYPE,
    GRANT_SUFFIXES,
    ROOT_READ_SUFFIXES,
    TRANSACTION_SUFFIXES,
    action_relation,
    create_parent_rung,
    resource_for,
    route_suffix,
    undeclared_door,
)
from catalog.api.authz.write_doors import judge_write, write_door
from catalog.api.dependencies import Resolver, ResolverDep
from catalog.api.route_path import route_path
from catalog.api.security import Caller
from catalog.core import write_judge
from catalog.core.identifiers import parse_identifier, transaction_table
from lakehouse.governed import fga_ids, fga_reads
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit


__all__ = [
    "authorize",
    "authorize_batch",
    "authorize_transaction",
]


log = logging.getLogger(__name__)


async def authorize_transaction(client: OpenFgaClient, resolver: Resolver, segments: list[str], *, suffix: str, token: Caller) -> None:
    """Authorize a transaction door on the table its id names (``<table id>$<transaction>``, [[LH-221]]).

    Describing a transaction reads the table's metadata (``can_get_metadata``) and altering one would write the table
    (``can_write_data``), asked on the object the table's name resolves to, as every table door asks. A describe naming
    a table the caller cannot see, absent or hidden, answers as every read door does (:func:`refused_read_as_absent`).

    A recoverable drop's transaction is ``<table id>$<the trashed table's uuid>`` (ADR 0146): its describe is asked on
    that trashed table's own grants, which another table holding the name since does not share.

    Raises:
        InvalidInputError: The id names no table.
        PermissionDeniedError: The caller does not hold the door's rung on the table.
        TableNotFoundError: A caller who sees above the table described a transaction of a table it cannot see.
    """
    table = transaction_table(segments)
    relation = "can_get_metadata" if suffix == "describe" else "can_write_data"
    if suffix == "describe" and (dropped := await resolver.trashed_as_transaction(segments)):
        named = write_judge.Named(kind="table", segments=tuple(table), in_trash=True)
        await require_trashed(
            client, resolver, user=token.subject, relation=relation, named=named, label=fga_ids.canonical_object_id(segments), trashed=dropped
        )
        return
    try:
        await require_rung(
            client, resolver, user=token.subject, rung=write_judge.Rung(relation=relation, on=write_judge.Named(kind="table", segments=tuple(table)))
        )
    except PermissionDeniedError:
        await refused_read_as_absent(client, resolver, token=token, fga_type="table", denied=relation, segments=table)
        raise


async def authorize_batch(request: Request, client: OpenFgaClient, resolver: Resolver, *, user: str) -> None:
    """Authorize body-keyed batch routes (no ``{id}`` path param); tables named in the body.

    version/commit/delete on an existing table -> ``can_write_data`` on the table; a
    ``declare_table`` operation is create-on-parent -> ``can_create_table`` on the parent
    namespace. One batch_check per relation, on the objects the body's names resolve to; a name
    that holds no object is refused with them. ``request.json()`` is cached by Starlette, so the
    downstream endpoint re-parses the same body.

    Fails CLOSED on malformed input: a non-dict body or a non-list ``entries``/``operations``
    (client-controlled JSON, validated here BEFORE Pydantic since authorize runs first) is a
    403 deny, never a 500 crash or a silently-skipped check; a body that is not JSON at all is a 400.

    Raises:
        InvalidInputError: The body is not JSON.
        PermissionDeniedError: The body is not a batch, or the caller may not write a table it names.
    """
    body = await json_body_or_400(request)
    if not isinstance(body, dict):
        raise PermissionDeniedError("malformed batch request body")
    entries = body.get("entries")
    operations = body.get("operations")
    if entries is not None and not isinstance(entries, list):
        raise PermissionDeniedError("malformed batch request body: 'entries' must be a list")
    if operations is not None and not isinstance(operations, list):
        raise PermissionDeniedError("malformed batch request body: 'operations' must be a list")
    rungs: set[write_judge.Rung] = set()
    for kind, segments in batch_ids(body):
        if kind == "declare_table":  # creating a table -> can_create_table on its parent
            if (parent := create_parent_rung("table", segments)) is not None:
                rungs.add(parent)
        elif kind is not None and kind in BATCH_OWNER_OPS:  # destructive -> owner tier (no writer back-door)
            rungs.add(rung_on(BATCH_OWNER_OPS[kind], "table", segments))
        else:  # an entry, or version/commit/append on an existing table -> can_write_data on it
            rungs.add(rung_on("can_write_data", "table", segments))

    denied: list[str] = []
    # ONE round trip per DISTINCT relation, not per object (catalog-api-12): `operations` is client-supplied, bounded
    # only by `MOST_TABLES_IN_A_BATCH`, so a per-object loop would cost that many sequential authorization round trips
    # before any work was authorized.
    for relation in sorted({rung.relation for rung in rungs}):
        asked = sorted((rung for rung in rungs if rung.relation == relation), key=lambda rung: rung_label(rung))
        objects = {rung_label(rung): await resolve_rung(resolver, rung) for rung in asked}
        present = sorted({obj for obj in objects.values() if obj is not None})
        allowed = await fga_reads.batch_check(client, user=user, relation=relation, objects=present) if present else {}
        for label, obj in objects.items():  # #41 audit every batch authz decision (allow AND deny), like the single-route gate
            granted = obj is not None and allowed.get(obj, False)
            audit(AuditAction.AUTHZ, AuditOutcome.ALLOW if granted else AuditOutcome.DENY, relation=relation, subject=user, resource=obj or label)
            if not granted:
                denied.append(label)
    if denied:
        log.info("access_denied", extra={"objects": sorted(denied)})
        raise PermissionDeniedError(f"permission denied on {', '.join(sorted(denied))}")


async def authorize(request: Request, context: AuthzContextDep, *, resolver: ResolverDep) -> None:
    """Enforce the caller's permission for this route, then the quarantine of the warehouse its ``{id}`` begins with.

    In that order: a deactivated warehouse suspends every operation beneath it, and that it is deactivated is told
    only to a caller the route would otherwise have admitted. One holding nothing on it is answered as for a name in
    any warehouse, standing or not ([[LH-593]]).

    Raises:
        PermissionDeniedError: The caller may not act here, or may and the warehouse is deactivated.
    """
    await _decide(request, context, resolver=resolver)
    object_id = request.path_params.get("id")
    # A classification's id is a label, in no warehouse.
    if object_id is None or resource_for(route_path(request.scope)) in (None, "classification"):
        return
    if segments := parse_identifier(object_id):
        await resolver.require_active_warehouse(segments[0])


async def _decide(request: Request, context: AuthzContext, *, resolver: Resolver) -> None:
    """The caller's permission for this route: the write judge decides every write door, with FGA on
    or off, and OpenFGA every other door (a no-op when FGA is off).

    A table or namespace is asked about as the object its name resolves to, ``table:<uuid>`` or ``namespace:<uuid>``
    (ADR 0173 §8, [[LH-524]]), through ``resolver``, which the door then reads the same resolution from.
    """
    if (write := await write_door(request)) is not None:
        await judge_write(request, context=context, resolver=resolver, write=write)
        return
    if not context.settings.fga_enabled:
        return
    # Fail closed: enabled but unwired authz must never silently allow the request. The client is
    # INJECTED (`FgaClientDep`) rather than read off `app.state` here — one place knows where it lives.
    client = require_wired(context.client)
    if context.token is None:
        raise UnauthenticatedError("authentication required")

    # The path routing matches (`catalog.api.route_path`), never `request.url.path`: that re-parses the
    # decoded path as a URL string and truncates at a decoded '#'/'?' inside an id, so for an
    # exotically-named table the suffix would stop naming the route that matched.
    path = route_path(request.scope)
    object_id = request.path_params.get("id")

    # Body-keyed batch routes have no {id} path param — authorize by reading the body.
    if object_id is None:
        if path.rstrip("/") in BATCH_PATHS:
            await authorize_batch(request, client, resolver, user=context.token.subject)
        # Other id-less routes (collection lists, health) need only authentication.
        return

    resource = resource_for(path)
    if resource is None:
        return  # not a guarded resource path — authentication already enforced
    suffix = route_suffix(path, resource, object_id)
    # Canonicalize the id ONCE, the same way the endpoints + grant path do (parse_identifier),
    # so the check object and the seeded object agree byte-for-byte even for edge forms
    # (e.g. the delimiter-only root id). Passing the raw URL string here would diverge.
    segments = parse_identifier(object_id)
    door = _Addressed(path=path, resource=resource, suffix=suffix, object_id=object_id, segments=segments)
    await _decide_on_id(request, client, resolver, token=context.token, door=door)


class _Addressed(BaseModel):
    """The door a request with an ``{id}`` addresses: its route path, resource, suffix, raw id and that id's segments."""

    model_config = ConfigDict(frozen=True)

    path: str
    resource: str
    suffix: str
    object_id: str
    segments: list[str]


async def _decide_on_id(request: Request, client: OpenFgaClient, resolver: Resolver, *, token: Caller, door: _Addressed) -> None:
    """The caller's permission for a route whose ``{id}`` names an object of the door's resource."""
    path, resource, suffix, object_id, segments = door.path, door.resource, door.suffix, door.object_id, door.segments
    # A create asks the parent, a transaction the table it names, the root only its per-item reads, and every other door
    # the object itself.
    # Create-on-parent: child does not exist yet → authorize the parent.
    if suffix in CREATE_ON_PARENT_SUFFIXES.get(resource, frozenset()):
        if (parent := create_parent_rung(resource, segments)) is not None:
            await require_rung(client, resolver, user=token.subject, rung=parent)
        return

    # A transaction is authorized on the table its id names, not on an object of its own.
    if resource == "transaction":
        if suffix not in TRANSACTION_SUFFIXES:
            record_undeclared(path, "transaction", suffix, user=token.subject, obj=addressed_object("transaction", segments))
            raise undeclared_door("transaction", suffix)
        await authorize_transaction(client, resolver, segments, suffix=suffix, token=token)
        return

    # THE ROOT NAMES NO OBJECT, and composing one is a 500 rather than a decision. lance-ns encodes
    # the root namespace as the delimiter alone, so the stock client's `list_namespaces(id=[])` issues
    # `GET /v1/namespace/%24/list` and `parse_identifier` returns ZERO segments — `addressed_object` then builds
    # `namespace:` with an empty id, which OpenFGA refuses outright:
    #   ValidationException: [check] HTTP 400 invalid relation: invalid 'object' field format
    # and the exception leaves this guard as a 500 on a spec-legal request. No tuple could ever name
    # that object, so the check was never a permission question. Measured 2026-09-24 by driving the
    # STOCK `lance_namespace` client at a governed catalog: `ServiceUnavailableError: Internal Server
    # Error`.
    #
    # ONLY THE PER-ITEM READS RETURN. `list` and `table/list` authorize every item they return on the object it
    # resolves to — the route opens and the ITEMS are checked, because a
    # 403 there breaks the breadcrumb a grantee needs to reach their own table. Every other action on the
    # root is refused: exempting a cascade drop of the root lets any signed-in caller remove every
    # default-root table. Authentication was enforced above.
    #
    # ANY OTHER RESOURCE WITH NO SEGMENTS IS MALFORMED, not a root: a table or transaction id has to
    # name something. That earns the spec's own typed 400 rather than an empty object or a silent
    # open, which is the estate's rule — raise a `lance_namespace` error and let
    # `install_problem_handlers` choose the status.
    if not segments:
        if resource != "namespace":
            raise InvalidInputError(f"{resource} id is empty: {object_id!r} names no {resource}")
        if suffix in ROOT_READ_SUFFIXES:
            return
        raise PermissionDeniedError(f"the root namespace names no object, so {suffix!r} cannot be authorized on it")
    await _decide_on_object(request, client, resolver, token=token, door=door)


async def _decide_on_object(request: Request, client: OpenFgaClient, resolver: Resolver, *, token: Caller, door: _Addressed) -> None:
    """The caller's permission on the object the door's segments name, by the rung the door asks."""
    path, fga_type, suffix, segments = door.path, FGA_TYPE[door.resource], door.suffix, door.segments
    # The rung a grant hands out, a classification's one read, or the `can_*` relation the door declares.
    # Grant/revoke authorize on the RUNG BEING HANDED OUT, which lives in the body — so, like the
    # batch routes above, this one cannot be answered from the path alone.
    if suffix in GRANT_SUFFIXES:
        gate = authorize_revoke if suffix == "access/revoke" else authorize_grant
        await gate(request, client, resolver, user=token.subject, fga_type=fga_type, segments=segments)
        return
    # A CLASSIFICATION IS A LABEL, NOT A DATASET ([[LH-055]]). Its router carries exactly three routes
    # and the two grant ones returned above, so only `access/my-permissions` reaches here. It resolves
    # HERE rather than in `action_relation`, whose vocabulary names relations this type does not define,
    # and OpenFGA answers an undefined relation with a 400 that fails closed to 503 — an outage wearing a
    # permission error's clothes.
    if fga_type == "classification":
        obj = addressed_object(fga_type, segments)
        if suffix != "access/my-permissions":
            record_undeclared(path, fga_type, suffix, user=token.subject, obj=obj)
            raise undeclared_door(fga_type, suffix)
        await require_audited(client, user=token.subject, relation="can_get_metadata", obj=obj)
        return
    try:
        relation = action_relation(fga_type, suffix, method=request.method)
    except InternalError:
        record_undeclared(path, fga_type, suffix, user=token.subject, obj=addressed_object(fga_type, segments))
        raise
    rung = rung_on(relation, fga_type, segments, suffix=suffix)
    try:
        await require_rung(client, resolver, user=token.subject, rung=rung)
    except PermissionDeniedError:
        # A READ door answers a name the caller cannot see as one that is not there: the spec's 404 to a caller who
        # sees above it, the 403 to anyone else. Owner ruling 2026-09-11, [[LH-236]].
        await refused_read_as_absent(client, resolver, token=token, fga_type=fga_type, denied=relation, segments=segments)
        raise
