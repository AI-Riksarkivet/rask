"""The bus ingress — `lineage.events.v1`, delivered by this app's own sidecar.

`DaprApp(app)` is constructed from `main` AFTER `app` exists, which is not a style preference: a
subscription is a route, and there is no app to hang it on before one is built. It always serves
`GET /dapr/subscribe` — the registration the sidecar reads at startup — so the wiring is inspectable
even in a deployment with no broker behind it.

**Three properties of this subscription that live in the chart, named here so they are not lost:**

* `deliverPolicy: new` + a durable name. Non-negotiable for this consumer: lineage's own
  `deliverPolicy: all` plus an ephemeral consumer replays the retained backlog on every pod restart,
  which for a graph rebuild is the durability story and for an INBOX would re-notify a week of history
  on every rollout.
* Its own per-app pubsub component. `queueGroupName` lives on the component, and lineage consumes the
  same topic — one shared queue group would split those messages across the two apps.
* Registration in the resiliency CRD before any dead-letter topic is configured. See
  `IngressSettings.dlq_topic`: a `deadLetterTopic` with no retry policy behind it parks on the FIRST
  failure, which is worse than having neither.

The token dependency proves **"arrived via Dapr", never "trusted caller"** — the gateway itself
invokes through Dapr, so a request can carry a valid `dapr-api-token` and still be an anonymous
stranger. It is not an authorization decision, and none is needed here: this handler writes into
inboxes it derives itself, and every recipient's visibility is re-checked before a pointer is written.
"""

from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI

from notifications.api.control_events import ingest_control_event
from notifications.api.dlq import register_dlq_route
from notifications.api.ingest import ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.security import VisibilityDep
from notifications.api.settings import get_ingress_settings
from notifications.proxies import inbox_for, watchers_of
from service_kit.governed.dapr_auth import assert_app_token_configured, require_dapr_token


def register_subscriptions(app: FastAPI) -> None:
    """Wire this service's Dapr-delivered routes onto `app`."""
    settings = get_ingress_settings()
    # Fail closed at build time: with Dapr ingest on, this route is live and MUST be authenticated, so
    # an unset APP_API_TOKEN is a misconfiguration rather than the dev default. Refusing to start is
    # the only answer that cannot be missed — the alternative is an unauthenticated ingest path that
    # looks configured.
    assert_app_token_configured(dapr_enabled=settings.dapr_enabled)
    dapr_app = DaprApp(app)
    if settings.dlq_topic:
        register_dlq_route(dapr_app, pubsub=settings.pubsub, dlq_topic=settings.dlq_topic, app_label=settings.dlq_topic.removeprefix("dlq."))

    @dapr_app.subscribe(
        pubsub=settings.pubsub,
        topic=settings.lineage_topic,
        route="/lineage-events",
        dead_letter_topic=settings.dlq_topic or None,
    )
    async def on_lineage_event(
        event: dict[str, Any],
        visibility: VisibilityDep,
        _: Annotated[None, Depends(require_dapr_token)],
    ) -> dict[str, str]:
        """Ingest one Dapr-delivered CloudEvent.

        `event` is typed `dict[str, Any]` and not `Any`: FastAPI reads a bare `Any` parameter as a
        QUERY param, so every delivery would answer 422 and the subscription would never handle a
        single message. `body["data"]` is the OpenLineage event — Dapr parses it because the publisher
        sends `datacontenttype=application/json`.
        """
        return await ingest_run_event(event.get("data"), lane=Lane.BUS, visibility=visibility, open_inbox=inbox_for, watchers=watchers_of)

    @dapr_app.subscribe(
        pubsub=settings.control_pubsub,
        topic=settings.control_topic,
        route="/control-events",
        dead_letter_topic=settings.dlq_topic or None,
    )
    async def on_control_event(
        event: dict[str, Any],
        _: Annotated[None, Depends(require_dapr_token)],
    ) -> dict[str, str]:
        """v3 targeting: a governance act that NAMED someone.

        No `VisibilityDep`, and that absence is the design rather than an omission — see
        `control_events`. Being named IS the targeting, and after a `grant_revoked` the subject can no
        longer see the object, so a visibility check would drop the one event they most need.
        """
        return await ingest_control_event(event.get("data"), open_inbox=inbox_for)
