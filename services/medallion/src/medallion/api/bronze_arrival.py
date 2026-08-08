"""lance-ray's Dapr pub/sub subscription route (``/bronze-arrival``) — the event-driven cascade head.

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
    ) -> dict[str, str]:
        """The Dapr subscription route — thin wrapper over the testable :func:`handle_bronze_arrival`.
        ``event`` is typed ``dict`` so FastAPI parses the CloudEvent JSON body (an ``Any`` param → query
        param → 422). Authenticated by the Dapr app-api-token so a forged bronze-arrival event can't drive
        the cascade."""
        return await handle_bronze_arrival(dapr, config, event)

    # THE PUBLICATION HEAD (§ D2 B8). Separate subscription, separate topic, separate signal: this one
    # fires on the catalog's `table_published` — the moment the quality gate passed a version and the
    # `published` tag moved — and carries the {from_version, to_version} range onto the stage trigger.
    #
    # It does not replace `/bronze-arrival` in this commit. Both heads publish the same
    # `medallion.bronze` trigger, and a table that emits BOTH signals CASCADES TWICE — there is no
    # token de-duplication in the movers (a comment here claimed one until 2026-08-08; transform.py
    # only reads the token into logs/lineage run-ids, and the two heads mint incompatible tokens
    # anyway — open_dapr.md §2.12). Accepted because the stage write is overwrite-convergent (the
    # single-flight `_write_lock` plus deterministic content make the second pass a same-bytes
    # overwrite), at the cost of duplicate compute and duplicate lineage Runs per hop. The real fix
    # is retiring one head so the `published` tag is the single trigger — open_ingest_design.md §4;
    # do NOT "restore" dedupe by scoping movers to the state store without reading that first.
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
