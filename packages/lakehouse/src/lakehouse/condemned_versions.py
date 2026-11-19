"""The one way the estate reclaims a table's versions: a reclaim CONDEMNS before it deletes, and a door PINS before it
reads (ADR 0246, ``specs/maintenance/Reclaim.tla``).

LANCE'S CLEANUP DELETES A VERSION'S FILES BEFORE ITS MANIFEST, AND COUNTS A REFUSED DELETE. Measured on pylance 13.0.0
(ADR 0243 §3): one ``cleanup_old_versions`` deletes the transaction files, the data files, each data file's blob sidecars,
and the manifests last, and returns with ``CleanupStats.failed_deletes`` counting what the store refused. A cleanup that
stops part-way leaves versions standing over deleted files. And it reads a table's tags once, before its manifests
(measured on pylance 13.0.0: a tag written after that read names a version it then deletes, ADR 0246 §5), an order
``lance_docs/`` does not promise.

THE RECORD (:class:`Record`). One per table root on the control root (:func:`record_key`), written only by the catalog,
each write conditional on the ETag it read (:func:`_rewrite`). An entry names a version by the IDENTITY of its ref (main,
or one incarnation of a branch: the uuid Lance lists last in its ``branch_identifier``, so a branch deleted and made
again under its name is another ref), the version's number, its state (``c`` condemned, or ``g``: a batch passed its
pin read and its policy read and may have begun deleting) and WHO condemns it: its owner, the ``version/delete`` call
that marked it, and the live batches claiming it (:class:`Claim`). Every ref door refuses a version an entry names. An
entry is dropped once its manifest is gone or its ref's identity no longer stands (GCOnlyGone), and a ``c`` entry once
no one condemns it any more: no owner, and every batch that claimed it withdrew (a hold, a tag or a pin), promoted it or
died and saw its claim lapse (:data:`CLAIM`). Nothing was deleted under a ``c`` entry; ``g`` and an owner's mark are
final while the manifest stands, and the next reclaim finishes them.

THE RECORD BELONGS TO THE BYTES, NOT TO THE CATALOG OBJECT (RecordKept, LocationKeyed; ADR 0246 §16). It is filed by the
table's root, the location Lance's cleanup deletes under, keyed as that location's claim is, so a deregister leaves it
(the versions it names, half-removed ones among them, stay where they are), a register at the location reads the record
already there, and it ends only when the bytes do (:func:`end`: a drop's purge, a warehouse's, the trash's). Every door
reads the record of the location it acts on. An entry of main names a version NUMBER at the location, whoever registered
it, since main's versions are the same bytes across a deregister and a register; a branch's names one incarnation.

ONE ENTRY PER VERSION (TakeOver), MANY CONDEMNERS. A batch condemning a version an entry already names adds its claim
to that entry instead of adding one, and ``version/delete`` sets its owner mark there, so the record holds as many
entries as versions stand condemned, however many reclaims pass over a stuck one; and each condemner withdraws only
itself, so none undoes another's condemn (ADR 0246 §17). What a batch then does with an entry is
:meth:`Batch.promote`'s rule. The record is bounded by the versions standing condemned, each carrying at most the claims
of the batches alive at once (a promotion spends a claim, a withdrawal removes it, a dead batch's lapses).

A RECLAIM BATCH (:class:`Batch`, at most :data:`BATCH` versions; every reclaim in the estate goes through it, a pass,
the catalog's ``maintenance/run``, an erasure's verification and its release). A reclaimer reads its ref's identity
(:func:`identity_of`) before it opens the handle it lists the versions it asks for on, and names it with every batch; a
batch of a ref that is now another incarnation, or whose registration no longer holds the location, is refused, nothing
condemned (ListedOn, :class:`ListedElsewhereError`): that is who may condemn, a question apart from what an entry names.

1. :meth:`Batch.condemn` (CondemnFirst) writes the batch's entries, ``c``, and claims every entry already naming one of
   its versions: the versions asked for, and every standing version of the ref an entry already names, which a dead
   batch or ``version/delete`` left;
2. :meth:`Batch.read_pins` (ExplicitPins) lists the table's tags, user tags and pins (:data:`PIN_PREFIX`): a version one
   names is excluded, unless an entry names it ``g`` (GoOnly: another batch's promotion, whose pin read came first). A
   pin older than :data:`PIN_LIFETIME` is a dead door's, and is deleted first (A3');
3. the caller reads the policy that governs the table, level by level (PolicyAfterCondemn);
4. :meth:`Batch.promote`: under a hold it withdraws its claim from every entry it claims, and from those of excluded
   versions otherwise (an entry no one condemns any more goes with it; nothing was deleted under it); the rest it claims
   are set ``g`` under its licence in the same conditional write, and the pins standing on another batch's ``g``
   versions are deleted (GoUnpins: a door that pinned one of those after that batch's pin read reads its
   entry and refuses, so the pin protects nothing, and Lance keeps every tagged version, so leaving it would keep a
   half-removed version so for good).

The reclaimer then runs ``cleanup_old_versions(versions=..., error_if_tagged_old_versions=False)`` on what
:meth:`Batch.promote` answered (:func:`reclaim`): with the default, a pin written between the batch's pin read and
Lance's own tag read makes the whole call raise (measured). A batch whose deletes the store refused keeps its entries
(KeepAfterDelete).

A LICENCE ENDS AT A STATED INSTANT (ADR 0246 §14). ``cleanup_old_versions(versions=...)`` names NUMBERS and deletes the
versions of those numbers under the path the handle names, and a branch deleted and made again under its name repeats
its numbers (measured on pylance 13.0.0: a cleanup through a handle of the deleted incarnation removed the versions 3 and 4
of the one made again). So a batch's promotion stamps its entries ``licensed_until`` (:data:`LICENCE` from now), the
reclaimer deletes only under a credential minted before it asked and refused by the store by then (:func:`covers`, C),
a branch create under a name a live licence names is refused (:func:`licensed_under`, NameGuard), a deleted
incarnation's licensed entries outlive it until their licence passes (GCAfterLicence), and a branch's batch reads the
branches after its promotion and licenses nothing where its incarnation went (StandsAfterGo).

A REF DOOR (a tag create or update, a branch create, a restore, a clone's source tag, an erasure release's recreate
point) acts on a version NUMBER, never "the latest", and writes a pin, a Lance tag named ``rask-pin-<32 hex>`` (an
erasure release's own are ``rask-release-<32 hex>``, which no sweep takes), on it (:func:`pin`, PinFirst), then reads the
record and refuses a version an entry names (:func:`refuse_condemned`), then acts, then deletes its pin (:func:`unpin`).
Each side writes, then reads the other's write, so on a linearizable store at least one sees the other (B1, measured on
RustFS 1.0.3). No vended credential can put or delete under ``_refs/`` (B2, measured) and the tag doors refuse the
``rask-`` prefix from callers, so a pin is the catalog's alone. No lease and no renewal decides what a reclaim deletes; a clock
bounds only how long a licence names a branch's name (above).

A DEAD DOOR'S PIN (A3'). A door unpins in a ``finally``; one whose process died leaves its pin, which on a version not
condemned keeps that version whole. A batch deletes a pin older than :data:`PIN_LIFETIME` by the tag's ``created_at``
against this process's clock, two catalog pods' clocks apart, and a door refuses to act once :data:`DOOR_DEADLINE` has
passed since it pinned, by its own monotonic clock, so a pin is swept only once its door can no longer act
(``ReclaimSweep.cfg`` holds under that, ``ReclaimPinSweepLive.cfg`` fails without it). Counted
(``lance.reclaim.pins_swept``).

Every function is blocking IO.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field, SkipValidation, ValidationError

from lakehouse import control_prefixes, location_claims, record_store, records, stored_objects
from lakehouse.storage_options import StorageOptions, object_client, on_s3
from lakehouse.version_retention import branch_identity, recorded_branch


if TYPE_CHECKING:
    import lance


log = logging.getLogger(__name__)
_meter = metrics.get_meter("lance.app")
_swept = _meter.create_counter(
    "lance.reclaim.pins_swept",
    unit="{pin}",
    description="Pins a reclaim batch deleted as a dead door's: older than a door's lifetime, left by a door whose process died.",
)

#: The most versions one batch condemns and one cleanup call removes: Lance plans each call by reading the ref's retained
#: manifests, so a call per version would read them once per version; and a batch in flight is what a hold answered
#: beside it may still lose (ADR 0246 §9).
BATCH: Final = 16

#: The prefix of every tag name the catalog keeps for itself: the tag doors refuse it from a caller and leave it out of a
#: listing, so no caller plants, moves, lifts or reads one of its pins.
RESERVED_PREFIX: Final = "rask-"
#: The prefix of a door's pin: a Lance tag the catalog writes on a version for the length of one door's request.
PIN_PREFIX: Final = "rask-pin-"
_PIN_NAME: Final = re.compile(r"rask-pin-[0-9a-f]{32}")
#: The prefix of an erasure release's pin on where its branches come back: a kind the sweep of dead doors' pins never
#: takes, since a release keeps no door's deadline and resumes its recorded plan whenever its tick runs; the release
#: removes it at its finish or its abandonment, and nothing else does (ADR 0246, ``ReclaimReleaseSwept.cfg``).
RELEASE_PIN_PREFIX: Final = "rask-release-"

#: The longest a credential any reclaimer deletes under lives: the maintain vend's cap and the catalog's own reclaim
#: sessions both (``catalog.api.v1.endpoints.credentials``, ``catalog.services.reclaims``); STS issues no session shorter
#: (RustFS 1.0.3 raises a shorter one to 900 s, measured 2026-11-18).
CREDENTIAL_LIFETIME: Final = timedelta(minutes=15)
#: How far the store's clock and the catalog's may disagree (assumption C of ``Reclaim.tla``): a licence's instant
#: carries it twice, once for the expiry the store stamped on the credential and once for the store judging it.
CLOCK_MARGIN: Final = timedelta(minutes=1)
#: How long a batch's licence to delete runs from its promotion: a credential minted before the batch was asked, and so
#: before its promotion, is refused by the store by then.
LICENCE: Final = CREDENTIAL_LIFETIME + 2 * CLOCK_MARGIN

#: How old a pin is, by the tag's ``created_at``, before a batch takes it for a dead door's and deletes it (A3').
PIN_LIFETIME: Final = timedelta(hours=24)

#: How long after writing its pin a door may still act, by its own monotonic clock: half of :data:`PIN_LIFETIME`, so the
#: clocks of two catalog pods may disagree by hours before a live door's pin is swept.
DOOR_DEADLINE: Final = PIN_LIFETIME / 2

#: The identity of main in an entry. A branch's is the uuid of its incarnation.
MAIN: Final = "main"

#: How long a batch's claim on an entry stands from its condemn, by the catalog's clock: a batch runs from its condemn
#: to its promotion in a few record and tag round trips (seconds, ADR 0246 §6), so a claim past it is a dead batch's,
#: which the next rewrite drops. A live batch whose claim was dropped promotes and deletes nothing of that version
#: (:meth:`Batch.promote` acts only on an entry it still claims), so the instant bounds the record and decides no delete.
CLAIM: Final = timedelta(minutes=10)

#: How many read-and-write rounds a rewrite of the record runs. Measured on RustFS 1.0.3 (ADR 0246 §5): 16 batches
#: condemning on one record at once took 1.2 s, and one batch at most 13 retries.
_ATTEMPTS: Final = 32


def is_reserved(name: str) -> bool:
    """Whether the tag ``name`` carries the prefix the catalog keeps for its own tags, which only it writes or deletes."""
    return name.startswith(RESERVED_PREFIX)


def is_door_pin(name: str) -> bool:
    """Whether the tag ``name`` is a door's pin, which a batch may take for a dead door's or licence deleting."""
    return _PIN_NAME.fullmatch(name) is not None


class Claim(BaseModel):
    """A live batch's claim on an entry: it condemned the version and has not withdrawn or promoted it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch: str
    #: The instant (the claiming catalog's clock) after which the next rewrite of the record drops the claim: a batch
    #: that died leaves it, and it condemns for nobody once that batch can no longer act on it.
    until: datetime


