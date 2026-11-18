"""The one way the estate reclaims a table's versions: a reclaim CONDEMNS before it deletes, and a door PINS before it
reads (ADR 0246, ``specs/maintenance/Reclaim.tla``).

LANCE'S CLEANUP DELETES A VERSION'S FILES BEFORE ITS MANIFEST, AND COUNTS A REFUSED DELETE. Measured on pylance 13.0.0
(ADR 0243 §3): one ``cleanup_old_versions`` deletes the transaction files, the data files, each data file's blob sidecars,
and the manifests last, and returns with ``CleanupStats.failed_deletes`` counting what the store refused. A cleanup that
stops part-way leaves versions standing over deleted files. And it reads a table's tags once, before its manifests
(measured on pylance 13.0.0: a tag written after that read names a version it then deletes, ADR 0246 §5), an order
``lance_docs/`` does not promise.

THE RECORD (:class:`Record`). One per table on the control root (:func:`record_key`), written only by the catalog, each
write conditional on the ETag it read (:func:`_rewrite`). An entry names a version by the IDENTITY of its ref (main, or
one incarnation of a branch: the uuid Lance lists last in its ``branch_identifier``, so a branch deleted and made again
under its name is another ref), the version's number, the batch that wrote it and its state: ``c`` condemned, or ``g``,
its batch passed its pin read and its policy read and may have begun deleting. Every ref door refuses a version an entry
names. An entry is dropped only once its manifest is gone or its ref's identity no longer stands (GCOnlyGone), or by its
own batch before that batch deleted anything (a pinned version, or a hold): condemned is final while the manifest stands,
and the next reclaim finishes it.

A RECLAIM BATCH (:class:`Batch`, at most :data:`BATCH` versions; every reclaim in the estate goes through it, a pass,
the catalog's ``maintenance/run``, an erasure's verification and its release):

1. :meth:`Batch.condemn` (CondemnFirst) writes the batch's entries, ``c``: the versions asked for, and every standing
   version of the ref an entry already names, which a dead batch or ``version/delete`` left;
2. :meth:`Batch.read_pins` (ExplicitPins) lists the table's tags, user tags and pins (:data:`PIN_PREFIX`): a version one
   names is excluded, unless an entry of ANOTHER batch names it ``g`` (GoOnly). A pin older than :data:`PIN_LIFETIME` is
   a dead door's, and is deleted first (A3');
3. the caller reads the policy that governs the table, level by level (PolicyAfterCondemn);
4. :meth:`Batch.promote`: under a hold its own entries are removed (nothing was deleted under them); else one conditional
   write removes its own entries of excluded versions and sets the rest ``g``, and the pins standing on another batch's
   ``g`` versions are deleted (GoUnpins: a door that pinned one of those after that batch's pin read reads its entry and
   refuses, so the pin protects nothing, and Lance keeps every tagged version, so leaving it would keep a half-removed
   version so for good).

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

from lakehouse import control_prefixes, records, stored_objects
from lakehouse.record_store import record_key as object_record_key
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

#: How many read-and-write rounds a rewrite of the record runs. Measured on RustFS 1.0.3 (ADR 0246 §5): 16 batches
#: condemning on one record at once took 1.2 s, and one batch at most 13 retries.
_ATTEMPTS: Final = 32


def is_reserved(name: str) -> bool:
    """Whether the tag ``name`` carries the prefix the catalog keeps for its own tags, which only it writes or deletes."""
    return name.startswith(RESERVED_PREFIX)


def is_door_pin(name: str) -> bool:
    """Whether the tag ``name`` is a door's pin, which a batch may take for a dead door's or licence deleting."""
    return _PIN_NAME.fullmatch(name) is not None


class Entry(BaseModel):
    """One condemned version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The identity of the version's ref: :data:`MAIN`, or the uuid of one incarnation of a branch.
    ref_id: str
    #: The branch's name when it was condemned, ``None`` for main: where a reader opens it.
    ref: str | None = None
    version: int
    #: The batch that wrote it, ``vd-<hex>`` for a ``version/delete``.
    batch: str
    state: Literal["c", "g"]
    #: For ``g``, the instant (the catalog's clock) after which the reclaimer its batch licensed can no longer delete:
    #: its credential was minted before the batch was asked and lives at most :data:`CREDENTIAL_LIFETIME`.
    licensed_until: datetime | None = None


