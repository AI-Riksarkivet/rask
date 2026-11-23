"""How a table leaves the catalog at its own door: a drop into the trash, a drop that destroys its bytes, and a detach
that keeps them.

Each acts on the table the door's name resolved to, under the fence that holds the claim that resolution read (ADR 0173
§7), and settles what it filed where it fails. The door authorizes the caller, files what it announces and ends the
table's grants; nothing here reads a request or a grant.
"""

from __future__ import annotations

import logging
from functools import partial

from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError, DeregisterTableRequest, DeregisterTableResponse, DropTableResponse, LanceNamespace
from pydantic import BaseModel, ConfigDict, SkipValidation

from catalog.core import native
from catalog.core.config import CatalogStore
from catalog.services import clone_endings, table_claims
from catalog.services.clone_ending_records import CloneEnding
from catalog.services.table_claims import ResolvedTable
from catalog.services.tables import destroy
from lakehouse.events.lineage_events import DatasetUuids
from lakehouse.governed import fga_ids
from lakehouse.problems.ns_errors import PartiallyAppliedError
from lakehouse.problems.outage import reads_as_store_outage
from lakehouse.process.supervision import is_fault
from lakehouse.records import base_registry, location_claims, object_records, protection, trash
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)


async def _stamp_the_drop(
    ns: LanceNamespace, settings: CatalogStore, record: trash.TrashRecord, *, fenced: table_claims.DoorFence, segments: list[str]
) -> bool:
    """Stamp the trash record a recoverable drop filed as detached, once the detach returned and while the drop still holds
    its fence (`table_claims.holds_fence`); whether the fence is to be lifted.

    ONLY ONCE THE DETACH RETURNED: a record a drop filed before dying ahead of the detach describes a table still live,
    and must not prove that drop. Held, a record withdrawn meanwhile is filed again, stamped (`trash.stamp_detached`). A
    stamp the store refuses, or does not answer, is logged as which and the drop answers: its fence is left to lapse,
    which is how the finish tells this drop's detach (`Announcer.landed`).

    LOST, the detach having returned after the fence lapsed, the door answers the drop's success where its record stands
    stamped for it (the finish judged it), and where no record stands and the table resolves under no name: the finish
    withdrew the record while the detach was in flight, judging the table still registered, and the record is filed
    again, stamped, create-if-absent. That cannot keep a live table in the trash: an undrop restores only from a record,
    and none stands; a registration since makes the table resolve; a record another drop filed since makes the create
    fail. Anything else answers the conflict a lost fence answers, and is the finish's to judge.

    Raises:
        ConcurrentModificationError: The drop lost its fence, and its record is not stamped for it.
    """
    root, so, claims = settings.registry_root, settings.storage_options(), table_claims.claim_store(settings)
    if not await run_in_threadpool(table_claims.holds_fence, claims, fenced):
        kept = await run_in_threadpool(partial(trash.get, root, so, record.uuid or ""))
        if kept is not None and kept.drop == record.drop and kept.detached:
            return False
        refiled = (
            kept is None
            and not await run_in_threadpool(_still_registered, ns, claims, record, segments=segments)
            and await run_in_threadpool(partial(trash.stamp_detached, root, so, record, refile=True))
        )
        if refiled:
            return False
        raise _lost_turn(fga_ids.canonical_object_id(segments))
    try:
        stamped = await run_in_threadpool(partial(trash.stamp_detached, root, so, record, refile=True))
    except OSError as exc:
        reason = "the store did not answer the stamp" if reads_as_store_outage(exc) else "the store refused the stamp"
        log.warning("drop_unstamped", extra={"uuid": record.uuid, "drop": record.drop, "reason": reason, "error": type(exc).__name__})
        return False
    if not stamped:
        log.warning("drop_unstamped", extra={"uuid": record.uuid, "drop": record.drop, "reason": "another drop's record stands in its place"})
    return stamped


