"""Maintenance service settings (pydantic-settings, ``MAINTENANCE_*`` env vars).

This service holds no OpenFGA settings: it holds no OpenFGA credential, since one could write any tuple ([[XC-084]]).
Its drift report reads the store's grant census through the catalog, and every grant change the reconcile tick asks for
is the catalog's (``maintenance.services.credentials``).

The signature door's knobs are the estate's too ([[XC-078]]): ``SignatureDoorSettings`` reads
``RASK_SIGNATURE_DOORS``, ``RASK_EVENT_SIGNERS`` and ``RASK_EVENT_DELEGATORS``, which decide how
``/maintenance-arrival`` treats the signature of a write event before it evaluates anything.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import AfterValidator, AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options
from lakehouse.openlineage import DEFAULT_DATASET_NAMESPACE, require_dataset_namespace
from lakehouse.signature_doors import SignatureDoorSettings
from lakehouse.storage_options import StorageOptions
from lakehouse.store_session import StoreIdentitySettings, env_alias, key_pair_in_force, require_a_token, require_one_store_identity


if TYPE_CHECKING:
    import lance


class MaintenanceSettings(SignatureDoorSettings, StoreIdentitySettings, BaseSettings):
    """Config for the maintenance service + its S3 access to the lakehouse buckets."""

    # `populate_by_name` also teaches the env source the bare FIELD NAME as a second lookup, so
    # every alias below silently gained an un-namespaced twin (a field named `address` would
    # answer to whatever $ADDRESS another program in the pod exports). `env_prefix` redirects that fallback onto the
    # namespace the aliases already declare; an explicit alias bypasses it, so the
    # deliberately-bare ones (DAPR_HTTP_PORT) still land.
    # See tests/unit/test_settings_env_namespace.py.
    model_config = SettingsConfigDict(populate_by_name=True, env_prefix="MAINTENANCE_", alias_generator=env_alias("MAINTENANCE"), extra="ignore")

    # #102: the shared Lance session's cache caps. Defaults sized for the pod tier (512Mi limit —
    # Lance's own defaults are 1 GiB metadata + 6 GiB index PER OPEN, which is the defect).
    lance_metadata_cache_mb: int = Field(default=128, ge=8, alias="MAINTENANCE_LANCE_METADATA_CACHE_MB")
    lance_index_cache_mb: int = Field(default=256, ge=8, alias="MAINTENANCE_LANCE_INDEX_CACHE_MB")

    # Serve /docs + /openapi.json (default on for dev; prod sets false, like the catalog's LANCE_REST_DOCS).
    #: OFF by default. It defaulted to True and NO deployment path ever set it — `grep -rn DOCS
    #: chart/ .docker/ scripts/` matched nothing — so the flag documented a choice nobody was making
    #: and the schemas shipped openly. A security default every deployment must remember to disable is
    #: one nobody disables. Turn it on per-environment (the chart's dev values do).
    docs_enabled: bool = Field(default=False, alias="MAINTENANCE_DOCS")

    #: Whether this service emits the `lance.audit` compliance stream. ON by default — the trail is
    #: evidence, so a deployment that forgets still has it.
    #:
    #: IT DID NOT EXIST UNTIL 2026-09-09, and the sweep is where that cost most: this is the component
    #: that REWRITES BYTES, and with no field there was nothing for `build_lance_service_app` to arm,
    #: so the `lance.audit` logger sat at NOTSET and followed `RASK_LOG_LEVEL` (§ Q17-27). Arming it is
    #: the precondition for § Q17-36 — the sweep's per-object outcome becoming a governed record rather
    #: than telemetry on a 14-day trace TTL.
    audit_enabled: bool = Field(default=True, alias="MAINTENANCE_AUDIT_ENABLED")
    # Datasets whose newest version is older than this are eligible for version GC (keep recent history).
    # ge=1 (not 0): timedelta(0) is falsy, so pylance collapses `older_than` to None and silently drops the
    # threshold — to GC aggressively, use a small positive value, not 0.
    older_than_days: int = Field(default=7, ge=1, alias="MAINTENANCE_OLDER_THAN_DAYS")

    # Behind a Dapr sidecar? — when true, boot fails closed if the app-token is unset (the cron route would
    # otherwise be an open forged-sweep path). Symmetric with the lineage service. Off in dev (no sidecar).
    dapr_enabled: bool = Field(default=False, alias="MAINTENANCE_DAPR_ENABLED")

    # --- The write-event lane (`/maintenance-arrival`). A write a signed producer announced on `lineage.events.v1`
    # refreshes that table's due record between ticks, so a bring-your-own compaction removes the table from the due
    # list on its own commit rather than at the next sweep, and expires the table's versions once enough are
    # reclaimable (`min_versions_to_expire`), through the same pass the sweep runs.
    #: Subscribe the write-event lane. Off by default: an estate opts in by naming the lane in the chart.
    arrival_enabled: bool = Field(default=False, alias="MAINTENANCE_ARRIVAL_ENABLED")
    #: Where an event the lane could not decide on parks once its redeliveries are spent, so a poison event leaves the
    #: subscription instead of recirculating.
    arrival_dlq_topic: str = Field(default="", alias="MAINTENANCE_ARRIVAL_DLQ_TOPIC")
    #: Expire a table's versions only once at least this many are RECLAIMABLE under its policy. Both lanes ask it: an
    #: expiry pass that reclaims one version a tick costs a vend, a manifest commit read and a listing for almost nothing.
    #: The count is Lance's own, ``explain_cleanup_old_versions`` with the arguments the pass's ``cleanup_old_versions``
    #: takes ([[LH-562]]).
    min_versions_to_expire: int = Field(default=20, ge=1, alias="MAINTENANCE_MIN_VERSIONS_TO_EXPIRE")
    #: How old a manifest a stock writer staged for a version its table already holds must be before expiry removes it
    #: ([[LH-529]]): one whose commit lost its version is garbage Lance's own cleanup never lists, and the grace outlasts any
    #: commit still deciding its outcome.
    staged_manifest_grace_seconds: int = Field(default=600, ge=0, alias="MAINTENANCE_STAGED_MANIFEST_GRACE_SECONDS")
    # --- The work queue (ADR 0198 §2). Each table's pass, each trash record's release and the hourly audit are tasks on
    # NATS JetStream, which every replica's worker pulls from; the streams and their durable consumers are the chart's
    # (`chart/templates/nats-stream-job.yaml`), so the worker binds to them and creates nothing.
    #: The bus the worker connects to; empty runs no worker, which leaves a stack with no NATS to the doors and the tests.
    nats_url: str = Field(default="", alias="MAINTENANCE_NATS_URL")
    #: The store secret holding the worker's own bus user (`jwt`, `seed`), read once at start and never from env. Its
    #: permissions are the chart's `nats.auth.users` row: pull and ack on the two consumers, publish tasks and schedules.
    nats_credentials_secret: str = Field(default="nats-user-maintenance-worker", alias="MAINTENANCE_NATS_CREDENTIALS_SECRET")
    #: How often a running task tells JetStream it is still being worked on, well inside the consumer's ack wait (the
    #: chart's `maintenance.queue.ackWaitSeconds`), so a long pass is never redelivered beneath itself while its worker lives.
    task_heartbeat_seconds: float = Field(default=20.0, gt=0, alias="MAINTENANCE_TASK_HEARTBEAT_SECONDS")
    #: The consumer's ack wait (the chart's `maintenance.queue.ackWaitSeconds`): how long JetStream waits for a heartbeat
    #: before it hands a running task to another replica. A worker whose heartbeats go unanswered until the ack wait the
    #: last answer restarted runs out ends itself, since its task may then be another replica's.
    task_ack_wait_seconds: float = Field(default=60.0, gt=0, alias="MAINTENANCE_TASK_ACK_WAIT_SECONDS")
    #: Deliveries of one task before the worker ends it and counts it (the chart's `maintenance.queue.maxDeliver`); the
    #: consumer allows one more, on which a task that outlived its worker every time is ended without running.
    task_max_deliver: int = Field(default=10, ge=1, alias="MAINTENANCE_TASK_MAX_DELIVER")
    #: A pass running longer than this is logged and counted once. It keeps its task, since a redelivery while the pass
    #: still runs would put the table in flight twice, until twice this, when its replica ends its own process.
    max_pass_seconds: int = Field(default=1800, ge=60, alias="MAINTENANCE_MAX_PASS_SECONDS")
    #: The longest a table goes without a pass: what bounds what no event announces, a lost commit event and the versions a
    #: deleted tag or branch releases (an erasure's pins among them), and a table only a commit can make due.
    recheck_hours: int = Field(default=24, ge=1, alias="MAINTENANCE_RECHECK_HOURS")
    #: When a pass that could not decide, or found the table's history held, is tried again.
    retry_seconds: int = Field(default=900, ge=60, alias="MAINTENANCE_RETRY_SECONDS")
    #: How long the first commit of a burst holds its table's pass, and so the most often commits pass a table: every commit
    #: until the pass runs is covered by it, and a pass costs a fixed handful of store requests however busy the table is.
    commit_pass_delay_seconds: int = Field(default=300, ge=1, alias="MAINTENANCE_COMMIT_PASS_DELAY_SECONDS")
    #: How long a stopping replica lets the task it is running finish before its connection drains, inside the pod's
    #: `terminationGracePeriodSeconds`; a task cut short goes to another replica once its heartbeat stops. A pass still
    #: running when the drain ends ends its replica (`Worker.drain`), since its heartbeat ends with the drain.
    drain_seconds: float = Field(default=50.0, gt=0, alias="MAINTENANCE_DRAIN_SECONDS")
    #: The port the worker's process serves its probes on (:mod:`maintenance.worker`), for the kubelet and for its
    #: sidecar's health check, which is what lets the sidecar leave when the worker does.
    worker_port: int = Field(default=8001, ge=1, le=65535, alias="MAINTENANCE_WORKER_PORT")
    #: The audit's schedule, a NATS message schedule (ADR-51): the drift report without the orphan scan, its repairs, the
    #: release and the lost-task backstop run once per firing on one replica, whatever the replica count.
    audit_schedule: str = Field(default="@hourly", alias="MAINTENANCE_AUDIT_SCHEDULE")
    #: The scan's schedule: the audit with the orphan scan, which opens every table, and then the listing floors and the
    #: purge its report certifies (owner 2026-11-09: daily). At half past midnight, so it does not fire with the hourly
    #: audit, whose claim it would wait out (the two never run at once). A six-field cron, seconds first: nats-server
    #: 2.15.2 refuses a five-field one (10189, measured 2026-11-09).
    scan_schedule: str = Field(default="0 30 0 * * *", alias="MAINTENANCE_SCAN_SCHEDULE")

    # --- Lineage emission (opt-in, best-effort) — record a maintenance run on each dataset whose versions
    # were reclaimed, to the lineage graph via Dapr pub/sub. Publishes to the SAME pubsub component + topic the
    # catalog publishes to and the lineage service subscribes to, so an expiry run shows up in producers()
    # next to the writes. Off by default; the sidecar owns retry (no DLQ), so a publish never fails a sweep.
    lineage_emit_enabled: bool = Field(default=False, alias="MAINTENANCE_LINEAGE_EMIT_ENABLED")
    # Bound each Dapr publish so a hung sidecar can't stall a sweep (best-effort → the outage is swallowed).
    publish_timeout_seconds: float = Field(default=5.0, gt=0, alias="MAINTENANCE_PUBLISH_TIMEOUT_SECONDS")
    lineage_pubsub: str = Field(default="lineage-pubsub", alias="MAINTENANCE_LINEAGE_PUBSUB")
    lineage_topic: str = Field(default="lineage.events.v1", alias="MAINTENANCE_LINEAGE_TOPIC")
    # "maintenance", the SERVICE: the namespace lands on every emitted RunEvent's job and is persisted into
    # AGE. The job NAME, `expire_versions.<table_id>`, names the operation.
    lineage_job_namespace: str = Field(default="maintenance", alias="MAINTENANCE_LINEAGE_JOB_NAMESPACE")
    #: The catalog's dataset namespace, every table's OpenLineage dataset namespace ([[LH-553]]): the value the catalog's
    #: `LANCE_LINEAGE_DATASET_NAMESPACE` holds, which the chart renders into both from one helper, so a pass lands on the
    #: dataset the catalog's events name.
    lineage_dataset_namespace: Annotated[str, AfterValidator(require_dataset_namespace)] = Field(
        default=DEFAULT_DATASET_NAMESPACE, alias="MAINTENANCE_LINEAGE_DATASET_NAMESPACE"
    )

    # --- S3 access to the Lance lakehouse bucket ----------------------------------------------------
    s3_endpoint: str = Field(default="http://localhost:9000", alias="MAINTENANCE_S3_ENDPOINT")
    # Maintenance's identity at the store is `StoreIdentitySettings`' (MAINTENANCE_S3_WEB_IDENTITY_TOKEN_FILE and the
    # rest): its own reads and records sign with sessions its token is exchanged for, and a rewrite signs with its table's
    # vend alone (`services/credentials.py`).
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
    # PLATFORM buckets — infrastructure the estate creates for itself, comma-separated. They hold no
    # governed tables, so no warehouse record will ever claim them, and `orphan_buckets` would otherwise
    # report each one as drift on every tick FOREVER — a finding no operator can ever action, on a report
    # whose whole contract is that a clean run certifies the estate.
    #
    # Measured live 2026-08-16: `rask-observability` — the bucket the chart's OWN mkbucket job creates
    # for GreptimeDB's object storage — sat in orphan_buckets, so the drift total could not reach zero by
    # any action short of deleting the observability store. The chart names this set in
    # `lance.maintenancePlatformBuckets` (the Job's platform set plus the multibase bases); this is where
    # it gets told.
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

    # --- #79 expired-trash purge (RECLAMATION — the only mutation this service makes outside a dataset).
    #
    # OFF by default, and report-only stays the SHIPPED default. The estate's rule is that a reclaimer
    # earns its delete permission by first proving its report runs clean, and turning this on is what
    # spends that permission: from here the tick DELETES the bytes a `_trash/` record names, under a session the catalog
    # vends for that record, and the catalog then ends the record and revokes its object's grants ([[XC-084]]). What it
    # costs to turn on, stated plainly:
    #
    #   * the bytes of a `drop_table` past its deadline are DELETED. The drop was final at the deadline
    #     (ADR 0146: undrop refuses it, and the reconcile tick has already revoked its grants), so what
    #     this spends is the bytes an operator could still re-register by hand;
    #   * the purge only runs on a tick whose drift report is CLEAN (`purge.report_is_clean`), so a
    #     permanently-drifting or permanently-unavailable estate never reclaims anything — that is the
    #     designed failure direction, not a bug to route around;
    #   * a false positive costs a live table's bytes, which is why the
    #     purge re-checks liveness against `__manifest` and refuses anything outside the maintained
    #     estate rather than trusting the record's `location` field.
    trash_purge_enabled: bool = Field(default=False, alias="MAINTENANCE_TRASH_PURGE_ENABLED")
    # Per-tick ceiling on records purged. The remainder is REPORTED (`TrashPurgeReport.capped`), never
    # silently dropped: a backlog is drained oldest-first over several ticks rather than turning one
    # cron fire into an unbounded delete storm against object storage.
    trash_purge_max_per_tick: int = Field(default=25, ge=1, le=1000, alias="MAINTENANCE_TRASH_PURGE_MAX_PER_TICK")
    #: Run the reclamation pass but perform NONE of its mutations — report the plan instead.
    #:
    #: The estate's rule is that a reclaimer earns its delete permission by first proving its report
    #: runs clean, and this is what lets an operator SEE the price of spending it. Without it,
    #: `trash_purge_enabled=false` returns before the trash prefix is even read, so the only way to
    #: learn what the purge would do was to let it do it — the wrong order for the one irreversible
    #: operation this service performs.
    #:
    #: It runs the REAL pass: the same `due_records`, the same liveness pre-pass, the same `check`. Only the mutations are skipped (the byte delete and the catalog's ending of the
    #: record and its grants) plus the refusal annotation, which is a write onto the very records being
    #: previewed. A separate preview routine would be a second mechanism that drifts from the one it
    #: previews, and the drift would surface as deleting the wrong thing.
    #:
    #: It subtracts every MUTATION: with both flags on the tick previews and reclaims nothing. The
    #: opposite wiring would be worse — a deployment that means to reclaim and quietly previews forever
    #: looks exactly like a healthy one — which is why `maintenance.trash.purge_planned` and every
    #: reclamation counter carry a `dry_run` dimension, so that state is visible rather than inferred.
    #:
    #: IT ADDS READ COST. With reclamation off the pass returns before the trash prefix is read at all;
    #: with the preview on it performs the full listing and a Lance `__manifest` open per maintained root.
    #:
    #: `would_purge` is an UPPER BOUND, never exact. Three refusal causes sit below the preview cut: the
    #: catalog's judgement (a registered clone still reading the bytes among them, ADR 0198 §1), a location
    #: that is not a dataset, and a failing delete cannot be evaluated without asking for or performing the
    #: act they guard.
    trash_purge_dry_run: bool = Field(default=False, alias="MAINTENANCE_TRASH_PURGE_DRY_RUN")

    #: Write one metadata-only commit to a dataset whose orphans sit above its Lance listing floor, so
    #: the next ordinary sweep can enumerate them ([[LH-094]], owner ruling 2026-09-19).
    #:
    #: OFF BY DEFAULT because it writes a version to a GOVERNED table for a GC side effect, which is a
    #: real cost an estate should choose rather than inherit. It is the cheaper of the two routes the
    #: ruling weighed: the alternative had this service delete the named bytes itself, on live tables,
    #: against a floor rule it derived rather than one upstream enforces.
    #:
    #: SEPARATE FROM ITS DRY RUN, for the reason the trash purge's pair already establishes: this flag
    #: is whether the estate wants the behaviour, the other is whether this tick acts. An operator
    #: turning it on to SEE what it would touch must not thereby commit to a governed dataset.
    floor_raise_enabled: bool = Field(default=False, alias="MAINTENANCE_FLOOR_RAISE_ENABLED")
    #: Run the real selection and refusals, write nothing. DEFAULT TRUE, the opposite of the trash
    #: purge's, and deliberately: the purge is gated behind a report that must first run clean, while
    #: this pass is what MAKES that report clean and so has no such gate in front of it. Its only brake
    #: is this flag.
    floor_raise_dry_run: bool = Field(default=True, alias="MAINTENANCE_FLOOR_RAISE_DRY_RUN")
    #: Per-tick ceiling on datasets committed to. The remainder is REPORTED (`FloorReport.capped`): a
    #: store rebuild strands EVERY dataset at once, so an uncapped pass would turn one cron fire into a
    #: commit against every governed table in the estate.
    floor_raise_max_per_tick: int = Field(default=10, ge=1, le=1000, alias="MAINTENANCE_FLOOR_RAISE_MAX_PER_TICK")

    #: Rebuild authorization tuples the control-plane registries still justify ([[LH-061]], owner
    #: ruling 2026-09-19 overturning the standing deferral on a write-capable reconcile).
    #:
    #: ADDITIVE ONLY — the pass writes and never deletes, so it cannot widen access beyond what the
    #: control plane already recorded. OFF BY DEFAULT anyway, because it writes to the AUTHORIZATION
    #: store and an estate should choose that rather than inherit it.
    tuple_rebuild_enabled: bool = Field(default=False, alias="MAINTENANCE_TUPLE_REBUILD_ENABLED")
    #: Plan and report, write nothing. DEFAULT TRUE: an operator turning the pass on to SEE what it
    #: would grant must not thereby grant it.
    tuple_rebuild_dry_run: bool = Field(default=True, alias="MAINTENANCE_TUPLE_REBUILD_DRY_RUN")
    #: Per-tick ceiling on tuples written. The remainder is REPORTED (`RebuildReport.capped`). A
    #: wholesale tuple loss makes every tenant eligible at once, and an uncapped pass would turn one
    #: cron fire into an estate-wide authorization write.
    tuple_rebuild_max_per_tick: int = Field(default=50, ge=1, le=5000, alias="MAINTENANCE_TUPLE_REBUILD_MAX_PER_TICK")

    #: [[LH-061]]'s REPAIR half — revoking tuples whose object no longer exists. OFF and DRY-RUN by
    #: default, the same two flags the rebuild and the floor raise carry, and for the same reason:
    #: "does this estate want the behaviour" and "does this tick act" are different questions, and an
    #: operator turning it on to SEE what it would revoke must not thereby write to the authz store.
    #: The default here is the stricter of the two available defaults on purpose — this pass DELETES,
    #: and `trash_purge_dry_run` defaulting false is a choice made for a pass whose targets are already
    #: expired records, not for one that has the catalog revoke grants.
    drift_repair_enabled: bool = Field(default=False, alias="MAINTENANCE_DRIFT_REPAIR_ENABLED")
    drift_repair_dry_run: bool = Field(default=True, alias="MAINTENANCE_DRIFT_REPAIR_DRY_RUN")
    #: Capped per tick so a first armed run on an estate carrying [[LH-148]]'s 1,033 ghost tables
    #: revokes a reviewable batch rather than sweeping the backlog in one unreviewable write.
    drift_repair_max_per_tick: int = Field(default=50, ge=1, le=5000, alias="MAINTENANCE_DRIFT_REPAIR_MAX_PER_TICK")

    #: [[LH-061]]'s STORAGE tier — removing an orphaned trash record once the bytes it names are
    #: PROVEN gone. `repair.py` refuses `orphaned_trash` because the record is the only remaining
    #: pointer to those bytes; this pass satisfies that objection rather than overturning it, by
    #: probing the location first and refusing anything that still holds objects. It deletes no bytes.
    #: OFF and DRY-RUN by default, the same pair every destructive pass here carries.
    tombstone_sweep_enabled: bool = Field(default=False, alias="MAINTENANCE_TOMBSTONE_SWEEP_ENABLED")
    tombstone_sweep_dry_run: bool = Field(default=True, alias="MAINTENANCE_TOMBSTONE_SWEEP_DRY_RUN")
    #: Capped per tick because the probe is one listing PER RECORD — 989 of them in a single tick would
    #: turn a reconcile pass into a whole-estate scan, which is the cost this service exists to bound.
    tombstone_sweep_max_per_tick: int = Field(default=50, ge=1, le=5000, alias="MAINTENANCE_TOMBSTONE_SWEEP_MAX_PER_TICK")

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

    # How deep `discover_datasets` walks a bucket looking for the `_versions/` marker. A subtree the
    # walk stops inside becomes an `IncompleteScan`, which blocks `purge.report_is_clean` — and the
    # walk cannot tell "no dataset here" from "did not look deep enough", so a prefix that will never
    # hold a dataset blocks reclamation exactly as hard as a manifest we failed to read.
    #
    # Exhausting the tree is the only way to clear those, and until this field existed nothing could:
    # both call sites took the function default. It is a POLICY LEVER, not a rescue — measured
    # 2026-09-08 on the primary bucket, depth 6 finds the same 30 datasets as depth 3 and stops
    # nothing, so raising it reveals no hidden data. The default stays 3 because that measurement
    # covers one of 93 buckets.
    #
    # BOUNDED at both ends deliberately. 0 scans nothing; an unbounded walk is not a freedom but a
    # foot-gun, because the orphan scan opens every dataset the walk discovers.
    discovery_max_depth: int = Field(default=3, ge=1, le=16, alias="MAINTENANCE_DISCOVERY_MAX_DEPTH")
    # The reconciler reads the catalog's registries (`_projects/`, `_warehouses/`) off the control root.
    # Defaults to the primary bucket, matching the catalog's own LANCE_CONTROL_ROOT default; override
    # only when the catalog's control root has been moved.
    control_root: str = Field(default="", alias="MAINTENANCE_CONTROL_ROOT")
    #: THE CATALOG'S ``LANCE_EXTERNAL_BLOB_BASES``, under the same name the catalog and ingest read ([[LH-279]]).
    #: A base inside one of these, declared as a plain base, is sanctioned by configuration as a pointer
    #: base, which no vend grants. The base-reference pre-pass judges each declared base against these
    #: and the catalog's base record, so an unset value here turns every table's blob base into a record
    #: read and, for a table with no record, a finding that protects nothing.
    external_blob_bases: str = Field(default="", validation_alias="LANCE_EXTERNAL_BLOB_BASES")
    # Warehouses are a catalog FEATURE FLAG: with them off the deployment is single-bucket by
    # configuration and an unbound namespace is CORRECT, not drift — so the reconciler skips that
    # category rather than reporting every namespace. Must match the catalog's setting.
    warehouses_enabled: bool = Field(default=False, alias="MAINTENANCE_WAREHOUSES_ENABLED")

    # --- Secret consumption from the Dapr secret store (OpenBao) — symmetric with catalog + lineage.
    # When on, this service's signing key is read from the store, and its object-store identity is its ServiceAccount
    # token, never a pair (`require_one_store_identity`).
    secrets_from_dapr: bool = Field(default=False, alias="MAINTENANCE_SECRETS_FROM_DAPR")
    #: THE ONE STORE, NAMED ONCE (DUP-17). The estate runs a single Dapr secret-store component and
    #: seven env vars named it, each defaulting to the same literal and none of them set by the chart —
    #: so repointing the store meant finding all seven. `RASK_SECRET_STORE` is the estate-wide name
    #: (the one every service reads); the per-service alias stays FIRST so a single service can
    #: still be moved on its own.
    dapr_secret_store: str = Field(default="lance-secrets", validation_alias=AliasChoices("MAINTENANCE_DAPR_SECRET_STORE", "RASK_SECRET_STORE"))

    #: THE CATALOG'S CREDENTIAL DOOR, the only source of a rewrite's credential ([[LH-219]]): each rewrite is signed by a
    #: `maintain`-tier credential scoped to its one table and expiring in 900 s ([[LH-473]]). Unset, or a catalog on
    #: `mode_b`, every rewrite is refused; maintenance's own session signs only its reads and its own records
    #: (`services/credentials.py`).
    catalog_url: str = Field(default="", alias="MAINTENANCE_CATALOG_URL")
    #: The subject this service acts as: the author of the lineage it signs, the actor on the tuples it
    #: repairs, and the name its refusals report. It is never sent as a claim. The catalog derives the
    #: caller from the verified service-account token, so this must be this pod's service-account subject
    #: (`kubernetes~system:serviceaccount:<ns>:<sa>`, the chart's `lance.saSubject`), or the two name different principals.
    catalog_service_identity: str = Field(default="kubernetes~system:serviceaccount:default:rask-sa-maintenance", alias="MAINTENANCE_CATALOG_SERVICE_IDENTITY")
    #: THE IDENTITY THIS SERVICE SIGNS ITS LINEAGE EVENTS AS ([[LH-064]]), one variable on every signer
    #: (`RASK_SIGNING_IDENTITY`). With `secrets_from_dapr` it decides whether the service signs at all: its private
    #: key is `signing-key-<its key name>` in the store (`signing_key_name`). When it signs, it is also the author the events stamp.
    signing_identity: str = Field(default="", alias="RASK_SIGNING_IDENTITY")
    #: This pod's projected service-account token for the catalog's doors (audience `rask-catalog`),
    #: re-read on every call because the kubelet rotates it at ~515 s of its 600 s life.
    catalog_identity_token_file: str = Field(default="/var/run/secrets/rask/identity/rask-catalog/token", alias="RASK_CATALOG_IDENTITY_TOKEN_FILE")

    @property
    def resolved_policy_root(self) -> str:
        """The policy-registry root — `MAINTENANCE_POLICY_ROOT` or the primary bucket."""
        return self.policy_root or f"s3://{self.s3_bucket}"

    @property
    def external_blob_base_list(self) -> list[str]:
        """The configured external blob bases, parsed as the catalog parses them."""
        return [base.strip() for base in self.external_blob_bases.split(",") if base.strip()]

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

    @model_validator(mode="after")
    def _one_store_identity(self) -> Self:
        """Refuse a pair beside the token, a token sent to the public AWS STS, and the secret store on without a token."""
        if self.secrets_from_dapr:
            require_a_token(self.web_identity, prefix="MAINTENANCE")
        require_one_store_identity(self.web_identity, env_pair=self.env_pair, prefix="MAINTENANCE")
        return self

    @model_validator(mode="after")
    def _a_bus_needs_the_store(self) -> Self:
        """Refuse a bus address without the secret store: the worker's bus credential is read from the store alone."""
        if self.nats_url and not self.secrets_from_dapr:
            raise ValueError("MAINTENANCE_NATS_URL needs MAINTENANCE_SECRETS_FROM_DAPR: the worker's bus credential is read from the store, never env")
        return self

    def s3_key_pair(self) -> S3KeyPair:
        """What the sweep's own reads and records sign with: the session its token was exchanged for, or the environment's pair.

        Raises:
            StoreCredentialUnavailableError: The service projects a token and holds no unexpired session (503).
        """
        return key_pair_in_force(self.web_identity, self.env_pair)

    def storage_options(self) -> StorageOptions:
        """The Lance ``storage_options`` for opening datasets on the (HTTP) S3 endpoint, under the key pair in force."""
        # Via the shared builder — which also stamps path-style addressing; the hand-rolled copy here had
        # silently dropped it (audit 2026-07-15), leaving the sweep one object-store default away from 403s.
        return lance_storage_options(self.s3_endpoint, self.s3_key_pair(), self.s3_region)


