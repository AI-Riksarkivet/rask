"""The doors maintenance reclaims the trash through ([[XC-084]]): rask's own operations, at ``/management/v1/trash``.

Maintenance holds no write of its own on the store and no client that writes grants, so each act on one trash record is
the catalog's, judged on its own reads (:mod:`catalog.services.trash_reclaim`): it releases a drop past its deadline,
vends the session that deletes a table's bytes, ends the record once nothing it names holds a byte, and notes
maintenance's own refusals on it. Every door asks ``can_reclaim_trash`` on the estate root, which only the ``reclaimer``
role opens (the chart's bootstrap-admin hook seeds it for maintenance's ServiceAccount, ``lance.fgaEstateRoles``). A
record a judgement refuses answers 409 (code 19) and the refusal is noted on it; a uuid no trash record holds answers
404.

The router guard leaves these paths to their handlers (``fga_deps._RESOURCES`` names no ``trash``), so the gate is this
router's own dependency, as the decision door's is.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidTableStateError, NamespaceNotFoundError, TableNotFoundError

from catalog.api import fga_deps
from catalog.api.dependencies import ClockDep, ControlEmitterDep, FgaClientDep, Resolver, ResolverDep, SettingsDep, VendorDep
from catalog.api.security import Caller, CurrentSubject, CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier
from catalog.core.vending import reclaim_or_refuse, role_session_name
from catalog.schemas import CredentialResponse, TrashEndResponse, TrashRefusalRequest, TrashRefusalResponse, TrashReleaseResponse, VendedCredentials
from catalog.services import trash_reclaim
from lakehouse import trash
from lakehouse.base_refs import decoded_path
from lakehouse.control_emit import ControlEmitter, emit_control
from lakehouse.governed import fga
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.location_claims import LocationClaim
from lakehouse.trash import TrashKind


async def require_can_reclaim_trash(client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> None:
    """Every trash door's gate: the caller holds ``can_reclaim_trash`` on the estate root."""
    await fga_deps.require_relation(client, settings, token, relation="can_reclaim_trash", obj=settings.fga_root_object)


router = APIRouter()
management_router = APIRouter(prefix="/management/v1/trash", tags=["trash-reclaim"], dependencies=[Depends(require_can_reclaim_trash)])


@management_router.post("/{kind}/{uuid}/release")
async def release_trash(
    kind: TrashKind,
    uuid: str,
    resolver: ResolverDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    clock: ClockDep,
) -> TrashReleaseResponse:
    """Release a drop past its deadline (ADR 0146): revoke every grant on its object, tell each person who lost one, and
    stamp the record released; its bytes wait for the purge.

    Refused 409 (code 19) until the deadline has settled (``lakehouse.trash.RELEASE_SETTLE``), and while a live object
    holds the record's place or name again: an undrop registers before it clears its record.
    """
    record = await run_in_threadpool(trash_reclaim.read_record, settings, kind, uuid)
    claim = await run_in_threadpool(trash_reclaim.location_claim, settings, record)
    if (reason := trash_reclaim.release_refusal(record, now=clock()) or await _standing_refusal(resolver, record, claim, kind=kind, uuid=uuid)) is not None:
        raise await _noted(settings, kind=kind, uuid=uuid, reason=reason)
    removed = await fga_deps.revoke_expired_object(client, settings, fga.object_of(kind, uuid), token=token)
    await _announce_revoked(control, record, removed, kind=kind, uuid=uuid, actor=_actor(token))
    stamped = await run_in_threadpool(partial(trash_reclaim.stamp_released, settings, kind=kind, uuid=uuid, now=clock()))
    return TrashReleaseResponse(tuples_revoked=len(removed), stamped=stamped)


@management_router.post("/{kind}/{uuid}/credentials", response_model_exclude_none=True)
async def vend_reclaim_credentials(
    kind: TrashKind, uuid: str, resolver: ResolverDep, settings: SettingsDep, token: CurrentToken, subject: CurrentSubject, vendor: VendorDep, clock: ClockDep
) -> CredentialResponse:
    """Vend the session that deletes one trashed table's bytes: every object at its location, in both spellings, and in
    its own data directories, and nothing on the control root.

    Vended for a table record past its deadline, at a place whose claim records the record's uuid, crossing no control
    prefix, held by no live table and read by no registered clone; anything else is refused 409 (code 19). The record and what it keeps are ended at
    ``DELETE /management/v1/trash/{kind}/{uuid}`` once the bytes are gone.
    """
    record = await run_in_threadpool(trash_reclaim.read_record, settings, kind, uuid)
    claim = await run_in_threadpool(trash_reclaim.location_claim, settings, record)
    if (
        reason := trash_reclaim.vend_refusal(record, claim, kind=kind, uuid=uuid, now=clock())
        or await _standing_refusal(resolver, record, claim, kind=kind, uuid=uuid)
        or await run_in_threadpool(trash_reclaim.clone_refusal, settings, record)
    ) is not None:
        raise await _noted(settings, kind=kind, uuid=uuid, reason=reason)
    scope = await run_in_threadpool(trash_reclaim.scope_of, settings, record)
    vended = await run_in_threadpool(partial(reclaim_or_refuse, vendor, scope, session_name=role_session_name(subject)))
    if vended is None:
        return CredentialResponse(mode="server_mediated")
    audit(
        AuditAction.VEND_CREDENTIALS,
        AuditOutcome.SUCCESS,
        subject=token.subject if token is not None else None,
        resource=fga.object_of(kind, uuid),
        tier="reclaim",
        door="trash_credentials",
    )
    creds = VendedCredentials(storage_options=vended.as_storage_options().in_clear(), expires_at_millis=vended.expires_at_millis)
    return CredentialResponse(mode="direct", credentials=creds, location=scope.location)


