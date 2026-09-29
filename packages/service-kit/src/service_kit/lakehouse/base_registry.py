"""The catalog's record of the foreign bases each table may declare — ``_bases/`` on the control root ([[LH-279]]).

A MANIFEST'S BASE LIST IS A WRITER'S CLAIM. Any holder of a table's write vend can commit to it, and the
vend grants ``PutObject`` on ``<table>/*``, which covers ``_versions/`` — so ``add_bases`` lands an
``UpdateBases`` commit naming any path at all. Measured on pylance 12.0.0 (lh279 m2/m4/s4): a planted base
let the planting table read another table's rows through it, froze that table's compaction and deletion
as a "shallow-clone source", and was laundered as maintenance by the lineage reconcile. Every decision
that trusted the manifest's list was a decision a writer made.

THIS RECORD IS THE CATALOG'S WORD INSTEAD. It is written by the catalog's own create and register doors,
through the control-root credential, never through a vend; and the register door's location-exclusivity
keeps every table prefix off the control root, so no vended session policy covers ``_bases/`` (lh279 s1:
without that rule a table registered at ``.`` or ``_bases`` is vended exactly that prefix).

KEYED BY THE TABLE ROOT LOCATION, not the table id. The consumers that read it — the base-reference
pre-pass and the lineage reconcile — hold URIs, not ids; medallion tiers have no uuid8 id at all; and a
location key survives the deregister→register an undrop performs, while a drop→re-declare mints a new
location and so a fresh, empty record (lh279 s3). The key is the path the location decodes to
(:func:`service_kit.lakehouse.base_refs.decoded_path`): the doors write the record under the catalog's
percent-encoded spelling while the pre-pass reads it under the decoded directory discovery lists (lh279 r6
spellings), and both name one table.

ONE PREDICATE DECIDES A BASE'S STANDING for every consumer (:func:`judge_base`): inside the table's own
root, inside an operator-configured external blob base, or named by this record. A fourth answer —
unrecorded — is what the doors refuse and the pre-pass reports.

Every IO function is BLOCKING — callers threadpool it, as they do for every control-root registry.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from service_kit.lakehouse import records
from service_kit.lakehouse.base_refs import decoded_path, location_in_store, names_a_location, normalise, store_of
from service_kit.lakehouse.features import BasePathRef
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import delete_record, record_key


_BASES_PREFIX = "_bases"

#: The record kind in the key. One kind today; it travels in the key so a second relation stored here
#: cannot collide with a table's record on the same location.
_KIND = "table"

#: Rounds :func:`claim_bases` spends when its create and a concurrent release interleave. The store
#: arbitrates each step; the bound turns a pathological livelock into an error rather than a hang.
_CLAIM_ATTEMPTS = 3


class BaseRole(StrEnum):
    """What a recorded base is FOR, which decides what a consumer may do with it."""

    #: A pointer base for ``Blob.from_uri`` descriptors; readable, never a data-file base.
    EXTERNAL_BLOB = "external_blob"
    #: A base a fragment's ``base_id`` may resolve data files through (a multi-base create).
    DATA = "data"
    #: Another table's root this one reads through after its create (a clone, a pinned derivation).
    DERIVED_FROM = "derived_from"


class BaseOrigin(StrEnum):
    """Which catalog door wrote the entry."""

    CREATE = "create"
    REGISTER = "register"
    SILVER = "silver"


class RecordedBase(BaseModel):
    """One base the catalog sanctioned for one table."""

    model_config = ConfigDict(frozen=True)

    #: The base location, normalised (:func:`service_kit.lakehouse.base_refs.normalise`).
    path: str
    #: The store the base resolves in (:func:`service_kit.lakehouse.base_refs.store_of`). The normalised
    #: ``path`` carries no scheme, and the same path under another scheme is another location. Read off
    #: ``path`` as spelled when none is given.
    store: str = ""
    role: BaseRole
    is_dataset_root: bool = False
    #: The manifest's alias for the base, carried for an operator; nothing decides on it.
    name: str | None = None
    origin: BaseOrigin
    #: The normalised root of the table this one derives from, for ``DERIVED_FROM``.
    source_table: str | None = None
    #: The source's tag that pins the relation, for a pinned ``DERIVED_FROM``.
    tag: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _the_store_its_spelling_names(cls, data: object) -> object:
        """``store`` read off ``path`` as spelled, before normalising drops the scheme, when none is given.

        A path that is neither a URI nor absolute names no store, and a guess would sanction the base in
        whichever store the guess picked — so it must come with its ``store``.
        """
        if not isinstance(data, dict) or data.get("store") or not isinstance(path := data.get("path"), str):
            return data
        if "://" not in path and not path.startswith("/"):
            raise ValueError(f"{path!r} names no store: spell it as a URI or an absolute path, or give its store")
        return {**data, "store": store_of(path)}

    @field_validator("path", "source_table")
    @classmethod
    def _a_location(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not names_a_location(value):
            raise ValueError(f"{value!r} does not name one location: a segment decodes to nothing, '.', '..', a control character or a new separator")
        return normalise(value)

    @property
    def identity(self) -> tuple[str, str, bool, BaseRole]:
        """What makes two entries the same base: where it is, what the manifest says it is, and what it is for.

        Not the ``origin`` or ``name``: a register that re-derives an entry a create already wrote is
        restating it, and counting it twice would make the record grow on every converge.
        """
        return self.path, self.store, self.is_dataset_root, self.role


class BaseRecord(BaseModel):
    """Every base the catalog sanctioned for the table rooted at ``location``."""

    #: The table's root location, normalised as the writer spelled it. The record's key and its self-check
    #: are the path it decodes to (:func:`record_key_for`, :func:`read_base_record`).
    location: str
    #: ADD-ONLY for a live table: an entry leaves only when the write that added it failed.
    entries: list[RecordedBase] = Field(default_factory=list)

    @field_validator("location")
    @classmethod
    def _a_location(cls, value: str) -> str:
        if not names_a_location(value):
            raise ValueError(f"{value!r} does not name one location: a segment decodes to nothing, '.', '..', a control character or a new separator")
        return normalise(value)


class BaseRegistry(BaseModel):
    """Where the records live: the control root, and the options that reach it."""

    control_root: str
    storage_options: StorageOptions = Field(default_factory=dict)


class BaseClaim(BaseModel):
    """What one :func:`claim_bases` call changed — exactly what :func:`release_claim` undoes."""

    location: str
    #: This call created the record.
    created: bool
    #: Entries this call added. All of them when ``created``.
    added: list[RecordedBase] = Field(default_factory=list)


class UnreadableBaseRecordError(RuntimeError):
    """A record exists and cannot be read as one.

    Distinct from "absent" on purpose: a caller treating an unreadable record as no record would
    demote a recorded clone source to a finding and hand its bytes to compaction.
    """


class BaseStanding(StrEnum):
    """Why a declared base may be honoured — or that nothing says it may."""

    #: Inside the table's own root: its branches, a same-root clone. Needs no read.
    OWN = "own"
    #: Inside an operator-configured external blob base, declared as a plain (non-root) base. Every
    #: vend already grants READ there, so declaring one adds no access. Needs no read.
    CONFIGURED = "configured"
    #: Named by the table's base record.
    RECORDED = "recorded"
    #: None of the above.
    UNRECORDED = "unrecorded"


class BaseJudgement(BaseModel):
    """The standing of one declared base, and the record entry that gave it, when one did."""

    ref: BasePathRef
    standing: BaseStanding
    entry: RecordedBase | None = None


def record_key_for(location: str) -> str:
    """The control-root key of the record for the table rooted at ``location``, in any spelling: the path it decodes to."""
    return record_key(_BASES_PREFIX, _KIND, decoded_path(location))


def read_base_record(registry: BaseRegistry, location: str) -> BaseRecord | None:
    """The record for the table rooted at ``location``, or ``None`` when there is none.

    Raises:
        UnreadableBaseRecordError: The record exists and is not JSON, not a record, or a record for
            another location.
    """
    try:
        found = records.read_json(registry.control_root, registry.storage_options, record_key_for(location))
    except ValueError as exc:  # a stored record that is not JSON
        raise UnreadableBaseRecordError(f"the base record for {normalise(location)!r} cannot be read: {exc}") from exc
    return None if found is None else _parsed(found[0], location)


def _distinct(entries: Sequence[RecordedBase]) -> list[RecordedBase]:
    seen: set[tuple[str, str, bool, BaseRole]] = set()
    kept: list[RecordedBase] = []
    for entry in entries:
        if entry.identity not in seen:
            seen.add(entry.identity)
            kept.append(entry)
    return kept


def _parsed(raw: dict[str, object], location: str) -> BaseRecord:
    """``raw`` as the record for the table rooted at ``location``: the self-check every read and claim shares.

    The record's location and ``location`` are compared as the paths they decode to, the form the key is
    derived from, so a record written under one spelling of a table reads under the other.
    """
    try:
        record = BaseRecord.model_validate(raw)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        raise UnreadableBaseRecordError(f"the base record for {normalise(location)!r} cannot be read: {exc}") from exc
    if decoded_path(record.location) != decoded_path(location):
        raise UnreadableBaseRecordError(f"the base record under {normalise(location)!r}'s key names {record.location!r}")
    return record


def claim_bases(registry: BaseRegistry, location: str, entries: Sequence[RecordedBase]) -> BaseClaim:
    """Ensure every entry is in the record for ``location``; return what this call changed.

    Creates the record when it is absent — an EMPTY entry list included, so a table with no foreign base
    still has a record — and otherwise adds the missing entries under the record's ETag. An entry
    already present (same :attr:`RecordedBase.identity`) costs no write.

    The store arbitrates both writes (``records.create_json`` put-if-not-exists, ``records.mutate_json``
    ETag-guarded), so two doors claiming one location converge instead of one overwriting the other.

    Raises:
        UnreadableBaseRecordError: The existing record cannot be read.
        records.RecordChangedError: The record kept changing under the add.
    """
    wanted = BaseRecord(location=location, entries=_distinct(entries))
    key = record_key_for(location)
    for _ in range(_CLAIM_ATTEMPTS):
        try:
            records.create_json(registry.control_root, registry.storage_options, key, wanted.model_dump(mode="json"))
        except records.RecordExistsError:
            pass
        else:
            return BaseClaim(location=wanted.location, created=True, added=wanted.entries)
        current = read_base_record(registry, location)
        if current is None:
            continue  # released between the create and the read; create again
        held = {entry.identity for entry in current.entries}
        if all(entry.identity in held for entry in wanted.entries):
            return BaseClaim(location=wanted.location, created=False)
        try:
            added = _add_missing(registry, key, wanted)
        except records.RecordMissingError:
            continue
        return BaseClaim(location=wanted.location, created=False, added=added)
    raise records.RecordChangedError(f"the base record for {wanted.location!r} kept appearing and disappearing across {_CLAIM_ATTEMPTS} attempts")


def _add_missing(registry: BaseRegistry, key: str, wanted: BaseRecord) -> list[RecordedBase]:
    """Add ``wanted``'s entries the stored record lacks, under its ETag; return the ones this write added."""
    added: list[RecordedBase] = []

    def _add(raw: dict[str, object]) -> dict[str, object]:
        record = _parsed(raw, wanted.location)
        present = {entry.identity for entry in record.entries}
        # Recomputed on every round: `mutate_json` re-applies this to a record another writer changed.
        added[:] = [entry for entry in wanted.entries if entry.identity not in present]
        return BaseRecord(location=record.location, entries=[*record.entries, *added]).model_dump(mode="json")

    records.mutate_json(registry.control_root, registry.storage_options, key, _add)
    return added