async def _settle_a_failed_drop(
    ns: LanceNamespace, settings: CatalogStore, record: trash.TrashRecord, *, fenced: table_claims.DoorFence, segments: list[str], detached: bool
) -> None:
    """Settle, on evidence only and only while the drop holds its fence, the trash record it filed before it failed,
    then lift the fence.

    Stamped where the detach returned (``detached``); withdrawn where the table still resolves, at this name or at the
    one its claim names, so the drop did not reach it. Otherwise whether the detach landed is unknown (it raised), and a
    drop that lost its fence touches nothing: the record and the claim are left, and the finish judges the record by the
    claim once the fence has lapsed (`Announcer.landed`), as it does where a read or write here fails.
    """
    claims = table_claims.claim_store(settings)
    root, so = settings.registry_root, settings.storage_options()
    try:
        if not await run_in_threadpool(table_claims.holds_fence, claims, fenced):
            settled = False
        elif detached:
            settled = await run_in_threadpool(partial(trash.stamp_detached, root, so, record, refile=True))
        elif await run_in_threadpool(_still_registered, ns, claims, record, segments=segments):
            settled = await run_in_threadpool(partial(trash.withdraw, root, so, record, claims=claims, token=fenced.token, margin=location_claims.DOOR_MARGIN))
        else:
            settled = False
    except Exception as exc:  # noqa: BLE001 — the drop's own failure is what the caller answers; the finish settles this
        log.warning("drop_unsettled", extra={"uuid": record.uuid, "drop": record.drop, "error": str(exc)[:300]})
        return
    if settled:
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)


def _still_registered(ns: LanceNamespace, claims: location_claims.ClaimStore, record: trash.TrashRecord, *, segments: list[str]) -> bool:
    """Whether the table ``record`` keeps resolves at ``segments``, or at the name its location's claim names now."""
    names = [segments]
    if (found := location_claims.read(claims, record.location)) is not None and found.claim.uuid == record.uuid and found.claim.segments != segments:
        names.append(found.claim.segments)
    return any((resolved := table_claims.resolve(ns, claims, name)).location is not None and resolved.uuid == record.uuid for name in names)


def _lost_turn(holder: str) -> ConcurrentModificationError:
    """What a drop answers once it no longer holds its fence: another request may have acted on the table since."""
    return ConcurrentModificationError(f"cannot drop {holder}: it changed while this request was being served; describe it and retry")


async def drop_resolved(
    ns: LanceNamespace,
    settings: CatalogStore,
    table: ResolvedTable,
    *,
    segments: list[str],
    table_uuid: str,
    known: DatasetUuids,
    grace: float,
    dropped_by: str | None,
    so: StorageOptions,
    drop: str | None,
) -> DropTableResponse:
    """Drop the table ``table`` resolved to: into the trash under a grace period, its record filed under the recoverable
    ``drop``'s id and stamped once the detach returned, else its bytes, under the fence that holds the claim its
    resolution read, which a recoverable drop sets under its id.

    A recoverable drop acts on its record and on the registration only while it holds its fence with
    ``location_claims.DOOR_MARGIN`` of the lease left (`table_claims.holds_fence`), and the finish judges a stopped drop
    only once that lease has passed. WHAT CANNOT BE CLOSED: the detach is a Lance manifest write, which cannot check the
    claim's fence, so a ``deregister_table`` call that outlasts the margin at the very end of the lease, with a finish
    pass and a second actor (an undrop, a rename, another drop) inside that window, can still interleave with them. It
    is bounded by the margin, by the door filing its record again where its detach landed and none stands
    (`_stamp_the_drop`), and by the alert on a filing left standing (``CatalogAnnouncementFilingStanding``). Accepted by
    the owner, 2026-11-20.
    """
    canonical = fga_ids.canonical_object_id(segments)
    trashed = grace > 0
    # THE DOOR ACTS ON THE TABLE ITS NAME RESOLVED TO (ADR 0173 §7, [[LH-521]]): the fence holds the claim that
    # resolution read, so a rename landing since answers this drop 409, and one landing after the fence answers 409
    # itself, rather than the drop reaching whatever table holds the name by then. The drop deletes the LOCATION, so it
    # also refuses while the claim names another table that still resolves to it ([[LH-204]]).
    claims = table_claims.claim_store(settings)
    fenced = await run_in_threadpool(partial(table_claims.fence, ns, claims, table, holder=canonical, verb="drop", token=drop))
    held = _FencedDrop(
        ns=ns,
        settings=settings,
        table=table,
        fenced=fenced,
        claims=claims,
        segments=segments,
        canonical=canonical,
        table_uuid=table_uuid,
        known=known,
        dropped_by=dropped_by,
    )
    if trashed:
        return await _drop_into_the_trash(held, grace=grace, drop=drop)
    return await _drop_and_destroy(held, so=so)