@management_router.delete("/{kind}/{uuid}")
async def end_trash(
    kind: TrashKind, uuid: str, resolver: ResolverDep, settings: SettingsDep, token: CurrentToken, client: FgaClientDep, clock: ClockDep
) -> TrashEndResponse:
    """End a trash record whose object holds no byte: revoke the grants it still holds, end the records it keeps under
    its uuid, a table's claim and base record, and the record last.

    Refused 409 (code 19) while its place, or a data directory its claim names, holds an object (the purge deletes them
    first), while a record naming no bytes is short of its deadline, at a place whose claim records another table or
    none, and while a live object holds the place or the name again.
    """
    record = await run_in_threadpool(trash_reclaim.read_record, settings, kind, uuid)
    claim = await run_in_threadpool(trash_reclaim.location_claim, settings, record)
    judged = partial(trash_reclaim.end_refusal, settings, record, claim, kind=kind, uuid=uuid, now=clock())
    if (reason := await run_in_threadpool(judged) or await _standing_refusal(resolver, record, claim, kind=kind, uuid=uuid)) is not None:
        raise await _noted(settings, kind=kind, uuid=uuid, reason=reason)
    revoked = await fga_deps.revoke_removed_object(client, settings, fga.object_of(kind, uuid), token=token)
    await run_in_threadpool(partial(trash.end, settings.registry_root, settings.storage_options(), record, kind=kind, uuid=uuid))
    return TrashEndResponse(tuples_revoked=revoked)


@management_router.post("/{kind}/{uuid}/refusals")
async def note_trash_refusal(kind: TrashKind, uuid: str, body: TrashRefusalRequest, settings: SettingsDep) -> TrashRefusalResponse:
    """Note maintenance's own refusal of a trash record, so a record refused tick after tick sorts behind the ones that
    move. Bookkeeping the catalog never judges on."""
    await run_in_threadpool(trash_reclaim.read_record, settings, kind, uuid)
    attempts = await run_in_threadpool(partial(trash_reclaim.note_refusal, settings, kind=kind, uuid=uuid, reason=body.reason))
    return TrashRefusalResponse(attempts=attempts)


async def _noted(settings: Settings, *, kind: TrashKind, uuid: str, reason: str) -> InvalidTableStateError:
    """Note ``reason`` on the record, as maintenance's refusals are noted, and return the refusal for the door to raise."""
    await run_in_threadpool(partial(trash_reclaim.note_refusal, settings, kind=kind, uuid=uuid, reason=reason))
    return InvalidTableStateError(reason)


async def _standing_refusal(resolver: Resolver, record: dict[str, Any], claim: LocationClaim | None, *, kind: TrashKind, uuid: str) -> str | None:
    """Why the trashed object stands again, or ``None``.

    A table with a place stands where its claim is held by a name that still resolves to the place: a crashed undrop
    leaves its record beside the table it restored, which a rename since carries along. An object with no place stands
    where its name resolves to an object holding the record's uuid. A drop kept in the trash holds its claim under a
    name that resolves nowhere, or elsewhere.
    """
    if claim is not None:
        return await _place_held(resolver, claim)
    segments = parse_identifier(trash.name_of(record))
    if not segments:
        return None
    try:
        standing = await resolver.namespace_uuid(segments) if kind == "namespace" else (await resolver.table(segments)).uuid
    except (TableNotFoundError, NamespaceNotFoundError):
        return None
    if standing != uuid:
        return None
    return f"the live {kind} {fga.canonical_object_id(segments)!r} holds the record's uuid again, so the record is not the trash's"


async def _place_held(resolver: Resolver, claim: LocationClaim) -> str | None:
    """Why a live table holds the place ``claim`` is on, or ``None``."""
    if claim.released_at is not None:
        return None
    try:
        resolved = await resolver.table(claim.segments)
    except (TableNotFoundError, NamespaceNotFoundError):
        return None
    if resolved.location is None or decoded_path(resolved.location) != decoded_path(claim.location):
        return None
    return f"the live table {fga.canonical_object_id(claim.segments)!r} holds the record's place, so its bytes are not the trash's"


async def _announce_revoked(control: ControlEmitter, record: dict[str, Any], removed: list[fga.ClientTuple], *, kind: TrashKind, uuid: str, actor: str) -> None:
    """Tell each person who lost a grant on the released object: none of them can see it any more, so no feed gated on
    seeing it would. Structural edges name objects, not people, and are not announced."""
    namespace_uuid = record.get("namespace_uuid")
    for tup in removed:
        if fga.names_someone(str(tup.user)):
            await emit_control(
                control,
                action="grant_revoked",
                object_type="grant",
                object_id=fga.object_of(kind, uuid),
                object_name=trash.name_of(record),
                object_uuid=uuid,
                namespace_uuid=namespace_uuid if isinstance(namespace_uuid, str) else None,
                actor=actor,
                extra={"relation": tup.relation, "subject": tup.user, "reason": "trash_expired"},
            )


def _actor(token: Caller | None) -> str:
    """Who acted: the verified caller, or the catalog itself on a stack with authentication off."""
    return token.subject if token is not None else "system:catalog"