def release_claim(registry: BaseRegistry, claim: BaseClaim) -> None:
    """Undo one :func:`claim_bases` whose write never landed: delete the record it created, or remove the entries it added.

    A record this claim created is deleted outright even if a concurrent claim merged into it since.
    That direction is fail-closed: the merged table's base becomes unrecorded, which the doors refuse,
    rather than a record outliving the write it sanctioned and vouching for a base no manifest names.
    """
    key = record_key_for(claim.location)
    if claim.created:
        delete_record(registry.control_root, registry.storage_options, key)
        return
    if not claim.added:
        return
    removed = {entry.identity for entry in claim.added}

    def _remove(raw: dict[str, object]) -> dict[str, object]:
        record = _parsed(raw, claim.location)
        return BaseRecord(location=record.location, entries=[entry for entry in record.entries if entry.identity not in removed]).model_dump(mode="json")

    try:
        records.mutate_json(registry.control_root, registry.storage_options, key, _remove)
    except records.RecordMissingError:
        return


def forget_base_record(registry: BaseRegistry, location: str) -> bool:
    """Remove the whole record for a table whose bytes are gone; ``False`` when there was none.

    The one removal that is not a claim's undo: a record outliving the manifest it describes would
    sanction those bases for whatever is next written at the location.
    """
    return delete_record(registry.control_root, registry.storage_options, record_key_for(location))


