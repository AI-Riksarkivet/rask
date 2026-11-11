"""A clone's ending, filed by the door that ends it and finished on the catalog's own reads (ADR 0198 §1).

Every door that ends a registered clone (deregister, a destructive drop, a namespace's destroying cascade and a warehouse
cascade) files the clone's ending before its row goes (:func:`file_ending`), naming what the clone holds: its tag on
each source its base record names, and its location's claim. A destructive drop and a destroying cascade file it
through ``dataplane.destroy_table``'s ``before_removal``, once every judgement of the drop has passed, so a refusal files
nothing; a deregister and a warehouse cascade file it before they change anything. The door releases the holds as
before and removes the ending last (:func:`clear_ending`). A door whose native call failed leaves the ending, whatever
the failure: a describe cannot tell the clone's row from one the same name registered at the location since, so no door
judges whether its row went, and :func:`finish` completes or removes it. Maintenance asks for the finish from its hourly
audit, holding no write on a source's ``_refs/tags`` and gaining none; the catalog alone deletes a tag. The one ending a
door removes after a failure is the warehouse cascade's, when the data-directory delete that precedes its native call
fails: that request never touched the row.

THE FINISH JUDGES ON ITS OWN READS, never the caller's word, and acts only on the claim it read. Nothing happens until
a location claim's lease has passed since the door filed the ending, the time a door's own fence holds the claim, and
none while another finish's mark on it is younger than that. Then the location's claim:

- held for another uuid than the ending's: another table's location now (a registration between its claim and its
  refusal, :func:`refuse_registration_while_ending`), so nothing is released and the ending stays (409);
- held for the clone, and the clone stands there (:func:`catalog.services.table_claims.holder_stands`: it resolves to
  the location, or the trash keeps it there): the door either never removed it or is still at work (a destructive drop
  deleting a large table's bytes keeps its residue record meanwhile), so nothing is released and the ending stays (409);
  once :data:`STANDING_BOUND` has passed since the filing the clone is taken to stand for good, its own end owning its
  holds, and the ending is removed with nothing released, so a door that crashed before its native call is not asked
  about hourly for ever;
- held for the clone, which no longer stands: the finish FENCES the claim under the ETag it read
  (:func:`lakehouse.location_claims.fence`), so no registration takes the location while it acts, and a claim that
  moved since its read answers 409 (code 14) with nothing released; it releases that claim under the clone's uuid, so a
  claim anything else wrote there is never freed by it;
- released or absent: nothing to fence; a registration taking the location meanwhile refuses itself while the ending
  stands.

Then each source's tag is released through Lance's own tag API (a source whose dataset is gone holds none), the base
record is forgotten where the door meant to destroy the bytes and no dataset stands at the location any more (a drop that
failed with the bytes in place, or a deregister that ended the clone since, leaves the record with the bytes; a read the
store refuses fails the finish closed), the fenced claim is released, and the ending removed last: a
finish failing part-way lifts its fence and leaves the ending for the next.

Nothing here commits a table version or announces anything: the door's own tag release announced nothing either.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

from botocore.exceptions import ClientError
from lance_namespace import (
    ConcurrentModificationError,
    InvalidTableStateError,
    LanceNamespace,
    ServiceUnavailableError,
    TableNotFoundError,
)
from pydantic import BaseModel, Field

from catalog.core.config import Settings
from catalog.services import table_claims
from lakehouse import base_registry, clone_endings, clone_sources, location_claims, objectfs, records
from lakehouse.base_refs import decoded_path, key_uri, store_of
from lakehouse.base_registry import BaseRegistry
from lakehouse.clone_endings import CloneEnding, EndingIntent
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: How long after its filing an ending whose clone still stands is taken to be one its door left without removing the
#: clone: no door acts on one table for a day. Until then the finish leaves it, since the door may still be at work.
STANDING_BOUND: Final = timedelta(days=1)

#: The answer a registration gets while a clone's ending stands at its location.
_STILL_ENDING: Final = (
    "a clone's hold at this location is still being released: the end of the clone registered here before has not "
    "finished, and maintenance's hourly audit finishes it; register again once it has"
)

#: What the store and the records raise when a read or a write could not be made.
_UNREAD: Final = (OSError, ClientError, ValueError, base_registry.UnreadableBaseRecordError, records.RecordChangedError, records.RecordExistsError)


class FinishOutcome(BaseModel):
    """What one finish did: the sources a tag was released on, or that the clone stands for good and keeps them."""

    #: The clone the ending ended, by its canonical id and its uuid (``None`` for a table no uuid identifies).
    table: str
    uuid: str | None = None
    released: list[str] = Field(default_factory=list)
    kept: bool = False


class _Refused(Exception):
    """The finish leaves the ending, for the reason given."""


def file_ending(
    registry: BaseRegistry, claims: location_claims.ClaimStore, location: str, *, table: str, forget_record: bool, door: str, filing: str
) -> CloneEnding | None:
    """File the ending of ``table``, a clone at ``location`` its door is about to end; ``None`` for a table that is no
    clone, which holds no tag and needs none.

    Raises:
        ServiceUnavailableError: The clone's base record, its claim, or the ending could not be read or written: the door
            refuses before anything changes.
    """
    try:
        sources = base_registry.clone_source_locations(base_registry.read_base_record(registry, location))
        if not sources:
            return None
        claim = location_claims.holder(claims, location)
        uuid = claim.uuid if claim is not None and claim.table == table else None
        intent = EndingIntent(location=location, table=table, uuid=uuid, sources=sources, forget_record=forget_record, door=door)
        return clone_endings.file(claims, intent, filing=filing)
    except _UNREAD as exc:
        log.error("clone_ending_unfiled", extra={"table": table, "location": location, "door": door, "error": f"{type(exc).__name__}: {exc}"[:300]})
        raise ServiceUnavailableError(f"cannot end {table}: the record of what it holds as a clone could not be filed; nothing was changed") from exc


def clear_ending(claims: location_claims.ClaimStore, ending: CloneEnding) -> None:
    """Remove ``ending``, its holds released by its door. Logged, never raised: an ending left standing is finished by
    maintenance's audit, which finds the holds released already."""
    try:
        clone_endings.remove(claims, ending)
    except OSError as exc:
        log.warning("clone_ending_left_standing", extra={"location": ending.location, "error": f"{type(exc).__name__}: {exc}"[:300]})


