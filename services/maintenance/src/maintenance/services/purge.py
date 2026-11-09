"""The expired-trash purge (#79) — the ONLY module in this service that reclaims a dropped object.

Everything mutating for reclamation lives here so that :mod:`maintenance.services.reconcile` stays
report-only and its AST gate (``tests/unit/test_reconcile_report.py``) stays green. The split is the
design, not a filing convention: the report is what earns the delete permission, and a reporter that can
delete cannot be trusted to have earned it.

**Trash purge first, orphan reclaim later — deliberately.** A trash record names ONE path that the
catalog itself recorded at drop time, so the delete is bounded and its target is a fact. The orphan half
is prefix subtraction: "list the bucket, subtract what a live manifest references", a rule with five
documented false-positive classes (branches, multi-base clones, MemWAL shards, data overlays, blob
sidecars) whose first live run called 29 MB of real page images reclaimable. That half stays REPORT-ONLY.

What the purge does per record, in this order, each step idempotent so a crash mid-record re-runs the
whole record cleanly on the next tick ([[XC-084]]: maintenance holds no write of its own on the store and no client that
writes grants, so every write is one the catalog makes or vends for this record alone):

1. **delete the recorded table's bytes**, under the session the catalog vends for this record once its own reads find it
   past its deadline, at a place whose claim records the record's uuid and no live table holds. By key, under the key
   its location decodes to and the reservation marker under its spelling (:func:`lakehouse.table_paths.spellings`), and
   in the table's own data directories. The listings that sum the reclaimed bytes double as the existence probes; an
   already-absent path is idempotent success. A namespace and a declared-only table name no bytes and skip this.
2. **have the catalog end the record** (``DELETE /management/v1/trash/{kind}/{uuid}``): it revokes the grants the object
   still holds, ends the records it keeps under its uuid, a table's claim and base record, and the trash record last,
   once its own listing finds the place empty. Any failure before it leaves the record and the next tick retries.
3. **announce it** on the control stream (best-effort).

Four refusals stand in front of all of that, in order, and each exists because of a specific way this
could destroy live data:

* **STILL REGISTERED.** The catalog's undrop registers and THEN clears the record
  (``tables.py`` / ``namespaces.py``); a crash in between — or a manual ``register_table`` at the same
  location — leaves a LIVE table with a stale trash record. Purging that record deletes a live table's
  bytes. The same holds when ANOTHER id resolves to the location (a rename or a register attached the
  bytes under a new id, [[LH-204]]), so the id and the location are checked against ``__manifest`` (the
  spec's own object index, which is the only place a namespace exists at all) TWICE: once against the tick's shared snapshot, as a cheap filter
  over every due record, and again in :func:`_purge_one` at the last instant before the first mutation.
  The second read is the one that closes the undrop window — a snapshot taken before the loop cannot
  see a registration that lands during it, and undrop re-registers BEFORE clearing the record, which is
  exactly that window. Either read failing refuses EVERY record rather than degrading to "purge
  anyway".
* **namespace / empty location.** A ``kind="namespace"`` record (#96) and a declared-only table both
  carry ``location=""`` — there are no bytes, so the path checks do not apply and the catalog ends the record
  without a delete.
* **outside the maintained estate.** A record's ``location`` is DATA; nothing constrains it. It must
  resolve under a bucket this service maintains (the configured sweep buckets plus every registered,
  non-deactivated warehouse) or it is refused. A deactivated warehouse is outside that set on purpose:
  quarantine means nobody touches those bytes, least of all a reclaimer.
* **a store root or a control prefix.** ``s3://bkt`` as a location would ``delete_dir`` a whole bucket;
  a path through ``_trash``/``_projects``/``_warehouses``/``_policies``/``_protection``/``__manifest``
  would delete the estate's own registries.

And one gate stands in front of the whole pass: :func:`report_is_clean`. The purge consumes the drift
report from the SAME tick, and reclaims nothing unless that report found no drift, could check every
category it did not deliberately skip, and completed every scan it started.

**Not proven against real object storage yet.** The suite below drives a real local-filesystem control
root and real Lance datasets through the real backend, which is what makes the refusal ladder honest —
but #80 (the sweep vs a live RustFS estate) is still open, so "tests green" is not "reclamation
witnessed". The flag ships OFF.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from typing import Any

import lance
import pyarrow.fs as pafs
from botocore.exceptions import BotoCoreError, ClientError
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from lakehouse import trash, warehouse_records
from lakehouse.base_refs import decoded_path, decoded_segments, location_within, names_a_location, store_of
from lakehouse.base_registry import BaseRegistry, RecordedBase, UnreadableBaseRecordError
from lakehouse.control_emit import ControlEmitter, NoopControlEmitter, emit_control
from lakehouse.control_events import ControlAction, ControlObjectType, ControlUuids
from lakehouse.control_prefixes import CONTROL_PREFIXES
from lakehouse.data_directories import delete_directories, directories_to_end
from lakehouse.objectfs import fs_and_base
from lakehouse.storage_options import StorageOptions
from lakehouse.stored_objects import StoredObject, delete_objects, list_under
from lakehouse.table_paths import objects_at, spellings
from maintenance.core.config import MaintenanceSettings, shared_lance_session
from maintenance.core.metrics import record_trash_purge
from maintenance.services.credentials import MaintenanceDenied, TableNotGoverned, VendUndecided, end_trash, note_trash_refusal, reclaim_options_for
from maintenance.services.naming import manifest_uuid
from maintenance.services.reconcile import MANIFEST_DIR, NON_GATING_CATEGORIES, ReconcileReport


log = logging.getLogger(__name__)

#: The principal recorded on every revoke and every control event. Not a user: nobody asked for this
#: delete at the moment it happens — the grace period expiring did. An audit row naming the dropper
#: would be a lie about who acted.
ACTOR = "service:maintenance"


_STILL_REGISTERED = "still registered — recovered or re-registered since the drop, so these bytes are LIVE"
_LOCATION_LIVE = "a registered table's location contains or sits inside these bytes, so they are LIVE"
_MANIFEST_UNREADABLE = "the object manifest could not be read, so liveness cannot be re-checked — refusing to delete blind"
_PURGE_OFF = (
    "MAINTENANCE_TRASH_PURGE_ENABLED is off — reclamation is opt-in and report-only is the shipped default "
    "(an expired drop's grants are already released; turning it on deletes its bytes); set MAINTENANCE_TRASH_PURGE_DRY_RUN to see what it would reclaim first"
)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


class PurgedRecord(BaseModel):
    """One expired trash record whose bytes and grants are now gone."""

    kind: str
    id: str
    #: The path that was deleted — ``""`` for a namespace record, which owns no bytes.
    location: str
    expires_at: str
    bytes_deleted: int = 0
    files_deleted: int = 0
    tuples_revoked: int = 0


class RefusedRecord(BaseModel):
    """One expired trash record the purge declined to act on, and WHY.

    A refusal is a finding, not an error: the common cases (still registered, outside the maintained
    estate) are precisely the states where deleting would have destroyed something live.
    """

    kind: str
    id: str
    reason: str
    #: How many ticks have now refused this record, INCLUDING this one — persisted on the trash record
    #: itself, so the count survives a tick. Without it a record refused every five minutes for thirty
    #: days is indistinguishable from one refused once, and a permanent exclusion — the state that
    #: actually needs a human — reads as a transient one. ``None`` when the annotation could not be
    #: written (the record was cleared concurrently, or the conditional write lost its race); the
    #: refusal itself is still reported, because the bookkeeping is the less important half.
    attempts: int | None = None


class PlannedRecord(BaseModel):
    """One record a DRY RUN would have reclaimed — the plan, not the deed.

    Deliberately NOT a ``PurgedRecord`` with zeroed counters: ``bytes_deleted=0`` on a plan reads as
    "there was nothing there", which is the one thing an operator inspecting a reclamation must not be
    told by accident. The byte counts are genuinely unknown until the delete walks the prefix.
    """

    kind: str
    id: str
    location: str
    expires_at: str


class TrashPurgeReport(BaseModel):
    """One tick's reclamation, machine-readable and honest about what it did NOT do.

    ``ran`` distinguishes "the purge executed and found nothing due" from "the purge never got past its
    gates" — the same distinction the drift report keeps between a zero count and an absent category.
    ``due`` is only meaningful when ``ran`` is true or the gate blocked after the listing: with the flag
    off nothing is listed at all, and ``reason`` says so.
    """

    enabled: bool = False
    ran: bool = False
    #: Why the purge did not run, when it did not. ``None`` on a run.
    reason: str | None = None
    due: int = 0
    #: True when this pass PREVIEWED rather than reclaimed. ``purged`` is then empty by construction and
    #: ``would_purge`` carries the plan — the two are never both populated, so a reader cannot mistake a
    #: preview for a reclamation.
    dry_run: bool = False
    purged: list[PurgedRecord] = Field(default_factory=list)
    #: What a real purge would have reclaimed on this tick. Populated only on a dry run.
    would_purge: list[PlannedRecord] = Field(default_factory=list)
    refused: list[RefusedRecord] = Field(default_factory=list)
    #: Records past the per-tick cap. REPORTED, never silently dropped — the next tick takes them.
    capped: int = 0
    #: Drift categories the report deliberately skipped. They do NOT block the purge (the shipped
    #: config skips ``orphan_files``, so blocking on a skip would make reclamation unreachable), but
    #: they are copied here so a purge can never read as "the whole estate was verified".
    skipped_categories: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Selection + the gate
# --------------------------------------------------------------------------- #


def due_records(control_root: str, storage_options: StorageOptions) -> list[dict[str, Any]]:
    """Every trash record past its deadline and released, OLDEST FIRST.

    Released, since the catalog vends a record's bytes session and ends it only then (``catalog.services.trash_reclaim``):
    the release waits out the settle an undrop admitted at the deadline needs, and tells each person who loses access.
    The reconcile tick releases before it purges, so a record is purged on the tick that releases it.

    One rule for "what is expired", shared with the sweep's report-only log, so the thing that gets
    reported and the thing that gets deleted can never be two different sets. ``trash.expired`` refuses
    an undated or unparseable record, which is the fail-toward-not-deleting direction.

    Ordered by refusal count, then oldest-first. `trash_purge_max_per_tick` slices this list BEFORE
    the refusal checks run, so an oldest-first order lets a permanently-refused record hold the same
    slot on every tick and the queue behind it never moves (measured 2026-09-20: 18 of a 25 cap).
    """
    return due_from(trash.list_all(control_root, storage_options))


def due_from(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """:func:`due_records` without the read — the expiry rule applied to records already in hand.

    Split out for F6(d): the sweep now reads the trash index ONCE and derives two sets from it (the
    datasets it must not rewrite, and this report-only due list). Before the split it called
    `due_records`, which re-listed the whole index — a second full read per tick for a log line.

    The rule itself stays in ONE place on purpose: the set that gets reported and the set that gets deleted can never be
    two different answers.
    """
    return sorted((record for record in trash.expired(records) if trash.is_released(record)), key=_drain_order)


def _drain_order(record: dict[str, Any]) -> tuple[int, str]:
    """Sort key: refusal count, then expiry. An unreadable count sorts as never-tried."""
    attempts = record.get("attempts")
    return (attempts if isinstance(attempts, int) and attempts > 0 else 0, str(record.get("expires_at") or ""))


def report_is_clean(report: ReconcileReport) -> str | None:
    """The gate: ``None`` when this tick's drift report certifies the estate, else the blocking reason.

    CLEAN := no findings, no category unavailable, no scan incomplete, and no skipped category that
    represents a COVERAGE GAP.

    A skip blocks or not depending on WHICH KIND it is (#128a), and this used to be a blanket pass.
    The old docstring defended that with: "the shipped configuration skips ``orphan_files`` … so
    treating a skip as drift would make the purge unreachable in every real deployment." That was
    true, and the reasoning was CIRCULAR — it held only because `orphan_scan_enabled` defaulted False
    and the chart shipped no lever to turn it on, so the scan could never run anywhere. The purge
    therefore reclaimed bytes against a file layer nobody had ever inspected, and the report printed
    the same thing for "we looked and it was clean" as for "we didn't look".

    So the two kinds are now distinguished by `CategorySkipped.coverage_gap`:

      * NOT a gap — ``unbound_namespaces`` with warehouses off. The rule does not apply; there are no
        bindings, nothing was missed. Blocking would make the purge unreachable on every single-bucket
        deployment over a question with no answer to give.
      * A GAP — ``orphan_files`` with the scan off. The rule applies and we chose not to run it.

    The lever landed with this change (chart ``maintenance.orphanScan``), which is what makes blocking
    reachable rather than fatal: an operator who wants the purge turns the scan on.

    An UNAVAILABLE or INCOMPLETE category blocks for the neighbouring reason: those are questions the
    report tried to answer and could not, and reclaiming on the strength of a scan that half-failed is
    the failure mode the whole report-first rule exists to prevent.

    Order matters. Real findings are reported FIRST because they are immediately actionable, whereas a
    gap is a configuration change — a caller staring at "turn the scan on" while three orphans sit
    unmentioned is being told the less useful of two true things.
    """
    if report.total:
        # THE GATING CATEGORIES ONLY, matching what `report.total` counts. `NON_GATING_CATEGORIES`
        # (`unbound_namespaces` among them) are excluded from the total because none is a storage fact,
        # and `unbound_namespaces` HAS NO DOOR — no endpoint binds a legacy namespace. Naming them here
        # pointed an operator at work that cannot be done and would not have unblocked the purge if it
        # could, which is the same failure the total's own exclusion exists to prevent: the categories that
        # DO matter hidden behind ones that never fall. Measured live 2026-09-10, where 13 findings were
        # reported "across" four categories and two of them contributed zero.
        # `orphan_files` is named by its BLOCKING count, not its total: the category can carry hundreds
        # of findings that the ordinary sweep will collect on its own, and naming it on those would send
        # an operator to work that time does for free while the real residue hides behind the number.
        blocking = {**report.counts, "orphan_files": report.orphan_files_blocking}
        drifting = sorted(name for name, count in blocking.items() if count and name not in NON_GATING_CATEGORIES)
        return f"the drift report is NOT clean: {report.total} finding(s) across {drifting}"
    if report.unavailable:
        return f"the drift report could not check {sorted(u.category for u in report.unavailable)} — a category nobody looked at is not a clean one"
    if report.incomplete:
        return f"the drift report is INCOMPLETE ({sorted({i.source for i in report.incomplete})}) — a partial scan cannot certify the estate"
    # EVERY gap, not just the first: an operator who enables one scan and re-runs must not discover a
    # second gap, then a third, each round trip costing a pass that opens every dataset in the estate.
    if gaps := sorted(s.category for s in report.skipped if s.coverage_gap):
        return f"the drift report SKIPPED {gaps} — that part of the estate was never examined, so this report cannot certify it"
    return None


# --------------------------------------------------------------------------- #
# The maintained estate + the liveness re-check
# --------------------------------------------------------------------------- #


def maintained_roots(settings: MaintenanceSettings, storage_options: StorageOptions) -> set[str]:
    """Every root URI whose bytes this service is allowed to reclaim.

    The configured sweep buckets UNION every bucket the warehouse registry names — the same runtime-read
    the sweep does (#81), because a warehouse bucket is created by an API call and a config-time list is
    stale by construction. ``maintainable_buckets`` already drops DEACTIVATED warehouses, and that
    exclusion matters more here than it does for the sweep: deactivate is offboarding step one, and
    destroying a quarantined tenant's dropped tables is not a maintenance decision to make on their
    behalf.

    Raises if the registry cannot be read. The caller turns that into a refusal to run — narrowing the
    estate silently would be safe for the purge, but "we could not enumerate what we maintain" is
    exactly the moment a reclaimer should stop rather than proceed on a partial map.
    """
    buckets = set(settings.sweep_buckets)
    buckets |= set(warehouse_records.maintainable_buckets(warehouse_records.list_warehouse_records(settings.resolved_control_root, storage_options)))
    return {f"s3://{bucket}" for bucket in buckets if bucket}


class Liveness(BaseModel):
    """What the estate's ``__manifest`` rows say is live: every object id, every table's location, every namespace's uuid.

    A table's location answers "was this table recovered" and "does any id still resolve to these bytes" ([[LH-204]]):
    a rename or a register can attach a trashed table's bytes under ANOTHER id, while a name is free once its object
    is dropped, so another table of the same name says nothing about this one ([[LH-525]]). A namespace's uuid answers
    for a namespace, and the ids for a table recorded with no location.
    """

    ids: set[str] = Field(default_factory=set)
    #: Absolute table locations, joined onto the root that recorded them.
    locations: set[str] = Field(default_factory=set)
    #: A live namespace's id -> the uuid its row holds: what a purge's control event names its object's namespace by.
    namespace_uuids: dict[str, str] = Field(default_factory=dict)

    def __or__(self, other: Liveness) -> Liveness:
        return Liveness(ids=self.ids | other.ids, locations=self.locations | other.locations, namespace_uuids=self.namespace_uuids | other.namespace_uuids)

    def resolves_into(self, location: str) -> bool:
        """Whether a live table's location equals, contains or sits under ``location``."""
        return any(location_within(live, location) or location_within(location, live) for live in self.locations)


def manifest_rows(root: str, storage_options: StorageOptions) -> list[dict[str, Any]] | None:
    """Every row of ``root``'s ``__manifest`` (``object_id``, ``object_type``, ``location``, ``metadata``), or ``None`` when unreadable.

    A root with no ``_versions`` directory is a fresh estate and yields ``[]``: an absent manifest is not an unreadable
    one, and collapsing the two would let a broken manifest read as "nothing is live".
    """
    try:
        fs, base = fs_and_base(root, storage_options)
        if fs.get_file_info(f"{base}/{MANIFEST_DIR}/_versions").type != pafs.FileType.Directory:
            return []
        manifest_uri = f"s3://{base}/{MANIFEST_DIR}" if root.startswith("s3://") else f"{base}/{MANIFEST_DIR}"
        dataset = lance.dataset(manifest_uri, storage_options=storage_options.in_clear(), session=shared_lance_session())
        return dataset.to_table(columns=["object_id", "object_type", "location", "metadata"]).to_pylist()
    except Exception:  # noqa: BLE001 — ANY failure here must read as unreadable, never as a partial set
        log.warning("trash_purge_manifest_unreadable", extra={"root": root}, exc_info=True)
        return None


def table_location(root: str, row: dict[str, Any]) -> str | None:
    """A table row's absolute location, joined onto the root that recorded it; ``None`` for any other row.

    A table row's ``location`` is relative to the root (``lance_docs/ns_catalog/catalog/dir/index.md`` § Manifest Table
    Schema) unless it carries a scheme.
    """
    stored = str(row.get("location") or "").strip()
    if row.get("object_type") != "table" or not stored:
        return None
    return stored if "://" in stored else f"{root.rstrip('/')}/{stored.strip('/')}"


def manifest_liveness(root: str, storage_options: StorageOptions) -> Liveness | None:
    """Every object id ``root``'s ``__manifest`` records — tables AND namespaces — and every table location, or ``None``.

    ``None`` means UNREADABLE, and the caller must treat it as "refuse everything". A missing manifest is
    NOT unreadable: a root with no ``_versions`` directory is a fresh estate and yields an empty answer (the
    same probe-first shape :func:`reconcile._top_level_namespaces` uses, so an absent dataset never rides
    an exception). The difference is load-bearing — collapsing the two would let a broken manifest read
    as "nothing is live" and authorise deleting all of it.

    No ``object_type`` filter on the ids, unlike the reconciler's read: a table record and a namespace
    record are both checked against them, and a namespace is only ever a manifest ROW. A table row's
    ``location`` is relative to the root (``lance_docs/ns_catalog/catalog/dir/index.md`` § Manifest Table
    Schema) unless it carries a scheme.
    """
    rows = manifest_rows(root, storage_options)
    if rows is None:
        return None
    live = Liveness()
    for row in rows:
        if row["object_id"]:
            live.ids.add(str(row["object_id"]))
        if (location := table_location(root, row)) is not None:
            live.locations.add(location)
        elif row.get("object_type") == "namespace" and row["object_id"] and (uuid := manifest_uuid(row)) is not None:
            live.namespace_uuids[str(row["object_id"])] = uuid
    return live


def live_object_ids(root: str, storage_options: StorageOptions) -> set[str] | None:
    """The ids half of :func:`manifest_liveness`: ``None`` when the manifest is unreadable."""
    live = manifest_liveness(root, storage_options)
    return None if live is None else live.ids


def liveness_across(roots: set[str], storage_options: StorageOptions) -> Liveness | None:
    """The union of every maintained root's liveness, or ``None`` if ANY root was unreadable.

    All-or-nothing on purpose. A ``kind="namespace"`` record carries no root of its own, so its liveness
    question is "is this id registered ANYWHERE in the estate", and a table's bytes can be resolved by a
    table registered under any root — an answer computed from a subset of the roots is not an answer, it
    is a smaller search that finds fewer live objects and therefore deletes more. One unreadable manifest
    poisons the whole tick; the next tick retries.

    Operational consequence, stated rather than discovered: a warehouse whose bucket the registry names
    but this process cannot REACH blocks reclamation estate-wide until it is fixed or the record is
    removed. That is the intended direction — an unreachable part of the estate is a part nobody checked
    — and it is visible: every failing root is logged by name (``trash_purge_manifest_unreadable``) and
    the refusal reason lands on every record. A bucket that merely has no ``__manifest`` yet is NOT this
    case; it lists as absent and contributes nothing.
    """
    live = Liveness()
    for root in sorted(roots):
        found = manifest_liveness(root, storage_options)
        if found is None:
            return None
        live |= found
    return live


def _root_of(location: str, roots: set[str]) -> str | None:
    """The maintained root ``location`` sits under, or ``None``. Longest match wins (nested roots)."""
    for root in sorted((r.rstrip("/") for r in roots), key=len, reverse=True):
        if location == root or location.startswith(f"{root}/"):
            return root
    return None


def still_registered(record: dict[str, Any], live: Liveness) -> bool:
    """Whether the object ``record`` keeps stands again: an undrop or a register re-attached it since its drop.

    A table stands when a live table resolves to its location; a namespace when a live namespace of its name keeps its
    uuid, or keeps none; a table recorded with no location when a live object holds its name, since nothing else tells
    it from another.
    """
    obj_id, location = trash.name_of(record), trash.location_of(record)
    if trash.kind_of(record) == "namespace":
        return obj_id in live.ids and live.namespace_uuids.get(obj_id) in (trash.object_uuid(record), None)
    if not location:
        return obj_id in live.ids
    return any(decoded_path(held) == decoded_path(location) for held in live.locations)


def check(record: dict[str, Any], *, roots: set[str], live: Liveness | None) -> str | None:
    """The refusal ladder — ``None`` to purge this record, else the reason not to. Order matters.

    Liveness is checked FIRST because it is the only refusal that protects data someone still has: the
    others protect the estate from a malformed record, this one protects a table the catalog just
    recovered. Path checks come after, and are skipped entirely for records that name no bytes; the
    last asks whether another id now resolves to the location ([[LH-204]]).

    The path checks judge the place the purge deletes, the location as it decodes ([[LH-319]], :func:`store_paths`), so a
    spelling that names no single place is refused before any of them.

    THE OBJECT IS ITS UUID, NOT ITS NAME ([[LH-525]]): a name is free once its object is dropped, so a live object of
    the same name may be another one entirely, and the purge reclaims the trashed object regardless. A table is still
    registered when a live table resolves to its location, a namespace when a live namespace of its name keeps its uuid.
    Only a table recorded with no location is judged by its name, since nothing else tells it from another.
    """
    if not trash.name_of(record):
        return "the record names no object id"
    uuid = trash.object_uuid(record)
    if uuid is None:
        return "the record keeps no uuid, so the object it names cannot be told from another of its name"
    if live is None:
        return _MANIFEST_UNREADABLE
    if still_registered(record, live):
        return _STILL_REGISTERED
    location = trash.location_of(record)
    if trash.kind_of(record) == "namespace" or not location:
        # A namespace is a manifest ROW and a declared-only table never had bytes: nothing to path-check.
        return None
    if not names_a_location(location):
        return f"location {location!r} names no single place once decoded — refusing to delete what its spelling cannot locate"
    root = _root_of(location, roots)
    if root is None:
        # The count, not the list: this reason repeats per refused record on a WARNING line, and
        # rendering ~100 bucket URIs made one tick's line ~36 KB (measured 2026-09-20).
        return f"location {location!r} is outside the maintained estate ({len(roots)} roots)"
    remainder = (decoded_segments(location) or [])[len(decoded_segments(root) or []) :]
    if not remainder:
        return f"location {location!r} IS a store root — refusing to delete a whole bucket"
    # Every segment, not only the last: ``s3://bkt/_trash/x`` names a file inside the trash registry.
    if crossed := sorted(CONTROL_PREFIXES.intersection(remainder)):
        return f"location {location!r} crosses the control prefix(es) {crossed} — refusing to delete the estate's own registries"
    # After the path checks, which name a malformed record more exactly; a store root holds every live table.
    if live.resolves_into(location):
        return _LOCATION_LIVE
    return None


# --------------------------------------------------------------------------- #
# The mutations
# --------------------------------------------------------------------------- #


#: The Lance table-layout marker — a directory IS a dataset iff it holds this child. The same string
#: `maintenance.services.discovery.discover_datasets` decides discovery by, named here so the purge and
#: the walk cannot drift apart about what a dataset is.
_VERSIONS_MARKER = "_versions"


#: The marker pylance's ``dir`` namespace writes where it reserves a table's location
#: (``lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md`` § DeclareTable, step 3).
_RESERVATION_MARKER = ".lance-reserved"


class NotADatasetRootError(RuntimeError):
    """The recorded location does not look like a Lance dataset, or its spelling holds more than the
    table's reservation marker, so a recursive delete of it is not a purge — it is data loss with a
    trash record for an alibi.

    The marker is `_versions/`, which is the SAME one `discover_datasets` uses to decide a directory
    is a dataset — one definition rather than a second opinion. On the happy path a trash record's
    location comes from `describe_table` at drop time and always has it; this guard is for the paths
    where it does not (a corrupted or hand-edited record, a location reused after a rename, a bug
    upstream writing the wrong string), none of which the other refusals in `check` can see.
    """


def objects_held(location: str, storage_options: StorageOptions) -> int:
    """How many objects the table recorded at ``location`` still holds, under both of its spellings
    (:func:`lakehouse.table_paths.spellings`).

    The tombstone sweep's proof that a record's bytes are gone ([[LH-061]]): counted as spelled alone, a table whose
    name holds a space reads as empty while every byte sits under the decoded key ([[LH-319]]).
    """
    return len(objects_at(location, storage_options))


def _store_path(place: str) -> str:
    """A listed place as the store paths beneath it are written: ``bucket/key`` on S3, the absolute path on a local root."""
    return place.removeprefix(f"{store_of(place)}://")


def _require_a_dataset(location: str, path: str, held: list[StoredObject]) -> None:
    """Refuse ``path`` when it holds something with no ``_versions/`` beside it: whatever it is, it is no Lance dataset.

    AN EMPTY OR ABSENT PATH PASSES, and that is the contract rather than an oversight: a crash between the delete and the
    record clear re-runs the whole record, and the delete takes ``_versions/`` last (:func:`delete_location`), so a
    half-completed purge leaves a path this still recognises and can finish.
    """
    prefix = f"{_store_path(path).rstrip('/')}/"
    if held and not any(stored.path.removeprefix(prefix).split("/")[0] == _VERSIONS_MARKER for stored in held):
        raise NotADatasetRootError(
            f"refusing to delete {location}: it holds {len(held)} file(s) but no {_VERSIONS_MARKER}/ marker, "
            f"so it is not a Lance dataset — a recursive delete here would destroy whatever it really is"
        )


def _require_only_the_marker(location: str, spelled: str, stray: list[StoredObject]) -> None:
    """Refuse when the location's spelling holds any object but the reservation marker: it is not this table's.

    Every such object is NAMED, a bounded sample with the count, never skipped: a purge that deleted what it could map
    and left the rest in silence would report a clean reclaim over bytes it never looked at.
    """
    marker = f"{_store_path(spelled).rstrip('/')}/{_RESERVATION_MARKER}"
    foreign = sorted(stored.path for stored in stray if stored.path != marker)
    if foreign:
        raise NotADatasetRootError(
            f"refusing to delete {location}: as spelled it names {spelled}, which holds {len(foreign)} object(s) beside the "
            f"table's reservation marker ({', '.join(foreign[:3])}) — they are not this table's, so neither path is deleted"
        )


def _versions_last(path: str, objects: list[StoredObject]) -> list[str]:
    """The store paths of ``objects``, those under ``path``'s ``_versions/`` last, so a delete that stops part-way leaves a
    path :func:`_require_a_dataset` still recognises."""
    versions = f"{_store_path(path).rstrip('/')}/{_VERSIONS_MARKER}/"
    return sorted((stored.path for stored in objects), key=lambda stored_path: stored_path.startswith(versions))


def delete_location(location: str, storage_options: StorageOptions) -> tuple[int, int]:
    """Delete the recorded table's bytes, under both of its spellings (:func:`lakehouse.table_paths.spellings`); return
    ``(bytes_deleted, files_deleted)``.

    Both paths are judged before either is touched, so a refusal deletes nothing: the one the location opens must be a
    Lance dataset (its ``_versions/`` marker, the one ``discover_datasets`` decides a dataset by) or empty, and the one
    it is spelled as may hold the reservation marker and nothing else. The listings that sum the bytes double as the
    existence probes, and an already-empty path deletes nothing (a crash between the delete and the record clear re-runs
    the whole record). Every object goes BY KEY, ``_versions/`` last, as the catalog's destructive drop deletes
    (``catalog.services.dataplane._delete_everything_under``): the reclaim session reaches the location alone, and
    pyarrow's directory delete puts the location's parent back as a marker outside it (measured on pyarrow 25.0.0). A
    store's refusal propagates as ``OSError``, so a purge is never reported over bytes that are still there.

    A trashed table a registered clone still reads through is never deleted here: the catalog vends no session to delete
    it with while the clone's tag stands on it (``catalog.services.trash_reclaim.clone_refusal``, ADR 0198 §1).
    """
    dataset, spelled = spellings(location)
    held = list_under(dataset, storage_options)
    stray = list_under(spelled, storage_options) if spelled != dataset else []
    _require_a_dataset(location, dataset, held)
    _require_only_the_marker(location, spelled, stray)
    objects = [*held, *stray]
    delete_objects(location, storage_options, _versions_last(dataset, objects))
    if store_of(location) == "file":
        _remove_emptied_directories(location, storage_options, dataset, spelled)
    return sum(stored.size for stored in objects), len(objects)


def _remove_emptied_directories(location: str, storage_options: StorageOptions, *places: str) -> None:
    """A local root keeps the directories its objects were deleted from, which an object store holds none of: they go too,
    as the catalog's destructive drop removes them (``catalog.services.dataplane._delete_everything_under``)."""
    fs, _ = fs_and_base(location, storage_options)
    for place in dict.fromkeys(places):
        with suppress(FileNotFoundError):
            fs.delete_dir(place)


#: The control-plane vocabulary for a purge — its OWN verbs since diff2 F10 item 6.
#:
#: It reused the drop actions, for a stated reason that has now been discharged rather than overruled:
#: ``ControlAction`` is a wire contract across three files (the Literal, ``docs/catalog-openapi.json``,
#: ``frontend/packages/api/src/generated/catalog.ts``), adding a member without regenerating all three
#: leaves the TS client unable to NAME an event the backend publishes, and "the regen could not be done
#: in this change". It has been done (``make openapi`` + ``bun run gen:types:catalog``, with
#: ``make openapi-check`` green), so the reuse no longer buys anything.
#:
#: WHY THE REUSE WAS WORTH UNDOING. The old argument — a consumer that re-reads state on either action
#: is correct either way, because an event is a refresh hint and never data — is true and is not the
#: whole job. These events are also the estate's governance FEED, read by humans at
#: ``/lakehouse/admin/events``, and there the two facts are not interchangeable: "someone decided to
#: remove this" and "the grace period ran out and the sweep collected it" differ in cause, in
#: appealability, and in finality (a purge is the last event an object ever emits). The distinguishing
#: `reason` lived in ``extra``, which that feed does not read — so a reclamation rendered as a person's
#: deletion, with ``ACTOR`` (a service identity) in the actor column.
#:
#: ``extra.reason`` is unchanged, so a consumer keying on it keeps working; this is purely additive.
_PURGE_ACTION: dict[str, ControlAction] = {"table": "table_purged", "namespace": "namespace_purged"}


async def _refuse(out: TrashPurgeReport, record: dict[str, Any], *, reason: str, settings: MaintenanceSettings) -> None:
    """Report a refusal AND have the catalog remember it on the record — the one place both halves happen.

    ONE HELPER RATHER THAN FIVE CALL SITES, because reporting the tick and remembering the history are
    not separable concerns: a refusal arm that appends without persisting produces a record whose
    ``attempts`` undercounts, which is worse than not counting at all — it reads as a transient
    exclusion with authority. Routing every arm through here means a NEW refusal cause cannot opt out
    of the memory by omission.

    The note is the catalog's to write (``POST /management/v1/trash/{kind}/{uuid}/refusals``, [[XC-084]]), and it is
    best-effort: a door that refuses or fails it leaves ``attempts`` unknown and the refusal reported, because
    ``_purge_one`` promises "never raises; every failure is a refusal". A record that keeps no ``uuid`` is not one any
    door files, and its refusal is reported, not remembered: there is no key to remember it under.
    """
    kind, obj_id, uuid = trash.kind_of(record), trash.name_of(record), trash.object_uuid(record)
    attempts: int | None = None
    if uuid is not None:
        try:
            attempts = await run_in_threadpool(partial(note_trash_refusal, kind, uuid, settings, reason=reason))
        except (MaintenanceDenied, TableNotGoverned, VendUndecided) as exc:
            log.warning("trash_refusal_not_recorded", extra={"kind": kind, "id": obj_id, "error": f"{type(exc).__name__}: {exc}"})
    out.refused.append(RefusedRecord(kind=kind, id=obj_id, reason=reason, attempts=attempts))


def refusal_log_entry(record: RefusedRecord) -> dict[str, object]:
    """One refusal as the result line reports it, attempt count included.

    `attempts` is what `_drain_order` sorts on, so a refusal reported without it leaves the operator
    asking the one question the line exists to answer: is this record being demoted, or refused afresh
    every tick? `None` is a DRY RUN answering honestly — a preview notes no refusal so it
    cannot inflate the evidence it exists to show.
    """
    return {"id": f"{record.kind}:{record.id}", "reason": record.reason, "attempts": record.attempts}


async def _delete_bytes_or_refuse(
    out: TrashPurgeReport, record: dict[str, Any], *, session: StorageOptions, settings: MaintenanceSettings, control_root: str
) -> tuple[int, int] | None:
    """Delete the bytes this record names, its root and its data directories, under its reclaim session, or record a
    refusal and answer ``None``.

    TWO ARMS RATHER THAN ONE, because they say different things to an operator and only one of them means the record
    itself is wrong: ``NotADatasetRootError`` means this record points at something that is not a dataset; an
    ``OSError``, or a base record that could not be read, is transient and the next run simply retries.

    The base record and the claims that say which data directories are the table's are READ with maintenance's own
    session, which reads the estate; only the deletes are the session's, which reaches the bytes and nothing else.
    """
    location, obj_id = trash.location_of(record), trash.name_of(record)
    registry = BaseRegistry(control_root=control_root, storage_options=settings.storage_options())
    try:
        return await run_in_threadpool(partial(_delete_guarded, location, session, registry=registry, table=obj_id))
    except NotADatasetRootError as exc:
        await _refuse(out, record, reason=str(exc), settings=settings)
        log.warning("trash_purge_refused_not_a_dataset", extra={"kind": trash.kind_of(record), "id": obj_id, "location": location})
    except (OSError, UnreadableBaseRecordError, ClientError, BotoCoreError) as exc:
        reason = f"deleting {location!r} failed ({type(exc).__name__}: {exc}) — the record survives and the next tick retries"
        await _refuse(out, record, reason=reason, settings=settings)
        log.error("trash_purge_delete_failed", extra={"kind": trash.kind_of(record), "id": obj_id, "location": location, "error": str(exc)})
    return None


async def _recovered_since_the_snapshot(
    record: dict[str, Any], out: TrashPurgeReport, *, roots: set[str], recheck_live: Callable[[], Liveness | None], settings: MaintenanceSettings
) -> bool:
    """Re-read liveness at the last instant before the first mutation. ``True`` when the record was refused.

    THE REASON THE LADDER IN :func:`check` IS NOT SUFFICIENT ON ITS OWN. ``live`` is ONE snapshot,
    taken before the loop and shared by every record, so an undrop landing after it is invisible to
    every record processed later in the same tick — and undrop re-registers BEFORE it clears the
    record, which is precisely that window. Re-reading here shrinks what an undrop must win from "this
    tick" to "this call".

    ONLY RECORDS THAT REACHED A MUTATION PAY FOR IT, which is why the shared snapshot stays: it is the
    cheap filter over every due record and this is the correctness gate over the few that survive it.
    The SAME ladder, so an unreadable manifest refuses here exactly as it does above rather than
    degrading to "purge anyway".

    Pinned by `test_a_table_recovered_AFTER_the_liveness_snapshot_is_still_refused`, which drives the
    window deterministically (re-registering from inside the estate pre-pass, which runs between the
    snapshot and the loop) rather than by timing — and which measured, before this existed, a live
    table losing 867 bytes across 4 files to a purge that answered success.
    """
    reason = check(record, roots=roots, live=await run_in_threadpool(recheck_live))
    if reason is None:
        return False
    await _refuse(out, record, reason=reason, settings=settings)
    log.warning("trash_purge_refused_on_recheck", extra={"kind": trash.kind_of(record), "id": trash.name_of(record), "reason": reason})
    return True


def _purged_uuids(record: dict[str, Any]) -> ControlUuids:
    """The uuids of a trashed object and of its namespace, for the control event its purge announces (ADR 0173 §10).

    Both are on the record, which keeps the uuid it is filed under and its namespace's at the drop: the name that
    namespace held then may be another namespace's now.
    """
    namespace_uuid = record.get("namespace_uuid")
    return ControlUuids(object_uuid=trash.object_uuid(record), namespace_uuid=namespace_uuid if isinstance(namespace_uuid, str) else None)


async def _asked_of_the_catalog[T](record: dict[str, Any], out: TrashPurgeReport, ask: Callable[[], T], *, asked: str) -> T | None:
    """What the catalog answers ``ask`` for this record, or ``None`` with its refusal reported ([[XC-084]]).

    A refusal the catalog judged (409) it has noted on the record itself, so a record refused tick after tick sorts
    behind the ones that move; a 403 or an undecided door is reported here and remembered nowhere, since the catalog
    decided nothing it could note.
    """
    try:
        return await run_in_threadpool(ask)
    except (MaintenanceDenied, TableNotGoverned, VendUndecided) as exc:
        out.refused.append(RefusedRecord(kind=trash.kind_of(record), id=trash.name_of(record), reason=str(exc)))
        log.warning(
            "trash_purge_refused_by_the_catalog", extra={"kind": trash.kind_of(record), "id": trash.name_of(record), "asked": asked, "reason": str(exc)}
        )
        return None


async def _preview_one(record: dict[str, Any], out: TrashPurgeReport, *, roots: set[str], live: Liveness | None) -> None:
    """Plan ONE record, or report why the purge would refuse it; writes nothing and asks for no session.

    The cut is placed after the REFUSAL CHECK deliberately: everything above it is a read, everything after it mutates,
    and the check is precisely the part an operator needs previewed — a plan that omitted the refusals would promise
    reclamation the real pass declines to perform. Its refusals are reported and never noted on the record: noting is a
    write onto the very records an operator is inspecting.
    """
    kind, obj_id, location = trash.kind_of(record), trash.name_of(record), trash.location_of(record)
    if (reason := check(record, roots=roots, live=live)) is not None:
        out.refused.append(RefusedRecord(kind=kind, id=obj_id, reason=reason))
        log.warning("trash_purge_refused", extra={"kind": kind, "id": obj_id, "reason": reason})
        return
    # PAST EVERY REFUSAL THIS PASS CAN EVALUATE WITHOUT MUTATING. `would_purge` remains an UPPER BOUND: the catalog's own
    # judgement, a non-dataset location and a failing delete can each still refuse a planned record, and none of them
    # can be evaluated without asking for or performing the act it guards.
    out.would_purge.append(PlannedRecord(kind=kind, id=obj_id, location=location, expires_at=str(record.get("expires_at") or "")))
    log.info("trash_purge_planned", extra={"kind": kind, "id": obj_id, "location": location})


async def _purge_one(
    record: dict[str, Any],
    out: TrashPurgeReport,
    *,
    settings: MaintenanceSettings,
    control_root: str,
    roots: set[str],
    live: Liveness | None,
    recheck_live: Callable[[], Liveness | None],
    control: ControlEmitter,
) -> None:
    """Delete → end → announce, for ONE record. Never raises; every failure is a refusal.

    NOTHING HERE IS SIGNED BY MAINTENANCE'S OWN SESSION ([[XC-084]]): a table's bytes go under the session the catalog
    vends for this record, and the record, what it keeps and its grants are the catalog's to end once its own listing
    finds the place empty.
    """
    kind, obj_id, location = trash.kind_of(record), trash.name_of(record), trash.location_of(record)
    if (uuid := trash.object_uuid(record)) is None:
        out.refused.append(RefusedRecord(kind=kind, id=obj_id, reason="the record keeps no uuid, so the catalog reclaims nothing of it"))
        return
    if (reason := check(record, roots=roots, live=live)) is not None:
        await _refuse(out, record, reason=reason, settings=settings)
        log.warning("trash_purge_refused", extra={"kind": kind, "id": obj_id, "reason": reason})
        return
    if await _recovered_since_the_snapshot(record, out, roots=roots, recheck_live=recheck_live, settings=settings):
        return
    deleted = (0, 0)
    if kind == "table" and location:
        if (session := await _asked_of_the_catalog(record, out, partial(reclaim_options_for, kind, uuid, settings), asked="reclaim session")) is None:
            return
        reclaimed = await _delete_bytes_or_refuse(out, record, session=session, settings=settings, control_root=control_root)
        if reclaimed is None:
            return
        deleted = reclaimed
    if (revoked := await _asked_of_the_catalog(record, out, partial(end_trash, kind, uuid, settings), asked="end")) is None:
        return
    deleted_bytes, deleted_files = deleted
    out.purged.append(
        PurgedRecord(
            kind=kind,
            id=obj_id,
            location=location,
            expires_at=str(record.get("expires_at") or ""),
            bytes_deleted=deleted_bytes,
            files_deleted=deleted_files,
            tuples_revoked=revoked,
        )
    )
    log.warning(
        "trash_purged",
        extra={"kind": kind, "id": obj_id, "location": location, "bytes": deleted_bytes, "files": deleted_files, "tuples_revoked": revoked},
    )
    uuids = _purged_uuids(record)
    object_type: ControlObjectType = "namespace" if kind == "namespace" else "table"
    await emit_control(
        control,
        action=_PURGE_ACTION[kind],
        object_type=object_type,
        object_id=f"{kind}:{uuid}",
        object_name=obj_id,
        object_uuid=uuids.object_uuid,
        namespace_uuid=uuids.namespace_uuid,
        actor=ACTOR,
        extra={
            "reason": "trash_expired",
            "expires_at": str(record.get("expires_at") or ""),
            "bytes_deleted": deleted_bytes,
            "tuples_revoked": revoked,
        },
    )


async def purge_expired_trash(
    settings: MaintenanceSettings,
    *,
    report: ReconcileReport,
    control: ControlEmitter | None = None,
    control_root: str | None = None,
    data_roots: set[str] | None = None,
) -> TrashPurgeReport:
    """Reclaim every expired trash record this tick is allowed to reclaim.

    ``report`` MUST be the report THIS tick produced — the gate is "the drift report ran clean", and a
    stale report certifies a state that no longer exists. ``data_roots`` overrides the registry-derived
    maintained estate (tests drive a ``file://`` root; production never passes it).

    Never raises: every failure lands in the returned report, because a reclamation pass that 500s tells
    an operator nothing about which records it got to first.
    """
    control = control or NoopControlEmitter()
    # DRY RUN WINS WHENEVER IT IS SET, including over an enabled purge: it subtracts every MUTATION.
    # Wired the other way, a deployment that means to reclaim and silently previews forever would be
    # indistinguishable from a healthy one — so the counters carry a `dry_run` dimension rather than
    # leaving that state to be inferred. Note it does NOT subtract WORK: with reclamation off this
    # turns a zero-I/O early return into the full listing plus the estate-wide pre-pass below.
    dry_run = settings.trash_purge_dry_run
    out = TrashPurgeReport(
        enabled=settings.trash_purge_enabled,
        dry_run=dry_run,
        skipped_categories=[s.category for s in report.skipped],
    )

    if not (settings.trash_purge_enabled or dry_run):
        out.reason = _PURGE_OFF
        return out
    if (blocker := report_is_clean(report)) is not None:
        out.reason = blocker
        log.warning("trash_purge_blocked", extra={"reason": blocker})
        return out
    storage_options = settings.storage_options()
    resolved_control_root = control_root or settings.resolved_control_root
    try:
        roots = data_roots if data_roots is not None else await run_in_threadpool(maintained_roots, settings, storage_options)
        due = await run_in_threadpool(due_records, resolved_control_root, storage_options)
    except Exception as exc:  # noqa: BLE001 — an unreadable registry/trash prefix stops the purge, it does not fail the tick
        out.reason = f"the maintained estate or the trash listing could not be read ({type(exc).__name__}: {exc}) — refusing to purge on a partial map"
        log.error("trash_purge_blocked", extra={"reason": out.reason})
        return out

    out.ran = True
    out.due = len(due)
    if len(due) > settings.trash_purge_max_per_tick:
        out.capped = len(due) - settings.trash_purge_max_per_tick
        due = due[: settings.trash_purge_max_per_tick]

    # One manifest read per root per tick, shared by every record — and `None` (unreadable) refuses all
    # of them rather than letting a broken index read as "nothing is live".
    live = await run_in_threadpool(liveness_across, roots, storage_options) if due else Liveness()
    for record in due:
        if dry_run:
            await _preview_one(record, out, roots=roots, live=live)
            continue
        await _purge_one(
            record,
            out,
            settings=settings,
            control_root=resolved_control_root,
            roots=roots,
            live=live,
            recheck_live=lambda: liveness_across(roots, storage_options),
            control=control,
        )

    _record_metrics(out)
    log.info(
        "trash_purge",
        extra={
            "due": out.due,
            "dry_run": out.dry_run,
            "purged": len(out.purged),
            "would_purge": len(out.would_purge),
            "refused": len(out.refused),
            "capped": out.capped,
            "bytes_reclaimed": sum(p.bytes_deleted for p in out.purged),
            "skipped_categories": out.skipped_categories,
        },
    )
    return out


def _record_metrics(out: TrashPurgeReport) -> None:
    """Emit the reclamation counters — always, including the zeroes, so the series exist from tick one."""
    purged: dict[str, int] = {}
    refused: dict[str, int] = {}
    planned: dict[str, int] = {}
    for entry in out.purged:
        purged[entry.kind] = purged.get(entry.kind, 0) + 1
    for refusal in out.refused:
        refused[refusal.kind] = refused.get(refusal.kind, 0) + 1
    for plan in out.would_purge:
        planned[plan.kind] = planned.get(plan.kind, 0) + 1
    record_trash_purge(
        purged_by_kind=purged,
        refused_by_kind=refused,
        bytes_reclaimed=sum(p.bytes_deleted for p in out.purged),
        planned_by_kind=planned,
        dry_run=out.dry_run,
    )


def _judged_data_directories(location: str, *, registry: BaseRegistry, table: str) -> list[RecordedBase]:
    """The data directories ending the table at ``location`` deletes, each judged before any byte goes ([[LH-286]]).

    :func:`lakehouse.data_directories.directories_to_end` decides which are the table's (its claim names
    the table, held or released by it, and every other one is left).

    Raises:
        UnreadableBaseRecordError: The record exists and cannot be read.
        botocore.exceptions.ClientError: The store refused a read of the record or a claim.
    """
    return directories_to_end(registry, location, table=table)


def _delete_guarded(location: str, storage_options: StorageOptions, *, registry: BaseRegistry, table: str) -> tuple[int, int]:
    """`delete_location`, and the table's data directories after its root; ``(bytes, files)``.

    Every directory is judged before the root is touched, so a refusal deletes nothing. The record names the directories,
    so it is forgotten only after them (:func:`_delete_bytes_or_refuse`): a failure here leaves the record for the next
    tick, which finds the root already empty and finishes the directories.
    """
    directories = _judged_data_directories(location, registry=registry, table=table)
    root_bytes, root_files = delete_location(location, storage_options)
    deleted = delete_directories(directories, storage_options)
    return root_bytes + deleted.bytes, root_files + deleted.files
