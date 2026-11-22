"""Trash records for the drop→undrop path (#75).

Lance version time-travel and `restore_table` do not stand in for an undrop: **time-travel does not survive
`drop_table`** — `restore_table` rewinds a LIVE table; a drop deletes the bytes and leaves no version
to rewind to. Time-travel covers bad-WRITE recovery, never bad-DROP recovery.

The durable fix: a drop DEREGISTERS the table (detach, bytes untouched) and writes a trash record
carrying where the bytes are and when the grace period ends. Undrop re-registers from that record until
then. Past the deadline the drop is DropTable's, its data deleted asynchronously (ADR 0146): on maintenance's
reconcile tick the catalog revokes the object's grants and marks the record released (:func:`mark_released`), and
the purge, a separate opt-in, deletes the bytes under a session the catalog vends for the record, after which the
catalog ends the record and what it keeps (:func:`end`, [[XC-084]]).

Two disciplines this module exists to keep:

- **Report-only by default.** ``expired()`` SELECTS what is past its deadline and does nothing else.
  Purging is a separate, explicitly-enabled step, exactly like the orphan pass — the estate's rule is
  that a reclaimer earns its delete permission by first proving its report runs clean.
- **The deadline is data, not policy read at expiry time.** ``expires_at`` is stamped WHEN THE DROP
  HAPPENS, so shortening the estate-wide grace period can never retroactively destroy something a
  user was still inside their window to recover.

Same store shape as ``maintenance_policies``/``protection``: one JSON per object under its own prefix, keyed by the
object's uuid (ADR 0173 §8, [[LH-525]]). A name is free again the moment its object is dropped, so several trashed
objects may share one, each under its own record: :func:`named` finds them by the name a door is addressed by, and
:func:`get` reads one by its uuid. The shape itself lives in :mod:`lakehouse.records.record_store`; this module is
the trash vocabulary over it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import pyarrow.fs as pafs
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from lakehouse.records import control_prefixes, location_claims, object_records
from lakehouse.records.base_registry import BaseRegistry, forget_base_record
from lakehouse.records.record_store import (
    UnreadableRecordError,
    delete_record,
    list_every_record,
    list_records,
    parse_record,
    put_record,
    read_record,
    record_key,
)
from lakehouse.storage.conditional_records import RecordChangedError, RecordExistsError, RecordMissingError, create_json, mutate_json, read_json, replace_json
from lakehouse.storage.objectfs import fs_and_base
from lakehouse.storage.options import StorageOptions


__all__ = [
    "RELEASE_SETTLE",
    "Refusal",
    "TrashKind",
    "TrashRecord",
    "UnreadableTrashRecordError",
    "clear",
    "end",
    "every_record",
    "expired",
    "get",
    "keeps",
    "key",
    "list_all",
    "make_record",
    "mark_released",
    "named",
    "note_refusal",
    "put",
    "read",
    "stamp_detached",
    "withdraw",
]


log = logging.getLogger(__name__)

_TRASH_PREFIX = control_prefixes.TRASH

#: What a trash record keeps: a dropped table, or a namespace a recoverable cascade dropped.
type TrashKind = Literal["table", "namespace"]

#: The event stem for this registry's store warnings (`<stem>_malformed`, `<stem>_unreadable`).
_EVENT = "trash_record"


def key(uuid: str, *, kind: TrashKind = "table") -> str:
    """The control-root key of the trash record of the ``kind`` object ``uuid`` names."""
    return record_key(_TRASH_PREFIX, kind, uuid)


class UnreadableTrashRecordError(UnreadableRecordError):
    """Raised when a trash record is there and cannot be read as one: it still keeps its object, and names its key."""


class Refusal(BaseModel):
    """Why the purge last declined a record, and when (:func:`note_refusal`)."""

    model_config = ConfigDict(frozen=True)

    at: str
    reason: str


class TrashRecord(BaseModel):
    """What a drop kept of an object for an undrop: one JSON object per dropped table or namespace.

    READ TOLERANTLY WHERE A WRONG VALUE HAS A SAFE READING, and refused where it has none. A record reaches the store
    through the drop door, and maintenance must still judge one that did not: a deadline that is missing or does not
    parse is "not yet" (:attr:`deadline`), a kind that is not ``namespace`` is a table, a residue mark that is not
    ``true`` is no mark, and the bookkeeping no decision reads (who dropped it, why the purge last declined it) is read
    as unknown. What a door acts on has no safe reading when it is wrong: a record whose name or location is not text,
    or whose kept properties are not a map of text, is unreadable (:class:`UnreadableTrashRecordError`), and so is one
    that is no JSON object.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    kind: TrashKind = "table"
    #: The canonical id the object was dropped under; empty for a record that names none.
    id: str = ""
    #: The uuid of the object the record keeps; ``None`` for a record that names none.
    uuid: str | None = None
    #: Where a table has its bytes, with no trailing slash; empty for a namespace and a declared-only table.
    location: str = ""
    dropped_by: str = "anonymous"
    dropped_at: str = ""
    #: The id the drop that filed the record minted, which its announcements and its keyed resend are filed under, so
    #: each finds this record as that drop's own.
    drop: str | None = None
    #: Stamped by that drop once its detach returned (:func:`stamp_detached`): the proof that it dropped the object. A
    #: record of its own left unstamped is judged by the catalog from the location's claim.
    detached: Literal[True] | None = None
    #: The deadline as the drop stamped it, so a later change to the estate's grace period cannot shorten a window
    #: someone is still inside. Read through :attr:`deadline`.
    expires_at: str | None = None
    #: A namespace's properties: their only copy, since its ``__manifest`` row went with the drop. Among them is
    #: ``rask.requires_run``, which only an admin may set, so an undrop that lost them would let the namespace's owner
    #: shed the run rule.
    properties: dict[str, str] | None = None
    #: The uuid of the namespace the object was dropped from, which its announcements name it under when no live
    #: namespace holds that name any more.
    namespace_uuid: str | None = None
    #: The uuid of the namespace a recoverable cascade dropped, on every record that cascade filed, its root's own
    #: included: the namespace undrop restores exactly the records one cascade filed.
    cascade: str | None = None
    #: Set on the record a destructive drop files before it removes a table's row ([[LH-532]]): the bytes were to be
    #: deleted, not kept for an undrop. It stands while the drop deletes them and is cleared once they are gone.
    residue: Literal[True] | None = None
    #: When maintenance released the record's grants past its deadline (ADR 0146).
    released_at: str | None = None
    #: How many times the purge declined the record, and why it last did (:func:`note_refusal`).
    attempts: int | None = None
    last_refusal: Refusal | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _a_namespace_or_a_table(cls, value: object) -> str:
        return "namespace" if value == "namespace" else "table"

    @field_validator("uuid", "namespace_uuid", "cascade", "drop", mode="before")
    @classmethod
    def _a_name_or_nothing(cls, value: object) -> object:
        return value if isinstance(value, str) and value else None

    @field_validator("location", mode="after")
    @classmethod
    def _without_a_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("expires_at", "released_at", mode="before")
    @classmethod
    def _text_or_nothing(cls, value: object) -> object:
        return value if isinstance(value, str) else None

    @field_validator("dropped_at", mode="before")
    @classmethod
    def _text_or_unknown(cls, value: object) -> object:
        return value if isinstance(value, str) else ""

    @field_validator("id", mode="before")
    @classmethod
    def _a_name_or_none_given(cls, value: object) -> object:
        return "" if value is None else value

    @field_validator("dropped_by", mode="before")
    @classmethod
    def _someone_or_nobody_known(cls, value: object) -> object:
        return value if isinstance(value, str) and value else "anonymous"

    @field_validator("last_refusal", mode="before")
    @classmethod
    def _a_refusal_or_nothing(cls, value: object) -> object:
        try:
            return Refusal.model_validate(value) if value is not None else None
        except ValidationError:
            return None

    @field_validator("residue", "detached", mode="before")
    @classmethod
    def _marked_only_by_true(cls, value: object) -> object:
        return True if value is True else None

    @field_validator("attempts", mode="before")
    @classmethod
    def _a_count_or_nothing(cls, value: object) -> object:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def is_residue(self) -> bool:
        """Whether the record keeps the bytes a failed destructive drop left, which no undrop restores ([[LH-532]])."""
        return self.residue is True

    @property
    def is_released(self) -> bool:
        """Whether maintenance has released the record's grants past its deadline (ADR 0146)."""
        return self.released_at is not None

    @property
    def deadline(self) -> datetime | None:
        """The record's deadline, or ``None`` when its ``expires_at`` is missing or does not parse: an undated record is
        a bug in whatever wrote it, and "we do not know when this expires" reads as "not yet". :func:`read` says so once."""
        if self.expires_at is None:
            return None
        try:
            deadline = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return None
        return deadline if deadline.tzinfo is not None else deadline.replace(tzinfo=UTC)

    @property
    def release_at(self) -> datetime | None:
        """When the record's grants may go (:meth:`releasable`): :data:`RELEASE_SETTLE` past its deadline."""
        deadline = self.deadline
        return deadline + RELEASE_SETTLE if deadline is not None else None

    def past_deadline(self, *, now: datetime | None = None) -> bool:
        """Whether the record's grace period has ended: no undrop restores it from then on (ADR 0146). An undated record
        is never past it: failing toward not-deleting is the stance the sweep takes when the policy registry is unreadable.

        The one rule the door that refuses an undrop, the tick that releases the grants and :func:`expired` read.
        """
        deadline = self.deadline
        return deadline is not None and deadline <= (now or datetime.now(UTC))

    def releasable(self, *, now: datetime | None = None) -> bool:
        """Whether the record is :data:`RELEASE_SETTLE` past its deadline, so its grants may go (ADR 0146)."""
        return self.past_deadline(now=(now or datetime.now(UTC)) - RELEASE_SETTLE)

    def stored(self) -> dict[str, Any]:
        """The record as its JSON object holds it: only what it states."""
        return self.model_dump(mode="json", exclude_none=True)