def refuse_registration_while_ending(claims: location_claims.ClaimStore, location: str) -> None:
    """Refuse a registration at ``location`` while a clone's ending stands there.

    Asked once the registration holds the location's claim, so an ending filed after this read is filed by a door ending
    THIS table, and one removed before it was released in full: of a finish and a registration, the one that comes
    second sees the other. Adopting the ending instead would let its finish release the tags the new clone takes, which
    are named for the same location.

    Raises:
        ConcurrentModificationError: An ending stands at the location (409, code 14).
    """
    if clone_endings.stands_at(claims, location):
        log.warning("register_refused_clone_ending", extra={"location": location})
        raise ConcurrentModificationError(_STILL_ENDING)


def listing(settings: Settings) -> list[CloneEnding]:
    """Every ending standing, oldest first: one listing, and one read for each ending found."""
    claims = table_claims.claim_store(settings)
    endings = [found.ending for digest, filing in clone_endings.standing(claims) if (found := clone_endings.read(claims, digest, filing)) is not None]
    return sorted(endings, key=lambda ending: ending.filed_at)


def finish(ns: LanceNamespace, settings: Settings, digest: str, filing: str, *, token: str) -> FinishOutcome:
    """Finish the ending ``filing`` filed under ``digest``: release what its door named and remove it, on the claim this
    finish read and fenced (the module's docstring has each case).

    Raises:
        TableNotFoundError: No such ending stands (404, code 4).
        InvalidTableStateError: The ending is too young, another finish acts on it, or the location stands as another
            table's or as the clone's (409, code 19); the ending stays.
        ConcurrentModificationError: The ending or the claim changed under the finish (409, code 14); the ending stays.
        ServiceUnavailableError: A read or a release could not be made; the ending stays for the next finish.
    """
    claims = table_claims.claim_store(settings)
    found = clone_endings.read(claims, digest, filing)
    if found is None:
        raise TableNotFoundError(f"no clone's ending stands under {digest}/{filing}")
    ending = found.ending
    if found.at - ending.filed_at < location_claims.LEASE:
        raise InvalidTableStateError(f"the ending was filed less than {location_claims.LEASE} ago, so the door that filed it may still be acting")
    if ending.finishing is not None and found.at - ending.finishing.at < location_claims.LEASE:
        raise InvalidTableStateError("another finish is acting on the ending")
    try:
        clone_endings.mark(claims, found, token=token)
        outcome = _finished(ns, settings, claims, found, token=token)
    except clone_endings.EndingChangedError as exc:
        raise ConcurrentModificationError(f"the ending changed while it was being finished: {exc}") from exc
    except location_claims.ClaimChangedError as exc:
        raise ConcurrentModificationError(f"the clone's location changed while its ending was being finished: {exc}") from exc
    except _Refused as refused:
        raise InvalidTableStateError(str(refused)) from None
    except _UNREAD as exc:
        log.error("clone_ending_unfinished", extra={"location": ending.location, "error": f"{type(exc).__name__}: {exc}"[:300]})
        raise ServiceUnavailableError(f"the ending could not be finished, and stands for the next finish: {type(exc).__name__}") from exc
    log.info(
        "clone_ending_finished",
        extra={"location": ending.location, "table": ending.table, "door": ending.door, "released": outcome.released, "kept": outcome.kept},
    )
    return outcome


