"""The write judge asked at the door: the write a route makes, the inputs its run declares, the deregister of a table no
uuid identifies, a stock client's staged run, and the credential tier a spec door vends."""

import logging
from collections.abc import Sequence
from functools import partial
from typing import Literal

from fastapi import Request
from lance_namespace import (
    InternalError,
    InvalidInputError,
    LanceNamespace,
    NamespaceNotFoundError,
    PermissionDeniedError,
    TableNotFoundError,
    UnauthenticatedError,
)
from openfga_sdk import OpenFgaClient

from catalog.api.authz.context import AuthzContext
from catalog.api.authz.decisions import authorization, check_recording_outage, require_rung, require_wired
from catalog.api.authz.objects import rung_label, rung_on
from catalog.api.authz.request_reads import json_body_or_none, rename_landing
from catalog.api.authz.rungs import (
    CLASSIFY_RELATION,
    CLASSIFYING_DOOR,
    CREATE_ON_PARENT_SUFFIXES,
    FGA_TYPE,
    GRANT_SUFFIXES,
    JUDGED_RESOURCES,
    READ_RELATIONS,
    UNIDENTIFIED_TABLE_ESTATE_VERB,
    UNIDENTIFIED_TABLE_RUNG,
    VEND_TIER_RUNGS,
    action_relation,
    create_parent_rung,
    rename_relation,
    resource_for,
    route_suffix,
)
from catalog.api.dependencies import Resolver, namespace_for_warehouse
from catalog.api.route_path import route_path
from catalog.core import run_context, vending, write_judge
from catalog.core.identifiers import parse_identifier
from catalog.services import object_uuids
from lakehouse.governed import fga_ids
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.door_caller import caller_subject
from lakehouse.naming import PLATFORM_PREFIX


__all__ = [
    "declared_inputs",
    "judge_staged_run",
    "judge_write",
    "require_declared_inputs",
    "spec_vend_tier",
    "write_door",
]


log = logging.getLogger(__name__)


def _classifies_only(body: object) -> bool:
    """Whether an ``update_field_metadata`` body changes governance keys (``rask.*``) and nothing else.

    Every update names at least one key and every key it names is a governance key. A ``replace`` drops every key its
    update does not name, ordinary ones among them, so it is never only a classification; nor is a body this cannot read,
    which keeps the writer's rung and is refused or answered by the door's own model.
    """
    updates = body.get("updates") if isinstance(body, dict) else None
    if not isinstance(updates, list) or not updates:
        return False
    for update in updates:
        if not isinstance(update, dict) or update.get("replace"):
            return False
        if not _names_only_governance_keys(update.get("metadata")):
            return False
    return True


def _names_only_governance_keys(metadata: object) -> bool:
    """Whether ``metadata`` is a map naming at least one key, every one of them a governance key (``rask.*``)."""
    return isinstance(metadata, dict) and bool(metadata) and all(isinstance(key, str) and key.startswith(PLATFORM_PREFIX) for key in metadata)


