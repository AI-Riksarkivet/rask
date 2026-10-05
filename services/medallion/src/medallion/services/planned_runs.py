"""What the medallion's two planned lanes share (CP-029): the stage runners' Ray stage runs and the producer's training runs.

Both lanes keep their runs as plans in `service_kit.lakehouse.run_plans`, resolve them through
`service_kit.lakehouse.run_outcomes`, and differ only in what a terminal does (`stage_plans`, `train_plans`). What
sits here is the medallion half of the seam both of them use: the floor a commit marker counts above, the address a
job reports to, the engine a plan is read and stopped through, and the plan's announcement on the control lane.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Final

import lance

from medallion.core.config import MedallionSettings, shared_lance_session
from medallion.core.lineage_publish import signed_control_event
from medallion.services.engine_names import RAY_ENGINE
from medallion.services.engine_registry import executor_for
from service_kit import dapr_publish
from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent
from service_kit.governed.signing_key import SigningKeyUnavailableError
from service_kit.lakehouse.executor import Executor, RunHandle
from service_kit.lakehouse.run_plans import PlanDocument, PlanStore
from service_kit.lancekit.absence import reads_as_absent


log = logging.getLogger(__name__)

#: Sweep ticks an id may go unknown to the engine before it counts as NEVER REGISTERED. The Jobs API writes its
#: record inside the submit call, so only a lost submission stays unknown; at the chart's 30 s tick this is two minutes.
MAX_UNSEEN_TICKS: Final = 4

#: How much of the engine's failure text rides a FAIL event: the message is unbounded upstream (a driver traceback)
#: and the event goes through the claim-check funnel, so the cap belongs here rather than at the broker.
FAIL_MESSAGE_CAP: Final = 800


def destination_version(uri: str, storage_options: dict[str, str]) -> int | None:
    """The destination's version now, ``None`` when it does not exist: the floor above which a run's marker counts."""
    try:
        return int(lance.dataset(uri, storage_options=storage_options, session=shared_lance_session()).version)
    except ValueError as exc:
        # pylance 12 raises ValueError for an absent dataset and for a store that cannot answer alike (measured
        # 2026-10-05), so the estate's absence vocabulary decides: an outage propagates and the delivery is retried.
        if reads_as_absent(exc):
            return None
        raise


def outcome_url(settings: MedallionSettings, action_id: str) -> str:
    """The outcome door a run's job reports to: this service's `/runs/<action id>/outcome`, or "" when unaddressed."""
    base = settings.outcome_url_base.rstrip("/")
    return f"{base}/runs/{action_id}/outcome" if base else ""


def run_handle(plan: PlanDocument) -> RunHandle:
    """The engine's handle for a planned run: the plan's action id IS the submission id."""
    return RunHandle(engine=plan.engine, handle=plan.action_id)


def ray_executor() -> Executor:
    """The Ray engine, resolved BY NAME through the registry ([[LH-159]]); it needs no storage options."""
    return executor_for(RAY_ENGINE, storage_options={})


async def publish_plan(settings: MedallionSettings, dapr: object, store: PlanStore, plan: PlanDocument) -> PlanDocument:
    """Announce ``plan`` on the control lane once per attempt (D-2); answer the plan as stored afterwards.

    The event NAMES the plan (its control-root URI) rather than carrying it: the control topic is claim-check, and the
    order inside a stage plan can hold a 64 KiB provenance document. Its object is `<kind>_run:<action id>`, the
    resource the operator doors name and the key `control_signer_role` reads to know whose signature it needs.
    Best-effort: a plan that did not reach the lane keeps ``published`` false and the sweep announces it on a later
    tick; a service with no control pubsub announces nothing. ``event_id`` is the action id and attempt, the dedupe key
    a redelivery repeats.
    """
    if plan.published or not settings.control_pubsub:
        return plan
    event = CatalogControlEvent(
        event_id=f"run-planned-{plan.action_id}-{plan.attempt}",
        action="run_planned",
        object_type="run",
        object_id=f"{plan.kind.value}_run:{plan.action_id}",
        actor=None,
        extra={
            "kind": plan.kind.value,
            "engine": plan.engine,
            "task": plan.task,
            "stage": plan.stage,
            "attempt": plan.attempt,
            "project": plan.project,
            "plan": store.plan_uri(plan.action_id),
            "report_url": plan.report_url,
        },
    )
    try:
        payload = signed_control_event(settings, json.loads(event.model_dump_json()))
    except SigningKeyUnavailableError:
        log.warning("medallion_plan_unsigned", extra={"kind": plan.kind.value, "action_id": plan.action_id})
        return plan
    landed = await dapr_publish.publish_json(
        dapr,
        pubsub_name=settings.control_pubsub,
        topic_name=CONTROL_TOPIC,
        payload=payload,
        timeout_seconds=settings.publish_timeout_seconds,
        failure_event=f"medallion_{plan.kind.value}_plan_publish_failed",
        context={"action_id": plan.action_id},
    )
    if not landed:
        return plan
    attempt = plan.attempt

    def mark(current: PlanDocument) -> PlanDocument:
        return current.model_copy(update={"published": True}) if current.attempt == attempt else current

    return await asyncio.to_thread(store.update, plan.action_id, mark)
