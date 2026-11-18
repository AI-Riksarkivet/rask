"""Maintenance policy registry (#50) — the catalog↔compaction contract, so it lives in lakehouse
(the same reasoning as the outbox: one service writes the records, another enforces them, and a
per-service copy of the format would drift).

Stateless-over-object-store, the same shape as the warehouse registry, written once in
:mod:`lakehouse.record_store`; this module is the policy vocabulary over it. Each policy is one
JSON record under ``<control_root>/_policies/``, written by the catalog (owner-gated) and read directly off
the bucket by maintenance:

* table policy — keyed by the table's ``uuid`` (ADR 0173 §8, [[LH-523]]), so a rename keeps it and a table
  created later under the same name holds none of it;
* namespace policy — keyed by the namespace's ``uuid``, governing every table under the namespace;
* project policy (#84) — the tenant-wide default, keyed by the project id: instead of a uuid it carries
  ``buckets`` (the project's active warehouse buckets, resolved from the warehouse registry at set time),
  matched at the bucket level.

A table or namespace record also keeps the object's name under ``id``, as it was when the policy was set, for
display. Resolution order (:func:`resolve_policy`): a table policy wins over the nearest namespace policy wins
over a project policy wins over maintenance's global defaults.

Trust boundary: like the warehouse registry, who wrote a record is trusted — the write path is the catalog's
owner-gated API, and a principal with direct write access to the control bucket is already inside the storage trust
boundary. What a record says is not: a record that reached the store past the door is read back strictly
(:class:`PolicySettings`), and one that cannot be read refuses what it governs rather than letting the defaults govern
it. A drop or deregister ends a table's record through :mod:`lakehouse.object_records`.

Fields: ``retention_days`` / ``retain_versions`` (old-version cleanup overrides — Lance itself exempts
tag-pinned versions from cleanup, so every tagged version survives any policy), ``compact_enabled``
(opt a dataset out of maintenance entirely), ``compact_interval_hours`` (cadence — maintenance skips the
dataset until the interval has elapsed since its last maintenance; maintenance records that stamp under
``_policies/state/<table uuid>/``, a separate prefix so the two writers never contend, which the table's ending ends). All IO is blocking; callers
threadpool it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from lakehouse import control_prefixes, records, stored_objects, warehouse_records
from lakehouse.record_store import delete_record, list_records, put_record, read_record, record_key
from lakehouse.storage import split_s3_uri
from lakehouse.storage_options import StorageOptions, on_s3


log = logging.getLogger(__name__)

#: Where the records live on the control root, one per object under ``record_key(PREFIX, kind, record.key)``.
PREFIX: Final = control_prefixes.POLICIES

#: The event stem for this registry's store warnings (`<stem>_malformed`, `<stem>_unreadable`).
_EVENT = "maintenance_policy"


#: What a policy governs: one table, a namespace and every table under it, or a project's buckets.
type PolicyKind = Literal["table", "namespace", "project"]

#: The kinds whose records are keyed by the object's uuid; a project's is keyed by its id.
_UUID_KEYED_KINDS: Final = frozenset({"table", "namespace"})


class UnreadablePolicyError(RuntimeError):
    """A policy record that may govern a table could not be read, so whether it keeps the table's history is unknown and
    nothing that would reclaim it may be decided. Raised as itself when the store did not answer the read, which a later
    read may get past; :class:`MalformedPolicyError` when the record is there and cannot be read as a policy."""


class MalformedPolicyError(UnreadablePolicyError):
    """A policy record that may govern a table is there and cannot be read as a policy: not JSON, not a record of its kind
    and key, or a setting no value of its type reads (``retention_days: "30d"``). It may be a legal hold, and no later
    read changes it until someone writes the record again."""


class PolicySettings(BaseModel):
    """What a stored policy record tells maintenance, parsed strictly: every reader of a setting reads it here, each time
    it reads one (:attr:`PolicyRecord.settings`), so no reader takes a setting the strict parse would refuse.

    A record reaches the store through the catalog's policy door, which types it (``catalog.schemas.PolicyRequest``), or
    past it: written by hand, by an older build, or by the catalog's own boot step. So the record is parsed again on the
    way out, with the door's types, and a value they refuse is a record that cannot be read, never a default:
    ``bool("false")`` is true, and a hold written ``cleanup_enabled: "false"`` read that way would be no hold. Pydantic's
    lax mode reads ``"false"``, ``"no"`` and ``0`` as false and ``"30"`` as 30, and refuses ``"30d"`` and ``"maybe"``.
    Of the door's bounds only the floors are kept, each a value below which the setting has no meaning (a retention of
    no days, a cadence of no hours, a fragment of no rows); its ceilings are the door's to keep for what it is asked.
    The record's own identity (``kind``, ``id``, ``uuid``, ``buckets``) and the settings only the catalog reads are not
    maintenance's, and are left to their readers.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    retention_days: int | None = Field(default=None, ge=1)
    retain_versions: int | None = Field(default=None, ge=1)
    cleanup_enabled: bool = True
    compact_enabled: bool = True
    optimize_indices_enabled: bool = True
    compact_interval_hours: int | None = Field(default=None, ge=1)
    target_rows_per_fragment: int | None = Field(default=None, ge=1)