class _FencedDrop(BaseModel):
    """What both paths of one drop share: the table its name resolved to, the fence that holds it, and who drops it."""

    # `arbitrary_types_allowed` for `ns`: a LanceNamespace is no pydantic type.
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # A namespace connection is opaque: what the door was handed is what it uses, a stand-in in a test included.
    ns: SkipValidation[LanceNamespace]
    # The store settings are read for a few attributes, and a door test stands in a plain namespace object for them
    # (`test_an_undrop_announces_the_table_exists_again.py`), so they are taken as given.
    settings: SkipValidation[CatalogStore]
    table: ResolvedTable
    fenced: table_claims.DoorFence
    claims: location_claims.ClaimStore
    segments: list[str]
    canonical: str
    table_uuid: str
    known: DatasetUuids
    dropped_by: str | None


async def _drop_into_the_trash(held: _FencedDrop, *, grace: float, drop: str | None) -> DropTableResponse:
    """File the trash record of the table ``held`` fences, detach its row, and stamp the record once the detach returned."""
    location = held.fenced.location
    filed: trash.TrashRecord | None = None
    detached = stamped = False
    try:
        # RECORD FIRST, DETACH SECOND (diff2 F6 leg a). This was deregister-then-record, and a
        # crash in that window produced the worst state in the plane: the manifest row was gone,
        # no trash record existed, and the bytes sat on storage unreachable by everything.
        # `undrop` 404s (no record), the purge never sees it (it enumerates records), and the
        # obvious retry cannot converge either — `describe_table` 404s, so the door can never reach
        # the write again. Only a hand-run `register_table` recovers it.
        #
        # Reversed, the crash window leaves a record on a LIVE table, which every consumer already
        # handles: the purge refuses it `STILL_REGISTERED` (`live_ids` is checked before anything
        # destructive), the drop retry re-describes cleanly and overwrites the same hashed key,
        # and `undrop` converges on it now that the register tolerates `TableAlreadyExists`. The
        # residue is bounded by one retry; the old one was permanent.
        #
        # Every record this branch writes owns bytes, which is why it is sound HERE and why the cascade
        # path is not reversed with it: a byte-LESS record (a declared-only table, a namespace row) skips
        # the purge's estate test, so a record filed on a still-live object inside a DEACTIVATED
        # warehouse would be revoked and cleared.
        #
        # Known interaction, deliberate: while the record stands on a live table the maintenance
        # sweep excludes that table (F6 leg d). Bounded by the same retry, and the alternative is
        # losing the data outright.
        record = trash.make_record(
            held.canonical,
            uuid=held.table_uuid,
            location=location,
            dropped_by=held.dropped_by,
            grace_days=grace,
            namespace_uuid=held.known.namespace_uuid,
            drop=drop,
        )
        await run_in_threadpool(trash.put, held.settings.registry_root, held.settings.storage_options(), record)
        filed = record
        if not await run_in_threadpool(table_claims.holds_fence, held.claims, held.fenced):
            raise _lost_turn(held.canonical)
        await run_in_threadpool(native.call, held.ns, "deregister_table", DeregisterTableRequest(id=held.segments))
        detached = True
        stamped = await _stamp_the_drop(held.ns, held.settings, record, fenced=held.fenced, segments=held.segments)
        # The bytes are deleted later, by the purge, so the spec's answer is a transaction id to track the deletion
        # by (`spec.yaml` DropTableRequest): `<table id>$<its uuid>`, which DescribeTransaction answers from the record.
        response = DropTableResponse(transaction_id=fga_ids.canonical_object_id([*held.segments, held.table_uuid]))
    except Exception:
        # A trash record filed before a failure is settled under the fence, which stays where it could not be.
        if filed is not None:
            await _settle_a_failed_drop(held.ns, held.settings, filed, fenced=held.fenced, segments=held.segments, detached=detached)
        else:
            await run_in_threadpool(table_claims.lift_fence, held.claims, held.fenced)
        raise
    # [[LH-204]] The trashed id keeps its location's claim until undrop or purge, so no other id registers over
    # bytes someone can still restore. An unstamped record keeps the fence too, which the finish judges it by.
    if stamped:
        await run_in_threadpool(table_claims.lift_fence, held.claims, held.fenced)
    # A recoverable drop keeps its maintenance policy for the undrop, as it keeps its grants, and the trash purge ends
    # both. Its protection goes now, because the drop it guarded has happened.
    await run_in_threadpool(protection.clear_protection, held.settings.registry_root, held.settings.storage_options(), "table", uuid=held.table_uuid)
    return response


