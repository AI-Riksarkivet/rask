"""The maintenance service's write-event subscription — the PRIMARY trigger.

The sweep discovers by walking every bucket every tick: measured at 87 datasets, one manifest open
each, reporting `fragments_removed: 0, versions_removed: 0` on every pass since 2026-08-16. This lane
replaces that as the primary trigger and leaves the cron as an HOURLY BACKSTOP, which is a correctness
requirement rather than caution — the bus is provably incomplete (ingest, Ray TRAIN and external
OpenLineage producers emit over HTTP only and never reach the topic; the catalog's lineage lane has no
outbox, so a lost trigger is silent), and old-version GC on a table nobody has written since has no
write to react to at all.

Subscribes to `lineage.events.v1`, the one lane every governed writer converges on, with a queue group
so replicas compete rather than each planning the same dataset. The control topic is deliberately not
used: it is a broadcast with no queue group, and it is the notifications plane's people-targeting lane.

Registered only when a work topic is configured — the same condition the cron uses to choose its lane,
so a deployment cannot advertise a subscription for a queue it never publishes to.

A WRITE IS ITS PUBLISHER'S CLAIM UNTIL ITS SIGNATURE IS CHECKED ([[XC-078]], owner ruling R4). The Dapr app token
proves that this pod's own sidecar delivered an event, not who published it, and the location a write names is what
this service plans a rewrite of. So before planning, the door checks the write's `rask_signature` against the keys its
signer publishes, with the signer sets lineage verifies with (`RASK_EVENT_SIGNERS`, `RASK_EVENT_DELEGATORS`), and
`RASK_SIGNATURE_DOORS` decides what it does with the verdict (`lineage_kit.door`): off checks nothing, observe plans as
before and counts what enforcing would refuse, and enforce plans only a write a listed signer's key verifies. A refusal
is acknowledged and plans nothing, since no redelivery can sign bytes already published; a key list that cannot be read
is retried, since it says nothing about the signature. Only a write the lane would plan is checked (`plannable_write`),
so an event it acknowledges untouched costs no key read.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool

from lineage_kit.door import DoorMode, DoorVerdict, judge
from lineage_kit.keys import PublishedKeys
from lineage_kit.signing import signature_of, verify_signature
from maintenance.api.dependencies import DaprClientDep, SettingsDep
from maintenance.core.config import MaintenanceSettings
from maintenance.core.metrics import SignatureDoor, record_signature_refused, record_signature_would_refuse
from maintenance.services.arrival import plannable_write, should_replan
from maintenance.services.sweep import plan_one
from maintenance.services.work_queue import RETRY, SUCCESS, enqueue_units
from service_kit.draining import retry_when_draining
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.governed.signing_key import published_key_fetch, retry_until_signed
from service_kit.lakehouse import maintenance_policies


log = logging.getLogger(__name__)

#: This door's name on the signature counters and log lines.
ARRIVAL_DOOR: Final[SignatureDoor] = "maintenance-arrival"

#: Bounds what an event's own text can put into a log line: the identity and the ids it carries are its publisher's.
_MAX_LOGGED_CHARS: Final = 300


def _published_keys(request: Request, settings: MaintenanceSettings) -> PublishedKeys:
    """This app's key reader, built on first use and kept on the app so its cache outlives one delivery."""
    state = request.app.state
    keys: PublishedKeys | None = getattr(state, "published_keys", None)
    if keys is None:
        keys = state.published_keys = PublishedKeys(published_key_fetch(settings.dapr_secret_store))
    return keys


def _bounded(value: object) -> str | None:
    """A string the event carries, cut to `_MAX_LOGGED_CHARS`; anything else is None."""
    if not isinstance(value, str):
        return None
    return value if len(value) <= _MAX_LOGGED_CHARS else f"{value[:_MAX_LOGGED_CHARS]}..."


async def _judge_arrival(arrived: dict[str, Any], *, event_id: object, request: Request, settings: MaintenanceSettings) -> DoorVerdict:
    """The door's verdict on a write this lane would plan, counted and logged whenever it is anything but a pass.

    Over the event exactly as it arrived, never a re-serialisation: the producer signed the document it published. Off
    answers before any key is read; otherwise the check reads the store through a blocking client, so it runs in the
    threadpool.
    """
    mode = DoorMode(settings.signature_doors)
    if mode is DoorMode.OFF:
        return DoorVerdict(act=True)
    keys = _published_keys(request, settings)
    claim = signature_of(arrived)
    source = keys.for_event(claim.kid if claim else None)
    verdict = await run_in_threadpool(
        judge, lambda: verify_signature(arrived, source=source, signers=settings.event_signers, delegators=settings.event_delegators), mode
    )
    run = arrived.get("run")
    subject = {
        "door": ARRIVAL_DOOR,
        "identity": _bounded(claim.identity) if claim else None,
        "event_id": _bounded(event_id),
        "run_id": _bounded(run.get("runId")) if isinstance(run, dict) else None,
    }
    if verdict.refused is not None:
        record_signature_refused(door=ARRIVAL_DOOR, reason=verdict.refused)
        log.warning("maintenance_signature_refused", extra={**subject, "reason": verdict.refused})
    elif verdict.observed is not None:
        record_signature_would_refuse(door=ARRIVAL_DOOR, reason=verdict.observed)
        log.warning("maintenance_signature_would_refuse", extra={**subject, "reason": verdict.observed})
    elif verdict.retry:
        log.warning("maintenance_signature_keys_unavailable", extra=subject)
    return verdict


