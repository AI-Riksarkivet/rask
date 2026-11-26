"""The doors maintenance reclaims the trash through ([[XC-084]]): rask's own operations, at ``/management/v1/trash``.

Maintenance holds no write of its own on the store and no client that writes grants, so each act on one trash record is
the catalog's, judged on its own reads (:mod:`catalog.services.trash_reclaim`): it releases a drop past its deadline,
vends the session that deletes a table's bytes, ends the record once nothing it names holds a byte, and notes
maintenance's own refusals on it. Every door asks ``can_reclaim_trash`` on the estate root, which only the ``reclaimer``
role opens (the catalog's declaration grants it to maintenance's ServiceAccount, ``lance.fgaEstateRoles``). A
record a judgement refuses answers 409 (code 19) and the refusal is noted on it; a uuid no trash record holds answers
404.

The router guard leaves these paths to their handlers (``rungs._RESOURCES`` names no ``trash``), so the gate is this
router's own dependency, as the decision door's is.
"""

from functools import partial

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidTableStateError, NamespaceNotFoundError, TableNotFoundError

from catalog.api import announcing
from catalog.api.announcing import AnnouncerDep, Filing
from catalog.api.authz import decisions, door_gates, object_grants
from catalog.api.authz.context import AuthzContextDep
from catalog.api.dependencies import ClockDep, Resolver, ResolverDep, SettingsDep, VendorDep
from catalog.api.security import Caller, CurrentSubject, CurrentToken, actor_of
from catalog.core.config import CatalogStore
from catalog.core.identifiers import parse_identifier
from catalog.core.vending import reclaim_or_refuse, role_session_name
from catalog.schemas import CredentialResponse, VendedCredentials
from catalog.services import announcement_intents, trash_reclaim
from catalog.services.announcement_intents import Announcement
from lakehouse.governed import fga_ids, fga_reads, fga_writes
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.door_caller import caller_subject
from lakehouse.lancekit.base_refs import decoded_path
from lakehouse.maintenance_doors import TrashEndResponse, TrashRefusalRequest, TrashRefusalResponse, TrashReleaseResponse
from lakehouse.problems.outage import store_refusal
from lakehouse.records import trash
from lakehouse.records.location_claims import LocationClaim
from lakehouse.records.trash import TrashKind, TrashRecord


async def require_can_reclaim_trash(context: AuthzContextDep) -> None:
    """Every trash door's gate: the caller holds ``can_reclaim_trash`` on the estate root."""
    await door_gates.require_relation(context, relation="can_reclaim_trash", obj=context.settings.fga_root_object)


management_router = APIRouter(prefix="/management/v1/trash", tags=["trash-reclaim"], dependencies=[Depends(require_can_reclaim_trash)])


@management_router.post("/{kind}/{uuid}/release")
async def release_trash(
    *,
    kind: TrashKind,
    uuid: str,
    resolver: ResolverDep,
    context: AuthzContextDep,
    announcer: AnnouncerDep,
    clock: ClockDep,
) -> TrashReleaseResponse:
    """Release a drop past its deadline (ADR 0146): revoke every grant on its object, tell each person who lost one, and
    stamp the record released; its bytes wait for the purge. What it tells is filed before the revoke, so a release that
    fails after the revoke still tells them ([[LH-576]]).


    Refused 409 (code 19) until the deadline has settled (``lakehouse.records.trash.RELEASE_SETTLE``), while a door's
    announcements on the object stand unsent, and while a live object holds the record's place or name again: an undrop
    registers before it clears its record. Audited as it lands or as it
    fails (``trash_release``): it revokes grants on the catalog's own session, for whoever asked.
    """
    try:
        record = await run_in_threadpool(trash_reclaim.read_record, context.settings, kind, uuid)
        # A wait for the finish, not a judgement of the record, so nothing is noted on it: the finish orders a record
        # against a filing by the store's stamp of the record's last write, and a note written now would read as a drop
        # of the table since the filing, which loses a dead deregister's announcement.
        if (waiting := await _unannounced_refusal(context.settings, kind=kind, uuid=uuid)) is not None:
            raise InvalidTableStateError(waiting)
        claim = await run_in_threadpool(trash_reclaim.location_claim, context.settings, record)
        if (reason := trash_reclaim.release_refusal(record, now=clock()) or await _standing_refusal(resolver, record, claim, kind=kind, uuid=uuid)) is not None:
            raise await _noted(context.settings, kind=kind, uuid=uuid, reason=reason)
        obj = fga_ids.object_of(kind, uuid)
        held = await fga_reads.read_object_tuples(wired, obj) if (wired := decisions.wired(context.settings, context.client)) is not None else []
        told = {
            (t.object, t.relation, t.user): _revoked(t, record, uuid=uuid, actor=actor_of(context.token)) for t in held if fga_ids.names_someone(str(t.user))
        }
        async with announcer.announcing(door="trash_release", target=obj, announcements=list(told.values())) as filing:
            removed = await object_grants.revoke_expired_object(context, obj)
            await _tell_who_lost_a_grant(filing, removed, told, record=record, uuid=uuid, token=context.token)
            stamped = await run_in_threadpool(partial(trash_reclaim.stamp_released, context.settings, kind=kind, uuid=uuid, now=clock()))
    except Exception as failed:
        _audited(AuditAction.TRASH_RELEASE, AuditOutcome.FAILURE, context.token, kind=kind, uuid=uuid, reason=f"{type(failed).__name__}: {failed}"[:300])
        raise
    _audited(AuditAction.TRASH_RELEASE, AuditOutcome.SUCCESS, context.token, kind=kind, uuid=uuid, tuples_revoked=len(removed), stamped=stamped)
    return TrashReleaseResponse(tuples_revoked=len(removed), stamped=stamped)