class PolicyRecord(BaseModel):
    """One maintenance policy as the registry keeps it: what it governs, and the settings its door stored.

    The identity is typed and checked at the read: a record of no known kind, or one that names nothing its kind is
    matched by (a table's or a namespace's ``uuid``, a project's ``id`` and ``buckets``), could never match a dataset and
    is no policy record. The settings ride as the door stored them (``extra="allow"``) and are parsed where one is read
    (:attr:`settings`): a record whose identity reads and whose setting does not still governs its tables, and refuses
    them, where a record left out at the read would hand them to the defaults.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    kind: PolicyKind
    #: The object's name as it was when the policy was set, for display; a project's is its id, which keys the record.
    id: str = ""
    #: The table's or the namespace's uuid, which keys the record ([[LH-523]]); a project's record carries none.
    uuid: str | None = None
    #: A project's active warehouse buckets, resolved at set time, which a dataset is matched by.
    buckets: list[str] = Field(default_factory=list)
    #: A table's location when the policy was set, by which a listing names the table as it is named now.
    location: str | None = None

    @model_validator(mode="after")
    def _names_what_its_kind_is_matched_by(self) -> Self:
        if self.kind in _UUID_KEYED_KINDS:
            if not self.uuid:
                raise ValueError(f"a {self.kind} policy record names no uuid")
        elif not self.id or not self.buckets:
            raise ValueError("a project policy record names no id, or no bucket")
        return self

    @property
    def key(self) -> str:
        """What the record is stored under: its object's ``uuid`` for a table or namespace, its ``id`` for a project."""
        return str(self.uuid) if self.kind in _UUID_KEYED_KINDS else self.id

    @property
    def settings(self) -> PolicySettings:
        """The settings the record holds, parsed strictly.

        Raises:
            MalformedPolicyError: A setting holds a value its type and bounds refuse.
        """
        try:
            return PolicySettings.model_validate(self.stored())
        except ValidationError as exc:
            raise MalformedPolicyError(f"the {self.kind} policy record {self.key} cannot be read as one: {exc}") from exc

    @property
    def keeps_whole_history(self) -> bool:
        """Whether the policy keeps the whole version history of what it governs (``cleanup_enabled`` false: a legal hold,
        or a time-travel window that is the product), so nothing of that history is reclaimed.

        Raises:
            MalformedPolicyError: The record's settings cannot be read, so whether it is a hold is unknown.
        """
        return not self.settings.cleanup_enabled

    def stored(self) -> dict[str, Any]:
        """The record as it is written: every field its door stored, and a ``uuid``, ``buckets`` and ``location`` only
        where it names them."""
        raw = self.model_dump()
        for field, named in (("uuid", self.uuid), ("buckets", self.buckets), ("location", self.location)):
            if not named:
                del raw[field]
        return raw


def read(raw: object, *, at: str) -> PolicyRecord:
    """``raw`` as a policy record; ``at`` names where it sits, for the refusal.

    Raises:
        MalformedPolicyError: It is no policy record.
    """
    try:
        return PolicyRecord.model_validate(raw)
    except ValidationError as exc:
        raise MalformedPolicyError(f"the policy record at {at} cannot be read as one: {exc.error_count()} field(s) are not what a record holds") from exc


