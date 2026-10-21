"""The event lane: after a commit, refresh the written table's due record and expire its versions when enough are due.

A write moves a table's fragment and index counts, and a bring-your-own job's compaction is a write through the
catalog like any other: refreshing the record after it is exactly what takes the table off the due list. Expiry runs
on the commit too, once the table carries ``MAINTENANCE_MIN_VERSIONS_TO_EXPIRE`` reclaimable versions — Lakekeeper
schedules expire-snapshots after a commit for the same reason: a cron that polls every table pays for the tables
nobody wrote. The sweep stays the backstop, because the bus is incomplete — an external OpenLineage producer emits
over HTTP only — and a table nobody writes has no commit to react to.

**The event is a hint; the evaluation is the decision.** A write event carries the table id, the version and the
operation, and no fragment or row count, so nothing here can tell whether work is due — only whether it is worth asking.

Subscribing to `lineage.events.v1` rather than minting a trigger topic: it is the one lane every governed writer
converges on. The control topic is deliberately not used — it is a broadcast with no queue group.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from maintenance.core.config import MaintenanceSettings
from maintenance.services.due import TableIds, refresh_due, table_ids
from maintenance.services.expiry import DatasetResult, DueVerdict
from maintenance.services.plan import PlanContext, buckets_to_sweep, load_policies, resolve_plan
from maintenance.services.sweep import NamedDataset, SweepPlan, expire_dataset, name_dataset, trashed_locations, why_in_trash
from service_kit.lakehouse import trash
from service_kit.lakehouse.base_refs import decoded_path
from service_kit.lakehouse.maintenance_due import CloneList, read_clone_list
from service_kit.openlineage import dataset_uuids


log = logging.getLogger(__name__)

#: Catalog operations that move NO BYTES. A registration is not an arrival: `register_table` emits a
#: COMPLETE event indistinguishable — on the fields a subscriber matches — from a batch landing.
_BYTE_FREE_OPERATIONS = frozenset({"register_table", "deregister_table", "declare_table"})

#: Commits that change what a due record measures without adding data: a bring-your-own compaction's two commits and an
#: index build or optimize (ADR 0162). Each refreshes the record whatever the debounce says, because the refresh after one
#: is what takes the table off the due list, and a job makes few of them.
_REFRESHING_OPERATIONS = frozenset({"compact_table", "create_index"})


class TriggeringWrite(BaseModel):
    """A write worth evaluating: which table, and the version it reached."""

    table_id: str
    #: The catalog operation the event announces (``insert``, ``compact_table``, ...).
    operation: str
    #: The version the write produced, when the event carried one. It is the DEBOUNCE input (compare
    #: against the last-maintained stamp), never the trigger — a missing facet must not lose a real
    #: write, because the cost of a needless manifest open is one listing and the cost of a lost
    #: trigger is a table that grows fragments until the sweep notices.
    version: int | None = None
    #: The physical Lance URI, from the standard ``dataSource`` facet. REQUIRED to act: the table's id comes from
    #: the ``__manifest`` of the bucket this location is in, never from the event.
    location: str | None = None
    #: The table's uuid as the event names it (ADR 0173 §10), the debounce's key; ``None`` when the event names none.
    table_uuid: str | None = None


def triggering_write(event: dict[str, Any]) -> TriggeringWrite | None:
    """The table this event says may need maintenance, or ``None`` to ignore it.

    FAILS CLOSED on anything it cannot read. Events arrive off a bus and are client-controlled, so a
    malformed one must be ignored rather than raise: a raise fails the subscription delivery, which
    Dapr redelivers, turning one bad publish into a retry loop. The sweep re-evaluates whatever this ignores.
    """
    run = event.get("run")
    facets = run.get("facets") if isinstance(run, dict) else None
    lance = facets.get("lance") if isinstance(facets, dict) else None
    operation = lance.get("operation") if isinstance(lance, dict) else None
    if not isinstance(operation, str) or operation in _BYTE_FREE_OPERATIONS:
        return None

    outputs = event.get("outputs")
    if not isinstance(outputs, list):
        return None
    for output in outputs:
        if not isinstance(output, dict):
            continue
        table_id = output.get("name")
        if not isinstance(table_id, str) or not table_id:
            continue
        return TriggeringWrite(
            table_id=table_id, operation=operation, version=_version_of(output), location=_location_of(output), table_uuid=dataset_uuids(output).table_uuid
        )
    return None


class PlannableWrite(BaseModel):
    """A triggering write this lane acts on: the physical location it evaluates, and the version that debounces it."""

    location: str
    operation: str
    version: int | None = None
    table_uuid: str | None = None

    def refreshes_regardless(self) -> bool:
        """Whether this commit refreshes the due record whatever the debounce says (``_REFRESHING_OPERATIONS``)."""
        return self.operation in _REFRESHING_OPERATIONS


def plannable_write(event: dict[str, Any]) -> PlannableWrite | None:
    """The write this lane would evaluate for ``event``, or ``None`` when it acknowledges the event untouched.

    ONE DECISION FOR THE DOOR AND THE HANDLER ([[XC-078]]). `/maintenance-arrival` checks the signature of exactly the
    events this selects and `handle_arrival` evaluates exactly those, so what the lane acts on cannot widen past what the
    door checks. A triggering write that names no location is acknowledged without a read, so it is not checked either: an
    event that drives nothing costs no key read.
    """
    hit = triggering_write(event)
    if hit is None or hit.location is None:
        return None
    return PlannableWrite(location=hit.location, operation=hit.operation, version=hit.version, table_uuid=hit.table_uuid)


def _version_of(output: dict[str, Any]) -> int | None:
    """The output's ``version`` facet as an int, or ``None`` when absent or unparseable.

    OpenLineage carries ``datasetVersion`` as a STRING; the caller compares it against an integer
    stamp, so the conversion happens once here rather than at each comparison site.
    """
    facets = output.get("facets")
    version_facet = facets.get("version") if isinstance(facets, dict) else None
    raw = version_facet.get("datasetVersion") if isinstance(version_facet, dict) else None
    if not isinstance(raw, str | int):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _location_of(output: dict[str, Any]) -> str | None:
    """The output's physical URI from the standard ``dataSource`` facet, or ``None`` when absent.

    Absent is a real case, not a defect to fail on: a producer outside the catalog may emit a write
    event without one, and the sweep reaches those tables by discovery anyway. What this
    must never do is guess a path — a URI nobody confirmed would point maintenance at the wrong object.
    """
    facets = output.get("facets")
    source = facets.get("dataSource") if isinstance(facets, dict) else None
    uri = source.get("uri") if isinstance(source, dict) else None
    return uri if isinstance(uri, str) and uri else None


def should_replan(*, last_planned: int | None, event_version: int | None, min_versions: int) -> bool:
    """Have enough versions accumulated since this dataset was last evaluated to be worth evaluating again?

    THE DEBOUNCE, and it runs before the evaluation rather than inside it: an evaluation reads the bucket's
    ``__manifest``, the policy registry and the clone list and opens the dataset, so a table taking a burst of writes
    would otherwise pay that per write. Lakekeeper's `min-snapshots-to-expire`, against rask's own version stamps.

    Three cases MAINTAIN rather than skip, and each is the direction that fails loud instead of silent:

    * **No stamp** — this lane has never evaluated the dataset.
    * **No version on the event** — a producer outside the catalog may stamp no version facet. There is
      nothing to compare; the stamp written after this pass debounces the next one.
    * **A threshold below one** — a misconfigured `0` must not read as "never evaluate", which would disable
      the lane silently. One is the smallest real threshold: every write evaluates.

    A REDELIVERY or an out-of-order event skips, and both matter: delivery is at-least-once so the same
    event arrives twice, and the bus does not order events, so an older version says nothing new.
    """
    if last_planned is None or event_version is None:
        return True
    return event_version - last_planned >= max(min_versions, 1)


class WrittenTable(BaseModel):
    """The table a write landed in, as this service governs it: its id, its plan's inputs and the last clone list."""

    location: str
    table: str
    ids: TableIds
    context: PlanContext
    #: The last sweep's clone list, ``None`` when none was written or it does not parse.
    clones: CloneList | None = None