def read(raw: Mapping[str, Any], *, at: str) -> TrashRecord:
    """The record the JSON object ``raw`` holds at the key ``at``.

    Raises:
        UnreadableTrashRecordError: It is no trash record.
    """
    record = parse_record(TrashRecord, raw, at=at, error=UnreadableTrashRecordError)
    if record.deadline is None:
        log.warning(
            "trash_record_undated" if record.expires_at is None else "trash_record_bad_deadline", extra={"id": record.id, "expires_at": record.expires_at}
        )
    return record


def _key_of(raw: Mapping[str, Any]) -> str:
    """The key a listed record is filed under, by the uuid and kind it states; its name where it states no uuid."""
    uuid = raw.get("uuid")
    if isinstance(uuid, str) and uuid:
        return key(uuid, kind="namespace" if raw.get("kind") == "namespace" else "table")
    return f"{_TRASH_PREFIX} (a record naming {raw.get('id')!r} and no uuid)"


def make_record(
    canonical_id: str,
    *,
    uuid: str,
    location: str,
    dropped_by: str | None,
    grace_days: float,
    now: datetime | None = None,
    kind: TrashKind = "table",
    properties: dict[str, str] | None = None,
    namespace_uuid: str | None = None,
    cascade: str | None = None,
    residue: bool = False,
    drop: str | None = None,
) -> TrashRecord:
    """The trash record of the object ``uuid`` names, dropped while ``canonical_id`` named it (:class:`TrashRecord`
    says what each field keeps).

    ``expires_at`` is stamped HERE, at drop time. ``kind`` distinguishes tables from namespaces (#96: a recoverable
    CASCADE trashes both). A namespace is a ``__manifest`` row with no bytes of its own, so its ``location`` is ``""``;
    the record's job is to let undrop know the row (and which subtree) to rebuild. A ``residue`` record's grace is 0, so
    the purge may take it on its first tick, and undrop refuses it.
    """
    at = now or datetime.now(UTC)
    return TrashRecord(
        kind=kind,
        id=canonical_id,
        uuid=uuid,
        location=location,
        dropped_by=dropped_by or "anonymous",
        dropped_at=at.isoformat(),
        expires_at=(at + timedelta(days=grace_days)).isoformat(),
        properties=dict(properties) if properties else None,
        namespace_uuid=namespace_uuid or None,
        cascade=cascade or None,
        residue=True if residue else None,
        drop=drop,
    )


