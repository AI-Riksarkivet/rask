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

import logging
from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, Request

from lineage.api.fga_deps import enforce_bus_authz
from lineage.core.config import get_settings
from lineage.core.metrics import Outcome, record_outcome
from lineage.models import RunEvent, author_sub_from_payload, run_id_from_payload
from lineage.services.consumer import handle_cloud_event
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)


async def on_lineage_event(event: dict[str, Any], request: Request, _: Annotated[None, Depends(require_dapr_token)]) -> dict[str, str]:
    """Ingest one Dapr-delivered OpenLineage CloudEvent into the graph; returns the Dapr ack status.

    TWO GUARDS, ANSWERING TWO DIFFERENT QUESTIONS (§ E2).

    ``require_dapr_token`` answers *may this TRANSPORT deliver* — without it the route was an
    unauthenticated, publicly-reachable ingest path co-mounted on the query app, and a forged POST could
    self-assert any ``author`` and inject fabricated nodes/edges into the authoritative AGE graph even
    with OIDC/FGA on (the security-audit prod-blocker).

    ``enforce_bus_authz`` answers *may the stamped subject record THIS* — which the token cannot,
    because it is shared by every producer that holds it. Its absence was the asymmetry E2 names: the
    HTTP twin applies ``enforce_author`` and ``enforce_output_authz`` and this door applied neither, so
    one shared credential authorized any provenance about any dataset — including a ``drop_table``
    operation on a table the producer had never seen, which the reconcile sweep then honours.

    Passed as a callback rather than a dependency because the subject is INSIDE the payload: there is no
    principal to resolve before the body is parsed, and the parse is the consumer's (it owns the
    malformed-payload ack contract)."""

    async def authorize(parsed: RunEvent) -> None:
        await enforce_bus_authz(parsed, request, get_settings())

    return await handle_cloud_event(request.app.state.repository, event, authorize)


async def _graph_already_holds(request: Request, run_id: str | None) -> bool:
    """Does the graph already have this run? Anything short of a clear YES is ``False``.

    Every fallback leans toward reporting LOSS. A parking route that cannot ask must not answer
    "nothing was lost", and an unreachable graph is the moment the signal matters most; a false loss
    costs an operator one lookup, a false all-clear costs them the event.

    The exception is deliberately broad and must stay that way: this route's only job is to ACK. A
    raise here becomes a 500, the sidecar retries the DLQ delivery, and the retry parks again — the
    parking route would manufacture the very inflation it is being taught to avoid.
    """
    if run_id is None:
        return False
    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        return False
    try:
        return await repository.run_status(run_id) is not None
    except Exception as exc:  # noqa: BLE001 — see the docstring: this route must always ack
        log.warning("dapr_dead_letter_graph_unreachable", extra={"run_id": run_id, "error": str(exc)})
        return False


async def on_dead_letter(event: dict[str, Any], request: Request, _: Annotated[None, Depends(require_dapr_token)]) -> dict[str, str]:
    """Park one dead-lettered ingest delivery: log + ack (Dapr-native DLQ, RESILIENCE gap #2).

    No auto-requeue — the delivery already exhausted the sidecar's Resiliency retry schedule, and
    lineage's recovery story stays replay-from-stream (the ephemeral deliverPolicy=all consumer re-reads
    the retained stream on restart); the DLQ adds operator VISIBILITY, not a second path. That bounds
    what recovery can reach: a dead letter older than the stream's retention has no path back, because
    nothing re-ingests the DLQ stream itself.

    SEVERITY IS THE ANSWER, not the event. The same replay that recovers also re-parks — it re-reads up
    to the retention window on every restart and parks whatever still fails — so an unconditional ERROR
    per parking counts restarts rather than losses, and buries the real ones among them. Measured on the
    live estate 2026-09-13: 8,515 parked deliveries of `lineage.events.v1` against a stream whose last
    sequence was 5,860, and 17 of 21 distinct parked run ids already present in the graph. Run ids are
    deterministic and `ingest_event` MERGEs on them, so a stage that runs again heals its own gap.
    """
    payload = event.get("data") if isinstance(event, dict) else None
    run_id = run_id_from_payload(payload)
    already_recorded = await _graph_already_holds(request, run_id)
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
    # the graph is missing that run, which is the claim an alert on it is making.
    record_outcome(Outcome.DEAD_LETTERED if not already_recorded else Outcome.PARKED_ALREADY_RECORDED)
    return {"status": "SUCCESS"}


def register_dapr(app: FastAPI) -> None:
    """Wire the Dapr subscription onto ``app``.

    ``DaprApp(app)`` is always constructed (it adds GET ``/dapr/subscribe``). The subscription itself is
    registered ONLY when Dapr ingest is enabled, so an HTTP-only deployment carries no always-live ingest
    route. Combined with the token guard above, the pubsub component's publisher scopes, and the gateway
    blocking this route, a forged external event cannot reach the graph. When ``dapr_dlq_topic`` is set
    (chart: ``dapr.resiliency.enabled``), the subscription declares a Dapr ``deadLetterTopic`` and the
    parking route is registered — exhausted deliveries become visible instead of vanishing."""
    settings = get_settings()
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
            # ingest component: that one is deliverPolicy=all + ephemeral so replay can rebuild the
            # graph — semantics that made this route re-park up to 168h of already-parked dead
            # letters on every pod restart, spiking the terminal-loss metric with no new loss
            # . The fallback keeps a dev stack without the component working.
            dapr_app.subscribe(
                pubsub=settings.dapr_dlq_pubsub or settings.dapr_pubsub,
                topic=settings.dapr_dlq_topic,
                route="/lineage-dlq",
            )(on_dead_letter)
