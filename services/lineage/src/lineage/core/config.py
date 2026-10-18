"""Lineage service settings (pydantic-settings, ``LINEAGE_*`` env vars).

Auth is **opt-in and default OFF** (``RASK_OIDC_ENABLED`` / ``RASK_FGA_ENABLED``, the estate's ONE
pair) so dev and tests run open; **production MUST enable both**. The knobs come from the shared
``GovernedAuthSettings`` rather than a ``LINEAGE_*`` twin: this service reuses the catalog's
``OIDCVerifier`` and the **shared** OpenFGA store read-only, so its store/model ids must match the
catalog's — and while the twin existed they were configured under two different names. Fail-closed
config: enabling a layer without the inputs it needs raises at startup, never silently opens.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import GovernedAuthSettings
from service_kit.governed.store_credential import S3KeyPair, store_key_pair
from service_kit.lakehouse.objectfs import lance_storage_options


class Signing(BaseSettings):
    """WHO MAY SIGN WHAT LINEAGE RECORDS, as ONE object ([[LH-064]]): the chart-derived signer and delegator sets.

    `signers` are the identities whose Ed25519 keys sign events put on the bus or into the outbox; `delegators`
    are the ones among them that may stamp a PERSON as the author (the catalog, which authenticated that
    person). Both are rendered by the chart from the identities it mints keys for (`LINEAGE_SIGNERS`,
    `LINEAGE_DELEGATORS`, JSON lists), so what lineage trusts and what the estate provisions cannot drift.

    EMPTY SIGNERS MEAN NOTHING IS VERIFIED. An estate that does not enforce signing (the store off, auth off, or
    `signing.enforce` false) renders both empty: a `rask_signature` an event carries is then ignored, and nothing
    a signature would attest is admitted on it. One object rather than two attributes so that a settings double
    carries the whole verifier configuration or none of it: a double without it fails on the missing attribute
    instead of silently skipping the check.
    """

    model_config = SettingsConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    signers: frozenset[str] = Field(default=frozenset(), alias="LINEAGE_SIGNERS")
    delegators: frozenset[str] = Field(default=frozenset(), alias="LINEAGE_DELEGATORS")

    @property
    def enforced(self) -> bool:
        """Whether lineage refuses an event no listed signer's key verifies."""
        return bool(self.signers)