#: How long past its deadline a record waits before its grants go: well over any one undrop request, which is the
#: window an undrop admitted at the deadline needs to finish (it registers before it clears the record, and a revoke
#: landing between the two would leave the restored object ownerless). The location claim's lease is the same order.
RELEASE_SETTLE: Final = timedelta(minutes=15)


def mark_released(control_root: str, storage_options: StorageOptions, uuid: str, *, kind: TrashKind = "table", now: datetime | None = None) -> bool:
    """Stamp the record of the ``kind`` object ``uuid`` names as released, once its grants are revoked.

    Conditional (:func:`conditional_records.mutate_json`), so a concurrent :func:`note_refusal` survives and ``expires_at`` never
    moves. ``False`` when the record is gone, a purge or an undrop having cleared it since it was read.

    Raises:
        UnreadableTrashRecordError: The record filed there is none, so nothing about it is stamped.
    """
    at, filed = (now or datetime.now(UTC)).isoformat(), key(uuid, kind=kind)

    def _released(raw: dict[str, Any]) -> dict[str, Any]:
        record = read(raw, at=filed)
        return record.model_copy(update={"released_at": record.released_at or at}).stored()

    try:
        mutate_json(control_root, storage_options, filed, mutate=_released)
    except RecordMissingError:
        return False
    return True