def _finished(ns: LanceNamespace, settings: Settings, claims: location_claims.ClaimStore, found: clone_endings.EndingRead, *, token: str) -> FinishOutcome:
    """Release what the ending ``found`` names on the claim read here, fenced for this finish, and remove the ending last.

    Raises:
        _Refused: The location stands as another table's, or as the clone's within :data:`STANDING_BOUND`.
        location_claims.ClaimChangedError: The claim changed since this finish read it.
    """
    ending = found.ending
    read_as = location_claims.read(claims, ending.location)
    claim = read_as.claim if read_as is not None and read_as.claim.released_at is None else None
    if claim is not None:
        if not ending.names(claim.uuid, claim.table):
            raise _Refused("another table holds the clone's location now; its registration refuses itself while the ending stands")
        if table_claims.holder_stands(ns, claims, claim):
            if found.at - ending.filed_at < STANDING_BOUND:
                raise _Refused("the clone still stands at its location: its door never removed it, or is still at work")
            clone_endings.remove(claims, ending)
            return FinishOutcome(table=ending.table, uuid=ending.uuid, kept=True)
        location_claims.fence(claims, ending.location, read_as, table=claim.table, segments=claim.segments, token=token, is_gone=lambda _claim: False)
    try:
        so = settings.storage_options()
        released = clone_sources.release_all(ending.sources, clone_location=ending.location, storage_options=so)
        if ending.forget_record and not _bytes_stand(ending.location, so):
            registry = BaseRegistry(control_root=settings.registry_root, storage_options=so)
            base_registry.forget_base_record(registry, ending.location, table=claim.table if claim is not None else ending.table)
        if claim is not None:
            location_claims.release(claims, ending.location, claim.table, uuid=claim.uuid)
        clone_endings.remove(claims, ending)
    except Exception:
        if claim is not None:
            location_claims.lift_fence(claims, ending.location, token)
        raise
    return FinishOutcome(table=ending.table, uuid=ending.uuid, released=released)


def _bytes_stand(location: str, so: StorageOptions) -> bool:
    """Whether a Lance dataset still stands at the clone's location, at the key it decodes to.

    ``forget_record`` is the filing door's intent to destroy the clone's bytes, and the base record goes with the bytes
    ([[LH-279]]: left behind, it would sanction its bases for the next table registered there) and stays with bytes that
    stand: a drop whose native call failed left them in place, and a deregister may have ended the clone since, keeping
    the record and its data directories' claims for them. So the record is forgotten on the evidence of the store, never
    on which door ran last.

    Raises:
        OSError: The store could not be read; the finish fails closed and the ending stays.
    """
    return objectfs.is_lance_dataset_root(key_uri(store_of(location), decoded_path(location)), so)
