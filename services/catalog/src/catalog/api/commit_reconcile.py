"""The tick that announces every committed version lineage's feed lacks: the commit is the outbox ([[LH-497]]).

A write is announced after its commit (`catalog.core.lineage_emit`), and a crash between the commit and the staging of
its event loses the announcement while the version stays committed, so a pipeline following ``/events?since=`` never
starts its next hop. This tick finds such a version from the table's own version log
(`catalog.services.commit_reconcile`) and announces it, as the catalog announced it the first time: the same event,
under the run id derived from the commit, signed by the catalog for the author it recorded authorizing the commit (ADR
0187). A late original and the re-announcement are then one run, which lineage's feed records once, on the catalog's
signature either way.

Each find is counted on ``catalog.commit_reconcile.reannounced``, each version the catalog recorded no author for on
``catalog.commit_reconcile.unauthored``, and each ref whose log could not be read on ``catalog.commit_reconcile.unreadable``,
as is each table, and the feed, that the store did not describe; each pages (`deploy/alerting/rules.yml`).

The same tick watches every released erasure obligation for a late derived table (ADR 0183 §5,
`catalog.services.erasure_derived.watch_released`): a table derived from the erased one, written before the release and
found only since, is listed on the obligation and counted on ``catalog.erasure.late_derived``, which pages, and a watch
that could not read what it judges counts ``catalog.erasure.late_derived_unwatched``, which pages too. It needs no commit
feed, so it runs whether or not lineage has created one.

THE TICK READS THE TABLES A DOOR FILED A COMMIT FOR, and no other ([[XC-204]]). Every door files an entry before a commit
it announces can land (`lakehouse.records.commit_records.file_pending`), so a table no entry names took no commit since
the tick last looked and costs it nothing: one listing of the entries, then the tables they name. An entry is removed
once it is older than a door's lease and the grace together, by a tick that read its table, or once no claim records
the table. A version committed past
every door files no entry, so a second timer walks every claimed table (:func:`walk`), at a period of its own: it is
what counts a version no door made, and what finds a lost announcement should an entry ever be lost.

Both run on the catalog's own timers (`catalog.api.timers`), once a window whatever the replica count. A tick that
overlaps another all the same (one that outlasts its window) announces a found version twice, which the derived run id
lands once on the feed; a mark one writes lower than another's costs a re-read, never a skipped version.
"""

from __future__ import annotations

import logging
from datetime import UTC, timedelta
from functools import partial

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import DescribeTableRequest, LanceNamespace, NamespaceNotFoundError, PermissionDeniedError, TableNotFoundError
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from catalog.api.dependencies import ObjectUuids, app_wire, estate_namespace, namespace_for_table
from catalog.api.signing_gate import commit_ids_of
from catalog.api.ticks import Tick
from catalog.core import native
from catalog.core.config import CatalogLineageSettings, Settings
from catalog.core.identifiers import parse_identifier
from catalog.core.lineage_emit import LineageEmitter, emit_write_event, run_inputs
from catalog.services import commit_reconcile, erasure_derived, table_claims
from catalog.services.tables import versions
from lakehouse.events.lineage_events import DatasetUuids
from lakehouse.governed import fga_ids
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.lancekit.base_refs import decoded_path
from lakehouse.process.correlation import continuing
from lakehouse.records import commit_records, location_claims
from lakehouse.records.location_claims import ClaimStore, LocationClaim
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")
_reannounced = _meter.create_counter(
    "catalog.commit_reconcile.reannounced",
    unit="{commit}",
    description="Committed versions whose announcement lineage's feed lacked past the grace, announced again from the "
    "version log ([[LH-497]]), by operation. Each is an announcement lost between a commit and its staging: a pipeline "
    "following the feed was not told until now.",
)
_unreadable = _meter.create_counter(
    "catalog.commit_reconcile.unreadable",
    unit="{read}",
    description="Reads the commit reconciler could not make: a table ref's version log past a point (the ref keeps its mark, "
    "and every lost announcement after it waits until it is read), or a describe of a table it claims or of lineage's feed "
    "(nothing is announced for that table, or on that tick, until it is read).",
)
_late_derived = _meter.create_counter(
    "catalog.erasure.late_derived",
    unit="{table}",
    description="Tables found derived from a released erasure obligation's table, whose write was declared before the release "
    "(ADR 0183): the subject may stand in them, and whoever may erase each records an obligation following the released one.",
)
_late_derived_unwatched = _meter.create_counter(
    "catalog.erasure.late_derived_unwatched",
    unit="{tick}",
    description="Ticks whose watch for late derived tables could not read the obligations, the input record, the edge list "
    "or a claim (ADR 0183): until it can, no late derived table is found.",
)
_unauthored = _meter.create_counter(
    "catalog.commit_reconcile.unauthored",
    unit="{commit}",
    description="Committed versions of an operation the catalog announces for which the catalog recorded no author "
    "(ADR 0187): a commit made past every catalog door, which nothing can announce on anyone's behalf.",
)