class Record(BaseModel):
    """A table's condemned versions."""

    model_config = ConfigDict(extra="forbid")

    #: The table's uuid: a record naming another is no record of this table's.
    uuid: str
    #: The table's location, as its claim is keyed: where a policy door finds the name it holds now.
    location: str
    entries: list[Entry] = Field(default_factory=list)


class CondemnedVersionError(Exception):
    """The version a door names is condemned: a reclaim may be deleting it."""


class RefGoneError(ValueError):
    """The branch a batch names does not stand."""


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


def record_key(table_uuid: str) -> str:
    """The key, on the control root, of the table ``table_uuid``'s condemned versions: one of the records its uuid holds,
    which end with it (``lakehouse.object_records``)."""
    return object_record_key(control_prefixes.CONDEMNED, "table", table_uuid)


def read(control_root: str, storage_options: StorageOptions, table_uuid: str) -> tuple[list[Entry], str | None]:
    """The table's entries and the ETag they were read under (:func:`read_record`)."""
    stored, etag = read_record(control_root, storage_options, table_uuid)
    return ([] if stored is None else stored.entries), etag


def read_record(control_root: str, storage_options: StorageOptions, table_uuid: str) -> tuple[Record | None, str | None]:
    """The table's record and the ETag it was read under, both ``None`` for no record.

    Raises:
        OSError: The store did not answer, or the record does not parse as this table's: fail closed, since only the
            catalog writes it and an entry it cannot read may name a half-removed version.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    key = record_key(table_uuid)
    try:
        stored = records.read_json_at(control_root, storage_options, key).stored
        if stored is None:
            return None, None
        parsed = Record.model_validate(stored.record)
    except (ClientError, BotoCoreError) as exc:
        raise OSError(f"the store did not answer reading {key}: {exc}") from exc
    except (ValueError, ValidationError) as exc:
        raise OSError(f"{key} does not parse as a table's condemned versions: {exc}"[:600]) from exc
    if parsed.uuid != table_uuid:
        raise OSError(f"{key} names table {parsed.uuid!r}, not {table_uuid!r}")
    return parsed, stored.etag


def recorded(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    """Whether the table's record stands, from one listing of its key alone, which a session that may list the control
    root and read none of it (maintenance's own) can ask: a record stands only while it names a condemned version.

    Raises:
        OSError: The store did not answer.
    """
    key = record_key(table_uuid)
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


def _rewrite(control_root: str, storage_options: StorageOptions, table_uuid: str, location: str, change: Callable[[list[Entry]], list[Entry]]) -> list[Entry]:
    """Apply ``change`` to the entries as read and write them back under the ETag read (create-if-absent for none); the
    entries the landed write holds. ``change`` may run once per attempt, on the record as each attempt read it.

    A record whose last entry goes is deleted under the ETag read, so a table has a record only while it has entries: the
    records standing are the tables with a reclaim in flight or stuck, which a policy door reads (:func:`every_record`).

    Raises:
        OSError: The store did not answer, or the record does not parse.
        records.RecordChangedError: Every attempt lost to another writer.
    """
    key = record_key(table_uuid)
    for _ in range(_ATTEMPTS):
        entries, etag = read(control_root, storage_options, table_uuid)
        changed = change(list(entries))
        if changed == entries and (changed or etag is None):
            return entries
        body = Record(uuid=table_uuid, location=location, entries=changed).model_dump(mode="json")
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


def _refs(dataset: lance.LanceDataset) -> dict[str | None, str]:
    """Every ref of the table standing, main as ``None``, each with its identity, from one listing of its branches."""
    listed: Mapping[str, Any] = dataset.branches.list()
    return {None: MAIN, **{str(name): ident for name, entry in listed.items() if (ident := branch_identity(entry)) is not None}}


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


def condemned(control_root: str, storage_options: StorageOptions, *, table_uuid: str, dataset: lance.LanceDataset, ref: str | None) -> set[int]:
    """The versions of ``ref`` (main for ``None``) as it stands now that an entry names: every ref door refuses them, and
    ``list versions`` leaves them out. One read of the record, and one listing of the branches for a branch.

    Raises:
        OSError: The store did not answer, or the record does not parse: whether a version is condemned is unknown.
    """
    entries, _etag = read(control_root, storage_options, table_uuid)
    if not entries:
        return set()
    ref = recorded_branch(ref)
    identity = MAIN if ref is None else _refs(dataset).get(ref)
    return {entry.version for entry in entries if identity is not None and entry.ref_id == identity}


def licensed_under(control_root: str, storage_options: StorageOptions, *, table_uuid: str, name: str, now: datetime | None = None) -> datetime | None:
    """The latest instant a live licence names a version under the branch name ``name``, ``None`` where none does: asked
    by a create that read the name free, so every such licence is a deleted incarnation's (NameGuard, ADR 0246).

    A licence names numbers under a path, and Lance's cleanup deletes by number under the path a branch's name holds when
    it runs (measured on pylance 13.0.0: a cleanup through a handle of a deleted incarnation removed the versions 3 and 4
    of the incarnation made again under its name), so the name is not made again while one runs.

    Raises:
        OSError: The store did not answer, or the record does not parse.
    """
    entries, _etag = read(control_root, storage_options, table_uuid)
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


def pin_whole(control_root: str, storage_options: StorageOptions, *, table_uuid: str, dataset: lance.LanceDataset, ref: str | None, version: int) -> Pinned:
    """PinFirst: pin ``version`` of ``ref``, then read the record and refuse it where an entry names it; the pin, which the
    door deletes once it has acted (:func:`unpin`). A refused door's pin is deleted here.

    Raises:
        ValueError: Lance refused the pin (no such version).
        CondemnedVersionError: An entry names the version.
        OSError: The store did not answer the pin or the read: nothing may be acted on.
    """
    pinned = Pinned(name=pin(dataset, ref, version), at=time.monotonic())
    try:
        if version in condemned(control_root, storage_options, table_uuid=table_uuid, dataset=dataset, ref=ref):
            raise CondemnedVersionError(f"version {version} of {recorded_branch(ref) or MAIN} is condemned: a reclaim may be deleting it")
    except BaseException:
        unpin(dataset, pinned.name)
        raise
    return pinned


# ---------------------------------------------------------------- a batch


class Batch:
    """One reclaim batch of one ref of one table, run by the catalog: :meth:`condemn`, :meth:`read_pins`, the caller's
    read of the governing policy, then :meth:`promote`. ``dataset`` is a handle of the table's main."""

    def __init__(
        self, control_root: str, storage_options: StorageOptions, *, table_uuid: str, location: str, dataset: lance.LanceDataset, ref: str | None
    ) -> None:
        self._root, self._options, self._uuid, self._location, self._dataset = control_root, storage_options, table_uuid, location, dataset
        self._ref = recorded_branch(ref)
        self.id = uuid.uuid4().hex
        self._chosen: set[int] = set()
        self._excluded: set[int] = set()
        self._licensed: set[int] = set()
        self._forks: set[int] = set()
        identity = _refs(dataset).get(self._ref)
        if identity is None:
            raise RefGoneError(f"branch {self._ref!r} not found")
        self._identity = identity

    def condemn(self, versions: Iterable[int], *, forks: bool = False) -> None:
        """Step 1 (CondemnFirst): this batch's entries written ``c``, for ``versions`` that stand below the ref's head and
        every standing version of the ref an entry already names; in the same write, every entry whose manifest is gone or
        whose ref no longer stands dropped (GCOnlyGone). A version a standing branch is cut from is condemned only where
        ``forks`` asks for it: an erasure's fork cleanup, which takes the files only that version names.

        Raises:
            OSError: The store did not answer, or the record does not parse.
            records.RecordChangedError: Every attempt lost to another writer.
        """
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
            finish = {entry.version for entry in kept if entry.ref_id == self._identity and entry.version not in self._forks}
            self._chosen = asked | finish
            mine = [Entry(ref_id=self._identity, ref=self._ref, version=version, batch=self.id, state="c") for version in sorted(self._chosen)]
            return kept + mine

        _rewrite(self._root, self._options, self._uuid, self._location, change)

    def read_pins(self, *, now: datetime | None = None) -> None:
        """Step 2 (ExplicitPins): the table's tags read, each dead door's pin deleted first (A3'); a chosen version a tag
        or a pin names is excluded, unless another batch's entry names it ``g`` (GoOnly).

        Raises:
            OSError: The store did not answer.
        """
        now = now or datetime.now(UTC)
        entries, _etag = read(self._root, self._options, self._uuid)
        self._licensed = {entry.version for entry in entries if entry.ref_id == self._identity and entry.state == "g" and entry.batch != self.id}
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
        """Step 4: under a hold, this batch's own entries removed and nothing licensed; else, in one conditional write,
        its own entries of excluded versions removed and the rest set ``g`` with the licence's instant
        (:data:`LICENCE` from now), then the pins on another batch's ``g`` versions deleted (GoUnpins). The verdict names
        what the reclaimer may delete, and until when.

        A batch of a branch then reads the branches (StandsAfterGo): where its incarnation no longer holds the name, it
        licenses nothing. A create under the name reads the name free, then the record (:func:`licensed_under`); so the
        incarnation went either after this read, and the create finds the licence, or before it, and nothing is deleted
        under the name the create may make again.

        Raises:
            OSError: The store did not answer, or the record does not parse.
            records.RecordChangedError: Every attempt lost to another writer.
        """
        if held_by is not None:
            left = _rewrite(self._root, self._options, self._uuid, self._location, lambda entries: [entry for entry in entries if entry.batch != self.id])
            return Verdict(held_by=held_by, standing=self._standing(left))
        until = datetime.now(UTC) + LICENCE

        def change(entries: list[Entry]) -> list[Entry]:
            return [
                entry.model_copy(update={"state": "g", "licensed_until": until}) if entry.batch == self.id else entry
                for entry in entries
                if not (entry.batch == self.id and entry.version in self._excluded)
            ]

        left = _rewrite(self._root, self._options, self._uuid, self._location, change)
        go = sorted(entry.version for entry in left if entry.batch == self.id)
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
    versions: Sequence[int],
    refuse_pinned: Callable[[list[int]], None],
) -> list[int]:
    """``version/delete``: condemn ``versions`` of ``ref`` that stand below its head, under a batch id of its own; the
    versions condemned. Nothing is deleted: the next reclaim of the ref finishes them unless a hold keeps them.

    In a batch's order (VDeleteCondemnsFirst, ``ReclaimVDeleteReadsFirst.cfg``): the entries are written, THEN
    ``refuse_pinned`` reads the tags, the pins and the branches cut from the ref and raises where one names a version
    condemned, and its entries are then removed (nothing was deleted under them). A tag door pins, then reads the record,
    so of the two at least one sees the other: a version a caller's tag names is never left condemned.

    Raises:
        RefGoneError: The branch does not stand.
        OSError: The store did not answer, or the record does not parse.
        records.RecordChangedError: Every attempt lost to another writer.
        Exception: Whatever ``refuse_pinned`` raises; nothing stays condemned.
    """
    ref = recorded_branch(ref)
    found = _refs(dataset).get(ref)
    if found is None:
        raise RefGoneError(f"branch {ref!r} not found")
    identity: str = found
    stands = _versions_of(dataset, ref)
    head = max(stands, default=0)
    named = sorted({version for version in versions if version in stands and version < head})
    batch = f"vd-{uuid.uuid4().hex}"
    _rewrite(
        control_root,
        storage_options,
        table_uuid,
        location,
        lambda entries: entries + [Entry(ref_id=identity, ref=ref, version=version, batch=batch, state="c") for version in named],
    )
    try:
        refuse_pinned(named)
    except BaseException:
        _rewrite(control_root, storage_options, table_uuid, location, lambda entries: [entry for entry in entries if entry.batch != batch])
        raise
    return named


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