def resolve_written_table(location: str, settings: MaintenanceSettings) -> WrittenTable | DueVerdict:
    """The table at ``location``, or why there is none to act on (``unnamed``) or no way to tell (``failed``).

    The table's id comes from the ``__manifest`` of the bucket ``location`` is in, and only a bucket the sweep
    maintains is read: a location elsewhere names nothing this service governs.

    The event names the URL the catalog opened, which percent-encodes the table's name (``…$r%C3%A4ksm%C3%B6rg%C3%A5s``),
    and the lane holds the table where discovery does instead, at the key that URL decodes to (``…$räksmörgås``,
    [[LH-319]]). Every reading after this one, the table's id, its policy, its cadence stamp, its trash gate and the
    vend's cover check, is then the sweep's own for the same table.
    """
    options = settings.storage_options()
    bucket = location.removeprefix("s3://").split("/", 1)[0]
    if bucket not in buckets_to_sweep(settings, options):
        return "unnamed"
    held = f"s3://{decoded_path(location)}"
    ids = table_ids({f"s3://{bucket}"}, options)
    if not ids.complete:
        return "failed"
    if (table := ids.of(held)) is None:
        return "unnamed"
    try:
        policies = load_policies(settings, options)
    except Exception:  # noqa: BLE001 — logged by `load_policies`; an unread policy registry decides nothing
        return "failed"
    context = PlanContext(policies=policies, now=datetime.now(UTC), older_than=timedelta(days=settings.older_than_days))
    return WrittenTable(location=held, table=table, ids=ids, context=context, clones=read_clone_list(settings.resolved_control_root, options))


