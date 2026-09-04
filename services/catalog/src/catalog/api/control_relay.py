"""The CONTROL lane's relay — the cron tick that re-publishes staged ``catalog.control.v1`` events.

``DaprControlEmitter.emit`` stages every control event to ``LANCE_CONTROL_OUTBOX_URI`` before it
publishes and drops it only on ack, so a NATS blip leaves the event durable rather than gone. This is
the other half: the thing that reads that prefix and delivers what it finds.

**Why it matters more than "a refresh hint".** Most control events are exactly that — a console ring
buffer or a tag-polling reader loses a redraw. ``table_published`` is not: the mover does not fire the
next stage's topic, and ``/publication-arrival`` receiving this event is the ONLY thing that WAKES
silver->gold. The medallion's cascade-lag cron re-reads the ``published`` tag since
``open_cascade_repair.md`` C3, and that does not weaken this argument by a word: it MEASURES how far a
tier has fallen behind and advances nothing, so a lost publish is still a cascade that stops. A dropped one ends the cascade with the tag advanced, the data consumable, the
route 200, every pod green, and nothing red.

**WHY HERE, IN THE CATALOG.** The lineage relay lives in the lineage service because its drain is an
INGEST — it writes each recovered event into the AGE graph and the durable feed, work only that service
can do. A control event has no graph to re-ingest into; the only thing owed to it is delivery onto the
topic it never reached. That makes the producer the right host: the catalog already owns the prefix
(``LANCE_CONTROL_OUTBOX_URI``), the component name (``LANCE_CONTROL_PUBSUB``), the S3 credentials that
address the prefix, and a Dapr sidecar. Any other service would have to be taught all four, and a
second copy of "which prefix does the control lane stage to" is how the two ends drift apart.

**WHY A CRON BINDING, NOT THE JOBS API.** ``.claude/skills/rask-dapr`` records the 2026-08-28 ruling:
recurring scan-and-converge is a cron binding, and the Jobs API is refused for it. This is convergence
work by the same test — level-triggered, idempotent, self-healing, late-is-free. The schedule is
Component config in the chart, so this module keeps no scheduler thread and nothing to drain at
shutdown.

**Delivered at the POD ROOT.** A Dapr input binding arrives as ``POST /<component name>``, never under a
prefix, and this app's routers already mount at the root (``service_kit.lance_app``). The Component's
name, ``LANCE_CONTROL_RELAY_BINDING_NAME`` and the served path are ONE string — see
:func:`build_control_relay_router`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError

from catalog.api.dependencies import SettingsDep
from catalog.core.config import Settings
from service_kit import dapr_publish
from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse import outbox, outbox_metrics


log = logging.getLogger(__name__)

#: How many staged events one tick drains. The lineage drain learned this the hard way (DECISIONS.md
#: P1.2): materialising a whole prefix inside the tick makes the relay fail hardest exactly when a
#: backlog exists, which is the only situation it is for. The remainder drains next tick, oldest-first,
#: so nothing starves. A literal rather than a knob because the control lane's event rate is bounded by
#: catalog MUTATIONS, not by row counts — there is no deployment shape that wants a different number,
#: and an unused setting is one more thing the chart can render wrong.
DRAIN_LIMIT = 500

#: Single-flight for THIS PROCESS. Dapr's cron binding has no overlap protection, so a tick that
#: outruns its period is delivered anyway and two passes would list the same objects.
#:
#: Deliberately NOT a cluster-wide lock, and the catalog may run several replicas. Two replicas racing
#: this prefix costs a duplicate PUBLISH, never a lost or corrupted event: `list_events` skips an object
#: another drain has already removed, and every consumer of this topic is built for at-least-once —
#: the ring buffer dedupes on `event_id`, and the cascade's deterministic instance id dedupes the work
#: (see `_republish`). Buying cross-replica exclusion would mean a distributed lock component, which the
#: estate has an open ruling against adopting while its API is Alpha.
_relay_lock = asyncio.Lock()


class ControlRelayReport(BaseModel):
    """One tick's findings — the response body and the shape its log line counts."""

    #: Staged events found under the prefix at the START of the tick (the saturation snapshot).
    depth: int = 0
    #: Age of the oldest staged event. Bounds how long the control lane has been undelivered.
    oldest_age_seconds: float = 0.0
    #: Events re-published onto the control topic and then dropped.
    republished: int = 0
    #: Unparseable objects discarded so they cannot wedge the drain. Non-zero is a producer bug.
    poison_dropped: int = 0
    #: A tick that found another pass already running on this replica.
    skipped: bool = False
    reason: str = ""