class LineageSettings(GovernedAuthSettings, BaseSettings):
    """Config for the lineage service, the catalog that keeps its tables, and its auth gate."""

    # `populate_by_name` also teaches the env source the bare FIELD NAME as a second lookup, so
    # every alias below silently gained an un-namespaced twin (MedallionSettings.ray_address
    # answered to Ray's own $RAY_ADDRESS). `env_prefix` redirects that fallback onto the
    # namespace the aliases already declare; an explicit alias bypasses it, so the
    # deliberately-bare ones (DAPR_HTTP_PORT) still land.
    # See tests/unit/test_settings_env_namespace.py.
    model_config = SettingsConfigDict(populate_by_name=True, env_prefix="LINEAGE_", extra="ignore")

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
    provenance_namespace: str = Field(default="provenance", alias="LINEAGE_PROVENANCE_NAMESPACE")
    #: Bounds each catalog call, so a hung catalog cannot pin a delivery for ever.
    catalog_timeout_seconds: float = Field(default=30.0, gt=0, alias="LINEAGE_CATALOG_TIMEOUT_SECONDS")

    # --- The forwarder (ADR 0158): every recorded event, as standard OpenLineage, to a bring-your-own graph -------
    #: The graph backend's base URL, the OpenLineage HTTP transport's `url`. Empty forwards nothing.
    forward_url: str = Field(default="", alias="LINEAGE_FORWARD_URL")
    #: The path events are POSTed to under `forward_url`, the transport's `endpoint`.
    forward_endpoint: str = Field(default="api/v1/lineage", alias="LINEAGE_FORWARD_ENDPOINT")
    #: The secret in the Dapr secret store holding the backend's API key, sent as `Authorization: Bearer`. Empty
    #: sends none. Read on every relay tick, so a rotated key is taken without a restart.
    forward_api_key_secret: str = Field(default="", alias="LINEAGE_FORWARD_API_KEY_SECRET")
    #: The field of that secret the key is under.
    forward_api_key_field: str = Field(default="api-key", alias="LINEAGE_FORWARD_API_KEY_FIELD")
    #: The most events one relay tick forwards; the rest follow on the next. At the relay's 30 s schedule, 100 a tick
    #: is 12,000 an hour, against a feed measured at about 450 rows a day.
    forward_batch: int = Field(default=100, ge=1, alias="LINEAGE_FORWARD_BATCH")
    #: Bounds each POST to the backend, so a hung backend cannot pin a tick.
    forward_timeout_seconds: float = Field(default=10.0, gt=0, alias="LINEAGE_FORWARD_TIMEOUT_SECONDS")

    # --- OpenFGA (authz) — reuses the catalog's store READ-ONLY -------------------------
    # The OIDC/FGA field-set itself is `GovernedAuthSettings`' (`RASK_OIDC_*` / `RASK_FGA_*`); only the
    # knob below is lineage's own. It used to be declared here under `LINEAGE_*`, and the copy had
    # already lost the shared HTTPS-issuer validator.
    # The FGA object type a Lance dataset maps to. A feed row names datasets by their catalog
    # ``table:<id>``, so a read is gated on ``can_get_metadata`` of ``table:<name>``.
    fga_object_type: str = Field(default="table", alias="LINEAGE_FGA_OBJECT_TYPE")
    #: Who may sign what the bus and the outbox carry; see `Signing`. Built by its own env aliases
    #: (`LINEAGE_SIGNERS`, `LINEAGE_DELEGATORS`), so a settings object made by `model_validate` reads them too.
    signing: Signing = Field(default_factory=Signing)

    # --- Dapr pub/sub durable ingest (opt-in) — the catalog publishes to the Dapr pubsub.jetstream
    # component and the sidecar delivers each event to this service's subscription handler over HTTP, so
    # a lineage outage never loses provenance (the sidecar persists to NATS + redelivers per backOff).
    # The HTTP /api/v1/lineage endpoint stays for external producers. Off by default (HTTP is dev default).
    dapr_enabled: bool = Field(default=False, alias="LINEAGE_DAPR_ENABLED")
    dapr_pubsub: str = Field(default="lineage-pubsub", alias="LINEAGE_DAPR_PUBSUB")
    dapr_topic: str = Field(default="lineage.events.v1", alias="LINEAGE_DAPR_TOPIC")
    #: Bounds the relay's re-publish of a drained event. An unbounded publish against a hung sidecar would
    #: stall the tick — the relay failing hardest exactly when a backlog means it matters most, which is the
    #: same failure the bounded drain was introduced to avoid.
    dapr_publish_timeout_seconds: float = Field(default=5.0, ge=0.1, alias="LINEAGE_DAPR_PUBLISH_TIMEOUT_SECONDS")
    # Dead-letter topic for the ingest subscription (Dapr-native DLQ). "" (default) = none — the
    # pre-existing behavior. Ships together with the chart's Resiliency retry policy (a DLQ without
    # one dead-letters on the FIRST failure per Dapr's documented default). Lineage's recovery story
    # stays replay-from-stream (its durable deliverPolicy=all consumer, re-created to rebuild); the DLQ
    # adds operator VISIBILITY for deliveries that exhausted retries, it does not replace the replay.
    dapr_dlq_topic: str = Field(default="", alias="LINEAGE_DLQ_TOPIC")
    # The PARKING subscription's own pubsub component (durable, deliverPolicy=new). "" (default) falls
    # back to `dapr_pubsub` — but that component starts at the stream's first message (a replay rebuilds
    # the feed), so riding it would re-park the whole retained DLQ backlog on a replay. The chart
    # always sets this alongside LINEAGE_DLQ_TOPIC; the fallback only
    # exists so a dev stack without the extra component keeps working.
    dapr_dlq_pubsub: str = Field(default="", alias="LINEAGE_DLQ_PUBSUB")
    # The relay tick: a Dapr cron binding POSTs to /<name> on a schedule, and the tick drains the outbox,
    # prunes the feed and forwards to the configured graph backend. Empty = the route isn't mounted.
    relay_binding_name: str = Field(default="", alias="LINEAGE_RELAY_BINDING_NAME")

    # --- The outbox the relay drains: object storage the producers stage to and this service deletes from.
    s3_endpoint: str | None = Field(default=None, alias="LINEAGE_S3_ENDPOINT")
    #: The key pair the outbox is reached with when the secret store is OFF; with it on, the pair is the store's
    #: (`dapr_secret_s3_id_field` and `dapr_secret_s3_field`, ADR 0166) and these two are not read. SecretStr so
    #: the secret is redacted in repr/model_dump (parity with the catalog).
    s3_access_key_id: str | None = Field(default=None, alias="LINEAGE_S3_ACCESS_KEY_ID")
    s3_secret_access_key: SecretStr = Field(default=SecretStr(""), alias="LINEAGE_S3_SECRET_ACCESS_KEY")
    s3_region: str = Field(default="us-east-1", alias="LINEAGE_S3_REGION")

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

    # --- Secret consumption from the Dapr secret store (OpenBao), symmetric with the catalog. When on, the
    # outbox's S3 key pair is the store's, NOT plaintext env: the chart renders neither half into pod env,
    # and `service_kit.governed.store_credential` reads it at boot (failing closed) and again while lineage runs.
    secrets_from_dapr: bool = Field(default=False, alias="LINEAGE_SECRETS_FROM_DAPR")
    #: THE ONE STORE, NAMED ONCE (DUP-17). The estate runs a single Dapr secret-store component and
    #: seven env vars named it, each defaulting to the same literal and none of them set by the chart —
    #: so repointing the store meant finding all seven. `RASK_SECRET_STORE` is the estate-wide name
    #: (already what viewer and ingest read); the per-service alias stays FIRST so a single service can
    #: still be moved on its own.
    dapr_secret_store: str = Field(default="lance-secrets", validation_alias=AliasChoices("LINEAGE_DAPR_SECRET_STORE", "RASK_SECRET_STORE"))
    dapr_secret_key: str = Field(default="lance", alias="LINEAGE_DAPR_SECRET_KEY")
    #: Lineage's own scoped pair (`objectStore.lineageAccessKey`), never the store's root (ADR 0168).
    dapr_secret_s3_field: str = Field(default="lineage-s3-secret-key", alias="LINEAGE_DAPR_SECRET_S3_FIELD")
    #: The field of the same secret holding the key id the secret signs under, read with it.
    dapr_secret_s3_id_field: str = Field(default="lineage-s3-access-key", alias="LINEAGE_DAPR_SECRET_S3_ID_FIELD")

    # --- Feed retention, in DAYS of arrival time (the estate's own `received_at`, never the
    # producer-supplied eventTime). 0 = unbounded. Pruned once per relay tick, never on the ingest path.
    #
    # DAYS BECAUSE THE CONSUMER REASONS IN DAYS. `services/notifications` walks this feed from a persisted
    # cursor as the estate's catch-up path after an outage, so the only question an operator needs a
    # retention knob to answer is "how long an outage does the feed survive". A count cannot answer it at
    # any volume.
    #
    # 7 DAYS IS THE HORIZON THE DEPLOYED FEED WAS MEASURED HOLDING (2026-08-31 → 2026-09-07, 3 133 rows),
    # so the default states the working estate's horizon rather than changing it.
    events_retention_days: int = Field(default=7, ge=0, alias="LINEAGE_EVENTS_RETENTION_DAYS")

    # Compliance audit trail (#41): gate the dedicated `lance.audit` stream (the DLQ-replay record) exactly
    # like the catalog — the SHARED LANCE_AUDIT_ENABLED env (not a LINEAGE_* twin), so one flag governs the
    # estate's compliance posture. Default on; without the lifespan's configure_audit call the stream is
    # silently OFF (the logger inherits root WARNING and the INFO records drop before any handler).
    audit_enabled: bool = Field(default=True, alias="LANCE_AUDIT_ENABLED")

    #: OFF by default. It defaulted to True and NO deployment path ever set it — `grep -rn DOCS
    #: chart/ .docker/ scripts/` matched nothing — so the flag documented a choice nobody was making
    #: and the schemas shipped openly. A security default every deployment must remember to disable is
    #: one nobody disables. Turn it on per-environment (the chart's dev values do).
    docs_enabled: bool = Field(default=False, alias="LINEAGE_DOCS")

    def s3_key_pair(self) -> S3KeyPair:
        """The key pair the outbox is reached with: the store's, or the environment's when the store is off.

        Raises:
            StoreCredentialUnavailableError: The store has not confirmed the pair within its TTL (503).
        """
        if self.secrets_from_dapr:
            return store_key_pair(self)
        return S3KeyPair(access_key_id=self.s3_access_key_id or "", secret_access_key=self.s3_secret_access_key)


@lru_cache
def get_settings() -> LineageSettings:
    """Return the process-wide cached lineage settings."""
    return LineageSettings()


def storage_options(settings: LineageSettings) -> dict[str, str]:
    """Object-store options for the outbox the relay drains.

    The same S3-compatible config the producers stage with, under the key pair in force. Empty strings let
    the object-store client fall back to its default credential chain (e.g. real AWS) when the secret store
    is off and the ``LINEAGE_S3_*`` env vars are unset.
    """
    pair = settings.s3_key_pair()
    return lance_storage_options(settings.s3_endpoint or "", pair.access_key_id, pair.secret_access_key.get_secret_value(), settings.s3_region)