class Entry(BaseModel):
    """One condemned version: the record holds at most one per ``(ref_id, version)``, and records separately WHO
    condemns it: an owner (``version/delete``), and the live batches claiming it (:meth:`Batch.promote`'s table)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The identity of the version's ref: :data:`MAIN`, or the uuid of one incarnation of a branch. Main's entry names the
    #: version's number at the table's location, whichever registration holds it.
    ref_id: str
    #: The branch's name when it was condemned, ``None`` for main: where a reader opens it.
    ref: str | None = None
    version: int
    state: Literal["c", "g"]
    #: The ``version/delete`` call (``vd-<hex>``) that marked the version deleted: an owner's condemn is final, and no
    #: batch's hold or exclusion removes it; the entry goes only once its manifest is gone (:func:`_rewrite`'s GC).
    owner: str | None = None
    #: The live batches that condemned it and have neither withdrawn (a hold, a tag or a pin) nor promoted it, each with
    #: the instant its claim lapses: at most the batches alive at once over this version.
    claims: tuple[Claim, ...] = ()
    #: For ``g``, the instant (the catalog's clock) after which no reclaimer a batch licensed on it can delete any more:
    #: each credential was minted before its batch was asked and lives at most :data:`CREDENTIAL_LIFETIME`, and a batch
    #: re-licensing the entry keeps the later of its own instant and the one there.
    licensed_until: datetime | None = None

    def claimed_by(self, batch: str) -> bool:
        """Whether ``batch`` holds a claim on the entry: it has, from its condemn on, since a removed entry takes every
        claim with it and a batch claims only at its condemn."""
        return any(claim.batch == batch for claim in self.claims)

    def without(self, batch: str) -> Entry:
        """The entry with ``batch``'s claim removed."""
        return self.model_copy(update={"claims": tuple(claim for claim in self.claims if claim.batch != batch)})

    def condemns(self) -> bool:
        """Whether anyone still condemns the version: it is ``g`` (deletion may have begun), an owner marked it, or a
        batch claims it. A ``c`` entry with none of them is removed by the rewrite that left it so."""
        return self.state == "g" or self.owner is not None or bool(self.claims)