def get_control_publisher(request: Request) -> object | None:
    """The lifespan-built Dapr client, or ``None`` when this deployment has no sidecar transport.

    Read off ``app.state`` rather than constructed here: a per-tick client would open and discard a
    gRPC channel every cron interval, and a deployment with control emission off has no client to build.
    Typed ``object`` because concrete Dapr clients differ in signature and this module only forwards one
    to ``dapr_publish``. ``None`` makes the tick a pure OBSERVATION — it still reports depth and age, so
    a backlog accruing on a mis-wired deployment is visible instead of silently unmeasured.
    """
    return getattr(request.app.state, "dapr_client", None)


ControlPublisherDep = Annotated[object | None, Depends(get_control_publisher)]


async def _republish(publisher: object, settings: Settings, event_json: str) -> None:
    """Deliver ONE staged event onto the control topic — the STAGED BYTES, verbatim.

    Never ``event.model_dump_json()``. Round-tripping through the model re-serializes ``occurred_at``
    and re-orders ``extra``, and the point of the redelivery is that subscribers see exactly what they
    would have seen the first time.

    Byte-identity is also what makes the redelivery IDEMPOTENT, and the chain is worth stating because
    it is the whole answer to "does this drive the cascade twice?":

    ``event_id`` survives -> ``/publication-arrival`` mints its stage ``token`` from it
    (`publication_trigger.py`) -> ``stage_submission_id(stage, token, from_uri, to_uri)`` hashes that
    into the workflow's deterministic ``instance_id`` (`medallion/services/transform.py`) ->
    ``schedule_new_workflow`` for a live instance errors, and the mover reports that as the RE-ATTACH it
    is. So a duplicate delivery attaches to the run already in flight rather than starting a second.

    Every other subscriber on this topic keys on the same id: the catalog's own ring buffer dedupes on
    ``event_id``, and the notifications plane's ledger on ``<event_id>@<ACTION>``.

    THE BOUND OF THAT GUARANTEE, stated rather than assumed: the engine dedupes against instances it
    still HOLDS. A redelivery arriving after the original instance has completed and been purged
    schedules a fresh one, which re-runs the same hop. That is duplicate COMPUTE, not duplicate data —
    the stage write is single-flighted and content-deterministic, so the second pass is a same-bytes
    overwrite (`bronze_arrival.py` records the same property for the two cascade heads).
    """
    await dapr_publish.publish_event(
        publisher,
        timeout_seconds=settings.control_emit_timeout_seconds,
        pubsub_name=settings.control_pubsub,
        topic_name=CONTROL_TOPIC,
        data=event_json,
        data_content_type="application/json",
    )