def create_series() -> None:
    """Create each unlabelled counter's series at 0 when the catalog starts its timers, so its first count is an
    increase an alert can read: a series born at 1 has no 0 before it. `catalog.commit_reconcile.reannounced` is counted
    by operation, an open set, so its alert reads a new series as its first find."""
    for counter in (_unreadable, _late_derived, _late_derived_unwatched, _unauthored):
        counter.add(0)


class CommitReconcileTick(BaseModel):
    """What one tick did."""

    skipped: bool = False
    tables: int = 0
    reannounced: int = 0
    unauthored: int = 0
    unreadable: int = 0
    late_derived: int = 0


class _LiveTable(BaseModel):
    """A table a held location claim names, as its name still describes it."""

    segments: list[str]
    location: str
    uuids: DatasetUuids


class _LiveTables(BaseModel):
    """The tables a tick reads, and how many claims named one it could not describe."""

    tables: list[_LiveTable] = Field(default_factory=list)
    #: Claims whose table the store did not describe: their versions are not read this tick, so nothing is announced for them.
    unreadable: list[LocationClaim] = Field(default_factory=list)


async def _live_tables(request: Request, settings: Settings, claims: list[LocationClaim]) -> _LiveTables:
    """The tables ``claims`` name, each whose name still describes to the claimed location.

    The claims, not a walk of the names: every table the catalog makes or registers takes one (ADR 0173 §6), across
    every warehouse, and it records the table's uuid. A claim kept for a trashed table, or one whose holder no longer
    describes to it (a rename or a drop in flight), names no live table this tick. A describe that failed otherwise is
    no answer about the name, so it is logged with its cause and counted, as an unreadable ref is.
    """
    store = table_claims.claim_store(settings)
    live = _LiveTables()
    for claim in claims:
        if claim.uuid is None or not claim.segments or await run_in_threadpool(table_claims.kept_in_trash, store, claim):
            continue
        try:
            ns = await namespace_for_table(request, settings, claim.segments)
            described = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=claim.segments))
        except (TableNotFoundError, NamespaceNotFoundError):
            continue
        except PermissionDeniedError:
            # The table's warehouse is deactivated (the resolver's quarantine): no door commits to it, so nothing is owed
            # until it is active again, when its marks are read from where they stand.
            continue
        except Exception as exc:  # noqa: BLE001 — a table this tick cannot reach is reached by the next; the others go on
            log.error("commit_reconcile_table_unreadable", extra={"table": claim.table, "error": str(exc)[:300]})
            _unreadable.add(1)
            live.unreadable.append(claim)
            continue
        location = str(described.location or "")
        if decoded_path(location) != claim.location:
            continue
        namespace_uuid = await ObjectUuids(request, settings).namespace(claim.segments[:-1]) if len(claim.segments) > 1 else None
        live.tables.append(
            _LiveTable(
                segments=claim.segments,
                location=location,
                uuids=DatasetUuids(table_uuid=claim.uuid, namespace_uuid=namespace_uuid.object_uuid if namespace_uuid is not None else None),
            )
        )
    return live