async def handle_arrival(event: dict[str, Any], settings: MaintenanceSettings, dapr: Any) -> dict[str, str]:  # noqa: ANN401 — DaprClient | None
    """Decide whether this write means maintenance, and enqueue one unit if it does.

    ACKS far more often than it enqueues, and every ack is a decision rather than a shrug: a byte-free
    catalog operation, one of maintenance's own completion events (the loop guard), an event carrying
    no physical URI, a trashed or policy-disabled dataset, or a table already at target. None of those
    is retryable, and the hourly backstop re-reaches anything this declines in error.

    RETRIES only when the unit could not be PUBLISHED. That is the one failure redelivery can fix, and
    dropping it silently would make a sidecar outage look like an estate with nothing to do.

    It checks no signature: the route calls it only for a write its door admitted, and a direct caller verifies nothing.
    """
    write = plannable_write(event.get("data", event))
    if write is None:
        return {"status": SUCCESS}
    # DEBOUNCE BEFORE PLANNING, never inside it. `plan_one` calls `sibling_base_refs`, which opens every
    # sibling manifest in the warehouse — so a write burst would drive one whole-warehouse sweep per
    # write. Checking a single JSON stamp first is what makes the lane affordable on a busy table.
    options = settings.storage_options()
    last = await run_in_threadpool(maintenance_policies.read_planned_version, settings.resolved_policy_root, options, write.location)
    if not should_replan(last_planned=last, event_version=write.version, min_versions=settings.event_min_versions):
        return {"status": SUCCESS}
    item = await run_in_threadpool(plan_one, write.location, settings)
    if item is None:
        return {"status": SUCCESS}
    published, _not_queued = await enqueue_units(
        dapr, [item], pubsub=settings.work_pubsub, topic=settings.work_topic, timeout_seconds=settings.publish_timeout_seconds
    )
    if published and write.version is not None:
        # Stamp only what actually reached the queue. Stamping a unit that failed to publish would
        # debounce a dataset whose work never got queued — the one combination that loses maintenance
        # silently, since the next events would be skipped against a plan that never happened.
        await run_in_threadpool(maintenance_policies.write_planned_version, settings.resolved_policy_root, options, write.location, write.version)
    return {"status": SUCCESS if published else RETRY}


def register_arrival_route(app: FastAPI, settings: MaintenanceSettings, dapr_app: DaprApp | None = None) -> DaprApp | None:
    """Register the write-event subscription, or nothing when this deployment has no queue.

    Takes an existing :class:`DaprApp` when one was already built for another subscription — a second
    ``DaprApp(app)`` would re-register ``/dapr/subscribe`` and the sidecar would read only one of them.
    """
    if not settings.work_topic:
        return None
    wrapper = dapr_app or DaprApp(app)

    @wrapper.subscribe(
        pubsub=settings.lineage_pubsub,
        topic=settings.lineage_topic,
        route="/maintenance-arrival",
        dead_letter_topic=settings.work_dlq_topic or None,
    )
    async def on_arrival(
        event: dict[str, Any],
        request: Request,
        *,
        config: SettingsDep,
        dapr: DaprClientDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
        signing: Annotated[dict[str, str] | None, Depends(retry_until_signed)] = None,
    ) -> dict[str, str]:
        """``event`` is typed ``dict`` so FastAPI parses the CloudEvent body — an ``Any`` param becomes
        a query param and 422s. The Dapr app-api-token authenticates the sidecar that delivered it; the
        signature door, when it enforces, is what stops a forged write from naming any URI in any bucket for
        this service to plan a rewrite of.

        While draining, ask for REDELIVERY rather than planning — Dapr's delivery does not consult a
        readiness probe, and the plan this would produce could outlive the pod that published it. The same answer
        while this service's signing key is unresolved: the work this plans emits signed events. Both answer before
        the event is read; the door reads it, so it comes after them and before anything is planned.
        """
        if drain is not None:
            return drain
        if signing is not None:
            return signing
        arrived = event.get("data", event)
        if plannable_write(arrived) is not None:
            verdict = await _judge_arrival(arrived, event_id=event.get("id"), request=request, settings=config)
            if verdict.retry:
                return {"status": RETRY}
            if not verdict.act:
                return {"status": SUCCESS}
        return await handle_arrival(event, config, dapr)

    return wrapper


__all__ = ["handle_arrival", "register_arrival_route"]