def put(control_root: str, storage_options: StorageOptions, record: TrashRecord) -> None:
    """File ``record`` under its object's uuid.

    Raises:
        ValueError: The record names no uuid, so there is no key to file it under.
    """
    if record.uuid is None:
        raise ValueError(f"a trash record is filed under its object's uuid, and the record of {record.id!r} names none")
    put_record(control_root, storage_options, key(record.uuid, kind=record.kind), record=record.stored())


def stamp_detached(control_root: str, storage_options: StorageOptions, record: TrashRecord, *, refile: bool = False) -> bool:
    """Stamp ``record``'s drop's own record as detached once that drop's detach returned; whether it is stamped.

    The record filed there is this drop's own when it names the same object and drop id (:func:`_own`), whatever else
    has been noted on it since; it is stamped as stored, under the ETag of that read, so a note landing meanwhile is
    kept or the stamp lost. A record another drop filed is left as it stands: ``False``. A record that is gone is left
    gone, ``False``, unless ``refile``: the door whose detach returned files it again, stamped, create-if-absent, where
    the finish withdrew it, having judged the table still registered while the door's detach was in flight and its fence
    lapsed (the door re-files only once it finds the table registered under no name, ``tables._stamp_the_drop``).

    Raises:
        ValueError: The record names no uuid, or the one filed is no trash record.
        OSError: The store refused the read or the write, or did not answer.
    """
    at, found = _filed(control_root, storage_options, record)
    try:
        if found is None:
            if not refile:
                return False
            create_json(control_root, storage_options, at, record=record.model_copy(update={"detached": True}).stored())
            return True
        kept = _own(found[0], record, at=at)
        if kept is None or kept.detached:
            return kept is not None
        replace_json(control_root, storage_options, at, record=kept.model_copy(update={"detached": True}).stored(), etag=found[1])
    except (RecordChangedError, RecordExistsError):
        return False
    return True


def withdraw(
    control_root: str, storage_options: StorageOptions, record: TrashRecord, *, claims: location_claims.ClaimStore, token: str, margin: timedelta = timedelta(0)
) -> bool:
    """Remove ``record``'s drop's own unstamped record once that drop is known not to have detached its object; whether
    it did.

    The store offers no conditional delete (:mod:`lakehouse.records.location_claims` says why the estate requires none),
    so the record is read and compared, and removed only while the caller's fence ``token`` stands on the object's
    claim with more than ``margin`` of its lease left (:meth:`location_claims.LocationClaim.fenced_by`; a door passes
    ``location_claims.DOOR_MARGIN``), or while that claim is not the object's (absent, released,
    another uuid's): every drop fences the object's claim before it files a record, so in either case no other drop's
    record lands between the read and the delete.

    Raises:
        ValueError: The record names no uuid, or the one filed, or the claim, is none.
        OSError: The store refused a read or the delete, or did not answer.
    """
    at, found = _filed(control_root, storage_options, record)
    kept = _own(found[0], record, at=at) if found is not None else None
    if kept is None or kept.detached:
        return False
    claim = location_claims.read(claims, record.location)
    objects = claim is not None and claim.claim.uuid == record.uuid and claim.claim.released_at is None
    if objects and claim is not None and not claim.claim.fenced_by(token, location_claims.store_now(claims, record.location), margin=margin):
        return False
    return delete_record(control_root, storage_options, at)


def _own(raw: dict[str, Any], record: TrashRecord, *, at: str) -> TrashRecord | None:
    """The record ``raw`` holds when ``record``'s drop filed it: the same kind, uuid and drop id, which nothing but that
    drop writes; what is noted on a record since (a refusal, a release) does not make it another drop's."""
    kept = read(raw, at=at)
    return kept if record.drop is not None and (kept.kind, kept.uuid, kept.drop) == (record.kind, record.uuid, record.drop) else None


