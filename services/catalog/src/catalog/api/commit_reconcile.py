"""The cron tick that announces every committed version lineage's feed lacks: the commit is the outbox ([[LH-497]]).

A write is announced after its commit (`catalog.core.lineage_emit`), and a crash between the commit and the staging of
its event loses the announcement while the version stays committed, so a pipeline following ``/events?since=`` never
starts its next hop. This tick finds such a version from the table's own version log
(`catalog.services.commit_reconcile`) and announces it, as the catalog announced it the first time: the same event,
under the run id derived from the commit, signed by the catalog for the commit's author. A late original and the
re-announcement are then one run, which lineage's feed records once.

Each find is counted on ``catalog.commit_reconcile.reannounced``, each version naming no author on
``catalog.commit_reconcile.unauthored``, and each ref whose log could not be read on ``catalog.commit_reconcile.unreadable``;
each pages (`deploy/alerting/rules.yml`).

The same tick watches every released erasure obligation for a late derived table (ADR 0183 §5,
`catalog.services.erasure_derived.watch_released`): a table derived from the erased one, written before the release and
found only since, is listed on the obligation and counted on ``catalog.erasure.late_derived``, which pages, and a watch
that could not read what it judges counts ``catalog.erasure.late_derived_unwatched``, which pages too. It needs no commit
feed, so it runs whether or not lineage has created one.

**Delivered at the POD ROOT**, as ``POST /<component name>``, so the Component's name,
``LANCE_COMMIT_RECONCILE_BINDING_NAME`` and the served path are one string. Guarded by ``require_dapr_token``: an open
door here would let anything that reaches the port make the catalog walk the estate.

`bindings.cron` fires on every replica, and two replicas reconciling at once announce a found version twice, which the
derived run id lands once on the feed; a mark one writes lower than another's costs a re-read, never a skipped version.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, timedelta
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import DescribeTableRequest, LanceNamespace
from opentelemetry import metrics
from pydantic import BaseModel

from catalog.api.dependencies import ClockDep, ObjectUuids, SettingsDep, StorageOptionsDep, estate_namespace, namespace_for_top_ns
from catalog.core.commit_ids import commit_ids_of
from catalog.core.config import Settings
from catalog.core.lineage_emit import LineageEmitter, emit_write_event, run_inputs
from catalog.services import commit_reconcile, dataplane, erasure_derived, native, table_claims
from service_kit.governed import fga
from service_kit.governed.audit import AuditAction, AuditOutcome, audit
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse import location_claims
from service_kit.lakehouse.base_refs import decoded_path
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.openlineage import DatasetUuids


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")
_reannounced = _meter.create_counter(
    "catalog.commit_reconcile.reannounced",
    unit="{commit}",
    description="Committed versions whose announcement lineage's feed lacked past the grace, announced again from the "
    "version log ([[LH-497]]). Each is an announcement lost between a commit and its staging: a pipeline following the "
    "feed was not told of the write until now.",
)
_unreadable = _meter.create_counter(
    "catalog.commit_reconcile.unreadable",
    unit="{ref}",
    description="Table refs whose version log the commit reconciler could not read past a point: the ref keeps its mark, "
    "and every lost announcement after it waits until it is read.",
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
    description="Committed versions of an operation the catalog announces that name no author and for which the catalog "
    "recorded none: a commit made past every catalog door, which nothing can announce on anyone's behalf.",
)

#: How much earlier than the oldest commit looked for the feed is read: a row is received after its commit, never
#: before, but the store's clock and the catalog's may disagree.
_FEED_SLACK = timedelta(minutes=10)

#: One tick at a time per process: a second arriving while one runs would only re-read what it reads.
_tick_lock = asyncio.Lock()


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


async def _live_tables(request: Request, settings: Settings) -> list[_LiveTable]:
    """Every table the catalog holds a location claim for, whose name still describes to the claimed location.

    The claims, not a walk of the names: every table the catalog makes or registers takes one (ADR 0173 §6), across
    every warehouse, and it records the table's uuid. A claim kept for a trashed table, or one whose holder no longer
    describes to it (a rename or a drop in flight), names no live table this tick.
    """
    store = table_claims.claim_store(settings)
    claims = await run_in_threadpool(location_claims.held_claims, store)
    live: list[_LiveTable] = []
    for claim in claims:
        if claim.uuid is None or not claim.segments or await run_in_threadpool(table_claims.kept_in_trash, store, claim):
            continue
        try:
            ns = await namespace_for_top_ns(request, settings, claim.segments[0]) if len(claim.segments) > 1 else await estate_namespace(request, settings)
            described = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=claim.segments))
        except Exception as exc:  # noqa: BLE001 — a table this tick cannot reach is reached by the next
            log.info("commit_reconcile_table_unreachable", extra={"table": claim.table, "error": str(exc)[:300]})
            continue
        location = str(described.location or "")
        if decoded_path(location) != claim.location:
            continue
        namespace_uuid = await ObjectUuids(request, settings).namespace(claim.segments[:-1]) if len(claim.segments) > 1 else None
        live.append(
            _LiveTable(
                segments=claim.segments,
                location=location,
                uuids=DatasetUuids(table_uuid=claim.uuid, namespace_uuid=namespace_uuid.object_uuid if namespace_uuid is not None else None),
            )
        )
    return live


def _feed_location(settings: Settings, ns: LanceNamespace) -> str | None:
    """Where ``provenance$events`` lives, or ``None`` before lineage has created it."""
    if not settings.provenance_namespace:
        return None
    try:
        described = native.call(ns, "describe_table", DescribeTableRequest(id=[settings.provenance_namespace, "events"]))
    except Exception:  # noqa: BLE001 — no feed yet is a feed holding nothing
        return None
    return str(described.location or "") or None


async def _announce(
    emitter: LineageEmitter, request: Request, settings: Settings, so: StorageOptions, table: _LiveTable, commit: commit_reconcile.Commit
) -> None:
    """Announce ``commit`` as its door announced it, for its author, and audit the announcement."""
    readback = await run_in_threadpool(dataplane.read_version_and_schema, table.location, so, table.segments, commit.version, commit.branch)
    await emit_write_event(
        emitter,
        table.segments,
        author=commit.author,
        version=commit.version,
        operation=commit.operation,
        authorization=None,
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
        resource=f"table:{fga.canonical_object_id(table.segments)}",
        version=commit.version,
        operation=commit.operation,
        branch=commit.branch,
    )


async def _on_cron(
    request: Request,
    settings: SettingsDep,
    so: StorageOptionsDep,
    clock: ClockDep,
    _: Annotated[None, Depends(require_dapr_token)],
) -> CommitReconcileTick:
    """Announce every committed version past the grace that lineage's feed lacks."""
    if _tick_lock.locked() or not commit_ids_of(request.app).ready:
        return CommitReconcileTick(skipped=True)
    emitter: LineageEmitter = request.app.state.lineage_emitter
    async with _tick_lock:
        estate = await estate_namespace(request, settings)
        late_derived = await _watch_released(settings, estate, so)
        feed = await run_in_threadpool(_feed_location, settings, estate)
        if feed is None:
            return CommitReconcileTick(skipped=True, late_derived=late_derived)
        tables = await _live_tables(request, settings)
        now = clock().astimezone(UTC)
        reannounced = unauthored = unreadable = 0
        for table in tables:
            try:
                found = await run_in_threadpool(
                    partial(
                        commit_reconcile.reconcile_table,
                        settings,
                        so,
                        segments=table.segments,
                        location=table.location,
                        table_uuid=str(table.uuids.table_uuid),
                        feed=feed,
                        now=now,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one table this tick cannot read must not stop the others
                log.error("commit_reconcile_table_failed", extra={"table": table.segments, "error": str(exc)[:300]})
                _unreadable.add(1)
                unreadable += 1
                continue
            if found.unreadable:
                _unreadable.add(len(found.unreadable))
                unreadable += len(found.unreadable)
            for scan in found.scans:
                for commit in scan.missing:
                    await _announce(emitter, request, settings, so, table, commit)
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
    log.info(
        "commit_reconcile_tick",
        extra={"tables": len(tables), "reannounced": reannounced, "unauthored": unauthored, "unreadable": unreadable, "late_derived": late_derived},
    )
    return CommitReconcileTick(tables=len(tables), reannounced=reannounced, unauthored=unauthored, unreadable=unreadable, late_derived=late_derived)


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


async def _ack_binding() -> dict[str, str]:
    """Dapr's input-binding handshake: an OPTIONS probe at the route, answered so the sidecar delivers to it."""
    return {}


def mount_commit_reconcile(app: FastAPI, binding_name: str) -> bool:
    """Mount the tick at ``POST /<binding_name>``; nothing when the chart names no binding. Whether it was mounted."""
    if not binding_name:
        return False
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", _on_cron, methods=["POST"], tags=["commit-reconcile"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    app.include_router(router)
    return True
