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
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import GovernedAuthSettings
from service_kit.lakehouse.objectfs import lance_storage_options


class LineageSettings(GovernedAuthSettings, BaseSettings):
    """Config for the lineage service, its Apache AGE graph store, and its auth gate."""

    # `populate_by_name` also teaches the env source the bare FIELD NAME as a second lookup, so
    # every alias below silently gained an un-namespaced twin (MedallionSettings.ray_address
    # answered to Ray's own $RAY_ADDRESS). `env_prefix` redirects that fallback onto the
    # namespace the aliases already declare; an explicit alias bypasses it, so the
    # deliberately-bare ones (DAPR_HTTP_PORT, RAY_DASHBOARD_URL) still land.
    # See tests/unit/test_settings_env_namespace.py.
    model_config = SettingsConfigDict(populate_by_name=True, env_prefix="LINEAGE_", extra="ignore")

    database_url: str = Field(
        default="postgresql://lineage:lineage@localhost:5433/lineage",
        alias="LINEAGE_DATABASE_URL",
    )
    graph: str = Field(default="lineage", alias="LINEAGE_GRAPH")
    # Server-side per-statement timeout on every pooled AGE connection — bounds a runaway Cypher traversal
    # so it can't pin a pooled connection forever (§4). Generous: normal statements are point MERGEs.
    age_statement_timeout_seconds: float = Field(default=30.0, ge=1.0, alias="LINEAGE_AGE_STATEMENT_TIMEOUT_SECONDS")

    # --- OpenFGA (authz) — reuses the catalog's store READ-ONLY -------------------------
    # The OIDC/FGA field-set itself is `GovernedAuthSettings`' (`RASK_OIDC_*` / `RASK_FGA_*`); only the
    # knob below is lineage's own. It used to be declared here under `LINEAGE_*`, and the copy had
    # already lost the shared HTTPS-issuer validator.
    # The FGA object type a Lance dataset maps to. A lineage Dataset node's ``name`` is the
    # catalog ``table:<id>``, so a read is gated on ``can_get_metadata`` of ``table:<name>``.
    fga_object_type: str = Field(default="table", alias="LINEAGE_FGA_OBJECT_TYPE")
    # Bare FGA subjects (comma-separated, e.g. "service-trainer") that may ingest over the HTTP route as
    # an in-cluster SERVICE — authenticated by the app token, not OIDC (lineage/api/security.py
    # ServicePrincipal). Empty (the default) = the service door is shut and the HTTP ingest is OIDC-only.
    # The allowlist is what stops an app-token holder from speaking as a human; outputs are still
    # FGA-checked as the named subject, so each service is bounded by its own rung (D5).
    service_subjects: str = Field(default="", alias="LINEAGE_SERVICE_SUBJECTS")

    # Subjects that may NOT be claimed with the estate's SHARED app token — each needs its own
    # dedicated credential, resolved from the Dapr secret store.
    #
    # THE HOLE THIS CLOSES, measured: `service_subjects` was `service-trainer,service-web` and ONE
    # token opened both, with the CALLER choosing which to be via the `x-lance-service-identity`
    # header. The two are not peers — `service-web` is a reader on the warehouse, while
    # `service-trainer` holds `writer` on `namespace:models` (`scripts/seed_medallion_fga.sh:80-82`).
    # And `service-web`'s token is the shared `{release}-dapr-app-token`, which sits in the env of
    # seven internet-facing web pods that have no Dapr sidecar. So anyone with env read in any of
    # them could present that token, claim `service-trainer`, and forge author-stamped WRITES into
    # the authoritative lineage graph — strictly more than the credential was scoped for.
    #
    # An allowlist cannot fix that on its own: it answers "may this subject use the door", never "may
    # THIS CALLER be that subject". Binding a privileged identity to its own credential is what turns
    # the header into a claim the door VERIFIES rather than one it believes.
    #
    # EMPTY BY DEFAULT — byte-identical to today's behaviour — because populating it is a deployment
    # decision: each listed subject must have its secret provisioned first, or it stops being able to
    # authenticate at all. That is a fail-closed outage, which is the right direction, but it must be
    # chosen rather than inherited.
    privileged_subjects: str = Field(default="", alias="LINEAGE_PRIVILEGED_SUBJECTS")

    # --- Dapr pub/sub durable ingest (opt-in) — the catalog publishes to the Dapr pubsub.jetstream
    # component and the sidecar delivers each event to this service's subscription handler over HTTP, so
    # a lineage outage never loses provenance (the sidecar persists to NATS + redelivers per backOff).
    # The HTTP /api/v1/lineage endpoint stays for external producers. Off by default (HTTP is dev default).
    dapr_enabled: bool = Field(default=False, alias="LINEAGE_DAPR_ENABLED")
    dapr_pubsub: str = Field(default="lineage-pubsub", alias="LINEAGE_DAPR_PUBSUB")
    dapr_topic: str = Field(default="lineage.events.v1", alias="LINEAGE_DAPR_TOPIC")
    #: Bounds the relay's re-publish of a drained event. The drain runs inside the tick's single-flight
    #: lock, so an unbounded publish against a hung sidecar would hold that lock and stall every later
    #: tick — the relay failing hardest exactly when a backlog means it matters most, which is the same
    #: failure the bounded drain was introduced to avoid.
    dapr_publish_timeout_seconds: float = Field(default=5.0, ge=0.1, alias="LINEAGE_DAPR_PUBLISH_TIMEOUT_SECONDS")
    # Dead-letter topic for the ingest subscription (Dapr-native DLQ). "" (default) = none — the
    # pre-existing behavior. Ships together with the chart's Resiliency retry policy (a DLQ without
    # one dead-letters on the FIRST failure per Dapr's documented default). Lineage's recovery story
    # stays replay-from-stream (ephemeral deliverPolicy=all consumer); the DLQ adds operator
    # VISIBILITY for deliveries that exhausted retries, it does not replace the replay.
    dapr_dlq_topic: str = Field(default="", alias="LINEAGE_DLQ_TOPIC")
    # The PARKING subscription's own pubsub component (durable, deliverPolicy=new). "" (default) falls
    # back to `dapr_pubsub` — but that component is deliverPolicy=all + ephemeral BY DESIGN (replay
    # rebuilds the graph), so riding it re-parked the whole retained DLQ backlog on every pod restart
    # . The chart always sets this alongside LINEAGE_DLQ_TOPIC; the fallback only
    # exists so a dev stack without the extra component keeps working.
    dapr_dlq_pubsub: str = Field(default="", alias="LINEAGE_DLQ_PUBSUB")
    # Freshness budget in hours (data-contract gap #2): 0 (default) = the axis is OFF (no probe).
    # >0 = the reconcile sweep + per-dataset GET flag `stale: true` for any dataset whose newest
    # version commit is older — arrival cadence becomes an ASSERTED contract clause, not a dashboard.
    freshness_budget_hours: float = Field(default=0, alias="LINEAGE_FRESHNESS_BUDGET_HOURS")
    # Declared consumer dependencies per dataset (data-contract, Batch 23): a JSON map
    # {"dataset": ["col", ...]} the chart derives from the movers' requiredColumns. The reconcile
    # sweep compares each declared dataset's STORAGE schema against it and reports
    # missing_declared_columns — the estate-patrol half of the gate's column_declared assertion
    # (same two-enforcement-point pattern as the blob probe). "" (default) = no checks, no reads.
    declared_columns: str = Field(default="", alias="LINEAGE_DECLARED_COLUMNS")
    # Periodic storage->graph reconciliation (B4) — a Dapr cron binding POSTs to /<name> on a schedule to
    # back-fill Lance writes whose lineage event was lost (the outbox gap). Empty = the cron route isn't
    # mounted (the /datasets/{name}/reconcile read endpoint is always available regardless).
    reconcile_binding_name: str = Field(default="", alias="LINEAGE_RECONCILE_BINDING_NAME")

    # --- Demo data peek (DEMO ONLY) — reads the real Lance datasets on S3 so the UI can show
    # what is changing in storage (schema/versions/rows). Off by default; never enable in prod.
    demo_data_enabled: bool = Field(default=False, alias="LINEAGE_DEMO_DATA_ENABLED")
    # Cap on how many (newest) Lance versions the peek reports per dataset — the peek's cost grew
    # LINEARLY with total versions (one S3 dataset-open per version, polled every 2s) before the
    # 2026-07-11 version-keyed cache; the cap bounds the very first (cold) tick too.
    demo_max_versions: int = Field(default=50, ge=1, alias="LINEAGE_DEMO_MAX_VERSIONS")
    s3_endpoint: str | None = Field(default=None, alias="LINEAGE_S3_ENDPOINT")
    s3_access_key_id: str | None = Field(default=None, alias="LINEAGE_S3_ACCESS_KEY_ID")
    # SecretStr so the value is redacted in repr/model_dump (parity with the catalog) — read it with
    # .get_secret_value() at the object-store call site.
    s3_secret_access_key: SecretStr = Field(default=SecretStr(""), alias="LINEAGE_S3_SECRET_ACCESS_KEY")
    s3_region: str = Field(default="us-east-1", alias="LINEAGE_S3_REGION")
    s3_bucket: str = Field(default="lakehouse", alias="LINEAGE_S3_BUCKET")
    # #4: the durable lineage-outbox prefix (shared with the medallion movers). When set, the reconcile
    # sweep also DRAINS it — re-ingesting any event a producer staged but whose publish never got acked
    # (a crash between the Lance commit and the publish), then deleting it. Empty = drain disabled. Must
    # point at the SAME object-store prefix as ``MEDALLION_LINEAGE_OUTBOX_URI``.
    outbox_uri: str = Field(default="", alias="LINEAGE_OUTBOX_URI")
    # Max staged events one reconcile tick drains, OLDEST FIRST (docs/DECISIONS.md P1.2 (bounded drain)).
    # The drain used to materialise the ENTIRE prefix in memory inside the single-flight lock, so a backlog
    # — precisely the situation the outbox exists to survive — could OOM or stall the tick, making the relay
    # fail hardest
    # exactly when it was needed most. The remainder drains on the next tick; oldest-first means nothing
    # starves behind a steady arrival rate. 0 = unbounded (the old behavior; not recommended).
    outbox_drain_limit: int = Field(default=500, alias="LINEAGE_OUTBOX_DRAIN_LIMIT")

    # --- Secret consumption from the Dapr secret store (OpenBao) — the audit's 'wired but never read' /
    # 'plaintext still ships' fix, symmetric with the catalog. When on, the S3 secret (reconcile reads the
    # real Lance file with it) and the AGE DB password come from the store at boot, NOT plaintext env: the
    # chart omits both from pod env. apply_lineage_secrets() splices them in (over the shared
    # service_kit.governed.secrets.apply_dapr_secrets seam) and fails closed on the S3 secret.
    secrets_from_dapr: bool = Field(default=False, alias="LINEAGE_SECRETS_FROM_DAPR")
    #: THE ONE STORE, NAMED ONCE (DUP-17). The estate runs a single Dapr secret-store component and
    #: seven env vars named it, each defaulting to the same literal and none of them set by the chart —
    #: so repointing the store meant finding all seven. `RASK_SECRET_STORE` is the estate-wide name
    #: (already what viewer and ingest read); the per-service alias stays FIRST so a single service can
    #: still be moved on its own.
    dapr_secret_store: str = Field(default="lance-secrets", validation_alias=AliasChoices("LINEAGE_DAPR_SECRET_STORE", "RASK_SECRET_STORE"))
    dapr_secret_key: str = Field(default="lance", alias="LINEAGE_DAPR_SECRET_KEY")
    dapr_secret_s3_field: str = Field(default="rustfs-secret-key", alias="LINEAGE_DAPR_SECRET_S3_FIELD")
    dapr_secret_db_field: str = Field(default="postgres-password", alias="LINEAGE_DAPR_SECRET_DB_FIELD")

    # --- Durable /events feed retention — keep at most this many most-recent rows in public.lineage_events
    # (older rows pruned on ingest). 0 = unbounded. The feed is a secondary projection of the AGE graph
    # (the authoritative provenance), so capping it bounds the high-volume log without losing lineage.
    events_retention: int = Field(default=20000, ge=0, alias="LINEAGE_EVENTS_RETENTION")

    # --- Run-node retention (§4) — prune graph :Run nodes older than this many days on each reconcile
    # sweep (under its cluster-wide lock). 0 = off (the dev/demo default: keep full provenance). Pruning a
    # run deletes its WROTE edges — per-version schema/stats history goes with it, which is what retention
    # means; the next sweep back-fills a fresh reconcile run for any dataset left without a versioned edge.
    run_retention_days: int = Field(default=0, ge=0, alias="LINEAGE_RUN_RETENTION_DAYS")

    # --- Read/access audit (#6) — record WHO READ which dataset on the gated read endpoints (an access
    # log, complementing the write provenance in the graph). Off by default; needs an authenticated subject,
    # so it is effectively a no-op unless OIDC is on. Best-effort: an audit-write failure never fails a read.
    read_audit_enabled: bool = Field(default=False, alias="LINEAGE_READ_AUDIT_ENABLED")

    # Compliance audit trail (#41): gate the dedicated `lance.audit` stream (the DLQ-replay record) exactly
    # like the catalog — the SHARED LANCE_AUDIT_ENABLED env (not a LINEAGE_* twin), so one flag governs the
    # estate's compliance posture. Default on; without the lifespan's configure_audit call the stream is
    # silently OFF (the logger inherits root WARNING and the INFO records drop before any handler).
    audit_enabled: bool = Field(default=True, alias="LANCE_AUDIT_ENABLED")

    # Serve /docs + /openapi.json (default on for dev; prod sets false, like the catalog's LANCE_REST_DOCS).
    #: OFF by default. It defaulted to True and NO deployment path ever set it — `grep -rn DOCS
    #: chart/ .docker/ scripts/` matched nothing — so the flag documented a choice nobody was making
    #: and the schemas shipped openly. A security default every deployment must remember to disable is
    #: one nobody disables. Turn it on per-environment (the chart's dev values do).
    docs_enabled: bool = Field(default=False, alias="LINEAGE_DOCS")