#: How many read, merge and conditional-write rounds a set runs before refusing: contention is admins setting one
#: object's policy at once, so a round that keeps losing is answered as a concurrent change rather than retried for ever.
_SET_ATTEMPTS: Final = 5


def set_policy(
    control_root: str, storage_options: StorageOptions, kind: PolicyKind, key: str, compose: Callable[[PolicyRecord | None], PolicyRecord]
) -> PolicyRecord:
    """Write the record ``compose`` makes of the record stored under ``key`` (:attr:`PolicyRecord.key`); the record written.

    The write is conditional on what it merged onto: a replace on the ETag of the record ``compose`` was handed, a first
    write on there being none. A set or a delete landing between the read and the write refuses that write, and the
    merge runs again on what stands then, so a partial set never drops a field another set wrote meanwhile and never
    brings back a record a delete removed. ``compose`` may run once per round, so it is a pure function of its argument.
    A stored body that is not JSON holds nothing to keep and is replaced: writing a policy again is what clears it. A
    stored object that is no policy record is refused, since the settings it holds may be a hold a merge onto nothing
    would drop; deleting the policy is what clears that one.

    Raises:
        MalformedPolicyError: A JSON object is stored under ``key`` and cannot be read as a policy record.
        records.RecordChangedError: Every round lost to another write.
        OSError: The store refused a read or the write.
    """
    full_key = record_key(PREFIX, kind, key)
    for _ in range(_SET_ATTEMPTS):
        try:
            stored = records.read_json_at(control_root, storage_options, full_key).stored
        except ValueError:
            record = compose(None)
            put_record(control_root, storage_options, full_key, record.stored())
            return record
        try:
            if stored is None:
                record = compose(None)
                records.create_json(control_root, storage_options, full_key, record.stored())
            else:
                record = compose(read(stored.record, at=full_key))
                records.replace_json(control_root, storage_options, full_key, record.stored(), etag=stored.etag)
        except (records.RecordExistsError, records.RecordChangedError):
            continue
        return record
    raise records.RecordChangedError(f"the {kind} policy {key} kept changing across {_SET_ATTEMPTS} attempts")


def get_policy(control_root: str, storage_options: StorageOptions, kind: PolicyKind, key: str) -> PolicyRecord | None:
    """The policy record stored under ``key`` (:attr:`PolicyRecord.key`), or ``None`` when none is set.

    Raises:
        MalformedPolicyError: A record is stored there and cannot be read as a policy of ``kind`` for ``key``: it may be
            a hold, so it is never answered as unset.
        OSError: The store failed the read.
    """
    return _keyed(control_root, storage_options, kind, key)


def delete_policy(control_root: str, storage_options: StorageOptions, kind: PolicyKind, key: str) -> bool:
    """Remove one policy record; ``False`` when it did not exist (delete is idempotent)."""
    return delete_record(control_root, storage_options, record_key(PREFIX, kind, key))


def list_policies(control_root: str, storage_options: StorageOptions) -> list[PolicyRecord]:
    """Every readable policy record (unordered). Absent prefix yields ``[]``.

    State stamps live under ``_policies/state/`` and are excluded (non-recursive listing). One corrupt
    or unreadable record is SKIPPED with a warning, never allowed to void the others — a tick that
    silently dropped every policy would maintain opted-out datasets and ignore protective retention.
    """
    out: list[PolicyRecord] = []
    for raw in list_records(control_root, storage_options, PREFIX, event=_EVENT):
        try:
            out.append(read(raw, at=PREFIX))
        except MalformedPolicyError:
            log.warning("maintenance_policy_malformed", extra={"id": raw.get("id"), "kind": raw.get("kind")})
    return out