class _Filed(BaseModel):
    """One table entries are filed for, beside what the claim on the location they name says of it now."""

    model_config = ConfigDict(frozen=True)

    pending: commit_records.PendingTable
    #: The claim that still records the table at that location; ``None`` when no entry reads, or no claim records it.
    claim: LocationClaim | None
    #: Whether the location is read and no claim there records the table: it is dropped for good or was never made, so
    #: no pass will read it and its entries only wait to lapse.
    gone: bool


def _filed(store: ClaimStore, pending: commit_records.PendingTable) -> _Filed:
    """``pending`` beside the claim on the location its entries name."""
    if pending.location is None:
        return _Filed(pending=pending, claim=None, gone=False)
    read = location_claims.read(store, pending.location)
    if read is None or read.claim.uuid != pending.table_uuid:
        return _Filed(pending=pending, claim=None, gone=True)
    return _Filed(pending=pending, claim=read.claim, gone=False)


def _feed_location(settings: CatalogLineageSettings, ns: LanceNamespace) -> str | None:
    """Where ``provenance$events`` lives, or ``None`` before lineage has created it.

    Raises:
        Exception: Whatever the describe raised other than the feed or its namespace not being there yet: a feed the
            store did not describe is not a feed that does not exist, so the tick stops rather than decides without it.
    """
    if not settings.provenance_namespace:
        return None
    try:
        described = native.call(ns, "describe_table", DescribeTableRequest(id=[*parse_identifier(settings.provenance_namespace), "events"]))
    except (TableNotFoundError, NamespaceNotFoundError):
        return None
    return str(described.location or "") or None


async def _announce(
    emitter: LineageEmitter, *, request: Request, settings: Settings, so: StorageOptions, table: _LiveTable, commit: commit_reconcile.Commit
) -> None:
    """Announce ``commit`` as its door announced it, for the author the catalog recorded authorizing it, in the trace of the
    request that made it (ADR 0209), and audit the announcement."""
    readback = await run_in_threadpool(versions.read_version_and_schema, table.location, so, table.segments, pin_version=commit.version, branch=commit.branch)
    with continuing(commit.traceparent):
        await emit_write_event(
            emitter,
            table.segments,
            author=commit.author,
            version=commit.version,
            operation=commit.operation,
            uuids=ObjectUuids(request, settings),
            known=table.uuids,
            branch=commit.branch,
            branch_identifier=commit.branch_identifier,
            schema_fields=readback.schema_fields,
            source_uri=readback.uri or table.location,
            inputs=run_inputs(commit.run),
            run=commit.run,
            commits_rows=commit.commits_rows,
            event_time=commit.committed_at,
        )
    audit(
        AuditAction.COMMIT_VERSION_ANNOUNCED,
        AuditOutcome.SUCCESS,
        subject=commit.author,
        resource=f"table:{fga_ids.canonical_object_id(table.segments)}",
        version=commit.version,
        operation=commit.operation,
        branch=commit.branch,
    )


async def tick(ctx: Tick) -> CommitReconcileTick:
    """Announce every committed version past the grace that lineage's feed lacks, on each table a door filed a commit for.

    A table's lapsed entries are removed only by a pass that read every ref of it, or once no claim records the table: one
    this pass did not read (its store did not answer, its warehouse is deactivated, it is in the trash, a rename is in
    flight) keeps them, so the tick after it is readable again looks for what it holds.
    """
    store = table_claims.claim_store(ctx.settings)
    lapse = location_claims.LEASE + timedelta(seconds=ctx.settings.commit_reconcile_grace_seconds)
    pending = await run_in_threadpool(partial(commit_records.pending_tables, store.control_root, store.storage_options, lapse=lapse))
    filed = [await run_in_threadpool(_filed, store, table) for table in pending]
    done = await _reconcile(ctx, [table.claim for table in filed if table.claim is not None], event="commit_reconcile_tick")
    lapsed = [path for table in filed if table.gone or table.pending.table_uuid in done.read for path in table.pending.lapsed]
    await run_in_threadpool(commit_records.settle_pending, store.control_root, store.storage_options, lapsed)
    return done.tick