def _standing_without_a_read(table_root: str, ref: BasePathRef, configured: Sequence[str]) -> BaseStanding | None:
    """OWN or CONFIGURED when the path alone says so; UNRECORDED when no record could; ``None`` when only the record can answer."""
    if not names_a_location(ref.path):
        # No record entry can name it (entries are validated locations), so no read is spent on it.
        return BaseStanding.UNRECORDED
    if location_in_store(table_root, ref.path):
        return BaseStanding.OWN
    if not ref.is_dataset_root and any(location_in_store(base, ref.path) for base in configured):
        return BaseStanding.CONFIGURED
    return None


def is_own(table_root: str, ref: BasePathRef) -> bool:
    """Whether the table rooted at ``table_root`` owns ``ref``: a location inside its root, in its store."""
    return _standing_without_a_read(table_root, ref, ()) is BaseStanding.OWN


def _recorded(ref: BasePathRef, record: BaseRecord | None) -> RecordedBase | None:
    if record is None:
        return None
    path, store = normalise(ref.path), store_of(ref.path)
    return next((entry for entry in record.entries if (entry.path, entry.store, entry.is_dataset_root) == (path, store, ref.is_dataset_root)), None)


def judge_base(table_root: str, ref: BasePathRef, *, configured: Sequence[str], record: BaseRecord | None) -> BaseJudgement:
    """The standing of one base the table rooted at ``table_root`` declares.

    ``configured`` is the operator's external-blob base list (``LANCE_EXTERNAL_BLOB_BASES``). A base
    inside one counts only when it is declared as a plain base: a configured base declared as a DATASET
    ROOT resolves data files through it, which no configuration sanctioned. A recorded entry matches on
    its normalised path AND its root-ness, for the same reason. Every one of the three compares the store
    as well as the path (:func:`service_kit.lakehouse.base_refs.store_of`): the table's own path, a
    configured base or a recorded one spelled under another scheme is another location.
    """
    fast = _standing_without_a_read(table_root, ref, configured)
    if fast is not None:
        return BaseJudgement(ref=ref, standing=fast)
    entry = _recorded(ref, record)
    return BaseJudgement(ref=ref, standing=BaseStanding.RECORDED if entry else BaseStanding.UNRECORDED, entry=entry)


def judge_bases(
    table_root: str,
    refs: Sequence[BasePathRef],
    *,
    configured: Sequence[str],
    load_record: Callable[[], BaseRecord | None],
) -> list[BaseJudgement]:
    """:func:`judge_base` over every declared base, reading the record at most once and only when a base needs it.

    The zero-IO path answers every base inside the table's own root and every configured one, which is
    what the live estate declares (lh279 inventory: 732 of 735 non-self edges on one sweep tick), so
    the common table costs no record read at all.
    """
    judgements: list[BaseJudgement] = []
    loaded = False
    record: BaseRecord | None = None
    for ref in refs:
        fast = _standing_without_a_read(table_root, ref, configured)
        if fast is not None:
            judgements.append(BaseJudgement(ref=ref, standing=fast))
            continue
        if not loaded:
            record, loaded = load_record(), True
        judgements.append(judge_base(table_root, ref, configured=configured, record=record))
    return judgements
