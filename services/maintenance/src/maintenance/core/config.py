"""Maintenance service settings (pydantic-settings, ``MAINTENANCE_*`` env vars)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.lakehouse.objectfs import lance_storage_options


class MaintenanceSettings(BaseSettings):
    """Config for the table-maintenance service + its S3 access to the lakehouse buckets."""

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")

    # #102: the shared Lance session's cache caps. Defaults sized for the pod tier (512Mi limit —
    # Lance's own defaults are 1 GiB metadata + 6 GiB index PER OPEN, which is the defect).
    lance_metadata_cache_mb: int = Field(default=128, ge=8, alias="MAINTENANCE_LANCE_METADATA_CACHE_MB")
    lance_index_cache_mb: int = Field(default=256, ge=8, alias="MAINTENANCE_LANCE_INDEX_CACHE_MB")

    # The Dapr cron binding name == the POST route the sidecar delivers ticks to (must match the
    # bindings.cron Component's metadata.name). Default matches the chart.
    binding_name: str = Field(default="maintenance-cron", alias="MAINTENANCE_BINDING_NAME")
    # The reconciler's OWN cron binding, separate from the sweep's. Two bindings rather than one route
    # doing both because they have genuinely different cadences: the sweep rewrites data files and is
    # expensive, while the drift report only reads three stores and is cheap enough to run often. One
    # binding would force the cheap read to inherit the expensive write's schedule.
    reconcile_binding_name: str = Field(default="maintenance-reconcile-cron", alias="MAINTENANCE_RECONCILE_BINDING_NAME")
    # Serve /docs + /openapi.json (default on for dev; prod sets false, like the catalog's LANCE_REST_DOCS).
    docs_enabled: bool = Field(default=True, alias="MAINTENANCE_DOCS")
    # Datasets whose newest version is older than this are eligible for version GC (keep recent history).
    # ge=1 (not 0): timedelta(0) is falsy, so pylance collapses `older_than` to None and silently drops the
    # threshold — to GC aggressively, use a small positive value, not 0.
    older_than_days: int = Field(default=7, ge=1, alias="MAINTENANCE_OLDER_THAN_DAYS")

    # --- The compaction READ bound (#93). Two knobs, because the memory is their PRODUCT and bounding
    # one alone bounds nothing.
    #
    # `scan_batch_size` shipped as a per-tier policy field with no global default, so out of the box
    # `compact_files` used Lance's own 8192-ROW batch. Rows are not a unit of memory: against ~1.8 MB
    # bronze page-image rows (measured) that is ~15 GB per compute thread, and the chart runs this
    # sweep every 120s over the medallion buckets with `maintenance.enabled: true` and no policy
    # anywhere. The pod names no `resources` tier, so it inherits `resources.default` — a 512Mi LIMIT.
    # A 15 GB read is a 30x overshoot of the whole container; the first blob-tier tick OOM-kills it,
    # and a maintenance pod in CrashLoopBackOff means NOTHING is being maintained.
    #
    # 64 rows x ~1.8 MB x 2 threads is ~230 MB worst case, which fits 512Mi with room for the
    # process itself. On tiers where rows are small this is slower than Lance's default and that is
    # the deliberate trade: slow compaction is recoverable, an OOM-killed sweep is an outage. Raise
    # both per tier via the maintenance policy (`scan_batch_size`) once a tier's row size is known —
    # that is what the per-tier surface is FOR; this only stops the UNPOLICIED estate killing itself.
    scan_batch_size: int = Field(default=64, ge=1, le=8192, alias="MAINTENANCE_SCAN_BATCH_SIZE")
    # Lance's `num_threads` defaults to the machine's parallelism, which is the HOST's core count, not
    # the pod's `limits.cpu: "1"` — so on a 64-core node the batch bound above would have been
    # multiplied by 64 while the cgroup still allowed one core's worth of work. Pinning it makes the
    # ceiling a number someone can actually compute.
    compact_threads: int = Field(default=2, ge=1, le=64, alias="MAINTENANCE_COMPACT_THREADS")

    # Behind a Dapr sidecar? — when true, boot fails closed if the app-token is unset (the cron route would
    # otherwise be an open forged-sweep path). Symmetric with the lineage service. Off in dev (no sidecar).
    dapr_enabled: bool = Field(default=False, alias="MAINTENANCE_DAPR_ENABLED")

    # --- Lineage emission (opt-in, best-effort) — record a maintenance run on each materially-compacted
    # dataset to the lineage graph via Dapr pub/sub. Publishes to the SAME pubsub component + topic the
    # catalog publishes to and the lineage service subscribes to, so a compaction shows up in producers()
    # next to the writes. Off by default; the sidecar owns retry (no DLQ), so a publish never fails a sweep.
    lineage_emit_enabled: bool = Field(default=False, alias="MAINTENANCE_LINEAGE_EMIT_ENABLED")
    # Bound each Dapr publish so a hung sidecar can't stall a sweep (best-effort → the outage is swallowed).
    publish_timeout_seconds: float = Field(default=5.0, gt=0, alias="MAINTENANCE_PUBLISH_TIMEOUT_SECONDS")
    lineage_pubsub: str = Field(default="lineage-pubsub", alias="MAINTENANCE_LINEAGE_PUBSUB")
    lineage_topic: str = Field(default="lineage.events.v1", alias="MAINTENANCE_LINEAGE_TOPIC")
    lineage_job_namespace: str = Field(default="compaction", alias="MAINTENANCE_LINEAGE_JOB_NAMESPACE")
    # The catalog id delimiter — to derive a dataset's parent namespace from its table id (matches the
    # catalog's LANCE_DELIMITER default). The catalog lays tables out as <uuid>_<table_id>; table_id is the
    # canonical lineage Dataset name == OpenFGA object id, and its parent is all-but-the-last segment.
    delimiter: str = Field(default="$", alias="MAINTENANCE_DELIMITER")

    # --- S3 access to the Lance lakehouse bucket ----------------------------------------------------
    s3_endpoint: str = Field(default="http://localhost:9000", alias="MAINTENANCE_S3_ENDPOINT")
    s3_access_key_id: str = Field(default="rustfsadmin", alias="MAINTENANCE_S3_ACCESS_KEY_ID")
    # Default "" so the chart can omit the plaintext env when the store is the source; apply_dapr_secrets
    # fails closed if neither the store nor env provides it (the audit's secret-consumption fix — the
    # compaction pod is a real S3 consumer (compacts/GCs the lakehouse), so it must NOT ship the key plain).
    # SecretStr so it's redacted in repr/model_dump (parity with the catalog) — .get_secret_value() to read.
    s3_secret_access_key: SecretStr = Field(default=SecretStr(""), alias="MAINTENANCE_S3_SECRET_ACCESS_KEY")
    s3_bucket: str = Field(default="lance-catalog", alias="MAINTENANCE_S3_BUCKET")
    # #50 maintenance policies: where the catalog's policy registry lives (`<root>/_policies/`). Defaults
    # to the primary bucket, which matches the catalog's LANCE_REST_ROOT default — override only when the
    # catalog's control root is moved.
    policy_root: str = Field(default="", alias="MAINTENANCE_POLICY_ROOT")
    # ADDITIONAL buckets to sweep, comma-separated (audit 2026-07-14). The sweep discovered exactly ONE
    # bucket, so every #3-A per-warehouse bucket and #3-B multi-base data bucket was INVISIBLE to GC: their
    # tables accumulated superseded manifest versions and small fragments FOREVER. A storage leak introduced
    # by the very features that create new buckets. The chart wires the medallion zone buckets + any
    # multibase data bases here; per-warehouse buckets are added as they are provisioned.
    s3_extra_buckets: str = Field(default="", alias="MAINTENANCE_S3_EXTRA_BUCKETS")
    s3_region: str = Field(default="us-east-1", alias="MAINTENANCE_S3_REGION")

    @property
    def sweep_buckets(self) -> list[str]:
        """Every bucket the sweep must cover: the primary lakehouse bucket + the extras. De-duplicated and
        order-stable so a bucket listed twice is swept once (and the report stays deterministic)."""
        extras = [b.strip().removeprefix("s3://").strip("/") for b in self.s3_extra_buckets.split(",")]
        return list(dict.fromkeys([self.s3_bucket, *[b for b in extras if b]]))

    # --- OpenFGA (READ-ONLY, for the drift reconciler) -----------------------------------------------
    # The reconciler compares three stores: OpenFGA says WHO, the control-root registries say WHAT
    # EXISTS, object storage holds the BYTES. Without a client here, FOUR of its seven categories
    # (ghost_projects, ghost_warehouses, unreferenced_projects, orphaned_annotation_tasks) can only
    # report UNAVAILABLE — including "today's seeded ghosts", which is the drift that motivated it.
    #
    # This service only ever READS tuples (`fga.read_tuples`); it holds no grant-writing path at all,
    # which is why it needs no model id beyond what pins the store.
    fga_enabled: bool = Field(default=False, alias="MAINTENANCE_FGA_ENABLED")
    fga_api_url: str = Field(default="http://openfga:8080", alias="MAINTENANCE_FGA_API_URL")
    fga_store_id: str | None = Field(default=None, alias="MAINTENANCE_FGA_STORE_ID")
    fga_model_id: str | None = Field(default=None, alias="MAINTENANCE_FGA_MODEL_ID")
    fga_timeout_seconds: float = Field(default=5.0, ge=0.1, alias="MAINTENANCE_FGA_TIMEOUT_SECONDS")
    # The estate root object, excluded from the ghost report BY DESIGN: it carries real tuples and has
    # no registry record, so without this every run would name a known-good finding and train the
    # reader to skip the category. Must match the catalog's LANCE_FGA_ROOT_OBJECT.
    fga_root_object: str = Field(default="warehouse:lance_catalog", alias="MAINTENANCE_FGA_ROOT_OBJECT")

    # The orphan-FILE pass is separately gated because it is a different ORDER of work from the rest
    # of the drift report: the others compare three stores (O(stores)), this opens every dataset and
    # unions the file references of every live version (O(datasets x versions x fragments)) across the
    # DATA buckets. Off by default so the cheap report stays cheap; step 4's policy surface gives it a
    # cadence of its own.
    orphan_scan_enabled: bool = Field(default=False, alias="MAINTENANCE_ORPHAN_SCAN_ENABLED")

    # The reconciler reads the catalog's registries (`_projects/`, `_warehouses/`) off the control root.
    # Defaults to the primary bucket, matching the catalog's own LANCE_CONTROL_ROOT default; override
    # only when the catalog's control root has been moved.
    control_root: str = Field(default="", alias="MAINTENANCE_CONTROL_ROOT")
    # Warehouses are a catalog FEATURE FLAG: with them off the deployment is single-bucket by
    # configuration and an unbound namespace is CORRECT, not drift — so the reconciler skips that
    # category rather than reporting every namespace. Must match the catalog's setting.
    warehouses_enabled: bool = Field(default=False, alias="MAINTENANCE_WAREHOUSES_ENABLED")

    # --- Secret consumption from the Dapr secret store (OpenBao) — symmetric with catalog + lineage.
    # When on, the S3 secret comes from the store at boot (NOT plaintext env); fails closed if absent.
    secrets_from_dapr: bool = Field(default=False, alias="MAINTENANCE_SECRETS_FROM_DAPR")
    dapr_secret_store: str = Field(default="lance-secrets", alias="MAINTENANCE_DAPR_SECRET_STORE")
    dapr_secret_key: str = Field(default="lance", alias="MAINTENANCE_DAPR_SECRET_KEY")
    dapr_secret_s3_field: str = Field(default="rustfs-secret-key", alias="MAINTENANCE_DAPR_SECRET_S3_FIELD")

    @property
    def resolved_policy_root(self) -> str:
        """The policy-registry root — `MAINTENANCE_POLICY_ROOT` or the primary bucket."""
        return self.policy_root or f"s3://{self.s3_bucket}"

    @property
    def resolved_control_root(self) -> str:
        """The catalog's control root (project/warehouse registries) — `MAINTENANCE_CONTROL_ROOT` or
        the primary bucket, which is what the catalog defaults to."""
        return self.control_root or f"s3://{self.s3_bucket}"

    def storage_options(self) -> dict[str, str]:
        """The Lance ``storage_options`` for opening datasets on the (HTTP) S3 endpoint."""
        # Via the shared builder — which also stamps path-style addressing; the hand-rolled copy here had
        # silently dropped it (audit 2026-07-15), leaving the sweep one object-store default away from 403s.
        return lance_storage_options(
            self.s3_endpoint,
            self.s3_access_key_id,
            self.s3_secret_access_key.get_secret_value(),
            self.s3_region,
        )


def shared_lance_session() -> object:
    """The process-wide bounded Lance session (#102). Every maintenance open threads this, so a
    tick's second dataset (and the orphan scan's 500 version checkouts) HIT the cache instead of
    minting and discarding Lance's default 1 GiB + 6 GiB ceilings per open — ceilings that dwarf
    the pod's own 512Mi limit. Caps are LRU soft bounds; session keys carry (uri, version, etag),
    so a compaction bumping a version writes new keys and freshness needs no design."""
    from service_kit.lakehouse.lance_session import lance_session

    settings = get_settings()
    return lance_session(settings.lance_metadata_cache_mb << 20, settings.lance_index_cache_mb << 20)


@lru_cache
def get_settings() -> MaintenanceSettings:
    """The process-wide compaction settings (read once from env)."""
    return MaintenanceSettings()


def apply_dapr_secrets(settings: MaintenanceSettings) -> None:
    """Consume the S3 secret from the Dapr secret store (OpenBao) and set it on ``settings`` in place. When
    ``secrets_from_dapr`` is on the store is the STRICT sole source: a store miss FAILS CLOSED (raises),
    never falling back to a plaintext env value — the chart ships none, and silently using one would
    contradict 'OpenBao is the sole source'. No-op (and no Dapr import) when off. Symmetric with
    lineage.config.apply_dapr_secrets / the catalog lifespan."""
    if not settings.secrets_from_dapr:
        return
    from service_kit.governed.secrets import fetch_required_secrets

    bundle = fetch_required_secrets(settings.dapr_secret_store, settings.dapr_secret_key, require=settings.dapr_secret_s3_field)
    settings.s3_secret_access_key = SecretStr(bundle[settings.dapr_secret_s3_field])