def refresh_written_table(written: WrittenTable, settings: MaintenanceSettings) -> DueVerdict:
    """Re-evaluate the written table's due record.

    The cadence does not apply here: a record left stale until the interval elapsed would keep a compacted table on the
    due list. The clone list is the last sweep's, empty when none was written.
    """
    named = name_dataset(written.location, sweep=_tick(written), settings=settings)
    if not isinstance(named, NamedDataset):
        return "failed"
    plan = resolve_plan(written.location, context=written.context, owners=named.owners)
    clones = written.clones.clones_of(written.location) if written.clones else []
    question = plan.due_question(table=written.table, uuid=named.owners.table_uuid, location=written.location, clones=clones)
    return refresh_due(question, settings, now=written.context.now)


def _tick(written: WrittenTable) -> SweepPlan:
    """A sweep plan holding the one written table, with the last sweep's clone list when there is one."""
    clones = written.clones or CloneList(taken_at=written.context.now)
    return SweepPlan(uris=[written.location], protected=clones.refs(), clones=clones, table_ids=written.ids, context=written.context)


def _is_fresh(clones: CloneList, settings: MaintenanceSettings, *, now: datetime) -> bool:
    return now - clones.taken_at <= timedelta(seconds=settings.clone_list_max_age_seconds)


def _in_trash(location: str, settings: MaintenanceSettings) -> bool:
    """Whether a trash record covers ``location``; an unreadable trash index answers yes (fail toward not deleting).

    The sweep's own rule (:func:`why_in_trash`) answers it, for the table held where discovery holds it
    (:func:`resolve_written_table`, [[LH-318]]).
    """
    try:
        records = trash.list_all(settings.resolved_control_root, settings.storage_options())
    except Exception as exc:  # noqa: BLE001 — an unread trash index decides nothing; the sweep re-reads it
        log.warning("maintenance_event_trash_unreadable", extra={"uri": location, "error": f"{type(exc).__name__}: {exc}"})
        return True
    return why_in_trash(location, trashed_locations(records)) is not None


def expire_written_table(written: WrittenTable, settings: MaintenanceSettings) -> DatasetResult | None:
    """Expire the written table's versions after its commit, or ``None`` when this lane leaves it to the sweep.

    Per table, and failing closed: a recoverably dropped table is frozen until undrop or purge, and without a fresh
    clone list nothing says whether another dataset resolves its files through this one, so both wait for the sweep,
    which reads the trash and takes a new clone list first. The pins, nested branches, manifest flags, the threshold
    and the maintain-tier vend are the expiry pass's own.
    """
    clones = written.clones
    if clones is None or not _is_fresh(clones, settings, now=written.context.now):
        log.info("maintenance_event_expiry_deferred", extra={"uri": written.location, "reason": "no clone list fresher than its bound"})
        return None
    if _in_trash(written.location, settings):
        return None
    return expire_dataset(written.location, sweep=_tick(written), settings=settings)


class Acted(BaseModel):
    """What the event lane did for one write: the due record's verdict, and the expiry pass when one ran."""

    verdict: DueVerdict
    expired: DatasetResult | None = None


def act_on_write(location: str, settings: MaintenanceSettings) -> Acted:
    """Refresh the written table's due record, then expire its versions; neither when the table cannot be resolved."""
    written = resolve_written_table(location, settings)
    if isinstance(written, str):
        return Acted(verdict=written)
    verdict = refresh_written_table(written, settings)
    if verdict == "failed":
        return Acted(verdict=verdict)
    return Acted(verdict=verdict, expired=expire_written_table(written, settings))
