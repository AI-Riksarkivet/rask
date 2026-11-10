"""The maintenance service's commit-lane subscription: the task each announced write, registration or drop asks for goes
on the work queue.

Subscribes to `lineage.events.v1`, the one lane every governed writer converges on and the catalog announces its tables'
births and drops on, with a queue group so replicas compete rather than each queueing the same event. Which task an event
asks for is :func:`maintenance.services.arrival.lane_task`'s: a table's pass, held a delay out, or a dropped table's
release. A task already queued or running for the table covers it, and one replica's worker runs it
(:mod:`maintenance.services.queue`). Registered when `MAINTENANCE_ARRIVAL_ENABLED` is on and a lineage topic is set.

A WRITE IS ITS PUBLISHER'S CLAIM UNTIL ITS SIGNATURE IS CHECKED ([[XC-078]], owner ruling R4). The Dapr app token
proves that this pod's own sidecar delivered an event, not who published it, and the location a write names is what
this service reads. So before evaluating, the door checks the write's `rask_signature` against the keys its signer
publishes, with the signer sets lineage verifies with (`RASK_EVENT_SIGNERS`, `RASK_EVENT_DELEGATORS`), and
`RASK_SIGNATURE_DOORS` decides what it does with the verdict (`lakehouse.lineage.door`): off checks nothing, observe evaluates
as before and counts what enforcing would refuse, and enforce evaluates only a write a listed signer's key verifies. A
refusal is acknowledged and queues nothing, since no redelivery can sign bytes already published; a key list that cannot
be read is retried, since it says nothing about the signature. Only an event the lane would queue a task for is checked
(`lane_task`), so an event it acknowledges untouched costs no key read.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool

from lakehouse import location_claims
from lakehouse.draining import retry_when_draining
from lakehouse.governed.dapr_auth import require_dapr_token
from lakehouse.governed.signing_key import published_key_fetch
from lakehouse.lineage.door import DoorMode, DoorVerdict, judge
from lakehouse.lineage.keys import PublishedKeys
from lakehouse.lineage.signing import signature_of, verify_signature
from maintenance.api.dependencies import SettingsDep, TaskQueueDep
from maintenance.core.config import MaintenanceSettings
from maintenance.core.metrics import SignatureDoor, create_signature_refused_series, record_signature_refused, record_signature_would_refuse
from maintenance.services.arrival import lane_task
from maintenance.services.naming import claim_store
from maintenance.services.queue import QueueUnavailableError, Task, TaskQueue


log = logging.getLogger(__name__)

#: This door's name on the signature counters and log lines.
ARRIVAL_DOOR: Final[SignatureDoor] = "maintenance-arrival"

#: Bounds what an event's own text can put into a log line: the identity and the ids it carries are its publisher's.
_MAX_LOGGED_CHARS: Final = 300

#: The Dapr subscription answers: acknowledge the delivery, or ask the sidecar to redeliver it.
SUCCESS: Final = "SUCCESS"
RETRY: Final = "RETRY"


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
    """The door's verdict on an event this lane would queue a task for, counted and logged whenever it is anything but a
    pass.

    Over the event exactly as it arrived, never a re-serialisation: the producer signed the document it published. Off
    answers before any key is read; otherwise the check reads the store through a blocking client, so it runs in the
    threadpool.
    """
    mode = settings.signature_doors
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


def _claimed_uuid(location: str, settings: MaintenanceSettings) -> str | None:
    """The uuid of the table holding ``location``'s claim, for an event that names none; ``None`` when no table does."""
    claims = claim_store(settings)
    held = location_claims.holder(claims, location)
    return held.uuid if held is not None else None


async def handle_arrival(event: dict[str, Any], settings: MaintenanceSettings, queue: TaskQueue | None) -> dict[str, str]:
    """Put the task this event asks for on the work queue (:func:`maintenance.services.arrival.lane_task`), now or held.

    A table's pass is held a delay out, unless one is held already, which covers this event; a drop's release is queued
    now, unless one is queued already. ACKS an event the lane leaves untouched and a location no table's claim holds: none
    of those is retryable. RETRIES only when the bus did not take the task, so a lost publish is asked for again. On a
    stack that runs no worker there is no queue, and the event is acknowledged untouched.

    It checks no signature: the route calls it only for an event its door admitted, and a direct caller verifies nothing.
    """
    lane = lane_task(event.get("data", event))
    if lane is None or queue is None:
        return {"status": SUCCESS}
    uuid = lane.uuid or await run_in_threadpool(_claimed_uuid, lane.location, settings)
    if uuid is None:
        return {"status": SUCCESS}
    task = Task(kind=lane.kind, uuid=uuid, location=lane.location)
    try:
        if task.kind == "table":
            await queue.follow_up(task, datetime.now(UTC) + timedelta(seconds=settings.commit_pass_delay_seconds))
        else:
            await queue.enqueue(task)
    except QueueUnavailableError as exc:
        log.warning("maintenance_arrival_not_queued", extra={"task": task.subject("task"), "error": str(exc)[:_MAX_LOGGED_CHARS]})
        return {"status": RETRY}
    return {"status": SUCCESS}


def register_arrival_route(app: FastAPI, settings: MaintenanceSettings) -> DaprApp | None:
    """Register the write-event subscription and create its door's refusal series at 0, or nothing when the lane is off."""
    if not (settings.arrival_enabled and settings.lineage_topic):
        return None
    create_signature_refused_series()
    wrapper = DaprApp(app)

    @wrapper.subscribe(
        pubsub=settings.lineage_pubsub,
        topic=settings.lineage_topic,
        route="/maintenance-arrival",
        dead_letter_topic=settings.arrival_dlq_topic or None,
    )
    async def on_arrival(
        event: dict[str, Any],
        request: Request,
        *,
        config: SettingsDep,
        queue: TaskQueueDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
    ) -> dict[str, str]:
        """``event`` is typed ``dict`` so FastAPI parses the CloudEvent body — an ``Any`` param becomes
        a query param and 422s. The Dapr app-api-token authenticates the sidecar that delivered it; the
        signature door, when it enforces, is what stops a forged write from naming any URI in any bucket for
        this service to read.

        While draining, ask for REDELIVERY rather than queueing — Dapr's delivery does not consult a readiness
        probe. The same answer while this service's signing key is unresolved, so the lane and the worker come up
        together. Both answer before the event is read; the door reads it, so it comes after them and before anything
        is evaluated.
        """
        if drain is not None:
            return drain
        arrived = event.get("data", event)
        if lane_task(arrived) is not None:
            verdict = await _judge_arrival(arrived, event_id=event.get("id"), request=request, settings=config)
            if verdict.retry:
                return {"status": RETRY}
            if not verdict.act:
                return {"status": SUCCESS}
        return await handle_arrival(event, config, queue)

    return wrapper


__all__ = ["handle_arrival", "register_arrival_route"]