async def _tell_who_lost_a_grant(
    filing: Filing,
    removed: list[fga_writes.ClientTuple],
    told: dict[tuple[str, str, str], Announcement],
    *,
    record: TrashRecord,
    uuid: str,
    token: Caller | None,
) -> None:
    """Send the ``grant_revoked`` filed for each person among ``removed``, the tuples a release took off its object."""
    # Tell each person who lost a grant on the released object: none of them can see it any more, so no feed gated
    # on seeing it would. Structural edges name objects, not people, and are not announced.
    for t in removed:
        if fga_ids.names_someone(str(t.user)):
            await filing.send(told.get((t.object, t.relation, t.user)) or _revoked(t, record, uuid=uuid, actor=actor_of(token)))


@management_router.post("/{kind}/{uuid}/credentials", response_model_exclude_none=True)
async def vend_reclaim_credentials(
    *,
    kind: TrashKind,
    uuid: str,
    resolver: ResolverDep,
    settings: SettingsDep,
    token: CurrentToken,
    subject: CurrentSubject,
    vendor: VendorDep,
    clock: ClockDep,
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
    scope = await run_in_threadpool(partial(trash_reclaim.scope_of, settings, record, uuid=uuid))
    vended = await run_in_threadpool(partial(reclaim_or_refuse, vendor, scope, session_name=role_session_name(subject)))
    if vended is None:
        return CredentialResponse(mode="server_mediated")
    audit(
        AuditAction.VEND_CREDENTIALS,
        AuditOutcome.SUCCESS,
        subject=caller_subject(token),
        resource=fga_ids.object_of(kind, uuid),
        tier="reclaim",
        door="trash_credentials",
    )
    creds = VendedCredentials(storage_options=vended.as_storage_options().in_clear(), expires_at_millis=vended.expires_at_millis)
    return CredentialResponse(mode="direct", credentials=creds, location=scope.location)


@management_router.delete("/{kind}/{uuid}")
async def end_trash(*, kind: TrashKind, uuid: str, resolver: ResolverDep, context: AuthzContextDep, clock: ClockDep) -> TrashEndResponse:
    """End a trash record whose object holds no byte: revoke the grants it still holds, end the records it keeps under
    its uuid, a table's claim and base record, and the record last.

    Refused 409 (code 19) while its place, or a data directory its claim names, holds an object (the purge deletes them
    first), while a record naming no bytes is short of its deadline, at a place whose claim records another table or
    none, and while a live object holds the place or the name again. Audited as it lands or as it fails (``trash_end``):
    it revokes grants and ends records on the catalog's own session, for whoever asked.
    """
    try:
        record = await run_in_threadpool(trash_reclaim.read_record, context.settings, kind, uuid)
        claim = await run_in_threadpool(trash_reclaim.location_claim, context.settings, record)
        judged = partial(trash_reclaim.end_refusal, context.settings, record, claim, kind=kind, uuid=uuid, now=clock())
        if (reason := await run_in_threadpool(judged) or await _standing_refusal(resolver, record, claim, kind=kind, uuid=uuid)) is not None:
            raise await _noted(context.settings, kind=kind, uuid=uuid, reason=reason)
        revoked = await object_grants.revoke_removed_object(context, fga_ids.object_of(kind, uuid))
        await run_in_threadpool(partial(trash.end, context.settings.registry_root, context.settings.storage_options(), record, kind=kind, uuid=uuid))
    except Exception as failed:
        _audited(AuditAction.TRASH_END, AuditOutcome.FAILURE, context.token, kind=kind, uuid=uuid, reason=f"{type(failed).__name__}: {failed}"[:300])
        raise
    _audited(AuditAction.TRASH_END, AuditOutcome.SUCCESS, context.token, kind=kind, uuid=uuid, tuples_revoked=revoked)
    return TrashEndResponse(tuples_revoked=revoked)


@management_router.post("/{kind}/{uuid}/refusals")
async def note_trash_refusal(*, kind: TrashKind, uuid: str, body: TrashRefusalRequest, settings: SettingsDep) -> TrashRefusalResponse:
    """Note maintenance's own refusal of a trash record, so a record refused tick after tick sorts behind the ones that
    move. Bookkeeping the catalog never judges on."""
    await run_in_threadpool(trash_reclaim.read_record, settings, kind, uuid)
    attempts = await run_in_threadpool(partial(trash_reclaim.note_refusal, settings, kind=kind, uuid=uuid, reason=body.reason))
    return TrashRefusalResponse(attempts=attempts)


def _audited(action: AuditAction, outcome: AuditOutcome, token: Caller | None, *, kind: TrashKind, uuid: str, **fields: object) -> None:
    """One audit record of a trash door's act on the object ``kind`` and ``uuid`` name, for the caller that asked."""
    audit(action, outcome, subject=caller_subject(token), resource=fga_ids.object_of(kind, uuid), **fields)


async def _noted(settings: CatalogStore, *, kind: TrashKind, uuid: str, reason: str) -> InvalidTableStateError:
    """Note ``reason`` on the record, as maintenance's refusals are noted, and return the refusal for the door to raise."""
    await run_in_threadpool(partial(trash_reclaim.note_refusal, settings, kind=kind, uuid=uuid, reason=reason))
    return InvalidTableStateError(reason)


async def _unannounced_refusal(settings: CatalogStore, *, kind: TrashKind, uuid: str) -> str | None:
    """Why the record waits for the finish, or ``None``: a door's filing on the object still stands.

    A drop that ended after its detach is announced from its filing by the finish, which reads the drop's own record to
    tell it landed. Released, the record is the purge's to end, and a purge ending it first would leave the drop announced
    by nobody. The finish settles every filing whose door has stopped (``catalog.api.announcing.Announcer.finish``), so
    the wait lasts as long as its lease and one finish.

    Raises:
        ServiceUnavailableError: The store did not answer the listing.
    """
    try:
        filed = (
            await run_in_threadpool(
                partial(announcement_intents.standing, settings.registry_root, settings.storage_options(), target=fga_ids.object_of(kind, uuid))
            )
        ).filings
    except OSError as exc:
        raise store_refusal(exc, reading="what a door filed on the trashed object", so="nothing was released") from exc
    return "a door's announcements on the object are not sent yet; the finish sends them before its grants go" if filed else None


async def _standing_refusal(resolver: Resolver, record: TrashRecord, claim: LocationClaim | None, *, kind: TrashKind, uuid: str) -> str | None:
    """Why the trashed object stands again, or ``None``.

    A table with a place stands where its claim is held by a name that still resolves to the place: a crashed undrop
    leaves its record beside the table it restored, which a rename since carries along. An object with no place stands
    where its name resolves to an object holding the record's uuid. A drop kept in the trash holds its claim under a
    name that resolves nowhere, or elsewhere.
    """
    if claim is not None:
        return await _place_held(resolver, claim)
    segments = parse_identifier(record.id)
    if not segments:
        return None
    try:
        standing = await resolver.namespace_uuid(segments) if kind == "namespace" else (await resolver.table(segments)).uuid
    except TableNotFoundError, NamespaceNotFoundError:
        return None
    if standing != uuid:
        return None
    return f"the live {kind} {fga_ids.canonical_object_id(segments)!r} holds the record's uuid again, so the record is not the trash's"


async def _place_held(resolver: Resolver, claim: LocationClaim) -> str | None:
    """Why a live table holds the place ``claim`` is on, or ``None``."""
    if claim.released_at is not None:
        return None
    try:
        resolved = await resolver.table(claim.segments)
    except TableNotFoundError, NamespaceNotFoundError:
        return None
    if resolved.location is None or decoded_path(resolved.location) != decoded_path(claim.location):
        return None
    return f"the live table {fga_ids.canonical_object_id(claim.segments)!r} holds the record's place, so its bytes are not the trash's"


def _revoked(t: fga_writes.ClientTuple, record: TrashRecord, *, uuid: str, actor: str | None) -> Announcement:
    """The ``grant_revoked`` a release announces for ``t``, naming the released object by its name and uuids."""
    namespace_uuid = record.namespace_uuid
    return announcing.grant_announcement(
        t,
        action="grant_revoked",
        actor=actor,
        object_name=record.id,
        object_uuid=uuid,
        namespace_uuid=namespace_uuid if isinstance(namespace_uuid, str) else None,
        extra={"reason": "trash_expired"},
    )