async def write_door(request: Request) -> write_judge.Write | None:
    """The write this route is about to make, or ``None`` for a door the write judge does not decide.

    A write door is every non-read door on a table or materialized view, owner-rung ones included, and every
    create of one (ADR 0160). The classification is the same one the router's own gate uses, so a door is
    judged by what its declared rung says it does. ``None`` leaves the request to the rest of ``authorize``:
    reads, the grant doors (authorized from the body), the root, and a door no map declares, which that path
    refuses and audits.
    """
    path = route_path(request.scope)
    object_id = request.path_params.get("id")
    resource = resource_for(path)
    if object_id is None or resource not in JUDGED_RESOURCES:
        return None
    suffix = route_suffix(path, resource, object_id)
    segments = parse_identifier(object_id)
    if not segments or suffix in GRANT_SUFFIXES:
        return None
    fga_type = FGA_TYPE[resource]
    obj = fga_ids.addressed_object(fga_type, segments)
    run = run_context.from_headers(request.headers) if suffix in write_judge.STAMPING_DOORS else None
    if suffix in CREATE_ON_PARENT_SUFFIXES.get(resource, frozenset()):
        return write_judge.Write(op=suffix, obj=obj, rung=create_parent_rung(resource, segments), table=tuple(segments), run=run)
    if suffix == "credentials":
        # A vend is a read at its default tier and a WRITE at a writing one: the credential is how a
        # direct-to-storage writer writes, so it is judged on the rung its tier opens.
        tier = vending.requested_tier(request.query_params)
        writing = VEND_TIER_RUNGS.get(tier) if tier is not None else None
        if writing is None:
            return None
        return write_judge.Write(op=suffix, obj=obj, rung=rung_on(writing, fga_type, segments), table=tuple(segments))
    try:
        relation = action_relation(fga_type, suffix, method=request.method)
    except InternalError:
        return None
    if relation in READ_RELATIONS:
        return None
    if fga_type == "table" and suffix == CLASSIFYING_DOOR and _classifies_only(await json_body_or_none(request)):
        relation = CLASSIFY_RELATION
    table = await rename_landing(request, segments) if suffix == "rename" else tuple(segments)
    moved_from = tuple(segments) if suffix == "rename" else ()
    if moved_from:
        relation = rename_relation(moved_from, table)
    return write_judge.Write(
        op=suffix,
        obj=obj,
        rung=rung_on(relation, fga_type, segments, suffix=suffix),
        table=table,
        run=run,
        moved_from=moved_from,
    )


async def require_declared_inputs(run: run_context.RunContext, context: AuthzContext, *, resolver: Resolver) -> dict[str, run_context.RunInput]:
    """Each input ``run`` declares, by the uuid of the table it names now, once each names a table the committer may read
    (ADR 0183 §2).

    The name is resolved through ``resolver``, the request's one resolution of every name, and asked ``can_read_data``
    on that uuid with FGA on. A name holding no table and a table the committer may not read are refused alike, so the
    refusal tells no committer which tables exist. A commit's provenance is then a fact the catalog checked, and an
    erasure names what it declared by uuid (:mod:`catalog.services.table_inputs`).

    Raises:
        InvalidInputError: An input names no table, or one the committer may not read.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    check = _rung_check(context, resolver=resolver)
    if check is not None:
        for entry in run.inputs:
            try:
                await check(write_judge.Rung(relation="can_read_data", on=write_judge.Named(kind="table", segments=tuple(entry.table))))
            except (PermissionDeniedError, object_uuids.UnidentifiedObjectError) as exc:
                raise _unreadable_input(entry) from exc
    return await declared_inputs(resolver, run)


async def declared_inputs(resolver: Resolver, run: run_context.RunContext | None) -> dict[str, run_context.RunInput]:
    """Each input ``run`` declares, by the uuid of the table its name resolves to through ``resolver``; none for no run.

    A door reads here what the write judge already judged (:func:`require_declared_inputs`), from the request's one
    resolution, so the uuids it records are the ones authorized.

    A table that records no uuid holds no grant ([[LH-534]]), so it is refused as one the committer may not read.

    Raises:
        InvalidInputError: An input names no table, or one no uuid identifies.
    """
    declared: dict[str, run_context.RunInput] = {}
    for entry in run.inputs if run is not None else ():
        try:
            resolved = await resolver.table(list(entry.table))
            if resolved.location is None:
                raise _unreadable_input(entry)
            declared.setdefault(resolved.require_uuid(), entry)
        except (PermissionDeniedError, NamespaceNotFoundError, TableNotFoundError, object_uuids.UnidentifiedObjectError) as exc:
            raise _unreadable_input(entry) from exc
    return declared


def _unreadable_input(entry: run_context.RunInput) -> InvalidInputError:
    return InvalidInputError(
        f"commit refused: the input {fga_ids.canonical_object_id(entry.table)!r} its run declares ({run_context.INPUTS_PROPERTY}) names no table "
        "the committer may read (can_read_data); a run declares each table it read, by its name now"
    )


def _rung_check(context: AuthzContext, *, resolver: Resolver) -> write_judge.RungCheck | None:
    """The write judge's rung check, asked through :func:`require_rung`; ``None`` with FGA off, when every rung allows.

    Raises:
        ServiceUnavailableError: FGA is on and unwired.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    if (authz := authorization(context)) is None:
        return None

    async def check(rung: write_judge.Rung) -> None:
        try:
            await require_rung(authz.client, resolver, user=authz.subject, rung=rung)
        except object_uuids.UnidentifiedObjectError:
            # Every door but the deregister keeps answering code 18 for an object no uuid identifies, and the
            # deregister is a judged write door, so this is the one place that door's rung is asked.
            if rung.relation != UNIDENTIFIED_TABLE_RUNG:
                raise
            await _require_estate_admin_for_unidentified(authz.client, user=authz.subject, root=context.settings.fga_root_object, rung=rung)

    return check


