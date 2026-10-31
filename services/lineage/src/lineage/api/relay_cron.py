"""The relay tick — a Dapr cron binding drains the lineage outbox, prunes the feed and forwards it (ADR 0158).

A ``bindings.cron`` component POSTs to ``/<binding-name>`` on a schedule; OPTIONS is Dapr's binding-discovery
pre-flight. Guarded by ``require_dapr_token`` so only the sidecar's cron may drive it. The schedule + binding
name live in the chart, not app code (no scheduler thread here). Blocking object-store reads run in the
threadpool.

EVERY STEP IS IDEMPOTENT, so a tick needs no cluster-wide lock. The cron fires on every replica's sidecar,
and two ticks overlapping is safe: a drained event is recorded under its feed key (a second record lands no
row), re-published at least once as the topic already promises, and deleted (a second delete finds nothing);
two prunes delete the same rows once; two forwards send an event at least once, as the forwarder promises, and
neither moves the cursor back (`lineage.services.forward`).
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import PermissionDeniedError
from pydantic import BaseModel

from lineage.api.dependencies import ProvenanceDep, PublisherDep, SettingsDep
from lineage.api.fga_deps import enforce_bus_authz
from lineage.core.config import storage_options
from lineage.models import DatasetEvent, RunEvent, author_sub_from_payload
from lineage.services.forward import forward
from lineage.services.staged import UnparseableEventError, parse_staged, republish_staged
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse import outbox, outbox_metrics
from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)


class DrainOutcome(BaseModel):
    """What one outbox drain did — THREE numbers, because any one alone reads as the opposite of the truth.

    ``drained`` alone says a tick succeeded while a specific event has been refused on every tick since
    the estate came up; ``stranded`` alone says a tick failed while it recovered everything else. The set
    is what distinguishes "the relay is working and one event needs a human" from "the relay is wedged",
    and those need different responses.

    ``refused`` IS THAT DISTINCTION MADE LOAD-BEARING rather than left to a reader. A governance refusal
    is not a failed tick: the event is well-formed, the answer is deterministic, and no retry changes it.
    Counted as ``stranded`` it made the pair say both things at once — measured on the live estate,
    `drained=0 stranded=6` unchanged for 2.1 days while the relay was healthy.

    A REFUSED EVENT IS RETIRED ONLY ONCE IT IS RECORDED. Its verdict and payload reach the refusals table
    first (`record_refusal`, [[LH-182]]) and the staged object is deleted after; deleting it unrecorded would
    destroy the only durable copy of a committed write's provenance.
    """

    drained: int = 0
    stranded: int = 0
    refused: int = 0
    #: Refusals whose verdict was recorded, and whose object was therefore safe to drop. `recorded == refused`
    #: means the loop is closing; `recorded == 0` with `refused > 0` means it is not.
    recorded: int = 0


class RelayReport(BaseModel):
    """One relay tick: what the drain did, how many feed rows retention removed, and what the forwarder sent."""

    outbox_drained: int = 0
    outbox_stranded: int = 0
    outbox_refused: int = 0
    outbox_refusals_recorded: int = 0
    pruned_events: int = 0
    #: Kept parks the feed now holds, however their rows got there, which the tick no longer keeps ([[LH-148]]).
    parks_retired: int = 0
    forwarded: int = 0
    forward_refused: int = 0
    #: Why the forward stopped short of the feed's head, ``None`` when it did not or no backend is configured.
    forward_stalled: str | None = None


async def _prune_old_events(provenance: ProvenanceDep, settings: SettingsDep) -> int:
    """Feed retention — drop rows RECEIVED longer ago than the budget; 0 days keeps everything.

    ON THE TICK, NOT THE INGEST PATH: the feed's hottest path must not pay for a retention delete per event.
    Isolated: a retention failure degrades to a warning rather than failing a tick whose drain already ran.
    """
    if not settings.events_retention_days:
        return 0
    try:
        pruned = await provenance.prune_events(settings.events_retention_days)
    except Exception as exc:
        log.warning("lineage_event_prune_failed", extra={"error": str(exc)})
        return 0
    if pruned:
        log.info("lineage_events_pruned", extra={"pruned": pruned, "retention_days": settings.events_retention_days})
    return pruned


async def _on_cron(
    request: Request,
    provenance: ProvenanceDep,
    settings: SettingsDep,
    publisher: PublisherDep,
    _: Annotated[None, Depends(require_dapr_token)],
) -> dict[str, Any]:
    """One relay tick, triggered by a Dapr cron tick: drain the outbox, prune the feed, then forward it.

    Best-effort per the cron contract: each step degrades to a warning, so the tick's report reaches the
    caller either way.
    """
    report = RelayReport()
    if settings.outbox_uri:
        try:
            outcome = await _drain_outbox(request, provenance, settings, storage_options(settings), publisher)
            report.outbox_drained, report.outbox_stranded, report.outbox_refused = outcome.drained, outcome.stranded, outcome.refused
            report.outbox_refusals_recorded = outcome.recorded
        except Exception as exc:
            log.warning("lineage_outbox_drain_failed", extra={"error": str(exc)})
    report.pruned_events = await _prune_old_events(provenance, settings)
    try:
        report.parks_retired = await provenance.retire_recovered_parks()
    except Exception as exc:
        log.warning("lineage_parks_retire_failed", extra={"error": str(exc)})
    client = getattr(request.app.state, "forward_client", None)
    if settings.forward_url and client is not None:
        try:
            forwarded = await forward(provenance, settings, client)
            report.forwarded, report.forward_refused, report.forward_stalled = forwarded.forwarded, forwarded.refused, forwarded.stalled
        except Exception as exc:
            report.forward_stalled = f"{type(exc).__name__}: {exc}"
            log.warning("lineage_forward_failed", extra={"error": str(exc)})
    log.info("lineage_relay_tick", extra=report.model_dump())
    return report.model_dump()


async def _drain_outbox(
    request: Request, provenance: ProvenanceDep, settings: SettingsDep, opts: StorageOptions, publisher: object | None = None
) -> DrainOutcome:
    """Re-ingest + delete every staged lineage event (#4) — the full-event recovery half of the outbox.

    An unparseable (poison) object is dropped so it can't wedge the drain. A well-formed event is parsed
    with the doors' own discriminator (``parse_staged``), recorded idempotently, re-published and then
    deleted; a delete that fails just leaves the object for the next tick to re-record (a no-op) and retry
    the delete.

    PER-EVENT ISOLATION. One event the feed refuses must not decide anything about the others: the failure is
    caught around a SINGLE event's work, counted as STRANDED and named, and the object is deliberately left
    staged for the next tick. Without it one un-ingestable event stranded every other staged event — measured
    live 2026-09-07, with the tick still answering 200 and depth climbing.

    STRANDED IS NOT POISON. Poison is unparseable and is dropped; a stranded event is intact and is RETRIED,
    because dropping on a non-validation error destroys the event's only durable copy.

    BOUNDED + OBSERVED (docs/adr/0003-p1-1-outbox-observability-the-four-signals.md P1.1/P1.2). The drain reads
    at most ``outbox_drain_limit`` events per tick, OLDEST FIRST, and publishes the saturation snapshot on EVERY
    tick — including an empty one, so ``outbox.depth`` falls back to 0 instead of going stale and alerting forever.
    """
    depth, oldest_age = await run_in_threadpool(outbox.backlog, settings.outbox_uri, opts)
    outbox_metrics.observe_backlog(depth, oldest_age)
    if depth:
        log.info("lineage_outbox_backlog", extra={"depth": depth, "oldest_age_seconds": round(oldest_age, 1)})

    cap = settings.outbox_drain_limit or None  # 0 => unbounded
    staged = await run_in_threadpool(lambda: list(outbox.list_events(settings.outbox_uri, opts, limit=cap)))
    drained = stranded = refused = recorded = 0
    for key, event_json in staged:
        try:
            # THE DOORS' DISCRIMINATOR, never `RunEvent` alone: every catalog create, drop and alter is a
            # `DatasetEvent`, so a run-only parse destroys the staged copy of each one as poison ([[LH-199]]).
            event: RunEvent | DatasetEvent = parse_staged(event_json)
        except UnparseableEventError as exc:
            # ONLY a genuinely-unparseable event is poison. The author rides the RAW json: this branch fires
            # because the event would not validate, so the strict model is unavailable exactly where the answer
            # is needed.
            poison_author: str | None = None
            poison_run: str | None = None
            with suppress(Exception):
                payload = json.loads(event_json)
                poison_author = author_sub_from_payload(payload)
                poison_run = str((payload.get("run") or {}).get("runId") or "") or None
            log.warning("lineage_outbox_poison_dropped", extra={"outbox_key": key, "run_id": poison_run, "error": str(exc), "author": poison_author})
            outbox_metrics.record_poison_dropped()
            # ITS OWN TRY: this handler is outside the per-event `except Exception`, so a delete that raised here
            # would leave the loop and strand every event behind it.
            try:
                await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, key)
            except Exception as undropped:
                log.warning("lineage_outbox_poison_left_staged", extra={"outbox_key": key, "error": str(undropped)})
            continue
        try:
            # THE BUS DOOR'S GATE, never a second copy: the outbox is writable by the producers that stage there,
            # so a stager refused at the bus could stage instead. It authorizes AS the subject the producer
            # stamped, over THE STAGED BYTES (the signature covers what the producer wrote).
            await enforce_bus_authz(event, request, settings, json.loads(event_json))
            await provenance.record(event)
            # RE-PUBLISH, then drop. Recording alone repairs the feed and leaves every SUBSCRIBER unaware:
            # maintenance's arrival door and the notifications inbox hear of a recovered event only this way.
            # BEFORE the drop: a publish that fails leaves the staged object for the next tick, whose re-record
            # is a no-op. A duplicate delivery is the at-least-once contract subscribers are built for.
            await republish_staged(publisher, settings, event_json)
            await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, key)
        except PermissionDeniedError as exc:
            # NEITHER POISON NOR STRANDED: a governance answer about a well-formed event, so the object is retired
            # only once the verdict AND the event are recorded below — never on the verdict alone.
            refused += 1
            refused_author = author_sub_from_payload(json.loads(event_json))
            log.warning("lineage_outbox_event_unauthorized", extra={"outbox_key": key, "run_id": event.run_id, "author": refused_author, "reason": str(exc)})
            # RECORD, THEN RETIRE ([[LH-182]]). A crash after the record leaves the object, the next tick
            # re-refuses it, and the upsert on `outbox_key` makes that a no-op; delete-first would lose the only
            # durable copy. ITS OWN TRY ([[LH-297]]): a record or a drop that raised here would leave the loop.
            landed = False
            try:
                await provenance.record_refusal(outbox_key=key, run_id=event.run_id, author=refused_author, reason=str(exc), event_json=event_json)
                landed = True
                recorded += 1
                await run_in_threadpool(outbox.drop_event, settings.outbox_uri, opts, key)
            except Exception as unretired:
                log.warning(
                    "lineage_outbox_refusal_left_staged",
                    extra={"outbox_key": key, "run_id": event.run_id, "recorded": landed, "error": str(unretired)},
                )
            continue
        except Exception as exc:
            # LEFT STAGED on purpose — see "STRANDED IS NOT POISON" above.
            stranded += 1
            log.warning("lineage_outbox_event_stranded", extra={"outbox_key": key, "run_id": event.run_id, "error": str(exc)})
            continue
        drained += 1
    # Always emit — adding 0 CREATES the series, so a dashboard/alert has data from the first tick.
    outbox_metrics.record_drained(drained)
    outbox_metrics.record_stranded(stranded)
    outbox_metrics.record_refused(refused)
    if drained or stranded or refused:
        log.info("lineage_outbox_drained", extra={"drained": drained, "stranded": stranded, "refused": refused})
    return DrainOutcome(drained=drained, stranded=stranded, refused=refused, recorded=recorded)


async def _ack_binding() -> dict[str, str]:
    """Dapr's startup pre-flight (OPTIONS /<binding-name>) — a 2xx confirms this app consumes the binding."""
    return {"status": "ok"}


def build_relay_cron_router(binding_name: str) -> APIRouter:
    """Register the relay tick's route at the exact binding name the sidecar delivers to (POST tick, OPTIONS ack)."""
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", _on_cron, methods=["POST"], tags=["relay"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    return router