async def _drop_and_destroy(held: _FencedDrop, *, so: StorageOptions) -> DropTableResponse:
    """Destroy the table ``held`` fences: its row, its bytes, its base record, its location's claim and its records."""
    location = held.fenced.location
    registry = base_registry.BaseRegistry(control_root=held.settings.registry_root, storage_options=held.settings.storage_options())
    ending: CloneEnding | None = None
    try:
        # [[LH-532]] The bytes go at the location this door resolved, at the key Lance opens, never where the
        # backend's own drop looks: the backend removes only the row. RECORD FIRST here too: a residue record is filed
        # before the row goes and cleared once the bytes are, so bytes a failed delete leaves keep their claim. A clone's
        # ending is filed once every judgement has passed, right before the row goes (ADR 0198 §1), so a refusal files
        # none: its tags are released after the row is gone, by this door or, when that fails, by maintenance's audit.
        endings: list[CloneEnding] = []

        def _file_the_ending() -> None:
            if (
                found := clone_endings.file_ending(
                    registry, held.claims, location, table=held.canonical, forget_record=True, door="drop", filing=held.fenced.token
                )
            ) is not None:
                endings.append(found)

        response = await run_in_threadpool(
            partial(
                destroy.destroy_table,
                held.ns,
                so,
                held.segments,
                location=location,
                estate_roots=(held.settings.registry_root, held.settings.root),
                table_uuid=held.table_uuid,
                namespace_uuid=held.known.namespace_uuid,
                control=held.claims,
                dropped_by=held.dropped_by,
                before_removal=_file_the_ending,
            )
        )
        ending = endings[0] if endings else None
    except destroy.TableBytesLeftError:
        # The row is gone and bytes remain: the claim stays held, kept by the residue record once the fence lapses, and the
        # grants stay. The trash purge reclaims the objects; the drop's announcements wait in the door's filing, which the
        # finish sends, since the table is registered under no name ([[LH-576]]).
        raise
    except Exception:
        # An ending filed before a failure stays: a describe cannot tell whether the row the failure followed is still
        # the clone's, and the finish judges it on its own reads once the claim's lease has passed. A destroying drop
        # whose row went ends a stale record the table carried, as a deregister does.
        await run_in_threadpool(_end_stale_trash_record, held.ns, held.settings, held.fenced, table=held.table, segments=held.segments)
        await run_in_threadpool(table_claims.lift_fence, held.claims, held.fenced)
        raise
    # [[LH-279]] The base record goes with the bytes: left behind, it would sanction its bases for the next
    # table registered at this location. The location's claim goes too ([[LH-204]]), and the clone's ending last. A
    # declared-only table has no residue record to overwrite a stale one, so that goes first, under the fence.
    await run_in_threadpool(_end_stale_trash_record, held.ns, held.settings, held.fenced, table=held.table, segments=held.segments)
    await run_in_threadpool(partial(base_registry.forget_base_record, registry, location, uuid=held.table_uuid))
    await run_in_threadpool(partial(location_claims.release, held.claims, location, held.canonical, uuid=held.table_uuid))
    if ending is not None:
        await run_in_threadpool(clone_endings.clear_ending, held.claims, ending)
    # What was kept for the table under its uuid ends with it ([[LH-523]]); a table created later at this name holds
    # another uuid, so nothing left here could govern it.
    await run_in_threadpool(object_records.end_object_records, held.settings.registry_root, held.settings.storage_options(), "table", uuid=held.table_uuid)
    return response