async def walk(ctx: Tick) -> CommitReconcileTick:
    """Announce every committed version past the grace that lineage's feed lacks, on every table the catalog claims, and
    count each version no door made.

    A table the walk could not read is filed (:func:`commit_records.file_pending`), so the tick reads it again within its
    own period and a table that stays unreadable keeps counting, rather than waiting a walk's period for each try.
    """
    store = table_claims.claim_store(ctx.settings)
    done = await _reconcile(ctx, await run_in_threadpool(location_claims.held_claims, store), event="commit_walk")
    for claim in done.unread:
        # Only where the tick runs to read the entry and remove it.
        if claim.uuid is not None and ctx.settings.commit_reconcile_period_seconds:
            await run_in_threadpool(partial(commit_records.file_pending, store.control_root, store.storage_options, claim.uuid, location=claim.location))
    return done.tick


class _Done(BaseModel):
    """What one pass did: the uuids of the tables it read every ref of, and the claims of those it could not read."""

    model_config = ConfigDict(frozen=True)

    tick: CommitReconcileTick
    read: frozenset[str] = frozenset()
    unread: list[LocationClaim] = Field(default_factory=list)


async def _reconcile(ctx: Tick, claims: list[LocationClaim], *, event: str) -> _Done:
    """Announce every committed version past the grace that lineage's feed lacks, on the tables ``claims`` names, and log
    what the pass did as ``event``."""
    request, settings, so, clock = ctx.request, ctx.settings, ctx.storage_options, ctx.clock
    if not commit_ids_of(request).ready:
        return _Done(tick=CommitReconcileTick(skipped=True))
    emitter = app_wire(request, "lineage_emitter", LineageEmitter)
    estate = await estate_namespace(request, settings)
    late_derived = await _watch_released(settings, estate, so)
    try:
        feed = await run_in_threadpool(_feed_location, settings, estate)
    except Exception as exc:  # noqa: BLE001 — a feed this tick cannot read is read by the next; nothing is announced without it
        log.error("commit_reconcile_feed_unreadable", extra={"error": str(exc)[:300]})
        _unreadable.add(1)
        return _Done(tick=CommitReconcileTick(skipped=True, unreadable=1, late_derived=late_derived))
    if feed is None:
        return _Done(tick=CommitReconcileTick(skipped=True, late_derived=late_derived))
    live = await _live_tables(request, settings, claims)
    tables = live.tables
    now = clock().astimezone(UTC)
    tick_pass = _Pass(emitter=emitter, request=request, settings=settings, so=so, on_feed=commit_reconcile.feed_reading(settings, so, feed=feed, now=now))
    total = _Reconciled(unreadable=len(live.unreadable))
    by_uuid = {claim.uuid: claim for claim in claims}
    read: set[str] = set()
    unread = list(live.unreadable)
    for table in tables:
        reconciled = await _reconcile_table(table, tick_pass=tick_pass)
        table_uuid = str(table.uuids.table_uuid)
        if reconciled.unreadable:
            unread.append(by_uuid[table_uuid])
        else:
            read.add(table_uuid)
        total = total.plus(reconciled)
    reannounced, unauthored, unreadable = total.reannounced, total.unauthored, total.unreadable
    log.info(
        event,
        extra={"tables": len(tables), "reannounced": reannounced, "unauthored": unauthored, "unreadable": unreadable, "late_derived": late_derived},
    )
    done = CommitReconcileTick(tables=len(tables), reannounced=reannounced, unauthored=unauthored, unreadable=unreadable, late_derived=late_derived)
    return _Done(tick=done, read=frozenset(read), unread=unread)


