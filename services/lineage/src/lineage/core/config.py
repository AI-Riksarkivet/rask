"""Lineage service settings (pydantic-settings, ``LINEAGE_*`` env vars).

Auth is **opt-in and default OFF** (``RASK_OIDC_ENABLED`` / ``RASK_FGA_ENABLED``, the estate's ONE
pair) so dev and tests run open; **production MUST enable both**. The knobs come from the shared
``GovernedAuthSettings`` rather than a ``LINEAGE_*`` twin: this service reuses the catalog's
``OIDCVerifier`` and the **shared** OpenFGA store read-only, so its store/model ids must match the
catalog's — and while the twin existed they were configured under two different names. Fail-closed
config: enabling a layer without the inputs it needs raises at startup, never silently opens.
"""

from functools import lru_cache
from typing import Annotated, Self

from pydantic import AfterValidator, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.events.bus_delivery import Lane
from lakehouse.events.openlineage import DEFAULT_DATASET_NAMESPACE, require_dataset_namespace
from lakehouse.governed.settings import GovernedAuthSettings
from lakehouse.storage.credential import S3KeyPair
from lakehouse.storage.objectfs import lance_storage_options
from lakehouse.storage.options import StorageOptions, on_s3
from lakehouse.storage.store_session import StoreIdentitySettings, env_alias, key_pair_in_force, require_one_store_identity


class Signing(BaseSettings):
    """WHO MAY SIGN WHAT LINEAGE RECORDS, as ONE object ([[LH-064]]): the chart-derived signer and delegator sets.

    `signers` are the identities whose Ed25519 keys sign events put on the bus or into the outbox; `delegators`
    are the ones among them that may stamp a PERSON as the author (the catalog, which authenticated that
    person). Both are rendered by the chart from the identities it mints keys for (`LINEAGE_SIGNERS`,
    `LINEAGE_DELEGATORS`, JSON lists), so what lineage trusts and what the estate provisions cannot drift.

    EMPTY SIGNERS MEAN NOTHING IS VERIFIED. A process with no secret store or with authentication off (the offline
    and Dagger lanes) lists neither, and a deployment with both lists its signers or is refused its start
    (:meth:`require_signers_where_they_can_be_verified`): a `rask_signature` an event carries is then ignored, and nothing
    a signature would attest is admitted on it. One object rather than two attributes so that a settings double
    carries the whole verifier configuration or none of it: a double without it fails on the missing attribute
    instead of silently skipping the check.
    """

    model_config = SettingsConfigDict(frozen=True, extra="ignore", validate_by_name=True, validate_by_alias=True)

    signers: frozenset[str] = Field(default=frozenset(), alias="LINEAGE_SIGNERS")
    delegators: frozenset[str] = Field(default=frozenset(), alias="LINEAGE_DELEGATORS")

    @property
    def enforced(self) -> bool:
        """Whether lineage refuses an event no listed signer's key verifies."""
        return bool(self.signers)

    def require_signers_where_they_can_be_verified(self, *, authenticated: bool, secret_store: bool) -> None:
        """Refuse a start that could verify and lists nobody to verify.

        A deployment with authentication and a secret store, the published keys' source, has one state: lineage
        verifies what its bus and outbox doors record. With no signer listed there, a `rask_signature` an event
        carries would be recorded unverified, take the catalog's own feed row and be served as verified. A process
        with no secret store has no key to verify against, and one with authentication off authenticates nobody.

        Raises:
            RuntimeError: Authentication and a secret store are on and ``LINEAGE_SIGNERS`` lists nobody.
        """
        if authenticated and secret_store and not self.enforced:
            raise RuntimeError(
                "lineage is authenticated and has a secret store, and LINEAGE_SIGNERS lists no signer: it would record "
                "signatures nobody verified. List the identities that sign (the chart renders them wherever auth is on)."
            )