async def _require_estate_admin_for_unidentified(client: OpenFgaClient, *, user: str, root: str, rung: write_judge.Rung) -> None:
    """Admit the estate admin to deregister a table no uuid identifies, and refuse anyone else as a name holding nothing is refused.

    Every grant is held by an object's uuid (ADR 0173 §8), so such a table holds none, and its deregister asks the estate
    admin's verb for it, ``can_deregister_unidentified_table`` on the estate root ([[LH-534]]). The decision is audited on the
    table by its name, with the root it was decided on; a refusal names the door's own rung on the name, so it tells no
    caller that the name holds a table.
    """
    label = rung_label(rung)
    allowed = await check_recording_outage(client, user=user, relation=UNIDENTIFIED_TABLE_ESTATE_VERB, obj=root, resource=label, decided_on=root)
    audit(
        AuditAction.AUTHZ,
        AuditOutcome.ALLOW if allowed else AuditOutcome.DENY,
        relation=UNIDENTIFIED_TABLE_ESTATE_VERB,
        subject=user,
        resource=label,
        decided_on=root,
        reason="unidentified_table",
    )
    if not allowed:
        log.info("access_denied", extra={"relation": rung.relation, "object": label})
        raise PermissionDeniedError(f"{rung.relation} required on {label}")


async def judge_write(request: Request, *, context: AuthzContext, resolver: Resolver, write: write_judge.Write) -> None:
    """Hand ``write`` to the write judge, with the rung asked through :func:`require_rung` when FGA is on."""
    check = _rung_check(context, resolver=resolver)

    async def namespace(warehouse_id: str) -> LanceNamespace:
        return await namespace_for_warehouse(request, context.settings, warehouse_id)

    await write_judge.judge(
        write,
        caller=caller_subject(context.token),
        check=check,
        inputs=partial(require_declared_inputs, context=context, resolver=resolver),
        namespace=namespace,
    )


async def judge_staged_run(
    context: AuthzContext,
    *,
    resolver: Resolver,
    ns: LanceNamespace,
    segments: Sequence[str],
    run: run_context.RunContext | None,
    adds_data: bool,
) -> dict[str, run_context.RunInput]:
    """Judge the run a stock client's staged commit to table ``segments`` names (ADR 0157), and answer each input it
    declares by the uuid of the table it names (:func:`require_declared_inputs`).

    Only a commit that ``adds_data`` must name a run, under a namespace that requires one, whose rule is then read
    through ``ns``, the connection the door resolved for the table; any declared input is judged either way.

    Raises:
        InvalidInputError: The table lies under a namespace that requires a run and the commit names no run or no input,
            or an input names no table the committer may read.
    """
    required = await write_judge.requires_run(ns, segments[:-1]) if adds_data and len(segments) > 1 else False
    names = f"the staged manifest's transaction properties {run_context.RUN_ID_PROPERTY} and {run_context.INPUTS_PROPERTY}"
    declared: dict[str, run_context.RunInput] = {}

    async def inputs(named: run_context.RunContext) -> None:
        declared.update(await require_declared_inputs(named, context, resolver=resolver))

    # The stock client's door asks the rule directly, so its decision is audited here as the catalog's own doors' is in
    # `write_judge.judge`: every decision under the run rule leaves a record, whichever door took the write.
    judged = {
        "subject": caller_subject(context.token),
        "resource": fga_ids.addressed_object("table", list(segments)),
        "op": "version_create",
        "requires_run": str(required).lower(),
    }
    try:
        await write_judge.judge_run(required, run, inputs=inputs, names=names)
    except InvalidInputError:
        audit(AuditAction.WRITE_JUDGE, AuditOutcome.DENY, **judged)
        raise
    audit(AuditAction.WRITE_JUDGE, AuditOutcome.ALLOW, **judged)
    return declared