class Record(BaseModel):
    """The condemned versions of the table whose root is :attr:`location`."""

    model_config = ConfigDict(extra="forbid")

    #: The table's root, as its claim is keyed: what the record is filed by, and where a policy door finds the name that
    #: holds it now.
    location: str
    entries: list[Entry] = Field(default_factory=list)


class CondemnedVersionError(Exception):
    """The version a door names is condemned: a reclaim may be deleting it."""


class RefGoneError(ValueError):
    """The branch a batch names does not stand."""


class ListedElsewhereError(Exception):
    """The ref a batch names is another incarnation than the one its versions were listed on: retry, listing again."""


class Verdict(BaseModel):
    """What a batch answered its reclaimer."""

    #: What keeps the table's history whole, when a hold does: nothing may be deleted.
    held_by: str | None = None
    #: The versions the reclaimer may delete now, each named ``g`` by this batch's entry.
    versions: list[int] = Field(default_factory=list)
    #: The versions of the ref still condemned that this batch does not license: pinned, under a hold, or a version a
    #: branch is cut from, whose manifest Lance keeps.
    standing: list[int] = Field(default_factory=list)
    #: When ``versions`` is not empty, the instant after which the licence ends: a reclaimer deletes them only under a
    #: credential the store refuses by then (:func:`reclaim`).
    licensed_until: datetime | None = None


def record_key(location: str) -> str:
    """The key, on the control root, of the condemned versions of the table whose root is ``location``: filed by the
    bytes, as the location's claim is (the SHA-256 of the path it decodes to, in either spelling,
    :func:`lakehouse.location_claims.claim_digest`), never by the uuid of the registration that holds them now. On the
    control root, which no vended table credential reaches, so no writer of the table can change or remove it."""
    return f"{control_prefixes.CONDEMNED}/{location_claims.claim_digest(location)}.json"


def read(control_root: str, storage_options: StorageOptions, location: str) -> tuple[list[Entry], str | None]:
    """The entries of the table at ``location`` and the ETag they were read under (:func:`read_record`)."""
    stored, etag = read_record(control_root, storage_options, location)
    return ([] if stored is None else stored.entries), etag


