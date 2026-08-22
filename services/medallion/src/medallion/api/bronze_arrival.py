"""medallion-producer's Dapr pub/sub subscription route (``/bronze-arrival``) — the event-driven cascade head.

The :class:`DaprApp` wrapper serves ``GET /dapr/subscribe`` (read by the sidecar at startup) and routes
deliveries of the shared lineage topic to :func:`handle_bronze_arrival`, which fires the cascade only for a
write to the bronze dataset — the arrival of external raw INTO the first governed tier (R23) —
loop-guarded. Authenticated by the Dapr app-api-token (``require_dapr_token``) so a forged event can't
drive the pipeline — symmetric with the movers' ``/medallion-event`` route.
"""

from __future__ import annotations

from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI

from medallion.api.dependencies import DaprClientDep, SettingsDep
from medallion.api.dlq import register_dlq_route
from medallion.core.config import get_settings
from medallion.services.ingest_trigger import handle_bronze_arrival
from medallion.services.publication_trigger import handle_publication
from service_kit.draining import retry_when_draining
from service_kit.governed.dapr_auth import require_dapr_token


def register_bronze_arrival_route(app: FastAPI) -> DaprApp:
    """Wrap ``app`` in a :class:`DaprApp` and register the bronze-arrival subscription (the cascade head).

    Registers the producer's ONE DLQ parking route here (train reuses this ``DaprApp`` — a second
    registration would duplicate ``/dlq-event``); both producer subscriptions declare the same
    ``deadLetterTopic`` when configured, so an exhausted head/train trigger parks visibly.
    """
    settings = get_settings()
    dapr_app = DaprApp(app)
    if settings.dlq_topic:
        register_dlq_route(dapr_app, pubsub=settings.pubsub, dlq_topic=settings.dlq_topic, app_label="producer")

    @dapr_app.subscribe(
        pubsub=settings.pubsub,
        topic=settings.lineage_topic,
        route="/bronze-arrival",
        dead_letter_topic=settings.dlq_topic or None,
    )
    async def on_bronze_arrival(
        event: dict[str, Any],
        dapr: DaprClientDep,
        config: SettingsDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
    ) -> dict[str, str]:
        """The Dapr subscription route — thin wrapper over the testable :func:`handle_bronze_arrival`.
        ``event`` is typed ``dict`` so FastAPI parses the CloudEvent JSON body (an ``Any`` param → query
        param → 422). Authenticated by the Dapr app-api-token so a forged bronze-arrival event can't drive
        the cascade.

        B6: while this replica is draining it asks for REDELIVERY rather than handling the event. Dapr's
        delivery does not consult a readiness probe, so without this a pod that had begun shutting down
        kept firing cascades it could not finish. RETRY and never DROP — these topics carry no DLQ, so a
        drop here silently cancels the whole bronze→silver→gold run."""
        if drain is not None:
            return drain
        return await handle_bronze_arrival(dapr, config, event)

    # THE PUBLICATION HEAD (§ D2 B8). Separate subscription, separate topic, separate signal: this one
    # fires on the catalog's `table_published` — the moment the quality gate passed a version and the
    # `published` tag moved — and carries the {from_version, to_version} range onto the stage trigger.
    #
    # IT DOES NOT REPLACE `/bronze-arrival`, AND RETIRING EITHER HEAD IS NOT THE FIX. This comment
    # said the opposite until 2026-08-22 — "the real fix is retiring one head" — and that was ruled
    # against: `docs/architecture/medallion-cascade.md` § "the two cascade heads are distinct events,
    # and both must fire". The two triggers do not describe the same work. This one fires on a table
    # being PUBLISHED and carries a {from_version, to_version} RANGE; `/bronze-arrival` fires on a
    # bronze WRITE reaching COMPLETE, names the dataset actually written, and has no concept of a
    # range. Unifying their tokens would collide two legitimate cascades onto one deterministic
    # instance_id, and Dapr would answer the second schedule as a duplicate — silently dropping one
    # of two pieces of work that must both happen.
    #
    # The two heads mint incompatible tokens by design (this one from the control event's `event_id`,
    # the other from the bronze-write run's `lance.token` facet), so the deterministic-instance dedupe
    # never engages between them, and there is no token de-duplication in the movers either — a
    # comment here claimed one until 2026-08-08; `transform.py` only reads the token into logs and
    # lineage run-ids. That is the intended shape: the token distinguishes EVENTS, while
    # `stage_submission_id` distinguishes WORK, and merging the two questions is the defect.
    #
    # A table that genuinely emits both signals for the same dataset does pay duplicate compute, which
    # is survivable rather than correct-by-luck: the stage write is overwrite-convergent (the
    # single-flight `_write_lock` plus deterministic content make the second pass a same-bytes
    # overwrite). What WOULD be a real duplicate is a future head publishing a trigger whose dataset
    # AND range match another's — and that one the instance_id correctly dedupes on its own.
    #
    # Do NOT "restore" dedupe by scoping movers to the state store: an adversarial review found the
    # key is not unique per legitimate message on a mover's own topic, so deploying it halts every
    # distributed cascade.
    if settings.control_pubsub:

        @dapr_app.subscribe(
            pubsub=settings.control_pubsub,
            topic=settings.control_topic,
            route="/publication-arrival",
            dead_letter_topic=settings.dlq_topic or None,
        )
        async def on_publication(
            event: dict[str, Any],
            dapr: DaprClientDep,
            config: SettingsDep,
            _: Annotated[None, Depends(require_dapr_token)],
        ) -> dict[str, str]:
            """A publication became consumable — wake the cascade for exactly the rows it added."""
            return await handle_publication(dapr, config, event)

    return dapr_app
