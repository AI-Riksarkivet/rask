"""Medallion service settings (pydantic-settings, ``MEDALLION_*`` env vars).

The movers run the **same** module (``medallion.mover:app``) and differ only by env — each is one stage
edge of the DAG (from-dataset → to-dataset, subscribe-topic → publish-topic). The producer
(``medallion.producer:app``) reads its own ``MEDALLION_BRONZE_*`` / producer fields. Both publish through
the shared Dapr ``pubsub.jetstream`` component the catalog/lineage already use.

R23: raw is NOT a governed tier — it is the external world (image APIs, external object
storage). The governed medallion is exactly bronze → silver → gold; the producer harvests external raw
and writes BRONZE directly (the bronze ingest head), so there is no raw dataset, raw topic, or
raw-to-bronze mover anywhere in this config.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.control_events import CONTROL_TOPIC
from service_kit.lakehouse.objectfs import lance_storage_options

# Re-exported, NOT redefined. The bronze naming convention is shared with the ingest plane, so its one
# definition lives in service-kit beside `is_safe_project`; every existing `from medallion.core.config
# import project_namespace` keeps working.
from service_kit.lakehouse.warehouse_registry import project_namespace as project_namespace


class MedallionSettings(BaseSettings):
    """Config for one medallion service (a mover stage, or the medallion-producer producer)."""

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")

    # --- shared Dapr wiring (same component + lineage topic as catalog/lineage) -----------------
    pubsub: str = Field(default="lineage-pubsub", alias="MEDALLION_PUBSUB")
    lineage_topic: str = Field(default="lineage.events.v1", alias="MEDALLION_LINEAGE_TOPIC")
    # #4: durable object-store outbox for lineage events — stage each event here BEFORE the fire-and-forget
    # publish so a crash between the Lance commit and the publish can't lose it (the lineage relay drains any
    # survivor, idempotent on run_id). Empty = disabled (plain publish, pre-#4). A shared prefix both the
    # movers and the lineage service can reach, e.g. ``s3://<bucket>/_lineage_outbox``.
    lineage_outbox_uri: str = Field(default="", alias="MEDALLION_LINEAGE_OUTBOX_URI")
    # Bound every Dapr publish so a hung sidecar raises TimeoutError → the mover's RETRY path fires (the
    # handler contract expects a prompt return), instead of pinning the worker until the ack window lapses.
    publish_timeout_seconds: float = Field(default=5.0, gt=0, alias="MEDALLION_PUBLISH_TIMEOUT_SECONDS")
    job_namespace: str = Field(default="lance-medallion", alias="MEDALLION_JOB_NAMESPACE")
    # Behind a Dapr sidecar? — when true, a mover fails closed at boot if the app-token is unset (its
    # /medallion-event route would otherwise be an open forged-trigger path). Symmetric with the lineage
    # service. Off in dev (no sidecar); the producer's plain-HTTP /produce carries no such route.
    dapr_enabled: bool = Field(default=False, alias="MEDALLION_DAPR_ENABLED")
    # Serve /docs + /openapi.json (default on for dev; prod sets false, like the catalog's LANCE_REST_DOCS).
    docs_enabled: bool = Field(default=True, alias="MEDALLION_DOCS")
    # Compliance audit trail (#41): gate the dedicated `lance.audit` stream (the /produce + /train admin-door
    # decisions) exactly like the catalog — the SHARED LANCE_AUDIT_ENABLED env (not a MEDALLION_* twin), so
    # one flag governs the estate's compliance posture. Default on; without the producer lifespan's
    # configure_audit call the stream is silently OFF (the logger inherits root WARNING).
    audit_enabled: bool = Field(default=True, alias="LANCE_AUDIT_ENABLED")
    # #84 per-tenant medallion routing (opt-in): the catalog's warehouse-registry/control root (the
    # LANCE_CONTROL_ROOT / LANCE_REST_ROOT value, e.g. ``s3://<bucket>``). When set, a ``project``-carrying
    # /produce request or stage trigger resolves that project's ACTIVE warehouse root off the registry
    # (``service_kit.lakehouse.warehouse_registry``) and the stage reads/writes ``<root>/medallion/<namespace>`` instead of
    # the env URIs. Empty (default) = resolution DISABLED: a project-carrying trigger is DROPPED (fail
    # closed — never a fallback to the shared roots), and every project-less path stays byte-identical.
    control_root: str = Field(default="", alias="MEDALLION_CONTROL_ROOT")
    # Gold SERVING warehouse (DECISIONS "Medallion tiers — hybrid physical layout", opt-in): when set, a
    # ``project``-carrying trigger's TARGET root becomes the project's gold serving warehouse (the registry
    # record carrying ``"serving": "gold"``) when one exists — the chart wires this env ONLY onto the
    # terminal silver→gold mover (medallion.goldWarehouse), so bronze/silver stay in the work
    # warehouse. Absent gold warehouse or flag off → byte-identical work-warehouse behavior; the
    # projectless path never retargets (it has no registry resolution at all).
    gold_warehouse_enabled: bool = Field(default=False, alias="MEDALLION_GOLD_WAREHOUSE_ENABLED")

    # --- mover stage config (the movers share medallion.mover:app, differ only by these; defaults
    # describe the first edge of the governed cascade, bronze→silver) ----------------------------
    from_dataset: str = Field(default="bronze$events", alias="MEDALLION_FROM_DATASET")
    from_namespace: str = Field(default="bronze", alias="MEDALLION_FROM_NAMESPACE")
    to_dataset: str = Field(default="silver$features", alias="MEDALLION_TO_DATASET")
    to_namespace: str = Field(default="silver", alias="MEDALLION_TO_NAMESPACE")
    operation: str = Field(default="embed_features", alias="MEDALLION_OPERATION")
    author: str = Field(default="data_eng", alias="MEDALLION_AUTHOR")
    sub_topic: str = Field(default="medallion.bronze", alias="MEDALLION_SUB_TOPIC")
    pub_topic: str = Field(default="", alias="MEDALLION_PUB_TOPIC")  # "" = terminal stage (gold)
    # Dead-letter topic for this app's subscriptions (Dapr-native DLQ - docs/RESILIENCE.md gap #2).
    # "" (default) = no DLQ declared, exactly the pre-existing behavior. MUST ship together with the
    # chart's Dapr Resiliency retry policy (dapr.resiliency.enabled wires both): a deadLetterTopic
    # WITHOUT a retry policy dead-letters on the FIRST failure (Dapr documented default), which would
    # replace the chaos-verified redelivery-with-backoff behavior with instant parking.
    dlq_topic: str = Field(default="", alias="MEDALLION_DLQ_TOPIC")
    # Declared consumer dependencies for THIS stage's output (data-contract gap #1): comma-separated
    # column names downstream consumers read. Empty (default) = no declaration, no new assertion.
    # When set, the quality gate adds a `column_declared` assertion per name — a promotion that
    # dropped/renamed a declared column is BLOCKED (the write itself still commits; audited FAIL run).
    quality_required_columns: str = Field(default="", alias="MEDALLION_REQUIRED_COLUMNS")
    # Ingest ceilings (audit 2026-07-12): the media ingest refuses (400) rather than OOM when a
    # source prefix exceeds these. Defaults generous for the demo; tune per deployment.
    ingest_max_objects: int = Field(default=10_000, alias="MEDALLION_INGEST_MAX_OBJECTS")
    ingest_max_total_bytes: int = Field(default=1 << 30, alias="MEDALLION_INGEST_MAX_TOTAL_BYTES")
    # Streaming chunk size (objects per Lance commit): memory high-water ≈ one chunk + URI strings.
    ingest_chunk_objects: int = Field(default=64, alias="MEDALLION_INGEST_CHUNK_OBJECTS")
    # The BYTE bound is the real memory guarantee (a chunk flushes on count OR bytes, whichever
    # first); the count bound is fragment hygiene for many tiny objects. Not a Dapr/NATS number —
    # the bus never carries payload bytes.
    ingest_chunk_bytes: int = Field(default=64 << 20, alias="MEDALLION_INGEST_CHUNK_BYTES")

    # --- Optional FGA gate (ReBAC enforcement) — the mover checks it is AUTHORIZED to produce the target
    # stage before emitting. The silver→gold mover checks `can_promote` (validator-only); the others check
    # `can_create_table` (writer). It checks as its own service identity, so a mover not granted the role
    # is DENIED — the cascade then ENFORCES the model, not just describes it. Off by default. -------------
    fga_enabled: bool = Field(default=False, alias="MEDALLION_FGA_ENABLED")
    fga_api_url: str = Field(default="http://openfga:8080", alias="MEDALLION_FGA_API_URL")
    # Pinned store/model ids (production posture, same as catalog/lineage): set → no boot-time provision,
    # no model rewrite, read-only OpenFGA access suffices. Unset (dev/e2e) → provision-by-name as before.
    fga_store_id: str = Field(default="", alias="MEDALLION_FGA_STORE_ID")
    fga_model_id: str = Field(default="", alias="MEDALLION_FGA_MODEL_ID")
    fga_timeout_seconds: float = Field(default=5.0, alias="MEDALLION_FGA_TIMEOUT_SECONDS")
    # BARE subject (no ``user:`` prefix) — ``service_kit.governed.fga.check`` adds ``user:`` itself, so ``user:service-*``
    # here would double-prefix (``user:user:service-*``) and the gate would always deny. Matches the
    # catalog's convention (it passes the bare OIDC sub to fga.check).
    fga_service_identity: str = Field(default="service-mover", alias="MEDALLION_FGA_SERVICE_IDENTITY")
    fga_required_action: str = Field(default="can_create_table", alias="MEDALLION_FGA_REQUIRED_ACTION")

    # --- Produce-trigger admin auth (#64): ``/produce`` accepts EITHER the Dapr app-api-token (service-to-
    # service, unchanged) OR a signed-in OIDC user who is a project admin (``can_administer``). The OIDC path
    # lets the web UI trigger produce WITHOUT the web pod ever holding the service token — the human/external
    # door, mirroring the catalog's OIDC verifier. Off by default; enabled with an issuer + audience. --------
    oidc_enabled: bool = Field(default=False, alias="MEDALLION_OIDC_ENABLED")
    oidc_issuer: str | None = Field(default=None, alias="MEDALLION_OIDC_ISSUER")
    # Split-horizon fetch location (see the catalog twin): empty = derive from the issuer.
    oidc_discovery_url: str | None = Field(default=None, alias="MEDALLION_OIDC_DISCOVERY_URL")
    oidc_audience: str | None = Field(default=None, alias="MEDALLION_OIDC_AUDIENCE")
    oidc_cache_ttl: int = Field(default=3600, alias="MEDALLION_OIDC_CACHE_TTL")
    oidc_leeway: int = Field(default=60, alias="MEDALLION_OIDC_LEEWAY")
    oidc_allow_insecure: bool = Field(default=False, alias="MEDALLION_OIDC_ALLOW_INSECURE")
    # The project a trigger-ing user must administer — the gate is ``can_administer`` on ``project:<this>``.
    produce_admin_project: str = Field(default="acme", alias="MEDALLION_PRODUCE_ADMIN_PROJECT")

    def fga_object(self, to_namespace: str | None = None) -> str:
        """The FGA object the mover must be authorized on — the target stage namespace.

        ``to_namespace`` overrides the env value for the per-project path (#84), where the target is the
        project-QUALIFIED namespace (``acme-bronze``) and needs its own FGA tuples; default ``None`` keeps
        today's fixed single-tenant object byte-identically.
        """
        return f"namespace:{to_namespace or self.to_namespace}"

    # --- Fake-Ray in-process compute (the medallion-producer SEAM) — OFF by default (movers stay dummy-emitters).
    # When on, each stage does a REAL Lance write: the producer seeds bronze$events; each mover reads its
    # upstream Lance dataset, applies a stage transform, and writes the downstream one — so the emitted
    # lineage carries the REAL version and the whole event-driven loop produces actual versioned data, not
    # just provenance. Same read→transform→write→version contract a distributed Ray Data job fills at rask;
    # here it runs in-process so the loop is end-to-end testable without a Ray cluster. (#25 / P1 #6 seam.)
    compute_enabled: bool = Field(default=False, alias="MEDALLION_COMPUTE_ENABLED")
    from_uri: str = Field(default="", alias="MEDALLION_FROM_URI")  # upstream Lance dataset (mover input)
    to_uri: str = Field(default="", alias="MEDALLION_TO_URI")  # downstream Lance dataset (mover output)

    # --- EVENT-DRIVEN real-Ray compute (opt-in, requires compute_enabled). When on, the mover submits its
    # stage transform as a `ray job submit` to the ray-lance cluster (via the Ray Jobs REST API, httpx-only —
    # no ray package in the mover image) IN RESPONSE TO its Dapr trigger, instead of the in-process fake-Ray
    # write. The submitted job (scripts/ray_stage_job.py, baked in the ray-lance image) reads upstream, stamps
    # the stage column across Ray workers, and writes downstream at 2.2 + stable row ids; the mover then reads
    # the written version/stats for the same lineage emit. OFF by default — fake-Ray stays the default path.
    # This is the production shape (KubeRay RayCluster at the rask merge; a raw Ray head on kind here).
    ray_enabled: bool = Field(default=False, alias="MEDALLION_RAY_ENABLED")
    ray_address: str = Field(default="http://ray-lance-head:8265", alias="MEDALLION_RAY_ADDRESS")
    ray_entrypoint: str = Field(default="python /home/ray/jobs/ray_stage_job.py", alias="MEDALLION_RAY_ENTRYPOINT")
    #: THE WORKLOAD'S OWN PARAMETERS — the channel that makes ``ray_entrypoint`` usable.
    #:
    #: A per-lane JSON object, forwarded into the job's ``runtime_env.env_vars`` under the
    #: ``RASK_PARAM_`` prefix. Without it a mover row can name a workload's entrypoint and then has no
    #: way to configure it: the submit built a FIXED env dict, so a second workload either reused the
    #: first one's variables or required a platform edit — which is exactly the coupling the agnostic
    #: ruling forbids.
    #:
    #: NAMESPACED ON PURPOSE. The prefix is what keeps this a workload channel rather than a hole in
    #: the platform: a lane cannot reach ``S3_SECRET``, ``LINEAGE_JSON`` or an ``OTEL_*`` key by
    #: choosing a colliding name, because every key it supplies is rewritten before it is sent.
    #:
    #: The platform never reads these values and never validates them beyond "it is a JSON object of
    #: strings". Their meaning belongs to the workload, which is the whole point.
    ray_job_params: dict[str, str] = Field(default_factory=dict, alias="MEDALLION_RAY_JOB_PARAMS")
    # Where a mover REGISTERS its output table — the catalog service, and the
    # catalog's own connection root so the location can be expressed RELATIVELY (the dir backend
    # refuses absolute URIs — the #75 lesson). Both empty by default: the lane fails at the
    # register seam naming the env var, never guessing — a gold table the catalog cannot govern
    # must not report success.
    catalog_url: str = Field(default="", alias="MEDALLION_CATALOG_URL")
    catalog_root: str = Field(default="", alias="MEDALLION_CATALOG_ROOT")
    # Optional bearer for auth-enabled catalogs (mirrors the annotator's MEDIA_CATALOG_TOKEN
    # pattern; the OpenBao/Dapr secret flow is the production source — this is the pinned override).
    catalog_token: str | None = Field(default=None, alias="MEDALLION_CATALOG_TOKEN")
    # The catalog id delimiter (`gold$catalog`) — matches LANCE_DELIMITER's default, same rationale as
    # MAINTENANCE_DELIMITER: a mismatch addresses a DIFFERENT table rather than failing.
    delimiter: str = Field(default="$", alias="MEDALLION_DELIMITER")
    ray_request_timeout_seconds: float = Field(default=10.0, ge=0.1, alias="MEDALLION_RAY_REQUEST_TIMEOUT_SECONDS")
    ray_poll_interval_seconds: float = Field(default=2.0, gt=0, alias="MEDALLION_RAY_POLL_INTERVAL_SECONDS")
    # The mover BLOCKS its Dapr handler until the job finishes. Redelivery is safe (the submission id is
    # deterministic per (stage, token), so a redelivered trigger re-attaches to the same job — not a second
    # one), but a job that outlives the trigger stream's ack window (dapr-component backOff first value, 30s)
    # WILL be concurrently redelivered; that only wastes a duplicate poll (re-attach), it does not double-run
    # the job. For jobs expected to run much longer than the ack window, raise both together.
    ray_job_timeout_seconds: float = Field(default=180.0, gt=0, alias="MEDALLION_RAY_JOB_TIMEOUT_SECONDS")

    # --- Optional quality GATE — when on, after the compute writes the downstream dataset the mover runs
    # data-quality assertions on it (row_count > 0, key column non-null) and BLOCKS promotion on a failure:
    # the failed run + its dataQualityAssertions facet are still emitted (auditable in lineage), but the next
    # stage is NOT triggered, so a bad batch can't cascade. The automated *validator* half of governance —
    # FGA decides who MAY promote, quality decides if the DATA is good enough to. Requires compute (there is
    # no data to check otherwise). Off by default. ---------------------------------------------------------
    quality_enabled: bool = Field(default=False, alias="MEDALLION_QUALITY_ENABLED")
    quality_key_column: str = Field(default="id", alias="MEDALLION_QUALITY_KEY_COLUMN")

    # --- S3 access for the fake-Ray compute (only used when compute_enabled). Empty creds let the
    # object-store client fall back to its default chain (or a local path needs no creds at all). ---------
    s3_endpoint: str = Field(default="", alias="MEDALLION_S3_ENDPOINT")
    s3_access_key_id: str = Field(default="", alias="MEDALLION_S3_ACCESS_KEY_ID")
    # SecretStr so it's redacted in repr/model_dump (parity with the catalog) — .get_secret_value() to read.
    s3_secret_access_key: SecretStr = Field(default=SecretStr(""), alias="MEDALLION_S3_SECRET_ACCESS_KEY")
    # --- Secret consumption from the Dapr secret store (OpenBao) — symmetric with catalog + lineage +
    # compaction (Batch 7, 2026-07-11: the movers/producer were the LAST real S3 consumers still shipping
    # the key in plaintext pod env). When on, the S3 secret comes from the store at boot as the STRICT
    # sole source (the chart omits the plaintext env entirely); a store miss FAILS CLOSED — an env
    # fallback would contradict "OpenBao is the sole source" (the audit's original finding).
    secrets_from_dapr: bool = Field(default=False, alias="MEDALLION_SECRETS_FROM_DAPR")
    dapr_secret_store: str = Field(default="lance-secrets", alias="MEDALLION_DAPR_SECRET_STORE")
    dapr_secret_key: str = Field(default="lance", alias="MEDALLION_DAPR_SECRET_KEY")
    dapr_secret_s3_field: str = Field(default="rustfs-secret-key", alias="MEDALLION_DAPR_SECRET_S3_FIELD")
    s3_region: str = Field(default="us-east-1", alias="MEDALLION_S3_REGION")

    @property
    def required_column_list(self) -> list[str]:
        """The declared consumer-dependency columns (parsed from the comma-separated env)."""
        return [c.strip() for c in self.quality_required_columns.split(",") if c.strip()]

    def storage_options(self) -> dict[str, str]:
        """Lance ``storage_options`` for the compute write — empty for a local path; S3 config otherwise."""
        if not self.s3_endpoint:
            return {}
        return lance_storage_options(
            self.s3_endpoint,
            self.s3_access_key_id,
            self.s3_secret_access_key.get_secret_value(),
            self.s3_region,
        )

    @model_validator(mode="after")
    def _compute_needs_s3_secret(self) -> Self:
        """Fail fast when compute writes to S3 but the secret is missing — else Lance 403s cryptically.

        Only fires when the plaintext env is the SOURCE: with ``secrets_from_dapr`` the chart deliberately
        withholds the plaintext secret and the lifespan fetches it from OpenBao fail-closed
        (``apply_dapr_secrets``, the same shape as catalog/lineage/compaction) — a guard here would crash
        exactly the compute+OpenBao combo the chart renders (audit 2026-07-15). Surface a genuinely
        credential-less deploy at boot instead of at first produce.
        """
        if self.compute_enabled and self.s3_endpoint and not self.secrets_from_dapr and not self.s3_secret_access_key.get_secret_value():
            raise ValueError(
                "MEDALLION_COMPUTE_ENABLED with an S3 endpoint but no MEDALLION_S3_SECRET_ACCESS_KEY and no "
                "MEDALLION_SECRETS_FROM_DAPR — every Lance write would 403. Provide the secret, or enable "
                "the OpenBao store path (helm default when openbao.enabled=true)."
            )
        if self.ray_enabled and not self.compute_enabled:
            # ray submits the STAGE transform (from_uri -> to_uri); with compute off there is no data path and
            # the mover would silently fall through to the dummy version-1 emit. Fail fast, don't degrade.
            raise ValueError(
                "MEDALLION_RAY_ENABLED requires MEDALLION_COMPUTE_ENABLED — the Ray path submits the stage's "
                "read->transform->write, which the compute config (from/to URIs + S3) provides."
            )
        return self

    # --- producer (medallion-producer) config — the BRONZE ingest head (R23: writes the first governed tier
    # directly; raw is the external world it harvests FROM) --------------------------------------
    bronze_dataset: str = Field(default="bronze$events", alias="MEDALLION_BRONZE_DATASET")
    bronze_namespace: str = Field(default="bronze", alias="MEDALLION_BRONZE_NAMESPACE")
    bronze_uri: str = Field(default="", alias="MEDALLION_BRONZE_URI")  # where the producer seeds bronze$events (compute)
    stage_base_uri: str = Field(default="", alias="MEDALLION_STAGE_BASE_URI")
    """The medallion (project) bucket base ``s3://<bucket>/medallion`` where the STAGE datasets (bronze/
    silver) and the model registry live. The trainer resolves feature + model URIs from HERE, not from
    ``bronze_uri`` — so it stays correct when gold (sink) is zoned into its OWN bucket. Empty → derive
    from ``bronze_uri`` (the single-bucket default, unchanged)."""
    producer_operation: str = Field(default="lance_ray_ingest", alias="MEDALLION_PRODUCER_OPERATION")
    producer_author: str = Field(default="ray", alias="MEDALLION_PRODUCER_AUTHOR")
    bronze_topic: str = Field(default="medallion.bronze", alias="MEDALLION_BRONZE_TOPIC")

    # The catalog's control channel, where `table_published` lands (§ D2 B8). Empty DISABLES the
    # publication head, which is what keeps this additive: a deployment that has not enabled the
    # catalog's control emitter registers no subscription and behaves exactly as before.
    control_pubsub: str = Field(default="", alias="MEDALLION_CONTROL_PUBSUB")
    # The topic name is IMPORTED, never re-typed: `catalog.control.v1` is a cross-plane versioned
    # contract owned by its producer's model module, and both other consumers (catalog, maintenance)
    # already import the same constant. A second literal is a second source of truth that a rename
    # would silently leave behind — this subscription would keep listening to a topic nobody publishes.
    control_topic: str = Field(default=CONTROL_TOPIC, alias="MEDALLION_CONTROL_TOPIC")
    # --- Ray TRAIN head (#115a, docs/RAY-TRAIN.md): OWN topic (D1 — long-running, terminal-on-failure;
    # never a field on the stage trigger) + submit-and-ack consumer (D2). The trainer has its OWN service
    # identity + rung (D5): reader on the feature namespaces, writer on namespace:<models> ONLY.
    train_topic: str = Field(default="training.jobs", alias="MEDALLION_TRAIN_TOPIC")
    train_entrypoint: str = Field(
        # Must match where ray-lance.dockerfile bakes the jobs (/home/ray/jobs/, like ray_entrypoint).
        default="python /home/ray/jobs/ray_train_job.py",
        alias="MEDALLION_TRAIN_ENTRYPOINT",
    )
    trainer_identity: str = Field(default="service-trainer", alias="MEDALLION_TRAINER_IDENTITY")
    models_namespace: str = Field(default="models", alias="MEDALLION_MODELS_NAMESPACE")
    # The lineage HTTP ingest the TRAINING JOB posts its own RunEvents to (D2/D3: Ray pods carry no
    # Dapr sidecar, so the job emits over plain HTTP like medallion_demo). Default = the chart's
    # in-cluster service for the standard `lance-ns` release (same convention as ray_address); set ""
    # to disable emission, e.g. in environments without the lineage service.
    train_lineage_url: str = Field(default="http://lance-ns-lineage:8000", alias="MEDALLION_TRAIN_LINEAGE_URL")

    # --- media ingest head (multimodal §9) — POST /ingest-media lands external media as bronze blobs and
    # triggers the media chain. The head reads an external S3 source prefix through the provider-agnostic
    # SourceAdapter seam (a real deployment points bucket/prefix at an image API / GCS / HF export; the demo seeds
    # sample PNGs there first). Requires compute (there is no dummy media): media_bronze_uri is where the
    # bronze blob-v2 table lands; the trigger on media_topic drives the media mover (bronze -> silver
    # derive). Empty media_bronze_uri = the media head is off (POST /ingest-media -> 409). ----------------
    # The media lane gets its OWN namespaces (bronze-media → silver-media): the chart derives each
    # mover's from/to URI from the NAMESPACE (s3://<bucket>/medallion/<namespace>), so reusing the events
    # chain's bronze/silver would collide with bronze$events / silver$features on the same paths.
    media_source_bucket: str = Field(default="", alias="MEDALLION_MEDIA_SOURCE_BUCKET")
    media_source_prefix: str = Field(default="media-src/batch", alias="MEDALLION_MEDIA_SOURCE_PREFIX")
    media_bronze_uri: str = Field(default="", alias="MEDALLION_MEDIA_BRONZE_URI")
    media_bronze_dataset: str = Field(default="bronze-media$objects", alias="MEDALLION_MEDIA_BRONZE_DATASET")
    media_bronze_namespace: str = Field(default="bronze-media", alias="MEDALLION_MEDIA_BRONZE_NAMESPACE")
    media_topic: str = Field(default="medallion.media", alias="MEDALLION_MEDIA_TOPIC")
    # Seed two deterministic sample PNGs into the source prefix before ingesting (the demo's stand-in for
    # an external media drop). Prod points the bucket/prefix at real media and turns this off.
    media_seed_samples: bool = Field(default=True, alias="MEDALLION_MEDIA_SEED_SAMPLES")

    # The Ray branch (requires ray_enabled): the self-contained harvest job baked into the unified ray
    # image, submitted via the Ray Jobs REST API exactly like the stage transforms.


@lru_cache
def get_settings() -> MedallionSettings:
    """The process-wide medallion settings (read once from env)."""
    return MedallionSettings()


def apply_dapr_secrets(settings: MedallionSettings) -> None:
    """Consume the S3 secret from the Dapr secret store (OpenBao) and set it on ``settings`` in place.

    When ``secrets_from_dapr`` is on the store is the STRICT sole source: a store miss FAILS CLOSED
    (raises), never falling back to a plaintext env value — the chart ships none, and silently using
    one would contradict 'OpenBao is the sole source'. No-op (and no fetch) when off. Symmetric with
    ``compaction.core.config.apply_dapr_secrets`` / lineage / the catalog lifespan. SYNC by design
    (the fetch retries while the store seeds) — lifespans call it via ``run_in_threadpool``.
    """
    if not settings.secrets_from_dapr:
        return
    from service_kit.governed.secrets import fetch_required_secrets

    bundle = fetch_required_secrets(settings.dapr_secret_store, settings.dapr_secret_key, require=settings.dapr_secret_s3_field)
    settings.s3_secret_access_key = SecretStr(bundle[settings.dapr_secret_s3_field])
