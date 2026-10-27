"""Dapr pub/sub subscription wiring for durable catalog→lineage ingest (#25).

The catalog publishes OpenLineage events to the Dapr ``pubsub.jetstream`` component; the sidecar
delivers each one to this service over HTTP. ``DaprApp(app)`` also serves GET ``/dapr/subscribe`` (the
registration the sidecar reads at startup); it is wired unconditionally — harmless without a sidecar —
while the actual subscription is registered only when Dapr ingest is enabled, so an HTTP-only deployment
carries no always-live ingest route.

The thin ``main`` builds ``app`` first, then calls :func:`register_dapr` so the subscription is wired
after ``app`` exists (the import-order constraint the split must preserve).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict

from lineage.api.fga_deps import enforce_bus_authz
from lineage.core.config import get_settings
from lineage.core.metrics import Door, Outcome, create_signature_refused_series, record_outcome
from lineage.models import DatasetEvent, RunEvent, author_sub_from_payload, run_id_from_payload
from lineage.services.consumer import handle_cloud_event
from lineage.services.provenance import PARKED_KEY_PREFIX, delivery_feed_key
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)


async def on_lineage_event(event: dict[str, Any], request: Request, _: Annotated[None, Depends(require_dapr_token)]) -> dict[str, str]:
    """Record one Dapr-delivered OpenLineage CloudEvent in the feed; returns the Dapr ack status.

    TWO GUARDS, ANSWERING TWO DIFFERENT QUESTIONS (§ E2).

    ``require_dapr_token`` answers *may this TRANSPORT deliver* — without it the route was an
    unauthenticated, publicly-reachable ingest path co-mounted on the query app, and a forged POST could
    self-assert any ``author`` and inject fabricated provenance even with OIDC/FGA on (the security-audit
    prod-blocker).

    ``enforce_bus_authz`` answers *may the stamped subject record THIS* — which the token cannot,
    because it is shared by every producer that holds it. Its absence was the asymmetry E2 names: the
    HTTP twin applies ``enforce_author`` and ``enforce_output_authz`` and this door applied neither, so
    one shared credential authorized any provenance about any dataset — including a ``drop_table``
    operation on a table the producer had never seen.

    Passed as a callback rather than a dependency because the subject is INSIDE the payload: there is no
    principal to resolve before the body is parsed, and the parse is the consumer's (it owns the
    malformed-payload ack contract)."""

    async def authorize(parsed: RunEvent | DatasetEvent, arrived: Mapping[str, Any]) -> None:
        await enforce_bus_authz(parsed, request, get_settings(), arrived)

    return await handle_cloud_event(request.app.state.provenance, event, authorize, door=Door.SUBSCRIBER)


async def _feed_already_holds(request: Request, event: Mapping[str, Any]) -> bool:
    """Does the feed already hold the row this delivery writes? Anything short of a clear YES is ``False``.

    The delivery's own row, never its run's: a run's START reaching the feed says nothing of its COMPLETE, the event
    that names its outputs and so the one a missing grant refuses ([[LH-148]]).

    Every fallback leans toward reporting LOSS. A parking route that cannot ask must not answer
    "nothing was lost", and an unreachable feed is the moment the signal matters most; a false loss
    costs an operator one lookup, a false all-clear costs them the event.

    The exception is deliberately broad and must stay that way: this route's only job is to ACK. A
    raise here becomes a 500, the sidecar retries the DLQ delivery, and the retry parks again — the
    parking route would manufacture the very inflation it is being taught to avoid.
    """
    key = delivery_feed_key(event)
    provenance = getattr(request.app.state, "provenance", None)
    if key is None or provenance is None:
        return False
    try:
        return key in await provenance.events_recorded({key})
    except Exception as exc:  # noqa: BLE001 — see the docstring: this route must always ack
        log.warning("dapr_dead_letter_feed_unreachable", extra={"event_key": key, "error": str(exc)})
        return False


class _Presented(BaseModel):
    """What one more presentation of a parked delivery did: whether the feed then holds its row, and what it answered."""

    model_config = ConfigDict(frozen=True)

    recovered: bool
    reason: str


async def _reingest_from_the_park(event: dict[str, Any], request: Request, run_id: str | None) -> _Presented:
    """Give a parked delivery ONE more presentation to the ingest path.

    WHY A PARK IS PRESENTED HERE, AND KEPT WHEN IT FAILS. The DLQ stream is `Max Age 7d` / `Discard Old` and this
    service reads no NATS, so a park nothing presents again is lost on a timer — measured on the live estate
    2026-09-22, 74 parked deliveries aged out of the window inside the minutes it took to measure it twice (Messages
    1,659 -> 1,585). What this presentation cannot recover the route keeps (:func:`_keep_the_park`).

    THE SAME FUNCTION THE LIVE SUBSCRIPTION USES, deliberately: `handle_cloud_event` carries the same
    authorization (`enforce_bus_authz` — may the stamped subject record THIS, and where signing is enforced
    does a listed signer's key verify it) and the same idempotence (every row is written under its key).
    So this is a re-PRESENTATION, not a second ingest path with its own policy, and a role-literal
    author is refused here exactly as it was on the bus — which is why this does not pre-empt the open ruling
    on that population. It names its own door, so a refusal here is counted as the parking route's and not as
    a second arrival on the subscription.

    IT PUBLISHES NOTHING. The write goes to the provenance store directly, so a recovered event never lands
    back on `lineage.events.v1`. That is what separates this from the reconcile relay's drain, which
    re-publishes by design and therefore cannot be used here.

    FAIL-QUIET, because the caller is a parking route that must ACK. Any failure leaves the outcome
    exactly as it was before this existed — parked and counted — so the worst case is today's behaviour.
    """
    provenance = getattr(request.app.state, "provenance", None)
    if provenance is None or not run_id:
        return _Presented(recovered=False, reason="no feed to present it to" if provenance is None else "the delivery names no run")
    try:
        answered = await handle_cloud_event(
            provenance, event, lambda parsed, arrived: enforce_bus_authz(parsed, request, get_settings(), arrived), door=Door.DEAD_LETTER
        )
    except Exception as exc:  # noqa: BLE001 — a replay must never turn a park into a 500 or a retry loop
        log.info("dapr_dead_letter_replay_failed", extra={"run_id": run_id, "error": str(exc)})
        return _Presented(recovered=False, reason=f"re-presentation failed: {exc}")
    # ASK THE FEED, never the ack status: `handle_cloud_event` answers SUCCESS for a committed write AND
    # for an unrepairable discard, so only a second read can tell recovery from a shrug.
    return _Presented(recovered=await _feed_already_holds(request, event), reason=f"re-presented at park, answered {answered.get('status')}")


async def _keep_the_park(event: dict[str, Any], request: Request, run_id: str | None, author: str | None, reason: str) -> None:
    """Keep a delivery the park could not recover, so the estate admin can re-present it once its cause is fixed ([[LH-148]]).

    The DLQ stream holds it 168 h and this service reads no NATS, so the provenance namespace's ``refusals`` table holds
    it instead, keyed by its CloudEvent id: a re-park of the same delivery lands on its row. FAIL-QUIET for the reason
    the parking route acks: a failure here leaves the delivery as parked as it was.
    """
    provenance = getattr(request.app.state, "provenance", None)
    event_id = event.get("id")
    if provenance is None or not isinstance(event_id, str) or not event_id:
        return
    try:
        await provenance.record_refusal(outbox_key=f"{PARKED_KEY_PREFIX}{event_id}", run_id=run_id, author=author, reason=reason, event_json=json.dumps(event))
    except Exception as exc:  # noqa: BLE001 — the parking route must always ack
        log.warning("dapr_dead_letter_unkept", extra={"run_id": run_id, "event_id": event_id, "error": str(exc)})


async def on_dead_letter(event: dict[str, Any], request: Request, _: Annotated[None, Depends(require_dapr_token)]) -> dict[str, str]:
    """Park one dead-lettered ingest delivery: log + ack (Dapr-native DLQ, RESILIENCE gap #2).

    No auto-requeue — the delivery already exhausted the sidecar's Resiliency retry schedule. It is presented to the
    ingest path once more here, and a delivery that presentation could not recover is kept in the provenance
    namespace's ``refusals`` table (:func:`_keep_the_park`), where the estate admin re-presents it on demand once its
    cause is fixed (``POST /admin/dlq/parked/{run_id}/replay``, [[LH-148]]), past the DLQ stream's retention.

    SEVERITY IS THE ANSWER, not the event. The same replay that recovers also re-parks — it re-reads up
    to the retention window on every restart and parks whatever still fails — so an unconditional ERROR
    per parking counts restarts rather than losses, and buries the real ones among them. Measured on the
    live estate 2026-09-13: 8,515 parked deliveries of `lineage.events.v1` against a stream whose last
    sequence was 5,860, and 17 of 21 distinct parked run ids already recorded. A feed row is
    written under its key, so a stage that runs again heals its own gap.
    """
    payload = event.get("data") if isinstance(event, dict) else None
    run_id = run_id_from_payload(payload)
    already_recorded = isinstance(event, dict) and await _feed_already_holds(request, event)
    presented = _Presented(recovered=False, reason="") if already_recorded else await _reingest_from_the_park(event, request, run_id)
    recovered = presented.recovered
    if not already_recorded and not recovered and isinstance(event, dict):
        await _keep_the_park(event, request, run_id, author_sub_from_payload(payload), presented.reason)
    log.log(
        logging.WARNING if already_recorded else logging.ERROR,
        "dapr_dead_letter_parked",
        extra={
            "app": "lineage",
            "event_id": event.get("id") if isinstance(event, dict) else None,
            "run_id": run_id,
            # Which of the two this is, on the record rather than inferred from the level — an operator
            # filtering a dashboard needs the field, and the level alone cannot be queried.
            "already_recorded": already_recorded,
            # WHOSE provenance this was. The payload being discarded carries the person it belonged to —
            # naming only the event id let an operator see THAT provenance was dropped and never whose.
            # `None` when the payload carries no verified sub: anonymous beats misattributed (see
            # `author_sub_from_payload`).
            "author": author_sub_from_payload(payload),
        },
    )
    # Without this counter the retries all counted RETRIED and the parking vanished from the metrics
    # (audit 2026-07-15). The split is what makes the number readable: a non-zero `DEAD_LETTERED` means
    # the feed is missing that run, which is the claim an alert on it is making.
    if recovered:
        record_outcome(Outcome.RECOVERED, door=Door.DEAD_LETTER)
    else:
        record_outcome(Outcome.DEAD_LETTERED if not already_recorded else Outcome.PARKED_ALREADY_RECORDED, door=Door.DEAD_LETTER)
    return {"status": "SUCCESS"}


def register_dapr(app: FastAPI) -> None:
    """Wire the Dapr subscription onto ``app``.

    ``DaprApp(app)`` is always constructed (it adds GET ``/dapr/subscribe``). The subscription itself is
    registered ONLY when Dapr ingest is enabled, so an HTTP-only deployment carries no always-live ingest
    route. Combined with the token guard above (a request through the cluster route reaches the app port
    and never carries a sidecar's token) and the pubsub component's publisher scopes, a forged external
    event cannot reach the feed. When ``dapr_dlq_topic`` is set
    (chart: ``dapr.resiliency.enabled``), the subscription declares a Dapr ``deadLetterTopic`` and the
    parking route is registered — exhausted deliveries become visible instead of vanishing.

    It also creates every reason series of the signature refusal counter at 0, which the doors that verify a bus or
    staged event share (`create_signature_refused_series`)."""
    settings = get_settings()
    create_signature_refused_series()
    dapr_app = DaprApp(app)
    if settings.dapr_enabled:
        dapr_app.subscribe(
            pubsub=settings.dapr_pubsub,
            topic=settings.dapr_topic,
            route="/lineage-events",
            dead_letter_topic=settings.dapr_dlq_topic or None,
        )(on_lineage_event)
        if settings.dapr_dlq_topic:
            # The parking route rides its OWN durable component (deliverPolicy=new), never the main
            # ingest component: that one is deliverPolicy=all so a replay can rebuild the feed, and
            # riding it would re-park up to 168h of already-parked dead letters on a replay, spiking
            # the terminal-loss metric with no new loss. The fallback keeps a dev stack without the
            # component working.
            dapr_app.subscribe(
                pubsub=settings.dapr_dlq_pubsub or settings.dapr_pubsub,
                topic=settings.dapr_dlq_topic,
                route="/lineage-dlq",
            )(on_dead_letter)