async def detach(ns: LanceNamespace, settings: CatalogStore, table: ResolvedTable, *, segments: list[str]) -> DeregisterTableResponse:
    """Remove ``table``'s row from ``__manifest`` and release its location's claim; the bytes stay where they are.

    The door acts on the table its name resolved to, under the fence that holds the claim it read (ADR 0173 §7,
    [[LH-521]]): a rename landing since answers this deregister 409, as one landing after the fence answers itself.
    """
    canonical = fga_ids.canonical_object_id(segments)
    claims = table_claims.claim_store(settings)
    fenced = await run_in_threadpool(partial(table_claims.fence, ns, claims, table, holder=canonical, verb="deregister"))
    registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
    try:
        # A clone's ending is filed before its row goes (ADR 0198 §1), so a tag release that fails below is finished by
        # maintenance's audit rather than left for good: the row it would be re-issued against is gone by then. One filed
        # before a failed native call stays too: a describe cannot tell whether the row the failure followed is still the
        # clone's, and the finish judges it on its own reads once the claim's lease has passed.
        ending = await run_in_threadpool(
            partial(clone_endings.file_ending, registry, claims, fenced.location, table=canonical, forget_record=False, door="deregister", filing=fenced.token)
        )
        response: DeregisterTableResponse = await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
    except Exception:
        # A detach whose answer was lost may have landed: the stale record goes where the row is gone.
        await run_in_threadpool(_end_stale_trash_record, ns, settings, fenced, table=table, segments=segments)
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        raise
    await run_in_threadpool(_end_stale_trash_record, ns, settings, fenced, table=table, segments=segments)
    # The detached bytes are nobody's now, so the version a clone among them was promised on its sources goes (ADR 0198
    # §1), and then the id's claim on them ([[LH-204]]): the base record stays, for the bytes it describes. The tags go
    # first, while the fence still holds the location, so no clone registered there meanwhile can hold a tag of the same
    # name, and a tag that cannot be deleted leaves the location held rather than free with the tag standing.
    try:
        await run_in_threadpool(partial(base_registry.release_clone_sources, registry, fenced.location, uuid=table.uuid))
    except (OSError, ValueError, base_registry.UnreadableBaseRecordError) as exc:
        log.error("deregister_clone_tags_kept", extra={"table": canonical, "location": fenced.location, "error": f"{type(exc).__name__}: {exc}"[:300]})
        raise PartiallyAppliedError(
            f"table {canonical} was detached, and its tag on a table it clones could not be deleted: its location stays held, and "
            "maintenance's hourly audit releases the tag and the location from the record of the clone's end this door filed",
            extensions={"table": canonical, "detached": True},
        ) from exc
    await run_in_threadpool(location_claims.release, claims, fenced.location, canonical)
    if ending is not None:
        await run_in_threadpool(clone_endings.clear_ending, claims, ending)
    return response


def _end_stale_trash_record(ns: LanceNamespace, settings: CatalogStore, fenced: table_claims.DoorFence, *, table: ResolvedTable, segments: list[str]) -> None:
    """End a trash record the table carried while live, once a door that retires it out of governance (a deregister, a
    destroying drop) has removed its row, and only while that door holds its fence. Logged, never raised: the door's own
    answer stands.

    The fence found the table registered, so a record of its uuid is stale: one an undrop left that ended before
    clearing it, or one a recoverable cascade filed and failed to detach after. Left once the row is gone, nothing
    resolves to its location, so the purge would delete the bytes a deregister keeps and the release revoke the grants.
    The row is judged gone as the drop judges it (`_still_registered`: at this name or the one its claim names), so a
    detach whose answer was lost ends the record where it landed and touches nothing where it did not. No drop files a
    record past the fence, so none can land between the read and the delete. A residue record is a destroying drop's,
    and is that drop's to clear.
    """
    if table.uuid is None:
        return
    root, so, claims = settings.registry_root, settings.storage_options(), table_claims.claim_store(settings)
    try:
        kept = trash.get(root, so, table.uuid)
        if kept is not None and not kept.is_residue and not _still_registered(ns, claims, kept, segments=segments) and table_claims.holds_fence(claims, fenced):
            trash.clear(root, so, table.uuid)
    except BaseException as exc:  # noqa: BLE001 — the door's answer stands; a record left is reported, not raised, a native panic too
        if not is_fault(exc):
            raise
        log.error("stale_trash_record_kept", extra={"uuid": table.uuid, "error": f"{type(exc).__name__}: {exc}"[:300]})