def _filed(control_root: str, storage_options: StorageOptions, record: TrashRecord) -> tuple[str, tuple[dict[str, Any], str] | None]:
    """The key ``record`` is filed under, and what is stored there with its ETag."""
    if record.uuid is None:
        raise ValueError(f"a trash record is filed under its object's uuid, and the record of {record.id!r} names none")
    at = key(record.uuid, kind=record.kind)
    return at, read_json(control_root, storage_options, at)


def keeps(control_root: str, storage_options: StorageOptions, uuid: str, *, kind: TrashKind = "table") -> bool:
    """Whether a record is filed for the ``kind`` object ``uuid`` names, readable or not.

    For a caller deciding from the answer that the object is gone: :func:`get` reads a record it cannot parse as absent,
    and one that is there but unreadable still keeps its object for an undrop.
    """
    fs, base = fs_and_base(control_root, storage_options)
    return fs.get_file_info(f"{base}/{key(uuid, kind=kind)}").type == pafs.FileType.File


def get(control_root: str, storage_options: StorageOptions, uuid: str, *, kind: TrashKind = "table") -> TrashRecord | None:
    """The trash record of the ``kind`` object ``uuid`` names, or ``None`` when none is filed.

    Every caller decides from the answer (whether a location's claim is still held, what an undrop restores), so a
    record that is filed and cannot be read raises: read as absent, its object would be taken for gone.

    Raises:
        UnreadableTrashRecordError: A record is filed there and is none: not JSON, not an object, or not a trash record.
        OSError: The store failed the read.
    """
    at = key(uuid, kind=kind)
    try:
        raw = read_record(control_root, storage_options, at)
    except ValueError as exc:
        raise UnreadableTrashRecordError(f"the trash record at {at} cannot be read: {exc}") from exc
    return read(raw, at=at) if raw is not None else None


def named(control_root: str, storage_options: StorageOptions, canonical_id: str, *, kind: TrashKind = "table") -> list[TrashRecord]:
    """Every trashed ``kind`` object that was dropped while ``canonical_id`` named it, oldest drop first.

    A door is addressed by name (ADR 0173 §9), and a name is free again once its object is dropped, so a name can lead
    to several records: the door says which one it acts on. Every caller decides something from the answer (what a
    door restores, whether a claim's holder is gone), so a record it cannot read raises rather than being left out.

    Raises:
        OSError: The store could not be listed, or a record read.
        ValueError: A trash record is not a JSON object, or not a record (:class:`UnreadableTrashRecordError`).
    """
    held = [record for record in every_record(control_root, storage_options) if record.kind == kind and record.id == canonical_id]
    return sorted(held, key=lambda record: record.dropped_at)


def clear(control_root: str, storage_options: StorageOptions, uuid: str, *, kind: TrashKind = "table") -> bool:
    return delete_record(control_root, storage_options, key(uuid, kind=kind))


def end(control_root: str, storage_options: StorageOptions, record: TrashRecord, *, kind: TrashKind, uuid: str) -> None:
    """End what the trashed object ``uuid`` names kept for an undrop besides its grants: the records it holds under its
    uuid (:func:`lakehouse.records.object_records.end_object_records`), a table's base record and claim, then ``record`` itself.

    The record goes last: it is all that still names what the object holds, so a failure before it is cleared leaves it
    for the next attempt to end. Every step is idempotent. The base record is forgotten with its data directories'
    claims (:func:`lakehouse.records.base_registry.forget_base_record`): a caller that deletes the bytes deletes the directories
    before this (:func:`lakehouse.records.data_directories.delete_table_directories`).

    Raises:
        UnreadableBaseRecordError: The table's base record exists and cannot be read.
        StorageError: The store refused a read or a write, or did not answer it.
    """
    object_records.end_object_records(control_root, storage_options, kind, uuid=uuid)
    if record.location:
        # The base record first: forgetting it deletes the tag a clone holds on each source, and one the store will not
        # delete must leave the location held, or a clone registered there next would hold a tag of the same name.
        forget_base_record(BaseRegistry(control_root=control_root, storage_options=storage_options), record.location, uuid=uuid)
        claims = location_claims.ClaimStore(control_root=control_root, storage_options=storage_options)
        location_claims.release(claims, record.location, record.id, uuid=uuid)
    clear(control_root, storage_options, uuid, kind=kind)


