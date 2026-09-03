"""Maintenance service settings (pydantic-settings, ``MAINTENANCE_*`` env vars).

The OpenFGA knobs are the estate's, not this service's: it mixes in ``FgaSettings`` and reads
``RASK_FGA_*``. ``FgaSettings`` alone rather than ``GovernedAuthSettings`` because this service has no
human door — its routes are gated by the Dapr app token and it only ever READS tuples, as itself.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import FgaSettings
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lakehouse.objectfs import lance_storage_options


if TYPE_CHECKING:
    import lance


class MaintenanceSettings(FgaSettings, BaseSettings):
    """Config for the table-maintenance service + its S3 access to the lakehouse buckets."""

    # `populate_by_name` also teaches the env source the bare FIELD NAME as a second lookup, so
    # every alias below silently gained an un-namespaced twin (MedallionSettings.ray_address
    # answered to Ray's own $RAY_ADDRESS). `env_prefix` redirects that fallback onto the
    # namespace the aliases already declare; an explicit alias bypasses it, so the
    # deliberately-bare ones (DAPR_HTTP_PORT, RAY_DASHBOARD_URL) still land.
    # See tests/unit/test_settings_env_namespace.py.
    model_config = SettingsConfigDict(populate_by_name=True, env_prefix="MAINTENANCE_", extra="ignore")

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
    #: OFF by default. It defaulted to True and NO deployment path ever set it — `grep -rn DOCS
    #: chart/ .docker/ scripts/` matched nothing — so the flag documented a choice nobody was making
    #: and the schemas shipped openly. A security default every deployment must remember to disable is
    #: one nobody disables. Turn it on per-environment (the chart's dev values do).
    docs_enabled: bool = Field(default=False, alias="MAINTENANCE_DOCS")
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

    # --- The WORK QUEUE. When set, the cron tick PLANS and enqueues instead of maintaining the estate
    # inside its own request; a subscription executes one dataset per delivery. That is the whole of what
    # this buys, and each part is a thing the serial tick cannot do: an overrunning tick is queued rather
    # than DROPPED by the single-flight guard, a poison dataset fails its own message instead of stopping
    # everything discovered after it, and work outlives a pod restart because JetStream holds it.
    #
    # Empty topic => the serial path, which is what local runs and the test suite take. There is no
    # separate on/off flag: the queue is used when there IS one, so production has exactly one path
    # rather than two that can drift.
    #
    # Delivery is AT-LEAST-ONCE and that is safe here rather than merely tolerated: compaction and GC are
    # both convergent — a redelivered unit finds the fragments already merged and the versions already
    # reclaimed, and does nothing. A unit is not a transaction and must never be treated as one.
    #: How many versions must accumulate before the EVENT lane re-plans one dataset — Lakekeeper's
    #: `min-snapshots-to-expire`, in rask's own registry. It exists because a plan is not cheap:
    #: `sibling_base_refs` opens every sibling manifest in the warehouse, so without a threshold a write
    #: burst drives one whole-warehouse sweep per write. The hourly backstop covers whatever this skips,
    #: which is what makes a threshold safe rather than a way to lose maintenance.
    event_min_versions: int = Field(default=10, ge=1, alias="MAINTENANCE_EVENT_MIN_VERSIONS")
    work_pubsub: str = Field(default="maintenance-work-pubsub", alias="MAINTENANCE_WORK_PUBSUB")
    work_topic: str = Field(default="", alias="MAINTENANCE_WORK_TOPIC")
    #: Where an exhausted unit parks. A dataset that fails every redelivery must LEAVE the queue — it
    #: would otherwise be redelivered forever, and a poison unit that recirculates is the failure the
    #: per-dataset boundary was supposed to fix.
    work_dlq_topic: str = Field(default="", alias="MAINTENANCE_WORK_DLQ_TOPIC")

    # --- Lineage emission (opt-in, best-effort) — record a maintenance run on each materially-compacted
    # dataset to the lineage graph via Dapr pub/sub. Publishes to the SAME pubsub component + topic the
    # catalog publishes to and the lineage service subscribes to, so a compaction shows up in producers()
    # next to the writes. Off by default; the sidecar owns retry (no DLQ), so a publish never fails a sweep.
    lineage_emit_enabled: bool = Field(default=False, alias="MAINTENANCE_LINEAGE_EMIT_ENABLED")
    # Bound each Dapr publish so a hung sidecar can't stall a sweep (best-effort → the outage is swallowed).
    publish_timeout_seconds: float = Field(default=5.0, gt=0, alias="MAINTENANCE_PUBLISH_TIMEOUT_SECONDS")
    lineage_pubsub: str = Field(default="lineage-pubsub", alias="MAINTENANCE_LINEAGE_PUBSUB")
    lineage_topic: str = Field(default="lineage.events.v1", alias="MAINTENANCE_LINEAGE_TOPIC")
    # "maintenance", the SERVICE, not "compaction", the operation: the namespace lands on every
    # emitted RunEvent's job and is persisted into AGE — the pre-rename default meant a service named
    # `maintenance` signed the graph as a service that no longer exists. (The job NAME keeps its
    # `compaction.<table_id>` form — that half names the operation.)
    lineage_job_namespace: str = Field(default="maintenance", alias="MAINTENANCE_LINEAGE_JOB_NAMESPACE")
    # The catalog id delimiter — to derive a dataset's parent namespace from its table id (matches the
    # catalog's LANCE_DELIMITER default). The catalog lays tables out as <uuid>_<table_id>; table_id is the
    # canonical lineage Dataset name == OpenFGA object id, and its parent is all-but-the-last segment.
    delimiter: str = Field(default=CATALOG_DELIMITER, alias="MAINTENANCE_DELIMITER")

    # --- S3 access to the Lance lakehouse bucket ----------------------------------------------------
    s3_endpoint: str = Field(default="http://localhost:9000", alias="MAINTENANCE_S3_ENDPOINT")
    # NO DEFAULT, and specifically not the tenant root it used to carry. The chart always sets this, so
    # nothing shipped relied on the default — which is what made it dangerous: a deployment that
    # configured a scoped SECRET and forgot the key would pair it with the root key id, and one that
    # configured neither ran the whole sweep as RustFS tenant root, reaching every tenant's bytes and
    # the records that govern maintenance itself. Empty so the boot check can refuse it, exactly as the
    # secret half is already refused; a credential is a PAIR and half of one is not a lesser risk.
    s3_access_key_id: str = Field(default="", alias="MAINTENANCE_S3_ACCESS_KEY_ID")
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
    # PLATFORM buckets — infrastructure the estate creates for itself, comma-separated. They hold no
    # governed tables, so no warehouse record will ever claim them, and `orphan_buckets` would otherwise
    # report each one as drift on every tick FOREVER — a finding no operator can ever action, on a report
    # whose whole contract is that a clean run certifies the estate.
    #
    # Measured live 2026-08-16: `rask-observability` — the RustFS bucket the chart's OWN mkbucket job
    # creates for GreptimeDB's object storage — sat in orphan_buckets, so the drift total could not reach
    # zero by any action short of deleting the observability store. The chart already names this set in
    # `rustfs.buckets`; this is where it gets told.
    s3_platform_buckets: str = Field(default="", alias="MAINTENANCE_S3_PLATFORM_BUCKETS")

    @property
    def sweep_buckets(self) -> list[str]:
        """Every bucket the sweep must cover: the primary lakehouse bucket + the extras. De-duplicated and
        order-stable so a bucket listed twice is swept once (and the report stays deterministic)."""
        extras = [b.strip().removeprefix("s3://").strip("/") for b in self.s3_extra_buckets.split(",")]
        return list(dict.fromkeys([self.s3_bucket, *[b for b in extras if b]]))

    @property
    def platform_buckets(self) -> list[str]:
        """Buckets `orphan_buckets` must never report: the swept set plus the declared platform ones.

        The swept set is included because a bucket this service maintains is by definition known to the
        estate; the declared extras cover infrastructure that holds no governed tables at all and so can
        never be claimed by a warehouse record.
        """
        declared = [b.strip().removeprefix("s3://").strip("/") for b in self.s3_platform_buckets.split(",")]
        return list(dict.fromkeys([*self.sweep_buckets, *[b for b in declared if b]]))

    # --- OpenFGA (READ-ONLY, for the drift reconciler) -----------------------------------------------
    # The reconciler compares three stores: OpenFGA says WHO, the control-root registries say WHAT
    # EXISTS, object storage holds the BYTES. Without a client here, FOUR of its seven categories
    # (ghost_projects, ghost_warehouses, unreferenced_projects, orphaned_annotation_tasks) can only
    # report UNAVAILABLE — including "today's seeded ghosts", which is the drift that motivated it.
    #
    # This service only ever READS tuples (`fga.read_tuples`); it holds no grant-writing path at all,
    # which is why it needs no model id beyond what pins the store.
    # The knobs themselves are `FgaSettings`' (`RASK_FGA_*`) — including `fga_root_object`, the estate
    # root this service excludes from the ghost report BY DESIGN: it carries real tuples and has no
    # registry record, so without the exclusion every run would name a known-good finding and train the
    # reader to skip the category. It has to be the SAME object the catalog gates on, which is exactly
    # what a per-service twin of that setting could not guarantee — which is why the 2026-08-30
    # rename collapsed every spelling of it onto the single `RASK_FGA_ROOT_OBJECT`.

    # --- #79 expired-trash purge (RECLAMATION — the only mutation this service makes outside a dataset).
    #
    # OFF by default, and report-only stays the SHIPPED default. The estate's rule is that a reclaimer
    # earns its delete permission by first proving its report runs clean, and turning this on is what
    # spends that permission: from here the tick DELETES the bytes a `_trash/` record names and REVOKES
    # that object's FGA tuples. What it costs to turn on, stated plainly:
    #
    #   * a `drop_table` past `LANCE_TRASH_GRACE_DAYS` becomes UNRECOVERABLE — `undrop` does not check
    #     expiry today, so a record that survives is a recovery that still works; a purged one is not;
    #   * the purge only runs on a tick whose drift report is CLEAN (`purge.report_is_clean`), so a
    #     permanently-drifting or permanently-unavailable estate never reclaims anything — that is the
    #     designed failure direction, not a bug to route around;
    #   * a false positive costs a table someone was inside their window to undrop, which is why the
    #     purge re-checks liveness against `__manifest` and refuses anything outside the maintained
    #     estate rather than trusting the record's `location` field.
    trash_purge_enabled: bool = Field(default=False, alias="MAINTENANCE_TRASH_PURGE_ENABLED")
    # Per-tick ceiling on records purged. The remainder is REPORTED (`TrashPurgeReport.capped`), never
    # silently dropped: a backlog is drained oldest-first over several ticks rather than turning one
    # cron fire into an unbounded delete storm against object storage.
    trash_purge_max_per_tick: int = Field(default=25, ge=1, le=1000, alias="MAINTENANCE_TRASH_PURGE_MAX_PER_TICK")

    # --- Control-plane change-events (#79). The purge is a governance mutation, so it announces itself
    # on the SAME broadcast topic the catalog publishes to (`catalog.control.v1`). Off by default and
    # best-effort when on: a bus outage must never fail — or half-fail — a reclamation. The component
    # must list this app-id in its `scopes` (chart/templates/dapr-component.yaml) or the sidecar refuses
    # the publish, and "best-effort" means it would refuse SILENTLY.
    control_emit_enabled: bool = Field(default=False, alias="MAINTENANCE_CONTROL_EMIT_ENABLED")
    control_pubsub: str = Field(default="catalog-control-pubsub", alias="MAINTENANCE_CONTROL_PUBSUB")

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
    #: THE ONE STORE, NAMED ONCE (DUP-17). The estate runs a single Dapr secret-store component and
    #: seven env vars named it, each defaulting to the same literal and none of them set by the chart —
    #: so repointing the store meant finding all seven. `RASK_SECRET_STORE` is the estate-wide name
    #: (already what viewer and ingest read); the per-service alias stays FIRST so a single service can
    #: still be moved on its own.
    dapr_secret_store: str = Field(default="lance-secrets", validation_alias=AliasChoices("MAINTENANCE_DAPR_SECRET_STORE", "RASK_SECRET_STORE"))
    dapr_secret_key: str = Field(default="lance", alias="MAINTENANCE_DAPR_SECRET_KEY")

    # THE CATALOG'S CREDENTIAL DOOR. Unset (the default) = no vending, and every rewrite is signed by
    # the ambient root key exactly as it always was — this adds a capability without removing one, and
    # a deployment on `mode_b` vends nothing by design. Set, a compaction's WRITE is signed by a
    # credential scoped to that one table and expiring in 900s. See `services/credentials.py` for what
    # deliberately STAYS on the root key (the whole-estate protection pre-pass, which is a read).
    catalog_url: str = Field(default="", alias="MAINTENANCE_CATALOG_URL")
    #: The subject this service claims at the catalog's identity door, paired with the Dapr app token
    #: daprd injects. Both halves or neither — the door requires both, and sending one is a refusal
    #: whose reason is invisible from this side.
    catalog_service_identity: str = Field(default="service-maintenance", alias="MAINTENANCE_CATALOG_SERVICE_IDENTITY")
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

    #: Stage lineage events here before publishing. EMPTY = today's behaviour exactly, because
    #: `publish_lineage_with_outbox` degrades to a plain publish when unset — so this is inert until a
    #: deployment opts in. The twin of the catalog's `LANCE_LINEAGE_OUTBOX_URI`: `_PUBLISH_INTENT`
    #: pinned exactly two bare lineage publishers and this is the second.
    lineage_outbox_uri: str = Field(default="", alias="MAINTENANCE_LINEAGE_OUTBOX_URI")

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


def shared_lance_session() -> lance.Session:
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