class _Pass(BaseModel):
    """What every table of one tick is reconciled against: the emitter, the request, the store, and the feed as the tick read it."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # The emitter (a protocol) and the request are collaborators pydantic cannot validate.
    emitter: SkipValidation[LineageEmitter]
    request: SkipValidation[Request]
    settings: Settings
    so: StorageOptions
    on_feed: SkipValidation[commit_reconcile.FeedReading]


class _Reconciled(BaseModel):
    """How many versions a reconcile re-announced, found unauthored and could not read."""

    model_config = ConfigDict(frozen=True)

    reannounced: int = 0
    unauthored: int = 0
    unreadable: int = 0

    def plus(self, other: _Reconciled) -> _Reconciled:
        """The two counts added."""
        return _Reconciled(
            reannounced=self.reannounced + other.reannounced,
            unauthored=self.unauthored + other.unauthored,
            unreadable=self.unreadable + other.unreadable,
        )


async def _reconcile_table(table: _LiveTable, *, tick_pass: _Pass) -> _Reconciled:
    """Announce every committed version of ``table`` past the grace that the tick's feed lacks, and count what it did."""
    reannounced = unauthored = unreadable = 0
    emitter, request, settings, so = tick_pass.emitter, tick_pass.request, tick_pass.settings, tick_pass.so
    try:
        found = await run_in_threadpool(
            partial(
                commit_reconcile.reconcile_table,
                settings,
                so,
                segments=table.segments,
                location=table.location,
                table_uuid=str(table.uuids.table_uuid),
                on_feed=tick_pass.on_feed,
            )
        )
    except Exception as exc:  # noqa: BLE001 — one table this tick cannot read must not stop the others
        log.error("commit_reconcile_table_failed", extra={"table": table.segments, "error": str(exc)[:300]})
        _unreadable.add(1)
        unreadable += 1
        return _Reconciled(reannounced=reannounced, unauthored=unauthored, unreadable=unreadable)
    if found.unreadable:
        _unreadable.add(len(found.unreadable))
        unreadable += len(found.unreadable)
    for scan in found.scans:
        for commit in scan.missing:
            await _announce(emitter, request=request, settings=settings, so=so, table=table, commit=commit)
            _reannounced.add(1, {"lance.catalog.operation": commit.operation})
            log.warning(
                "commit_reannounced",
                extra={"table": table.segments, "version": commit.version, "branch": commit.branch, "operation": commit.operation},
            )
        if scan.unauthored:
            _unauthored.add(len(scan.unauthored))
            log.error("commit_unauthored", extra={"table": table.segments, "versions": list(scan.unauthored), "branch": scan.branch})
        reannounced += len(scan.missing)
        unauthored += len(scan.unauthored)
    return _Reconciled(reannounced=reannounced, unauthored=unauthored, unreadable=unreadable)


async def _watch_released(settings: Settings, estate: LanceNamespace, so: StorageOptions) -> int:
    """List and count every late derived table of a released erasure obligation; how many were new this tick."""
    provenance = estate if settings.provenance_namespace else None
    try:
        found = await run_in_threadpool(erasure_derived.watch_released, settings, provenance, so)
    except Exception as exc:  # noqa: BLE001 — a watch this tick cannot read is read by the next; the announcing goes on
        log.error("erasure_late_derived_unread", extra={"error": str(exc)[:300]})
        _late_derived_unwatched.add(1)
        return 0
    for late in found:
        _late_derived.add(len(late.tables))
        log.error("erasure_late_derived", extra={"obligation": late.obligation_id, "tables": late.tables})
    return sum(len(late.tables) for late in found)