@lru_cache
def get_settings() -> LineageSettings:
    """Return the process-wide cached lineage settings."""
    return LineageSettings()


def _with_db_password(url: str, password: str) -> str:
    """Return ``url`` with its userinfo password set to ``password`` — so the AGE password comes from the
    secret store, not a plaintext connection string in pod env (the chart ships the URL password-less)."""
    parts = urlsplit(url)
    userinfo = f"{parts.username or ''}:{quote(password, safe='')}"
    host = parts.hostname or ""
    netloc = f"{userinfo}@{host}{f':{parts.port}' if parts.port else ''}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def apply_lineage_secrets(settings: LineageSettings) -> None:
    """The shared store consumption PLUS the one field only lineage needs: the AGE DB password.

    The S3 half is ``service_kit.governed.secrets.apply_dapr_secrets`` — the estate's one
    implementation (DUP-09) — and this reads the DB password off the SAME bundle rather than issuing a
    second fetch. Named for the service rather than sharing the seam's name so a reader can tell at the
    call site which one they are looking at.

    The DB password is spliced only when present: the chart ships a password-less URL, so a missing
    password leaves an unusable DSN that ``pool.open()`` rejects — still fail-closed. Both halves are
    no-ops when ``secrets_from_dapr`` is off, and the import is lazy so dev/tests never pull the
    dependency in.
    """
    from service_kit.governed.secrets import apply_dapr_secrets

    bundle = apply_dapr_secrets(settings)
    db_password = bundle.get(settings.dapr_secret_db_field)
    if db_password:
        settings.database_url = _with_db_password(settings.database_url, db_password)


def declared_columns_map(settings: LineageSettings) -> dict[str, list[str]]:
    """The parsed dataset→declared-columns map — fail-safe: malformed JSON logs and yields {} (the
    contract check silently OFF beats a lineage service that won't boot over a values typo)."""
    import json as _json
    import logging as _logging

    if not settings.declared_columns:
        return {}
    try:
        raw = _json.loads(settings.declared_columns)
        return {str(dataset): [str(c) for c in columns] for dataset, columns in raw.items() if isinstance(columns, list)}
    except Exception as exc:
        _logging.getLogger(__name__).warning("declared_columns_unparseable", extra={"error": str(exc)})
        return {}


def storage_options(settings: LineageSettings) -> dict[str, str]:
    """Object-store options for reading Lance datasets directly (reconcile #23, demo peek).

    The same S3-compatible config the catalog writes with, so the lineage service reads the *actual*
    on-disk version to cross-check the graph. Empty strings let the object-store client fall back to
    its default credential chain (e.g. real AWS) when the ``LINEAGE_S3_*`` env vars are unset.
    """
    return lance_storage_options(
        settings.s3_endpoint or "",
        settings.s3_access_key_id or "",
        settings.s3_secret_access_key.get_secret_value(),
        settings.s3_region,
    )