def resolve_policy(policies: list[PolicyRecord], uri: str, *, table_uuid: str, namespace_uuids: Sequence[str]) -> PolicyRecord | None:
    """The policy governing the table ``table_uuid`` names, whose dataset is ``uri`` (``s3://<bucket>/<path>``).

    The table's own record wins, else the record of the nearest namespace in ``namespace_uuids`` (the table's
    ancestors, outermost first), else a project record (#84) whose ``buckets`` contain the dataset's bucket,
    else ``None`` (maintenance's global defaults apply). A branch is its table's dataset too, so its table's
    record governs it.

    The project record is strictly the LAST fallback. A bucket belongs to one project, so DISTINCT project
    records both claiming the dataset's bucket are a registry misconfiguration (a possible cross-tenant
    claim): the tier then resolves to NO match with a warning — never first-encountered-wins, which would let
    record ordering decide whose retention policy destroys whose version history (audit 2026-07-23).
    """
    tables = _by_uuid(policies, "table")
    if table_uuid in tables:
        return tables[table_uuid]
    namespaces = _by_uuid(policies, "namespace")
    for namespace_uuid in reversed(namespace_uuids):
        if namespace_uuid in namespaces:
            return namespaces[namespace_uuid]
    return _project_policy(policies, uri)


def governing_policy(
    control_root: str, storage_options: StorageOptions, *, claims_root: str, location: str, table_uuid: str, namespace_uuids: Sequence[str]
) -> PolicyRecord | None:
    """The policy governing the table ``table_uuid`` names at ``location``, read by key: the table's own record, else the
    nearest namespace's in ``namespace_uuids`` (outermost first), else the record of the project the bucket's claim names
    as its owner, where that record names the bucket; ``None`` for the global defaults. The precedence is
    :func:`resolve_policy`'s, which a caller may hand the one record this returns; the project level differs only where
    several project records name one bucket, which :func:`resolve_policy` refuses to choose between and the store's
    claim, written once by the warehouse that took the bucket, settles. The policy records are read under
    ``control_root`` and the bucket's claim under ``claims_root``, the catalog's registry, which a deployment may keep
    apart from its policies.

    Each record is read by its key, so what this costs is the table's depth, never the estate's policies. STRICT: a record
    that is there and cannot be read as a policy, and a read the store fails, raise, since the record may be a legal hold,
    and a caller that went on without it would reclaim history its owner kept. A record that is returned holds settings
    :attr:`PolicyRecord.settings` reads.

    Raises:
        MalformedPolicyError: A record, or the claim naming the bucket's project, is there and cannot be read as one.
        UnreadablePolicyError: The store failed a read.
    """
    try:
        if (own := _governing(control_root, storage_options, "table", table_uuid)) is not None:
            return own
        for namespace_uuid in reversed(namespace_uuids):
            if (nearest := _governing(control_root, storage_options, "namespace", namespace_uuid)) is not None:
                return nearest
        bucket = split_s3_uri(location)[0] if on_s3(location) else ""
        project = warehouse_records.bucket_project(claims_root, storage_options, bucket) if bucket else None
        tenant = _governing(control_root, storage_options, "project", project) if project is not None else None
    except OSError as exc:
        raise UnreadablePolicyError(f"a maintenance policy that may govern the table {table_uuid} could not be read: {exc}") from exc
    except (MalformedPolicyError, ValueError) as exc:
        raise MalformedPolicyError(f"a maintenance policy that may govern the table {table_uuid} cannot be read as one: {exc}") from exc
    return tenant if tenant is not None and bucket in tenant.buckets else None


def _governing(control_root: str, storage_options: StorageOptions, kind: PolicyKind, key: str) -> PolicyRecord | None:
    """The record :func:`_keyed` reads, its settings parsed, for a caller about to act on them.

    Raises:
        MalformedPolicyError: The record is no policy of ``kind`` for ``key``, or holds a setting :class:`PolicySettings` refuses.
        OSError: The store failed the read.
    """
    record = _keyed(control_root, storage_options, kind, key)
    if record is not None:
        _ = record.settings
    return record


def _keyed(control_root: str, storage_options: StorageOptions, kind: PolicyKind, key: str) -> PolicyRecord | None:
    """The ``kind`` policy record stored under ``key`` (:attr:`PolicyRecord.key`), or ``None`` when none is.

    Raises:
        MalformedPolicyError: The stored record is not a policy record of ``kind`` for ``key``.
        OSError: The store failed the read.
    """
    full_key = record_key(PREFIX, kind, key)
    try:
        raw = read_record(control_root, storage_options, full_key)
    except ValueError as exc:
        raise MalformedPolicyError(f"the policy record at {full_key} cannot be read as one: {exc}") from exc
    if raw is None:
        return None
    record = read(raw, at=full_key)
    if record.kind != kind or record.key != key:
        raise MalformedPolicyError(f"the {kind} policy record stored for {key} is not one")
    return record


