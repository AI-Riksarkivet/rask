"""The maintenance service's write-event subscription: after a commit, the written table's due record and its expiry.

Subscribes to `lineage.events.v1`, the one lane every governed writer converges on, with a queue group so replicas
compete rather than each evaluating the same table. On a write it re-evaluates that one table's due record and, once
enough of its versions are reclaimable, expires them (:func:`maintenance.services.arrival.act_on_write`), emitting the
same `expire_versions` lineage run the sweep does. Registered when `MAINTENANCE_ARRIVAL_ENABLED` is on and a lineage
topic is set.

A WRITE IS ITS PUBLISHER'S CLAIM UNTIL ITS SIGNATURE IS CHECKED ([[XC-078]], owner ruling R4). The Dapr app token
proves that this pod's own sidecar delivered an event, not who published it, and the location a write names is what
this service reads. So before evaluating, the door checks the write's `rask_signature` against the keys its signer
publishes, with the signer sets lineage verifies with (`RASK_EVENT_SIGNERS`, `RASK_EVENT_DELEGATORS`), and
`RASK_SIGNATURE_DOORS` decides what it does with the verdict (`lineage_kit.door`): off checks nothing, observe evaluates
as before and counts what enforcing would refuse, and enforce evaluates only a write a listed signer's key verifies. A
refusal is acknowledged and evaluates nothing, since no redelivery can sign bytes already published; a key list that
cannot be read is retried, since it says nothing about the signature. Only a write the lane would evaluate is checked
(`plannable_write`), so an event it acknowledges untouched costs no key read.
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
from maintenance.api.dependencies import LineageEmitterDep, SettingsDep
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import MaintenanceEmitter
from maintenance.core.metrics import SignatureDoor, create_signature_refused_series, record_signature_refused, record_signature_would_refuse
from maintenance.services.arrival import act_on_write, plannable_write, should_replan
from maintenance.services.sweep import emit_sweep_lineage
from service_kit.draining import retry_when_draining
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.governed.signing_key import published_key_fetch, retry_until_signed
from service_kit.lakehouse import maintenance_policies


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


async def handle_arrival(event: dict[str, Any], settings: MaintenanceSettings, emitter: MaintenanceEmitter) -> dict[str, str]:
    """Refresh the due record of the table this write names and expire its versions, when enough have accumulated.

    A compaction's commits and an index build or optimize skip the debounce: the refresh after one is what takes the
    table off the due list.

    ACKS a byte-free operation, an event carrying no physical URI, a debounced write and a table no manifest names:
    none of those is retryable, and the sweep re-evaluates anything this declines. RETRIES only when the evaluation
    could not decide (a manifest, the policy registry or the dataset unreadable), and stamps the debounce only after a
    decision, so a failed refresh is not skipped by the next event. The debounce is keyed by the table uuid the event
    names ([[LH-523]]), so a table registered later at the same location starts unstamped; an event naming none is
    evaluated every time.

    It checks no signature: the route calls it only for a write its door admitted, and a direct caller verifies nothing.
    """
    write = plannable_write(event.get("data", event))
    if write is None:
        return {"status": SUCCESS}
    options = settings.storage_options()
    last = (
        await run_in_threadpool(maintenance_policies.read_planned_version, settings.resolved_policy_root, options, write.table_uuid)
        if write.table_uuid is not None
        else None
    )
    if not write.refreshes_regardless() and not should_replan(last_planned=last, event_version=write.version, min_versions=settings.event_min_versions):
        return {"status": SUCCESS}
    acted = await run_in_threadpool(act_on_write, write.location, settings)
    if acted.verdict == "failed":
        return {"status": RETRY}
    if acted.expired is not None:
        await emit_sweep_lineage(emitter, [acted.expired], delimiter=settings.delimiter)
    if write.version is not None and write.table_uuid is not None:
        await run_in_threadpool(maintenance_policies.write_planned_version, settings.resolved_policy_root, options, write.table_uuid, write.version)
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
        emitter: LineageEmitterDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
        signing: Annotated[dict[str, str] | None, Depends(retry_until_signed)] = None,
    ) -> dict[str, str]:
        """``event`` is typed ``dict`` so FastAPI parses the CloudEvent body — an ``Any`` param becomes
        a query param and 422s. The Dapr app-api-token authenticates the sidecar that delivered it; the
        signature door, when it enforces, is what stops a forged write from naming any URI in any bucket for
        this service to read.

        While draining, ask for REDELIVERY rather than evaluating — Dapr's delivery does not consult a readiness
        probe. The same answer while this service's signing key is unresolved, so the lane and the sweep come up
        together. Both answer before the event is read; the door reads it, so it comes after them and before anything
        is evaluated.
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
        return await handle_arrival(event, config, emitter)

    return wrapper


__all__ = ["handle_arrival", "register_arrival_route"]