def read_record(control_root: str, storage_options: StorageOptions, location: str) -> tuple[Record | None, str | None]:
    """The record of the table at ``location`` and the ETag it was read under, both ``None`` for no record.

    Raises:
        OSError: The store did not answer, or the record does not parse as this location's: fail closed, since only the
            catalog writes it and an entry it cannot read may name a half-removed version.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    key = record_key(location)
    try:
        stored = records.read_json_at(control_root, storage_options, key).stored
        if stored is None:
            return None, None
        parsed = Record.model_validate(stored.record)
    except (ClientError, BotoCoreError) as exc:
        raise OSError(f"the store did not answer reading {key}: {exc}") from exc
    except (ValueError, ValidationError) as exc:
        raise OSError(f"{key} does not parse as a table's condemned versions: {exc}"[:600]) from exc
    if location_claims.claim_digest(parsed.location) != location_claims.claim_digest(location):
        raise OSError(f"{key} names the table at {parsed.location!r}, not {location!r}")
    return parsed, stored.etag


def end(control_root: str, storage_options: StorageOptions, location: str) -> bool:
    """End the record of the table at ``location``, once its bytes are gone (a drop's purge, a warehouse's, the
    trash's): whether one stood. Never by a deregister, which leaves the bytes, half-removed versions among them, for the
    next registration at the location to read. Idempotent.

    Raises:
        OSError: The store refused the delete.
    """
    return record_store.delete_record(control_root, storage_options, record_key(location))


def recorded(control_root: str, storage_options: StorageOptions, location: str) -> bool:
    """Whether the record of the table at ``location`` stands, from one listing of its key alone, which a session that
    may list the control root and read none of it (maintenance's own) can ask: a record stands only while it names a
    condemned version.

    Raises:
        OSError: The store did not answer.
    """
    key = record_key(location)
    if not on_s3(control_root):
        return os.path.exists(os.path.join(control_root.removeprefix("file://").rstrip("/"), key))
    from botocore.exceptions import BotoCoreError, ClientError

    bucket, _, prefix = control_root.removeprefix("s3://").rstrip("/").partition("/")
    full = f"{prefix}/{key}" if prefix else key
    try:
        listed = object_client(storage_options).list_objects_v2(Bucket=bucket, Prefix=full, MaxKeys=1)
    except (ClientError, BotoCoreError) as exc:
        raise OSError(f"the store did not answer listing {full}: {exc}") from exc
    return any(str(entry.get("Key")) == full for entry in listed.get("Contents", []))


class Standing(BaseModel):
    """The condemned records standing on the control root: each table with a reclaim in flight or stuck."""

    records: list[Record] = Field(default_factory=list)
    #: The keys listed whose record could not be read or does not parse: each may name a version of any table.
    unread: list[str] = Field(default_factory=list)


def every_record(control_root: str, storage_options: StorageOptions) -> Standing:
    """Every record standing, from one listing of the prefix and a read of each record listed. A record stands only while
    its table has entries (:func:`_rewrite` deletes it with its last), so this reads the tables with a reclaim in flight
    or stuck, not every table that was ever reclaimed. A record that cannot be read or parsed is named, not raised.

    Raises:
        OSError: The store did not answer the listing.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        listed = stored_objects.list_under(f"{control_root.rstrip('/')}/{control_prefixes.CONDEMNED}", storage_options)
    except (ClientError, BotoCoreError) as exc:
        raise OSError(f"the store did not answer listing {control_prefixes.CONDEMNED}: {exc}") from exc
    found = Standing()
    for stored in listed:
        key = f"{control_prefixes.CONDEMNED}/{stored.path.rsplit('/', 1)[-1]}"
        try:
            read_at = records.read_json_at(control_root, storage_options, key).stored
            if read_at is not None:
                found.records.append(Record.model_validate(read_at.record))
        except (ClientError, BotoCoreError, ValueError, ValidationError) as exc:
            log.warning("condemned_record_unread", extra={"key": key, "error": f"{type(exc).__name__}: {exc}"[:300]})
            found.unread.append(key)
    return found


def _rewrite(control_root: str, storage_options: StorageOptions, location: str, change: Callable[[list[Entry]], list[Entry]]) -> list[Entry]:
    """Apply ``change`` to the entries as read and write them back under the ETag read (create-if-absent for none); the
    entries the landed write holds. ``change`` may run once per attempt, on the record as each attempt read it.

    Every rewrite also drops each claim past its instant (a dead batch's, :data:`CLAIM`) and each entry no one condemns
    any more (:meth:`Entry.condemns`), so the record holds one entry per version standing condemned and, on each, at
    most the claims of the batches alive at once. A reader never judges a claim's instant: an entry stands for it until
    a rewrite drops it.

    A record whose last entry goes is deleted under the ETag read, so a table has a record only while it has entries: the
    records standing are the tables with a reclaim in flight or stuck, which a policy door reads (:func:`every_record`).

    Raises:
        OSError: The store did not answer, or the record does not parse.
        records.RecordChangedError: Every attempt lost to another writer.
    """
    key = record_key(location)
    for _ in range(_ATTEMPTS):
        entries, etag = read(control_root, storage_options, location)
        changed = _settled(change(list(entries)), datetime.now(UTC))
        if changed == entries and (changed or etag is None):
            return entries
        body = Record(location=location, entries=changed).model_dump(mode="json")
        try:
            if etag is None:
                records.create_json(control_root, storage_options, key, body)
            elif not changed:
                records.delete_json(control_root, storage_options, key, etag=etag)
            else:
                records.replace_json(control_root, storage_options, key, body, etag=etag)
        except (records.RecordExistsError, records.RecordChangedError):
            continue
        return changed
    raise records.RecordChangedError(f"{_ATTEMPTS} attempts to rewrite {key} each lost to another writer")


def _settled(entries: list[Entry], now: datetime) -> list[Entry]:
    """``entries`` with every lapsed claim dropped, and every entry no one condemns any more with it (ClaimLapse)."""
    out: list[Entry] = []
    for entry in entries:
        live = tuple(claim for claim in entry.claims if claim.until > now)
        kept = entry if live == entry.claims else entry.model_copy(update={"claims": live})
        if kept.condemns():
            out.append(kept)
    return out


def _refs(dataset: lance.LanceDataset) -> dict[str | None, str]:
    """Every ref of the table standing, main as ``None``, each with its identity, from one listing of its branches."""
    listed: Mapping[str, Any] = dataset.branches.list()
    return {None: MAIN, **{str(name): ident for name, entry in listed.items() if (ident := branch_identity(entry)) is not None}}


def identity_of(dataset: lance.LanceDataset, ref: str | None, *, table_uuid: str) -> str:
    """The identity of ``ref`` as it stands now, which a reclaimer reads BEFORE it opens the handle it lists its victims on
    and names with every batch it asks for (ListedOn): ``table_uuid`` for main, the registration that holds the location
    (a table deregistered and registered again there is another uuid, which may condemn only once a reclaimer has read
    it), and for a branch the uuid of its incarnation, from one listing of the branches through ``dataset``, a handle of
    the table.

    Raises:
        RefGoneError: The branch does not stand.
    """
    ref = recorded_branch(ref)
    if ref is None:
        return table_uuid
    found = _refs(dataset).get(ref)
    if found is None:
        raise RefGoneError(f"branch {ref!r} not found")
    return found


def _require_listed_on(listed_on: str, standing: str, ref: str | None) -> None:
    """Refuse a batch whose versions were listed on another incarnation of ``ref`` than the one standing (ListedOn):
    Lance numbers each incarnation from its fork, so a branch deleted and made again under its name repeats numbers, and
    the numbers listed on one would condemn the other's, versions nobody chose (``ReclaimListedElsewhere.cfg``).

    Raises:
        ListedElsewhereError: ``listed_on`` is not the identity standing.
    """
    if listed_on != standing:
        raise ListedElsewhereError(
            f"the versions were listed on {recorded_branch(ref) or MAIN} as {listed_on!r}, which now stands as {standing!r}: list them again"
        )


def registration_at(claims: location_claims.ClaimStore, location: str) -> str | None:
    """The uuid of the registration that holds ``location`` now, by its claim; ``None`` where none does: what a batch and
    ``version/delete`` read at their condemn (:class:`Batch`'s ``registered``).

    Raises:
        OSError: The claim could not be read, or does not parse: whether a registration stands is unknown.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        held = location_claims.holder(claims, location)
    except (ClientError, BotoCoreError, ValueError) as exc:
        raise OSError(f"the claim on {location} could not be read: {exc}"[:600]) from exc
    return None if held is None else held.uuid


def _require_registered(registered: Callable[[], str | None], table_uuid: str) -> None:
    """Refuse a batch asked for the registration ``table_uuid`` once ``registered`` reads another, or none (ListedOn, read
    at the condemn): who may condemn is the registration a reclaimer read, a question apart from what an entry names,
    which for main is a number at the location whoever holds it (``ReclaimReregister.cfg``).

    Raises:
        ListedElsewhereError: Another registration holds the location, or none does.
        OSError: The registration could not be read.
    """
    if (now := registered()) != table_uuid:
        raise ListedElsewhereError(
            f"the batch was asked for registration {table_uuid!r} of the table's location, which {'no registration' if now is None else repr(now)} "
            "holds now: list the versions again"
        )


def _forks(dataset: lance.LanceDataset) -> set[tuple[str | None, int]]:
    """``(ref, version)`` of every version a standing branch is cut from, whose manifest Lance keeps for it."""
    return {
        (recorded_branch(entry.get("parent_branch")), int(entry["parent_version"]))
        for entry in dataset.branches.list().values()
        if isinstance(entry, dict) and isinstance(entry.get("parent_version"), int)
    }


def _versions_of(dataset: lance.LanceDataset, ref: str | None) -> set[int]:
    """The versions whose manifests stand on ``ref``, one listing of its ``_versions/``."""
    handle = dataset if ref is None else dataset.checkout_version((ref, None))
    return {int(entry["version"]) for entry in handle.versions()}


def condemned(control_root: str, storage_options: StorageOptions, *, location: str, dataset: lance.LanceDataset, ref: str | None) -> set[int]:
    """The versions of ``ref`` (main for ``None``) of the table at ``location`` as it stands now that an entry names: every
    ref door refuses them, and ``list versions`` leaves them out. One read of the location's record, whichever
    registration wrote it, and one listing of the branches for a branch.

    Raises:
        OSError: The store did not answer, or the record does not parse: whether a version is condemned is unknown.
    """
    entries, _etag = read(control_root, storage_options, location)
    if not entries:
        return set()
    ref = recorded_branch(ref)
    identity = MAIN if ref is None else _refs(dataset).get(ref)
    return {entry.version for entry in entries if identity is not None and entry.ref_id == identity}


def licensed_under(control_root: str, storage_options: StorageOptions, *, location: str, name: str, now: datetime | None = None) -> datetime | None:
    """The latest instant a live licence names a version under the branch name ``name`` of the table at ``location``,
    ``None`` where none does: asked by a create that read the name free, so every such licence is a deleted
    incarnation's (NameGuard, ADR 0246), granted under whichever registration held the location.

    A licence names numbers under a path, and Lance's cleanup deletes by number under the path a branch's name holds when
    it runs (measured on pylance 13.0.0: a cleanup through a handle of a deleted incarnation removed the versions 3 and 4
    of the incarnation made again under its name), so the name is not made again while one runs.

    Raises:
        OSError: The store did not answer, or the record does not parse.
    """
    entries, _etag = read(control_root, storage_options, location)
    now = now or datetime.now(UTC)
    return max((until for entry in entries if entry.ref == name and (until := _live_until(entry, now)) is not None), default=None)


def _live_until(entry: Entry, now: datetime) -> datetime | None:
    """The instant ``entry``'s licence ends, where it carries one that has not."""
    return entry.licensed_until if entry.state == "g" and entry.licensed_until is not None and entry.licensed_until > now else None


# ---------------------------------------------------------------- the pins


def pin_name() -> str:
    """A fresh door pin's tag name."""
    return f"{PIN_PREFIX}{uuid.uuid4().hex}"


def release_pin_name(release_id: str, version: int) -> str:
    """The pin the erasure release ``release_id`` writes on main's ``version``: derived from the release's own record, so a
    tick resumed from it writes the one a dead tick left, and its finish or abandonment deletes it."""
    return f"{RELEASE_PIN_PREFIX}{hashlib.sha256(f'{release_id}:{version}'.encode()).hexdigest()[:32]}"


def pin(dataset: lance.LanceDataset, ref: str | None, version: int, *, name: str | None = None) -> str:
    """Write a pin on ``version`` of ``ref`` (main for ``None``); its name. A pin of ``name`` standing there already is a
    resumed caller's, and kept.

    Raises:
        ValueError: Lance refused the tag: no such version, or a pin of ``name`` on another version.
        OSError: The store refused the write.
    """
    name = name or pin_name()
    reference: Any = version if recorded_branch(ref) is None else (recorded_branch(ref), version)
    try:
        dataset.tags.create(name, reference)
    except ValueError:
        held = dataset.tags.list().get(name)
        if held is None or (recorded_branch(held.get("branch")), int(held["version"])) != (recorded_branch(ref), version):
            raise
    return name


def unpin(dataset: lance.LanceDataset, name: str) -> None:
    """Delete the pin ``name``. Never raises: a pin left keeps its version whole, and a batch deletes it once it is older
    than :data:`PIN_LIFETIME`, or once a batch's ``g`` entry names its version."""
    try:
        dataset.tags.delete(name)
    except Exception as exc:
        log.warning("reclaim_pin_left", extra={"pin": name, "reason": f"{type(exc).__name__}: {exc}"[:300]})


class Pinned(BaseModel):
    """A door's pin on one version, and when it was written by the door's own monotonic clock."""

    model_config = ConfigDict(frozen=True)

    name: str
    at: float

    def require_alive(self) -> None:
        """Refuse a door that would act past :data:`DOOR_DEADLINE` after its pin: a batch may since have taken the pin for
        a dead door's (A3').

        Raises:
            TimeoutError: The door ran too long; it must not act.
        """
        if time.monotonic() - self.at > DOOR_DEADLINE.total_seconds():
            raise TimeoutError(f"the door ran {DOOR_DEADLINE} past its pin, which a reclaim may have taken for a dead door's")


def pin_whole(control_root: str, storage_options: StorageOptions, *, location: str, dataset: lance.LanceDataset, ref: str | None, version: int) -> Pinned:
    """PinFirst: pin ``version`` of ``ref``, then read the record of the table at ``location`` and refuse it where an entry
    names it; the pin, which the door deletes once it has acted (:func:`unpin`). A refused door's pin is deleted here.

    Raises:
        ValueError: Lance refused the pin (no such version).
        CondemnedVersionError: An entry names the version.
        OSError: The store did not answer the pin or the read: nothing may be acted on.
    """
    pinned = Pinned(name=pin(dataset, ref, version), at=time.monotonic())
    try:
        if version in condemned(control_root, storage_options, location=location, dataset=dataset, ref=ref):
            raise CondemnedVersionError(f"version {version} of {recorded_branch(ref) or MAIN} is condemned: a reclaim may be deleting it")
    except BaseException:
        unpin(dataset, pinned.name)
        raise
    return pinned


# ---------------------------------------------------------------- a batch


class Batch:
    """One reclaim batch of one ref of one table, run by the catalog: :meth:`condemn`, :meth:`read_pins`, the caller's
    read of the governing policy, then :meth:`promote`. ``dataset`` is a handle of the table's main, ``location`` its
    root, whose record the batch writes.

    ``table_uuid`` is the registration the batch was asked for, and ``listed_on`` the identity of the ref its reclaimer
    read before it opened the handle it listed the versions asked on (:func:`identity_of`). The ref's identity is read
    here, as the one its entries are keyed by, and the batch is refused where it is another (ListedOn): the reclaimer
    listed one incarnation and its numbers would condemn the versions of another. :meth:`condemn` refuses it too once
    ``registered`` (the registration holding the location now, :func:`registration_at`) reads another: who may condemn
    is the registration read, whatever an entry names.

    Raises:
        RefGoneError: The branch does not stand.
        ListedElsewhereError: ``listed_on`` is not the ref's identity now.
    """

    def __init__(
        self,
        control_root: str,
        storage_options: StorageOptions,
        *,
        table_uuid: str,
        location: str,
        dataset: lance.LanceDataset,
        ref: str | None,
        listed_on: str,
        registered: Callable[[], str | None],
    ) -> None:
        self._root, self._options, self._uuid, self._location, self._dataset = control_root, storage_options, table_uuid, location, dataset
        self._registered = registered
        self._ref = recorded_branch(ref)
        self.id = uuid.uuid4().hex
        self._chosen: set[int] = set()
        self._excluded: set[int] = set()
        self._licensed: set[int] = set()
        self._promoted: set[int] = set()
        self._forks: set[int] = set()
        identity = _refs(dataset).get(self._ref)
        if identity is None:
            raise RefGoneError(f"branch {self._ref!r} not found")
        _require_listed_on(listed_on, table_uuid if self._ref is None else identity, self._ref)
        self._identity = identity

    def _claimed(self, entry: Entry) -> bool:
        """Whether ``entry`` names one of the batch's versions and the batch still claims it: so it has stood since the
        batch's condemn, before its pin read, and every door that pinned since read it and refused."""
        return entry.ref_id == self._identity and entry.version in self._chosen and entry.claimed_by(self.id)

    def condemn(self, versions: Iterable[int], *, forks: bool = False) -> None:
        """Step 1 (CondemnFirst): the batch's versions condemned, ``versions`` that stand below the ref's head and every
        standing version of the ref an entry already names: the batch's claim is added to an entry naming one (its
        owner, the other claims, its state and licence kept) and the others written ``c`` under its claim alone, so the
        record holds one entry per version (TakeOver). In the same write, every entry whose manifest is gone or whose
        ref no longer stands is dropped (GCOnlyGone), and every lapsed claim (:func:`_rewrite`). A version a standing
        branch is cut from is condemned only where ``forks`` asks for it: an erasure's fork cleanup, which takes the
        files only that version names. Refused, nothing written, once the registration the batch was asked for no longer
        holds the location (ListedOn).

        Raises:
            ListedElsewhereError: Another registration holds the location, or none does.
            OSError: The store did not answer, or the record or the claim does not parse.
            records.RecordChangedError: Every attempt lost to another writer.
        """
        _require_registered(self._registered, self._uuid)
        names = {identity: name for name, identity in _refs(self._dataset).items()}
        listed: dict[str, set[int]] = {}

        def stands(identity: str) -> set[int]:
            # Listed once per ref an entry names, and none of a ref that no longer stands.
            if identity not in listed:
                listed[identity] = _versions_of(self._dataset, names[identity]) if identity in names else set()
            return listed[identity]

        head = max(stands(self._identity), default=0)
        self._forks = {version for ref, version in _forks(self._dataset) if ref == self._ref}
        asked = {version for version in versions if version in stands(self._identity) and version < head and (forks or version not in self._forks)}
        now = datetime.now(UTC)

        def change(entries: list[Entry]) -> list[Entry]:
            # A deleted incarnation's licensed entry stays until its licence passes (GCAfterLicence): a create under its
            # name reads it there (:func:`licensed_under`) while the reclaimer it licensed may still delete under the name.
            kept = [entry for entry in entries if entry.version in stands(entry.ref_id) or (entry.ref_id not in names and _live_until(entry, now) is not None)]
            # A version a standing branch is cut from keeps its manifest whatever a cleanup is asked (an erasure's fork
            # cleanup took its files), so it is finished only once no branch is cut from it.
            held = {entry.version for entry in kept if entry.ref_id == self._identity}
            self._chosen = asked | (held - self._forks)
            created = self._chosen - held
            claim = Claim(batch=self.id, until=now + CLAIM)
            claimed = [
                entry.model_copy(update={"claims": (*entry.claims, claim)}) if entry.ref_id == self._identity and entry.version in self._chosen else entry
                for entry in kept
            ]
            mine = [Entry(ref_id=self._identity, ref=self._ref, version=version, state="c", claims=(claim,)) for version in sorted(created)]
            return claimed + mine

        _rewrite(self._root, self._options, self._location, change)

    def read_pins(self, *, now: datetime | None = None) -> None:
        """Step 2 (ExplicitPins): the table's tags read, each dead door's pin deleted first (A3'); a chosen version a tag
        or a pin names is excluded, unless an entry names it ``g`` (GoOnly): another batch's promotion set it after that
        batch's own pin read, so a door that pinned it since read its entry and refused. No batch sets ``g`` before its
        own pin read, so a ``g`` entry this batch claimed at its condemn was set by a batch before it.

        Raises:
            OSError: The store did not answer.
        """
        now = now or datetime.now(UTC)
        entries, _etag = read(self._root, self._options, self._location)
        self._licensed = {entry.version for entry in entries if entry.ref_id == self._identity and entry.state == "g"}
        named: set[int] = set()
        for name, tag in self._dataset.tags.list().items():
            if recorded_branch(tag.get("branch")) != self._ref:
                continue
            created = tag.get("created_at")
            # A door's pin only: an erasure release's is never a dead door's (ReleasePinKind).
            if is_door_pin(name) and isinstance(created, datetime) and now - created > PIN_LIFETIME:
                log.warning("reclaim_dead_pin_swept", extra={"table_uuid": self._uuid, "pin": name, "version": tag.get("version")})
                unpin(self._dataset, name)
                _swept.add(1)
                continue
            named.add(int(tag["version"]))
        self._excluded = (self._chosen & named) - self._licensed

    def promote(self, held_by: str | None) -> Verdict:
        """Step 4, in one conditional write. THE RULE (ADR 0246 §17's table), for an entry naming one of the batch's
        versions:

        - the batch's claim on it is gone (it lapsed and was dropped, or the entry was removed and another batch,
          whose pin read this one never saw, wrote a new one): left as it is, neither promoted nor withdrawn from;
        - the batch claims it, and a hold keeps the table or a tag or a pin excludes the version: the batch WITHDRAWS,
          its own claim removed and nothing else, whoever wrote the entry. The entry goes with that write only once no
          one condemns it (:meth:`Entry.condemns`: ``c``, no owner, no claim left), so an owner's condemn stands, a live
          batch's claim stands, and a ``g`` entry stands;
        - the batch claims it and nothing keeps it: set ``g`` (or, ``g`` already, re-licensed), its licence the later of
          the instant it carries and this batch's (:data:`LICENCE` from now), so no reclaimer a licence was granted to
          loses it while its credential lives, and the batch's claim spent.

        The verdict names what this write set or re-licensed ``g`` (the reclaimer deletes exactly those), and until when;
        then the pins on another batch's ``g`` versions are deleted (GoUnpins).

        A batch of a branch then reads the branches (StandsAfterGo): where its incarnation no longer holds the name, it
        licenses nothing. A create under the name reads the name free, then the record (:func:`licensed_under`); so the
        incarnation went either after this read, and the create finds the licence, or before it, and nothing is deleted
        under the name the create may make again.

        Raises:
            OSError: The store did not answer, or the record does not parse.
            records.RecordChangedError: Every attempt lost to another writer.
        """
        if held_by is not None:

            def withdrawn(entries: list[Entry]) -> list[Entry]:
                return [entry.without(self.id) if self._claimed(entry) else entry for entry in entries]

            left = _rewrite(self._root, self._options, self._location, withdrawn)
            return Verdict(held_by=held_by, standing=self._standing(left))
        until = datetime.now(UTC) + LICENCE

        def change(entries: list[Entry]) -> list[Entry]:
            promoted: set[int] = set()
            out: list[Entry] = []
            for entry in entries:
                if not self._claimed(entry):
                    out.append(entry)
                elif entry.version in self._excluded:
                    out.append(entry.without(self.id))
                else:
                    carried = entry.licensed_until if entry.state == "g" and entry.licensed_until is not None else until
                    out.append(entry.without(self.id).model_copy(update={"state": "g", "licensed_until": max(carried, until)}))
                    promoted.add(entry.version)
            self._promoted = promoted
            return out

        left = _rewrite(self._root, self._options, self._location, change)
        go = sorted(self._promoted)
        if go and self._ref is not None and _refs(self._dataset).get(self._ref) != self._identity:
            log.warning("reclaim_branch_gone_after_promotion", extra={"table_uuid": self._uuid, "ref": self._ref, "versions": go[:20]})
            return Verdict(standing=self._standing(left))
        licensed = set(go) & self._licensed
        for name, tag in self._dataset.tags.list().items():
            if is_door_pin(name) and recorded_branch(tag.get("branch")) == self._ref and int(tag["version"]) in licensed:
                log.info("reclaim_pin_on_a_go_version_deleted", extra={"table_uuid": self._uuid, "pin": name, "version": tag.get("version")})
                unpin(self._dataset, name)
        return Verdict(versions=go, standing=self._standing(left, go), licensed_until=until if go else None)

    def _standing(self, entries: Sequence[Entry], go: Sequence[int] = ()) -> list[int]:
        """The ref's condemned versions this batch leaves standing: not licensed now, and not a fork point, whose
        manifest Lance keeps for its branch and which stands condemned for as long as that branch does."""
        return sorted({entry.version for entry in entries if entry.ref_id == self._identity} - set(go) - self._forks)


def condemn_named(
    control_root: str,
    storage_options: StorageOptions,
    *,
    table_uuid: str,
    location: str,
    dataset: lance.LanceDataset,
    ref: str | None,
    listed_on: str,
    versions: Sequence[int],
    refuse_pinned: Callable[[list[int]], None],
    registered: Callable[[], str | None],
) -> list[int]:
    """``version/delete``: mark ``versions`` of ``ref`` that stand below its head deleted, as their OWNER, under a call id
    of its own: on the entry already naming one (a batch beside it may be condemning it) as on a new one, so the record
    keeps one entry per version; the versions whose owner mark this call set (one another call marked stays that
    call's). An owner's condemn is final: no batch's hold or exclusion removes it, so the version stays condemned until
    its manifest is gone, whatever batch runs beside this call (VDeleteMarksNamed, OwnerFinal). Nothing is deleted: the
    next reclaim of the ref finishes them unless a hold keeps them. ``listed_on`` is the ref's identity as the door read
    it before it opened the handle it judged the request on, and the delete is refused where the ref is now another, or
    ``registered`` reads another registration than ``table_uuid`` holding the location (ListedOn), as a batch is.

    In a batch's order (VDeleteCondemnsFirst, ``ReclaimVDeleteReadsFirst.cfg``): the marks are written, THEN
    ``refuse_pinned`` reads the tags, the pins and the branches cut from the ref and raises where one names a version
    asked for, and the marks this call set are then taken back (nothing was deleted under them), each entry with its mark
    once no one else condemns it; a batch's claim on it stands, and that batch's own pin read excludes the version. A tag
    door pins, then reads the record, so of the two at least one sees the other.

    Raises:
        RefGoneError: The branch does not stand.
        ListedElsewhereError: ``listed_on`` is not the ref's identity now, or the registration no longer holds the location.
        OSError: The store did not answer, or the record or the claim does not parse.
        records.RecordChangedError: Every attempt lost to another writer.
        Exception: Whatever ``refuse_pinned`` raises; nothing it condemned stays its own.
    """
    ref = recorded_branch(ref)
    found = _refs(dataset).get(ref)
    if found is None:
        raise RefGoneError(f"branch {ref!r} not found")
    _require_listed_on(listed_on, table_uuid if ref is None else found, ref)
    _require_registered(registered, table_uuid)
    identity: str = found
    stands = _versions_of(dataset, ref)
    head = max(stands, default=0)
    named = sorted({version for version in versions if version in stands and version < head})
    call = f"vd-{uuid.uuid4().hex}"
    marked: list[int] = []

    def change(entries: list[Entry]) -> list[Entry]:
        there = {entry.version: entry for entry in entries if entry.ref_id == identity}
        marked[:] = [version for version in named if version not in there or there[version].owner is None]
        out = [entry.model_copy(update={"owner": call}) if entry.ref_id == identity and entry.version in marked else entry for entry in entries]
        return out + [Entry(ref_id=identity, ref=ref, version=version, state="c", owner=call) for version in marked if version not in there]

    _rewrite(control_root, storage_options, location, change)
    try:
        refuse_pinned(named)
    except BaseException:
        _rewrite(
            control_root,
            storage_options,
            location,
            lambda entries: [entry.model_copy(update={"owner": None}) if entry.owner == call else entry for entry in entries],
        )
        raise
    return list(marked)


# ---------------------------------------------------------------- the reclaim

#: How many candidate files a dry run names before it says it truncated: past it the victims are every version below the
#: ref's head, a superset the batch narrows to what stands and the pin read narrows to what nothing pins.
_MOST_CANDIDATES: Final = 200_000

#: A manifest's name under ``_versions/``: V2's ``<u64::MAX - version, 20 digits>.manifest`` or V1's ``<version>.manifest``
#: (``lance_docs/lance/format/table/layout.md``).
_V2_MANIFEST: Final = re.compile(r"(\d{20})\.manifest")
_V1_MANIFEST: Final = re.compile(r"(\d{1,19})\.manifest")
_U64_MAX: Final = (1 << 64) - 1


def _version_of(path: str) -> int | None:
    name = path.rsplit("/", 1)[-1]
    if (v2 := _V2_MANIFEST.fullmatch(name)) is not None:
        return _U64_MAX - int(v2.group(1))
    if (v1 := _V1_MANIFEST.fullmatch(name)) is not None:
        return int(v1.group(1))
    return None


def victims(dataset: lance.LanceDataset, *, older_than: timedelta | None, retain_versions: int | None) -> list[int]:
    """The versions of the ref ``dataset`` opens that ``cleanup_old_versions`` with these arguments would remove (tagged
    versions exempt), as Lance's own dry run names their manifests; every version below the ref's head when the dry run
    truncated its list.

    Raises:
        OSError: The store failed a read of the dry run.
    """
    explained = dataset.explain_cleanup_old_versions(
        older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=False, include_files=True, max_files=_MOST_CANDIDATES
    )
    named = sorted(
        {version for candidate in explained.candidate_files if candidate.kind == "manifest" and (version := _version_of(candidate.path)) is not None}
    )
    if not explained.candidate_files_truncated and len(named) == explained.stats.old_versions:
        return named
    log.warning("reclaim_victims_superset", extra={"uri": str(dataset.uri), "explained": explained.stats.old_versions, "named": len(named)})
    return sorted(version for entry in dataset.versions() if (version := int(entry["version"])) < int(dataset.version))


class Done(BaseModel):
    """What one reclaim of a ref did."""

    old_versions: int = 0
    bytes_removed: int = 0
    failed_deletes: int = 0
    #: What kept the table whole, when a hold did.
    held_by: str | None = None
    #: The versions of the ref left condemned and standing once it returned (:attr:`Verdict.standing`), and those whose
    #: deletes the store refused.
    standing: list[int] = Field(default_factory=list)


class Credential(BaseModel):
    """What one batch's cleanup deletes through: a handle of the ref opened under a credential minted before the batch was
    asked, and when the store stops honouring that credential."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    #: A ``lance.LanceDataset`` of the ref, every request of whose cleanup the credential signs.
    dataset: SkipValidation[Any]
    #: When the store refuses it, as the store stamped it; ``None`` for a key pair no store expires (a development
    #: fixture's), where the licence's instant binds nothing (ADR 0246 §14, the residual).
    expires_at: datetime | None


def covers(verdict: Verdict, credential: Credential) -> bool:
    """Whether the store refuses ``credential`` by the instant ``verdict``'s licence ends, the clocks apart by
    :data:`CLOCK_MARGIN` (assumption C): a credential minted before the batch was asked always is."""
    if credential.expires_at is None:
        return True
    return verdict.licensed_until is not None and credential.expires_at + CLOCK_MARGIN <= verdict.licensed_until


class RefReclaim(BaseModel):
    """How one ref's reclaim asks the catalog for each batch, and mints the credential each batch deletes under."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    #: One batch run by the catalog (:class:`Batch`) for the versions asked.
    condemn: SkipValidation[Callable[[list[int]], Verdict]]
    #: A credential minted now, and a handle of the ref opened under it.
    mint: SkipValidation[Callable[[], Credential]]


def reclaim(victims: Sequence[int], ref: RefReclaim, *, first: Verdict | None = None) -> Done:
    """Reclaim ``victims`` of one ref, a batch of :data:`BATCH` at a time: for each batch a credential minted
    (``ref.mint``, a handle of the ref under it), THEN the batch asked of the catalog (``ref.condemn``, which runs a
    :class:`Batch`; ``first`` is the first batch's answer where the caller already asked), then ``cleanup_old_versions``
    restricted to what it answered, through that handle and nothing else. At least one batch is asked, so a dead batch's versions are finished
    whatever ``victims`` holds. A hold stops the reclaim; a batch whose deletes the store refused stops it too, its entries
    standing for the next reclaim (KeepAfterDelete).

    A LICENCE ENDS (ADR 0246 §14). Lance's ``versions=`` deletes by number under the path the handle names, and a branch
    deleted and made again under its name repeats its numbers, so a licence is good only while the name means one
    incarnation: until its instant, after which a create may make the name again (:func:`licensed_under`). A batch is
    acted on only under a credential the store refuses by then (:func:`covers`); one asked before the credential was
    minted (``first``, asked before the pass's vend) is asked again.

    Raises:
        Exception: Whatever ``condemn`` or ``mint`` raises: nothing more is deleted.
        OSError: Lance's cleanup failed on the store; the batch's entries stand.
    """
    done = Done()
    batches = [list(victims[start : start + BATCH]) for start in range(0, len(victims), BATCH)] or [[]]
    for index, batch in enumerate(batches):
        credential = ref.mint()
        verdict = first if index == 0 and first is not None else ref.condemn(batch)
        if verdict.versions and not covers(verdict, credential):
            verdict = ref.condemn(batch)
        done.standing = verdict.standing
        if verdict.held_by is not None:
            done.held_by = verdict.held_by
            return done
        if not verdict.versions:
            continue
        if not covers(verdict, credential):
            log.warning("reclaim_credential_outlives_licence", extra={"versions": verdict.versions, "licensed_until": str(verdict.licensed_until)})
            done.standing = sorted({*verdict.standing, *verdict.versions})
            return done
        stats: Any = credential.dataset.cleanup_old_versions(versions=verdict.versions, error_if_tagged_old_versions=False)
        done.old_versions += int(stats.old_versions)
        done.bytes_removed += int(stats.bytes_removed)
        if stats.failed_deletes:
            done.failed_deletes += int(stats.failed_deletes)
            done.standing = sorted({*verdict.standing, *verdict.versions})
            log.warning(
                "reclaim_deletes_refused", extra={"uri": str(credential.dataset.uri), "failed_deletes": stats.failed_deletes, "versions": verdict.versions}
            )
            return done
    return done