def _by_uuid(policies: list[PolicyRecord], kind: PolicyKind) -> dict[str, PolicyRecord]:
    return {str(record.uuid): record for record in policies if record.kind == kind and record.uuid}


def _project_policy(policies: list[PolicyRecord], uri: str) -> PolicyRecord | None:
    """The one project record whose ``buckets`` contain the dataset's bucket, ``None`` when none or several do."""
    bucket = uri.removeprefix("s3://").split("/", 1)[0]
    hits = [record for record in policies if record.kind == "project" and bucket in record.buckets]
    if len(hits) > 1:
        log.warning("maintenance_policy_project_overlap", extra={"bucket": bucket, "projects": sorted(record.id for record in hits)})
        return None
    return hits[0] if hits else None


#: Maintenance's and the event lane's stamps, one directory per table uuid ([[LH-562]]), so a table's ending ends them all
#: (:func:`end_table_state`, at the control root, which is the policy root the stamps are written at, as the policy
#: records the catalog writes there already require) and none outlives the table it paced. A table registered later at
#: the same location holds another uuid and starts unstamped.
STATE_PREFIX: Final = f"{PREFIX}/state"


def _table_state(table_uuid: str) -> str:
    return f"{STATE_PREFIX}/{table_uuid}"


def cadence_key(table_uuid: str, uri: str) -> str:
    """The key of the cadence stamp of the dataset ``uri`` of the table ``table_uuid``."""
    # Per DATASET: a branch is a dataset of its own under its table's uuid, and a shared stamp would let one ref's pass
    # starve the others until the interval elapsed (audit 2026-07-16).
    return record_key(_table_state(table_uuid), "cadence", uri)


class CadenceStamp(BaseModel):
    """When a pass last maintained one dataset, and the policy it ran under."""

    model_config = ConfigDict(frozen=True)

    #: The policy the pass ran under, as ``<kind>:<key>``.
    policy: str
    #: The pass's clock. It carries its offset, so it compares with every other reading of the pass's clock.
    last_maintained_at: AwareDatetime

    @classmethod
    def under(cls, record: PolicyRecord, at: datetime) -> CadenceStamp:
        """The stamp of a pass at ``at`` under the policy ``record``."""
        return cls(policy=f"{record.kind}:{record.key}", last_maintained_at=at)


def read_state(control_root: str, storage_options: StorageOptions, record: PolicyRecord, *, table_uuid: str, uri: str) -> datetime | None:
    """When a pass last maintained the dataset ``uri`` of the table ``table_uuid`` under the policy ``record``, or
    ``None``. A stamp taken under another policy paces nothing: the policy that governs now has not maintained the
    dataset yet. Neither does a stamp that cannot be read as a time with its offset: an absent stamp maintains, and one
    this reading refused would otherwise silence maintenance for as long as it stood.

    Raises:
        OSError: The store failed the read.
    """
    key = cadence_key(table_uuid, uri)
    try:
        held = read_record(control_root, storage_options, key)
        if held is None:
            return None
        stamp = CadenceStamp.model_validate(held)
    except ValueError:
        log.warning("maintenance_policy_state_malformed", extra={"key": key})
        return None
    return stamp.last_maintained_at if stamp == CadenceStamp.under(record, stamp.last_maintained_at) else None


def write_state(control_root: str, storage_options: StorageOptions, record: PolicyRecord, *, table_uuid: str, uri: str, at: datetime) -> None:
    """Persist a pass's stamp for one dataset under the policy ``record`` (overwrite).

    Raises:
        pydantic.ValidationError: ``at`` carries no offset.
    """
    put_record(control_root, storage_options, cadence_key(table_uuid, uri), CadenceStamp.under(record, at).model_dump(mode="json"))


def end_table_state(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    """Delete every stamp the table ``table_uuid`` holds; whether any existed. Writes no directory marker
    (:func:`stored_objects.delete_objects`), since maintenance, which ends a purged table's records, may put none here."""
    listed = stored_objects.list_under(f"{control_root.rstrip('/')}/{_table_state(table_uuid)}", storage_options)
    stored_objects.delete_objects(control_root, storage_options, [stored.path for stored in listed])
    return bool(listed)