def shared_lance_session() -> lance.Session:
    """The process-wide bounded Lance session (#102). Every maintenance open threads this, so a
    tick's second dataset (and the orphan scan's 500 version checkouts) HIT the cache instead of
    minting and discarding Lance's default 1 GiB + 6 GiB ceilings per open — ceilings that dwarf
    the pod's own 512Mi limit. Caps are LRU soft bounds; session keys carry (uri, version, etag),
    so a commit bumping a version writes new keys and a new version is never served stale. An old
    version whose data files were reclaimed can be (`lakehouse.lance_session`)."""
    from lakehouse.lance_session import affordable_cache_bytes, lance_session

    settings = get_settings()
    # CLAMPED TO WHAT THIS CONTAINER HOLDS, because the configured pair did not fit it. 128 + 256 MB
    # inside a 512Mi pod with a ~153Mi process baseline is 537 MB, and the caps are LRU SOFT bounds —
    # the size the cache grows toward, not a ceiling it stops at. `rask-maintenance` was OOMKilled
    # (exit 137) on 2026-09-10 after reconcile passes warmed the session, which is exactly the workload
    # that fills it: the scan opens every dataset across 93 buckets.
    #
    # Derived from the cgroup rather than by lowering these defaults, because a literal cannot track
    # `resources.limits.memory` — raise the pod and the caps should follow, lower it and they must.
    metadata, index = affordable_cache_bytes(settings.lance_metadata_cache_mb << 20, settings.lance_index_cache_mb << 20)
    return lance_session(metadata, index)


@lru_cache
def get_settings() -> MaintenanceSettings:
    """The process-wide maintenance settings (read once from env)."""
    return MaintenanceSettings()