def note_refusal(
    control_root: str,
    storage_options: StorageOptions,
    uuid: str,
    *,
    kind: TrashKind = "table",
    reason: str,
    now: datetime | None = None,
) -> int | None:
    """Record that the purge declined to act on this record, and return the new attempt count.

    WHY THE RECORD REMEMBERS. The purge reports refusals per TICK and carried nothing across them, so a
    record refused every five minutes for thirty days read exactly like one refused once — and a
    PERMANENT exclusion, the state an operator actually has to act on, was invisible as permanent.
    ``attempts`` accumulates and ``last_refusal`` is replaced, so the record answers both "how long has
    this been stuck" and "why is it stuck NOW"; the second matters because a record moves between causes
    (a protected base is dropped and it becomes still-registered instead).

    THE COST IS DELIBERATE AND IS THE REASON THIS IS CONDITIONAL. It makes the trash record MUTABLE —
    everything else about it is stamped once at drop time — and gives the purge a WRITE per refused
    record per tick where it previously only read. So it goes through :func:`conditional_records.mutate_json`
    rather than a plain overwrite, for exactly the reason the warehouse registry does: a read-modify-
    write with no precondition silently reverts whatever a concurrent writer put in the fields it
    merely carried forward, and ``expires_at`` is the field that must never move.

    NEVER RAISES, and returns ``None`` when it could not annotate. The annotation is bookkeeping ABOUT
    a refusal and is strictly less important than the refusal: a record another replica already cleared
    (``RecordMissingError``) or a lost CAS race (``RecordChangedError``) must not convert a correctly
    reported refusal into an error on the reclamation path. ``_purge_one``'s contract — "never raises;
    every failure is a refusal" — is what this must not break.
    """
    at, filed = (now or datetime.now(UTC)).isoformat(), key(uuid, kind=kind)

    def _refused(raw: dict[str, Any]) -> dict[str, Any]:
        # A pure function of what was READ, because `mutate_json` may call it more than once — carrying
        # a count in from an earlier read is exactly the staleness the conditional write exists to stop.
        record = read(raw, at=filed)
        return record.model_copy(update={"attempts": (record.attempts or 0) + 1, "last_refusal": Refusal(at=at, reason=reason)}).stored()

    try:
        updated = mutate_json(control_root, storage_options, filed, mutate=_refused)
    except (RecordMissingError, RecordChangedError, UnreadableTrashRecordError) as exc:
        log.warning("trash_refusal_not_recorded", extra={"kind": kind, "uuid": uuid, "reason": str(exc)})
        return None
    return read(updated, at=filed).attempts


def list_all(control_root: str, storage_options: StorageOptions) -> list[TrashRecord]:
    """Every readable trash record. An absent prefix yields ``[]``; one unreadable record is skipped
    with a warning rather than blinding the whole listing. A caller deciding from the listing what a drop left asks
    :func:`every_record`."""
    found: list[TrashRecord] = []
    for raw in list_records(control_root, storage_options, _TRASH_PREFIX, event=_EVENT):
        try:
            found.append(read(raw, at=_key_of(raw)))
        except UnreadableTrashRecordError:
            log.warning(f"{_EVENT}_malformed", extra={"key": _key_of(raw)})
    return found


def every_record(control_root: str, storage_options: StorageOptions) -> list[TrashRecord]:
    """Every trash record, read whole. An absent prefix yields ``[]``.

    For a caller deciding from the listing what the drops left (an undrop restoring its cascade, a warehouse ending its
    own trash): a record it could not read may be one of them, so one that cannot be read raises rather than being left
    out as :func:`list_all` leaves it ([[LH-572]]).

    Raises:
        OSError: The store could not be listed, or a record read.
        ValueError: A record is not a JSON object, or not a record (:class:`UnreadableTrashRecordError`).
    """
    return [read(raw, at=_key_of(raw)) for raw in list_every_record(control_root, storage_options, _TRASH_PREFIX, event=_EVENT)]


def expired(records: list[TrashRecord], *, now: datetime | None = None) -> list[TrashRecord]:
    """Which records are past their deadline: a SELECTION, never a deletion (#75 report-only). An undated record is not
    selected (:meth:`TrashRecord.past_deadline`)."""
    at = now or datetime.now(UTC)
    return [record for record in records if record.past_deadline(now=at)]
