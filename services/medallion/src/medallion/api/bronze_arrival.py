"""medallion-producer's Dapr pub/sub subscription routes — the event-driven cascade heads.

The :class:`DaprApp` wrapper serves ``GET /dapr/subscribe`` (read by the sidecar at startup) and routes
deliveries of the shared lineage topic to ``/bronze-arrival``, which fires the cascade only for a
write to the bronze dataset — the arrival of external raw INTO the first governed tier (R23) —
loop-guarded, and deliveries of the catalog's control topic to ``/publication-arrival``.

TWO CHECKS STAND BETWEEN A DELIVERY AND THE CASCADE IT WAKES. The Dapr app-api-token (``require_dapr_token``)
proves the delivery came through this pod's sidecar, so nothing that merely reaches the port can post one, as on
the stage runners' ``/medallion-event``. It cannot say who wrote the event: any identity the bus lets publish on
the topic can put one there. So a head acts only on an event whose ``rask_signature`` a signer its door allows has
made, in the mode the chart sets ([[XC-078]], `medallion.api.signature_door`).
"""

from __future__ import annotations

from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool

from medallion.api import signature_door
from medallion.api.dependencies import DaprClientDep, SettingsDep
from medallion.api.dlq import register_dlq_route
from medallion.core.config import get_settings
from medallion.services.ingest_trigger import bronze_arrival_of, fire_bronze_arrival
from medallion.services.publication_trigger import PUBLISHED_ACTION, fire_publication, publication_arrival_of
from service_kit.draining import retry_when_draining
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.governed.signing_key import retry_until_signed


_SUCCESS = {"status": "SUCCESS"}


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
        request: Request,
        dapr: DaprClientDep,
        config: SettingsDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
        signing: Annotated[dict[str, str] | None, Depends(retry_until_signed)] = None,
    ) -> dict[str, str]:
        """The Dapr subscription route over the head's two halves in `medallion.services.ingest_trigger`.
        ``event`` is typed ``dict`` so FastAPI parses the CloudEvent JSON body (an ``Any`` param → query
        param → 422).

        B6: while this replica is draining it asks for REDELIVERY rather than handling the event. Dapr's
        delivery does not consult a readiness probe, so without this a pod that had begun shutting down
        would fire cascades it could not finish. RETRY and never DROP: a DROP is acknowledged and only
        parked on the dead-letter topic, so the bronze→silver→gold run it should start never happens.

        The same answer while this producer's signing key is unresolved: the cascade this head wakes is signed
        from its first event, and a producer that cannot sign is not yet one that may start it.

        Then the event's own signature, and only for an event the head would act on: one it ignores is
        acknowledged unverified, and one it would act on fires the cascade only when the door admits it."""
        if drain is not None:
            return drain
        if signing is not None:
            return signing
        # In a worker thread: deciding may list the declared lanes, a blocking read of the control root, and a read held
        # on the event loop stalls every other delivery and the liveness probe with it.
        arrival = await run_in_threadpool(bronze_arrival_of, event, config)
        if arrival is None:
            return _SUCCESS  # not a bronze write: ack so Dapr doesn't redeliver, but drive nothing
        withheld = await signature_door.withhold_lineage_event(request, config, door="bronze-arrival", arrived=arrival.event)
        if withheld is not None:
            return withheld
        return await fire_bronze_arrival(dapr, config, arrival)

    # Each head's refusal series exist before its first refusal ([[XC-078]]), created where the head is registered: the
    # chart runs this app under `opentelemetry-instrument`, which installs the MeterProvider before the app is imported.
    signature_door.start_refusal_series("bronze-arrival")

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
    # never engages between them, and there is no token de-duplication in the stage runners either — a
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
    # Do NOT "restore" dedupe by scoping stage runners to the state store: an adversarial review found the
    # key is not unique per legitimate message on a stage runner's own topic, so deploying it halts every
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
            request: Request,
            dapr: DaprClientDep,
            config: SettingsDep,
            _: Annotated[None, Depends(require_dapr_token)],
            drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
            signing: Annotated[dict[str, str] | None, Depends(retry_until_signed)] = None,
        ) -> dict[str, str]:
            """A publication became consumable — wake the cascade for exactly the rows it added.

            Behind the same two gates as `/bronze-arrival`, and then the catalog's signature on the
            `table_published` the head would act on."""
            if drain is not None:
                return drain
            if signing is not None:
                return signing
            arrival = publication_arrival_of(event, config)
            if arrival is None:
                return _SUCCESS  # not a publication of a declared lane: ack, drive nothing
            withheld = await signature_door.withhold_control_event(
                request, config, door="publication-arrival", arrived=arrival.event, action=PUBLISHED_ACTION, object_id=arrival.object_id
            )
            if withheld is not None:
                return withheld
            return await fire_publication(dapr, config, arrival)

        signature_door.start_refusal_series("publication-arrival")

    return dapr_app