class LineageForward(BaseSettings):
    """The graph backend lineage forwards each recorded event to, when one is named."""

    # --- The forwarder (ADR 0158): every recorded event, as standard OpenLineage, to a bring-your-own graph -------
    #: The graph backend's base URL, the OpenLineage HTTP transport's `url`. Empty forwards nothing.
    forward_url: str = Field(default="", alias="LINEAGE_FORWARD_URL")
    #: The path events are POSTed to under `forward_url`, the transport's `endpoint`.
    forward_endpoint: str = Field(default="api/v1/lineage", alias="LINEAGE_FORWARD_ENDPOINT")
    #: The secret in lineage's secret store holding the backend's API key, sent as `Authorization: Bearer`. Empty
    #: sends none. Read on every relay tick, so a rotated key is taken without a restart.
    forward_api_key_secret: str = Field(default="", alias="LINEAGE_FORWARD_API_KEY_SECRET")
    #: The field of that secret the key is under.
    forward_api_key_field: str = Field(default="api-key", alias="LINEAGE_FORWARD_API_KEY_FIELD")
    #: The most events one relay tick forwards; the rest follow on the next. At the relay's 30 s schedule, 100 a tick
    #: is 12,000 an hour, against a feed measured at about 450 rows a day.
    forward_batch: int = Field(default=100, ge=1, alias="LINEAGE_FORWARD_BATCH")
    #: Bounds each POST to the backend, so a hung backend cannot pin a tick.
    forward_timeout_seconds: float = Field(default=10.0, gt=0, alias="LINEAGE_FORWARD_TIMEOUT_SECONDS")


class LineageDelivery(BaseSettings):
    """The lane lineage takes its deliveries on, and how one is retried, bounded and published again."""

    # --- Durable ingest off the bus: the catalog publishes to NATS JetStream, which keeps each event, and this service
    # pulls it from its own consumer on its own connection wherever the chart names it a bus (`lineage.api.ingest_lane`,
    # `lakehouse.events.bus`), so a lineage outage never loses provenance. The HTTP /api/v1/lineage endpoint stays for
    # external producers.
    #: The topic lineage's events ride: the one its consumer takes, and the one the relay publishes a recovered event on.
    lineage_topic: str = Field(default="lineage.events.v1", alias="RASK_LINEAGE_TOPIC")
    #: How many deliveries are handled at once, across this service's replicas: the consumer's `max_ack_pending`, which
    #: the chart declares from the same value, so the bus hands out no more than the replicas handle.
    deliveries_in_flight: int = Field(default=64, ge=1, alias="LINEAGE_DELIVERIES_IN_FLIGHT")
    #: How many deliveries an event is handled on before it is parked, and how long one that is asked for again waits.
    delivery_attempts: int = Field(default=16, ge=1, alias="LINEAGE_DELIVERY_ATTEMPTS")
    delivery_retry_seconds: float = Field(default=120.0, ge=0, alias="LINEAGE_DELIVERY_RETRY_SECONDS")
    #: The consumer's ack wait, as the chart declares it: a handling is cut short of it, so the bus never delivers an
    #: event again beneath a handling still running.
    delivery_ack_wait_seconds: float = Field(default=300.0, gt=Lane.SETTLE_SECONDS, alias="LINEAGE_DELIVERY_ACK_WAIT_SECONDS")
    #: Bounds the relay's re-publish of a drained event. An unbounded publish against a bus that does not answer
    #: would stall the tick — the relay failing hardest exactly when a backlog means it matters most, which is the
    #: same failure the bounded drain was introduced to avoid.
    publish_timeout_seconds: float = Field(default=5.0, ge=0.1, alias="RASK_PUBLISH_TIMEOUT_SECONDS")


class LineageRelay(BaseSettings):
    """The relay's tick: its period, the root its timer claims windows under, the outbox it drains and the feed it prunes."""

    # Seconds between two ticks of the relay, which drains the outbox, forwards to the configured graph backend and
    # prunes the feed, on lineage's own timer (`lineage/api/relay.py`). 0 = no relay.
    relay_period_seconds: float = Field(default=0, ge=0, alias="LINEAGE_RELAY_PERIOD_SECONDS")
    # The control root the relay's timer claims each window under (`lakehouse.timers`), so one replica runs each tick.
    # Required with a period: a relay that could not claim would run on every replica.
    timer_root: str = Field(default="", alias="LINEAGE_TIMER_ROOT")

    # #4: the durable lineage-outbox prefix (shared with the producers that stage there). When set, the relay
    # tick DRAINS it — re-ingesting any event a producer staged but whose publish never got acked (a crash
    # between the Lance commit and the publish), then deleting it. Empty = drain disabled. Must point at the
    # SAME object-store prefix as ``LANCE_LINEAGE_OUTBOX_URI`` and ``MAINTENANCE_LINEAGE_OUTBOX_URI``.
    outbox_uri: str = Field(default="", alias="LINEAGE_OUTBOX_URI")
    # Max staged events one relay tick drains, OLDEST FIRST (docs/adr/0004-p1-2-bounded-oldest-first-outbox-drain.md P1.2 (bounded drain)).
    # The drain used to materialise the ENTIRE prefix in memory, so a backlog — precisely the situation the
    # outbox exists to survive — could OOM or stall the tick. The remainder drains on the next tick;
    # oldest-first means nothing starves behind a steady arrival rate. 0 = unbounded (not recommended).
    outbox_drain_limit: int = Field(default=500, alias="LINEAGE_OUTBOX_DRAIN_LIMIT")

    # --- Feed retention, in DAYS of arrival time (the estate's own `received_at`, never the
    # producer-supplied eventTime). 0 = unbounded. Pruned once per relay tick, never on the ingest path.
    #
    # DAYS BECAUSE A CONSUMER REASONS IN DAYS. A notifier walks this feed from a persisted
    # cursor as its catch-up path after an outage, so the only question an operator needs a
    # retention knob to answer is "how long an outage does the feed survive". A count cannot answer it at
    # any volume.
    #
    # 7 DAYS IS THE HORIZON THE DEPLOYED FEED WAS MEASURED HOLDING (2026-08-31 → 2026-09-07, 3 133 rows),
    # so the default states the working estate's horizon rather than changing it.
    events_retention_days: int = Field(default=7, ge=0, alias="LINEAGE_EVENTS_RETENTION_DAYS")

    @model_validator(mode="after")
    def _a_relay_claims_its_windows(self) -> Self:
        """Refuse a relay period with no root to claim its windows under."""
        if self.relay_period_seconds and not self.timer_root:
            raise ValueError("LINEAGE_RELAY_PERIOD_SECONDS is set and LINEAGE_TIMER_ROOT is empty: the relay's timer claims each window under a control root")
        return self