async def _drain(settings: Settings, publisher: object | None) -> ControlRelayReport:
    """Re-publish and drop every staged control event, oldest first, up to :data:`DRAIN_LIMIT`.

    PUBLISH BEFORE DROP, never the other way round: a publish that fails must leave the object for the
    next tick, which is the entire point of staging. A redelivery costs a duplicate the lane already
    tolerates; a premature drop costs the cascade.

    Blocking object-store IO runs in the threadpool so a slow prefix never stalls the event loop.
    """
    options = settings.storage_options()
    depth, oldest_age = await run_in_threadpool(outbox.backlog, settings.control_outbox_uri, options)
    # ONE instrument for both lanes, on purpose. `chart/alerting/rules.yml` alerts on
    # `max(outbox_depth) > 0` and `max(outbox_oldest_age_seconds) > 300` — lane-agnostic by
    # construction — so observing here makes a stuck CONTROL outbox pageable with no new rule, and the
    # OTel resource's `service.name` (catalog vs lineage) says which lane is stuck. The alternative,
    # staying silent to keep the lineage series pure, is exactly the invisible-failure shape this whole
    # relay exists to remove.
    outbox_metrics.observe_backlog(depth, oldest_age)
    report = ControlRelayReport(depth=depth, oldest_age_seconds=round(oldest_age, 1))
    if not depth:
        return report
    if publisher is None:
        # No transport, so nothing here can be delivered — and NOTHING is dropped, because the staged
        # objects are the only copies. Reported rather than silent: a rendered outbox with no sidecar
        # client is a misconfiguration whose only symptom is a backlog that never falls.
        log.warning("control_relay_no_publisher", extra={"depth": depth, "oldest_age_seconds": report.oldest_age_seconds})
        return report

    staged = await run_in_threadpool(lambda: list(outbox.list_events(settings.control_outbox_uri, options, limit=DRAIN_LIMIT)))
    for key, event_json in staged:
        try:
            # VALIDATE ONLY — the parsed model is deliberately discarded. This asks one question, "could
            # a subscriber read this?", and the answer decides poison-drop vs relay; the bytes on the
            # wire are the staged ones, never a re-serialization of what was parsed here.
            CatalogControlEvent.model_validate_json(event_json)
        except ValidationError as exc:
            # NARROW on purpose — only a genuinely unvalidatable object is poison. A broad `except`
            # here would delete a staged event on any transient failure, i.e. destroy the one durable
            # copy this module exists to deliver. The action rides the RAW json because the model is
            # unavailable exactly where the answer is wanted.
            action: str | None = None
            try:
                action = str((json.loads(event_json) or {}).get("action") or "") or None
            except (ValueError, TypeError):
                action = None
            log.warning("control_outbox_poison_dropped", extra={"key": key, "action": action, "error": str(exc)})
            outbox_metrics.record_poison_dropped()
            await run_in_threadpool(outbox.drop_event, settings.control_outbox_uri, options, key)
            report.poison_dropped += 1
            continue
        try:
            await _republish(publisher, settings, event_json)
        except Exception as exc:
            # The bus is still down. Stop the pass rather than grinding the whole backlog against it:
            # every remaining object stays staged and the next tick retries from the oldest.
            log.warning("control_outbox_republish_failed", extra={"key": key, "error": str(exc)})
            outbox_metrics.record_publish_failed()
            break
        await run_in_threadpool(outbox.drop_event, settings.control_outbox_uri, options, key)
        report.republished += 1
    outbox_metrics.record_drained(report.republished)
    return report


async def _on_cron(
    settings: SettingsDep,
    publisher: ControlPublisherDep,
    _: Annotated[None, Depends(require_dapr_token)],
) -> ControlRelayReport:
    """One relay tick, driven by the Dapr cron binding.

    Guarded by ``require_dapr_token`` so only the sidecar's cron may drive it: an unauthenticated door
    here would let anything that can reach the port replay every staged control event on demand.
    """
    if _relay_lock.locked():
        log.info("control_relay_skipped_overlap")
        return ControlRelayReport(skipped=True, reason="another control relay pass is in progress")
    async with _relay_lock:
        report = await _drain(settings, publisher)
    if report.poison_dropped:
        log.warning("control_outbox_poison", extra={"dropped": report.poison_dropped})
    log.info(
        "control_relay_tick",
        extra={
            "depth": report.depth,
            "oldest_age_seconds": report.oldest_age_seconds,
            "republished": report.republished,
            "poison_dropped": report.poison_dropped,
        },
    )
    return report


async def _ack_binding() -> dict[str, str]:
    """Dapr's startup pre-flight (``OPTIONS /<binding name>``) — a 2xx confirms this app consumes the
    binding. Without it the app answers 405, the sidecar logs the binding as not consumed, and the
    schedule ticks into nothing."""
    return {"status": "ok"}


def build_control_relay_router(binding_name: str) -> APIRouter:
    """Register the relay at the EXACT binding name the sidecar delivers to (POST drain, OPTIONS ack)."""
    router = APIRouter()
    router.add_api_route(f"/{binding_name}", _on_cron, methods=["POST"], tags=["control-relay"])
    router.add_api_route(f"/{binding_name}", _ack_binding, methods=["OPTIONS"], include_in_schema=False)
    return router


def mount_control_relay(app: FastAPI, binding_name: str) -> bool:
    """Mount the relay when a binding name is configured; report whether it was.

    Opt-in like lineage's reconcile route: an unnamed binding means a deployment that stages nothing
    (or has no sidecar), and mounting an always-live cron door for it would add a replay surface with
    no component behind it.
    """
    if not binding_name:
        return False
    app.include_router(build_control_relay_router(binding_name))
    return True
