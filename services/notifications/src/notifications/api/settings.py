"""What the ingress is WIRED TO — component, topic and feed coordinates the chart renders.

Separate from `notifications.config` by OWNER rather than by taste. `NotificationsSettings` holds what
an inbox IS (retention, cap, page size) and the actor reads it with no sidecar in sight; this holds
what the SURFACE talks to — a pubsub component the chart mints, a topic whose name is the
compatibility unit, a lineage base URL and the service-door credential. Nothing in the actor plane
reads a field here, and nothing here means anything without a deployment behind it.

Every one is a setting rather than a literal for the reason the estate keeps paying for: the
per-subscriber component name is `<pubsub.name>-<app-id>`, rendered by `lance.subPubsub`
(`chart/templates/_helpers.tpl:516-519`), and the app's own `*_PUBSUB` env must agree with it — a
literal on this side and a rendered name on that side are two places that drift into a subscription
nothing is ever delivered to.
"""

from functools import lru_cache
from typing import Self

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngressSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    #: The per-subscriber pubsub component. One component per subscriber app-id is not a style choice:
    #: `queueGroupName` lives on the component, and lineage and this service both consume
    #: `lineage.events.v1` — one shared queue group would SPLIT those messages across the two apps
    #: instead of delivering to each.
    pubsub: str = Field(default="lineage-pubsub-notifications", alias="RASK_NOTIFICATIONS_PUBSUB")

    #: The catalog's control-plane pubsub component — v3 targeting's ingress. Its OWN per-subscriber
    #: component, cloned from the `lance-ray` precedent (own queue group + durable), never a new scope
    #: on the BROADCAST component: that one's whole point is every-replica delivery, and a competing
    #: consumer added to it would split the broadcast instead of joining it.
    control_pubsub: str = Field(default="catalog-control-pubsub-notifications", alias="RASK_NOTIFICATIONS_CONTROL_PUBSUB")

    #: The governance topic. The `.v1` is the compatibility unit, like its lineage sibling.
    control_topic: str = Field(default="catalog.control.v1", alias="RASK_NOTIFICATIONS_CONTROL_TOPIC")

    #: The run-lifecycle topic. The `.v1` in the NAME is the compatibility unit — a subscriber is
    #: entitled to that payload shape forever — so this default is pinned in `tests/unit/test_invariants.py`
    #: alongside its four siblings and a bump is a deliberate act, never a drive-by edit.
    lineage_topic: str = Field(default="lineage.events.v1", alias="RASK_NOTIFICATIONS_LINEAGE_TOPIC")

    #: This app's OWN dead-letter subject (`dlq.notifications`), empty = no dead-lettering.
    #:
    #: EMPTY BY DEFAULT, AND THAT IS A SAFETY DEFAULT RATHER THAN A DEFERRAL. Declaring a
    #: `deadLetterTopic` on a subscription whose app has no entry in `chart/templates/dapr-resiliency.yaml`
    #: means the sidecar has no retry policy to exhaust, so the FIRST transient RETRY parks the message
    #: — measured on the medallion publication head and fixed there this session. Turning this on is
    #: therefore one act with registering the app in the resiliency CRD, and the ordering is enforced by
    #: the operator rather than by code, so the default has to be the safe one.
    dlq_topic: str = Field(default="", alias="RASK_NOTIFICATIONS_DLQ_TOPIC")

    #: The lineage service's own base URL — the reconciler's `GET /events` origin. NOT the gateway:
    #: the feed poll authenticates at lineage's SERVICE door, and the gateway is a public front door
    #: whose invocations that door refuses by construction (`dapr_auth.is_public_caller`).
    lineage_url: str = Field(default="http://127.0.0.1:8000", alias="RASK_NOTIFICATIONS_LINEAGE_URL")

    #: The subject this service claims at lineage's service door. It must appear in that deployment's
    #: `LINEAGE_SERVICE_SUBJECTS` allowlist, and — because the feed is governed — it must also hold the
    #: grants whose rows the reconciler is expected to see.
    #:
    #: `RASK_LINEAGE_SERVICE_IDENTITY` is read FIRST, and that is the whole point rather than a
    #: courtesy alias. The chart builds lineage's allowlist by scanning every service's own
    #: `env.RASK_LINEAGE_SERVICE_IDENTITY` (`chart/templates/services.yaml`), so reading a
    #: differently-named var here would make the door's allowlist and the caller's claim two values
    #: that a deployment can set to disagree — and the failure of that disagreement is a 401 that
    #: reads like a credential fault instead of the naming drift it is. One declaration, both halves.
    #: The `RASK_NOTIFICATIONS_`-prefixed name stays as a fallback so a deployment can still override
    #: this service alone without moving what it claims to lineage.
    service_identity: str = Field(
        default="notifications",
        validation_alias=AliasChoices("RASK_LINEAGE_SERVICE_IDENTITY", "RASK_NOTIFICATIONS_SERVICE_IDENTITY"),
    )

    #: The credential daprd injects when the pod carries `dapr.io/app-token-secret`. Read as a setting
    #: rather than through `os.environ` at the call site so there is one declared name for it, and
    #: `SecretStr` so it cannot land in a log line or a repr by accident.
    app_api_token: SecretStr | None = Field(default=None, alias="APP_API_TOKEN")

    #: Rows per `GET /events` page. 500 is the server's own hard cap (`runs.py` `_EVENTS_RETURN`), so a
    #: larger value here would be silently truncated and the walk would think it had reached the floor.
    feed_page_limit: int = Field(default=500, ge=1, le=500, alias="RASK_NOTIFICATIONS_FEED_PAGE_LIMIT")

    #: How far back one tick will walk. The feed pages OLDER (`seq < after`), so catching up after an
    #: outage is a walk DOWN from the newest row toward the cursor — and the product of this and
    #: `feed_page_limit` is how far that walk can reach. The default covers the feed's whole 20 000-row
    #: retention, so a walk that runs out of pages has reached rows the feed has already pruned: the
    #: gap is unrecoverable rather than merely unread, which is why exhausting it is an ERROR and not a
    #: reason to stall. Pages are processed one at a time, so this bounds the WALK, never memory.
    feed_max_pages: int = Field(default=40, ge=1, alias="RASK_NOTIFICATIONS_FEED_MAX_PAGES")

    #: Per-request timeout on the feed poll. Must stay BELOW the cron tick period, or a stalled lineage
    #: makes ticks pile up on each other instead of the next one finding the same work still to do.
    feed_timeout_seconds: float = Field(default=10.0, gt=0, alias="RASK_NOTIFICATIONS_FEED_TIMEOUT_SECONDS")

    #: Where the reconciler's cursor is persisted — the same store the inbox actors use, addressed
    #: through the sidecar's plain state API rather than the actor API (the cursor belongs to the
    #: service, not to any subject).
    state_store: str = Field(default="lance-statestore", alias="RASK_NOTIFICATIONS_STATE_STORE")

    #: The local sidecar's HTTP port — daprd's own env var, so a deployment that moves it moves this too.
    dapr_http_port: int = Field(default=3500, alias="DAPR_HTTP_PORT")

    #: The output-binding component names for the two channels, and the ONE place they are spelled on
    #: this side. A binding this deployment did not render is simply absent from the dispatch table, so
    #: an opted-in subject is skipped quietly rather than erroring per send.
    email_binding: str = Field(default="notifications-email", alias="RASK_NOTIFICATIONS_EMAIL_BINDING")
    slack_binding: str = Field(default="notifications-slack", alias="RASK_NOTIFICATIONS_SLACK_BINDING")

    #: Which channels this DEPLOYMENT can send at all. Empty = the bell only, which is the default and
    #: the safe posture: a per-user opt-in cannot conjure a channel the estate never enabled.
    enabled_channels: str = Field(default="", alias="RASK_NOTIFICATIONS_CHANNELS")

    #: Per-send ceiling on a channel egress. Below the sidecar's redelivery window, so a hung provider
    #: fails this send rather than stacking the next delivery on top of it.
    channel_timeout_seconds: float = Field(default=10.0, gt=0, alias="RASK_NOTIFICATIONS_CHANNEL_TIMEOUT_SECONDS")

    #: The estate's one Dapr switch, read here as well as in `service_kit.config.Settings` because the
    #: subscriptions are wired at app-build time — before any lifespan exists to hand that object over.
    #: The same env var, so the two can disagree only if someone changes it between two reads.
    dapr_enabled: bool = Field(default=False, alias="RASK_DAPR_ENABLED")

    #: The cron binding that TICKS the reconciler, and therefore also the route path it is delivered
    #: to: Dapr POSTs an input binding to `/<component name>` at the ROOT, so the two are one string
    #: and a mismatch is a component that fires into a 404 forever while the chart looks correct.
    #: Must equal `services.notifications.reconcileBindingName` in the chart.
    binding_name: str = Field(default="notifications-reconcile-cron", alias="RASK_NOTIFICATIONS_BINDING_NAME")

    #: Wall-clock ceiling on ONE reconcile pass, handed to `reconcile()`'s `asyncio.timeout`.
    #:
    #: It exists as a SETTING because it must stay under the cron period, and the period lives in the
    #: chart component: a pass that outlives its own tick lets the next delivery stack on top of it,
    #: which turns a slow lineage into an unbounded pile of concurrent walks rather than one late one.
    #: The default sits below the default `@every 30s` schedule with room for the cursor write, which
    #: `reconcile()` deliberately performs OUTSIDE the budget.
    reconcile_budget_seconds: float = Field(default=25.0, gt=0, alias="RASK_NOTIFICATIONS_RECONCILE_BUDGET_SECONDS")

    @model_validator(mode="after")
    def _timeout_fits_the_budget(self) -> Self:
        """A per-request timeout above the whole pass's budget is unsatisfiable, so refuse it at boot.

        This is the half of settings.py's stated ordering rule that this service CAN check. The other
        half — that the budget stays under the cron period — remains unenforceable here by
        construction: the schedule is component config in the chart and never reaches this process.
        """
        if self.feed_timeout_seconds > self.reconcile_budget_seconds:
            raise ValueError(
                f"feed_timeout_seconds ({self.feed_timeout_seconds}) exceeds reconcile_budget_seconds "
                f"({self.reconcile_budget_seconds}): one page request may not outlast the whole pass"
            )
        return self


@lru_cache(maxsize=1)
def get_ingress_settings() -> IngressSettings:
    """Read once, at first use — `register_subscriptions` runs at import, before any lifespan."""
    return IngressSettings.model_validate({})
