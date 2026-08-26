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

from typing import TYPE_CHECKING, Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, Request

from notifications.api.control_events import UsersetExpander, ingest_control_event
from notifications.api.dlq import register_dlq_route
from notifications.api.ingest import ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.security import VisibilityDep
from notifications.api.settings import get_ingress_settings
from notifications.config import get_notifications_settings
from notifications.proxies import channel_push, inbox_for, watchers_of
from service_kit.governed import fga


if TYPE_CHECKING:  # no runtime cost, and it breaks no import cycle
    from openfga_sdk import OpenFgaClient
from service_kit.draining import retry_when_draining
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
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
    ) -> dict[str, str]:
        """Ingest one Dapr-delivered CloudEvent.

        `event` is typed `dict[str, Any]` and not `Any`: FastAPI reads a bare `Any` parameter as a
        QUERY param, so every delivery would answer 422 and the subscription would never handle a
        single message. `body["data"]` is the OpenLineage event — Dapr parses it because the publisher
        sends `datacontenttype=application/json`.
        """
        if drain is not None:
            return drain
        return await ingest_run_event(event.get("data"), lane=Lane.BUS, visibility=visibility, open_inbox=inbox_for, watchers=watchers_of, push=channel_push())

    def _make_expander(client: "OpenFgaClient | None") -> UsersetExpander | None:
        """Resolve `role:reviewers#assignee` to the people holding that relation, via the SAME
        `list_users` primitive the access review uses.

        `None` when authorization is off or unwired — the lane then names nobody for a userset rather
        than inventing an audience. Raises on an FGA outage rather than answering empty: the caller
        turns that into a RETRY, because an empty answer would read as "this group has no members" and
        silently drop everyone's notification.
        """
        # `get_notifications_settings()`, NOT the ingress settings closed over above: the FGA flag
        # lives on the service settings and `IngressSettings` has never carried one. Reading it off the
        # wrong object raised AttributeError on the FIRST control event with a userset — invisible to
        # the unit tests, which inject an expander directly and never build one.
        if client is None or not get_notifications_settings().fga_enabled:
            return None

        async def expand(userset: str) -> tuple[str, ...]:
            obj, _, relation = userset.partition("#")
            if not obj or not relation:
                return ()
            return tuple(await fga.list_users(client, relation=relation, obj=obj))

        return expand

    @dapr_app.subscribe(
        pubsub=settings.control_pubsub,
        topic=settings.control_topic,
        route="/control-events",
        dead_letter_topic=settings.dlq_topic or None,
    )
    async def on_control_event(
        event: dict[str, Any],
        request: Request,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
    ) -> dict[str, str]:
        """v3 targeting: a governance act that NAMED someone.

        No `VisibilityDep`, and that absence is the design rather than an omission — see
        `control_events`. Being named IS the targeting, and after a `grant_revoked` the subject can no
        longer see the object, so a visibility check would drop the one event they most need.

        It DOES take an FGA client, and that is a different question from a visibility check: a grant
        may name a GROUP (`role:reviewers#assignee`), and expanding it is how the people in it get
        told at all. Absent a client the expander is `None`, which resolves a userset to no audience —
        quiet, never a phantom inbox keyed on the group string.
        """
        if drain is not None:
            return drain
        return await ingest_control_event(
            event.get("data"),
            open_inbox=inbox_for,
            expand=_make_expander(getattr(request.app.state, "fga", None)),
        )