class LineageSettings(GovernedAuthSettings, StoreIdentitySettings, LineageForward, LineageDelivery, LineageRelay, BaseSettings):
    """Config for the lineage service, the catalog that keeps its tables, and its auth gate."""

    # `validate_by_name` also teaches the env source the bare FIELD NAME as a second lookup, so
    # every alias below silently gained an un-namespaced twin (a field named `address` would
    # answer to whatever $ADDRESS another program in the pod exports). `env_prefix` redirects that fallback onto the
    # namespace the aliases already declare; an explicit alias bypasses it, so the
    # deliberately-bare ones (RASK_CATALOG_IDENTITY_TOKEN_FILE) still land.
    # See tests/unit/test_settings_env_namespace.py.
    model_config = SettingsConfigDict(
        validate_by_name=True, validate_by_alias=True, env_prefix="LINEAGE_", alias_generator=env_alias("LINEAGE"), extra="ignore"
    )

    # --- The catalog that keeps the feed and the edge list (ADR 0158) ---------------------------------
    #: The catalog's base URL. The feed, the edge list and the outbox refusals are Lance tables it governs, and
    #: every read and write of them goes through its doors, so lineage cannot start without it.
    catalog_url: str = Field(default="", alias="LINEAGE_CATALOG_URL")
    #: This pod's projected service-account token for the catalog's doors (audience `rask-catalog`), re-read on
    #: every call because the kubelet rotates it at about 515 s of its 600 s life. Empty sends no bearer, which
    #: only a catalog with authentication off accepts.
    catalog_identity_token_file: str = Field(default="", alias="RASK_CATALOG_IDENTITY_TOKEN_FILE")
    #: The namespace the catalog keeps for provenance (its `LANCE_PROVENANCE_NAMESPACE`), where this service
    #: creates its tables. The two must name the same namespace.
    provenance_namespace: str = Field(default="lance_catalog$provenance", alias="LINEAGE_PROVENANCE_NAMESPACE")
    #: The dataset namespace the catalog names its tables under (its `LANCE_LINEAGE_DATASET_NAMESPACE`, [[LH-553]]): a
    #: dataset under it is one of this estate's tables, and one under another catalog's is another estate's, which this
    #: service records as an outside source. The chart renders both from `rask.lineageDatasetNamespace`.
    catalog_dataset_namespace: Annotated[str, AfterValidator(require_dataset_namespace)] = Field(
        default=DEFAULT_DATASET_NAMESPACE, alias="LINEAGE_CATALOG_DATASET_NAMESPACE"
    )
    #: Bounds each catalog call, so a hung catalog cannot pin a delivery for ever.
    catalog_timeout_seconds: float = Field(default=30.0, gt=0, alias="LINEAGE_CATALOG_TIMEOUT_SECONDS")

    # --- OpenFGA (authz) — reuses the catalog's store READ-ONLY -------------------------
    # The OIDC/FGA field-set itself is `GovernedAuthSettings`' (`RASK_OIDC_*` / `RASK_FGA_*`). A governed dataset is
    # asked about as the table its uuid names (`table:<uuid>`, [[LH-524]]).
    #: Who may sign what the bus and the outbox carry; see `Signing`. Built by its own env aliases
    #: (`LINEAGE_SIGNERS`, `LINEAGE_DELEGATORS`), so a settings object made by `model_validate` reads them too.
    signing: Signing = Field(default_factory=Signing)

    # The lakehouse's read-only window, rendered from the chart's one `readOnlyWindow` with the catalog's and maintenance's:
    # the relay's timer is held through it, so the first window after it drains, forwards and prunes.
    read_only: bool = Field(default=False, alias="LINEAGE_READ_ONLY")

    # --- The outbox the relay drains: object storage the producers stage to and this service deletes from.
    s3_endpoint: str | None = Field(default=None, alias="LINEAGE_S3_ENDPOINT")
    # Lineage's identity at the store is `StoreIdentitySettings`' (LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE and the rest): the
    # sessions its token is exchanged for sign its outbox drain, and it holds no key.

    # Compliance audit trail (#41): gate the dedicated `lance.audit` stream (the DLQ-replay record) exactly
    # like the catalog — the SHARED RASK_AUDIT_ENABLED env (not a LINEAGE_* twin), so one flag governs the
    # estate's compliance posture. Default on; without the lifespan's configure_audit call the stream is
    # silently OFF (the logger inherits root WARNING and the INFO records drop before any handler).
    audit_enabled: bool = Field(default=True, alias="RASK_AUDIT_ENABLED")

    #: OFF by default. It defaulted to True and NO deployment path ever set it — `grep -rn DOCS
    #: chart/ .docker/ scripts/` matched nothing — so the flag documented a choice nobody was making
    #: and the schemas shipped openly. A security default every deployment must remember to disable is
    #: one nobody disables. Turn it on per-environment (the chart's dev values do).
    docs_enabled: bool = Field(default=False, alias="LINEAGE_DOCS")

    #: The largest request body lineage reads; one over it is refused 413 before it is buffered (`lakehouse.service_app.body_limit`).
    #: An event the bus delivers is at most NATS's message ceiling, 1 MiB (the bus fixture sets no `max_payload`), plus its
    #: envelope; the intake takes events from producers that never pass the bus, whose schema facets can run to a few MiB.
    max_body_bytes: int = Field(default=8 * 1024 * 1024, ge=1, alias="LINEAGE_MAX_BODY_BYTES")

    #: A request with no answer begun after this long is answered 503 while its handling runs on (`lakehouse.service_app.deadline`):
    #: five times the longest lineage request measured over two weeks of the estate's traces (2026-10-29..11-12: the
    #: relay's tick, 58.1 s).
    request_deadline_seconds: float = Field(default=300.0, gt=0, alias="LINEAGE_REQUEST_DEADLINE_SECONDS")
    #: The port lineage answers `/livez` and `/readyz` on from a thread of their own (`lakehouse.process.probe_server`),
    #: so a probe does not wait behind a burst of requests; unset, they are answered on the serving port alone.
    probes_port: int | None = Field(default=None, ge=1, le=65535, alias="LINEAGE_PROBES_PORT")

    @model_validator(mode="after")
    def _one_store_identity(self) -> Self:
        """Refuse a store identity that is doubled or sent to AWS's STS."""
        require_one_store_identity(self.web_identity, env_pair=self.env_pair, prefix="LINEAGE")
        return self

    @property
    def outbox_on_the_store(self) -> bool:
        """Whether the outbox the relay drains is on the object store."""
        return on_s3(self.outbox_uri)

    @property
    def signs_store_requests(self) -> bool:
        """Whether lineage signs object-store requests at all: for an outbox it drains, or a timer it claims, there."""
        return self.outbox_on_the_store or on_s3(self.timer_root)

    def s3_key_pair(self) -> S3KeyPair:
        """What the outbox is reached with: the session lineage's token was exchanged for, or the environment's pair.

        Raises:
            StoreCredentialUnavailableError: Lineage projects a token and holds no unexpired session (503).
        """
        return key_pair_in_force(self.web_identity, self.env_pair)


@lru_cache
def get_settings() -> LineageSettings:
    """Return the process-wide cached lineage settings."""
    return LineageSettings()


def storage_options(settings: LineageSettings) -> StorageOptions:
    """Object-store options for the outbox the relay drains: the S3-compatible config the producers stage with, under the
    key pair in force (:meth:`LineageSettings.s3_key_pair`)."""
    return lance_storage_options(settings.s3_endpoint or "", settings.s3_key_pair(), settings.s3_region)