async def _granted_vend_tier(client: OpenFgaClient, subject: str, obj: str) -> Literal["read", "write"] | None:
    """The tier ``subject``'s rungs on ``obj`` open at a spec door, each rung that decided it audited ALLOW."""
    writing = VEND_TIER_RUNGS["write"]
    if await check_recording_outage(client, user=subject, relation=writing, obj=obj):
        audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, relation=writing, subject=subject, resource=obj)
        return "write"
    if not await check_recording_outage(client, user=subject, relation="can_read_data", obj=obj):
        return None
    audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, relation="can_read_data", subject=subject, resource=obj)
    if not await check_recording_outage(client, user=subject, relation="can_maintain", obj=obj):
        return "read"
    audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, relation="can_maintain", subject=subject, resource=obj)
    return "write"


async def spec_vend_tier(context: AuthzContext, *, ns: LanceNamespace, segments: Sequence[str], table_uuid: str, door: str) -> Literal["read", "write"] | None:
    """The highest tier a spec door (``describe``, ``declare``) vends the caller on table ``segments`` (ADR 0156 C3).

    ``table_uuid`` is the uuid the door resolved the name to (or minted, for a declare), whose object the grants are
    held by. ``can_write_data`` opens the write tier, and that is a write, so the write judge decides it and audits the
    tier it was taken under, as it does for the management vend door; ``ns`` is the connection the door already
    resolved for the table, which is the one its namespace path's tier is read through. ``can_read_data`` with
    ``can_maintain``, a compaction job's rungs, opens the write tier too: its puts are what a compaction stages, and
    ``version/create`` admits a maintainer's commit for a compaction's two operations alone (ADR 0162, ADR 0174). Those
    puts reach what a writer's do, so a put over a committed data file is as open to it as to a writer: no session
    policy refuses one (``vending.build_session_policy``). ``can_read_data`` alone opens the read tier. None of them, no
    credential: the door still answers, because describing a table is a metadata read. Maintain is never vended here;
    it is the maintenance identity's (LH-473). Through a read-only window (``maintenance_read_only``) a write-tier grant
    opens the read tier: the window refuses every write at the catalog's doors, and a write-tier credential would write
    storage straight past them.

    The rungs are probed through :func:`check_recording_outage`, which writes no DENY: a reader's describe asking whether it may also
    write is not an access attempt. An outage is recorded, as at every gate. The rung that decides the tier is audited ALLOW, as every authorization
    decision is; whether a credential is then minted is the door's ``vend_credentials`` record. With FGA off
    every rung allows, so the write tier is vended, as the management door vends it.

    Raises:
        ServiceUnavailableError: FGA is on and unwired, or OpenFGA failed: fail closed, as every gate here does.
        UnauthenticatedError: FGA is on and nobody is signed in.
    """
    obj = fga_ids.table_object(table_uuid)
    if context.settings.fga_enabled:
        client = require_wired(context.client)
        if context.token is None:
            raise UnauthenticatedError("authentication required")
        granted = await _granted_vend_tier(client, context.token.subject, obj)
        if granted != "write":
            return granted
    if context.settings.maintenance_read_only:
        return "read"

    async def namespace(_warehouse_id: str) -> LanceNamespace:
        return ns

    write = write_judge.Write(op=door, obj=fga_ids.addressed_object("table", list(segments)), rung=None, table=tuple(segments))
    await write_judge.judge(write, caller=caller_subject(context.token), check=None, inputs=_names_no_run, namespace=namespace)
    return "write"


async def _names_no_run(run: run_context.RunContext) -> None:
    """The inputs check of a write that names no run, which the judge never asks."""
    raise InternalError(f"a vend names no run, yet the write judge asked for the inputs of run {run.run_id!r}")
