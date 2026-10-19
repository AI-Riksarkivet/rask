"""Maintenance service settings (pydantic-settings, ``MAINTENANCE_*`` env vars).

The OpenFGA knobs are the estate's, not this service's: it mixes in ``FgaSettings`` and reads
``RASK_FGA_*``. ``FgaSettings`` alone rather than ``GovernedAuthSettings`` because this service has no
human door — its routes are gated by the Dapr app token and it only ever READS tuples, as itself.

The signature door's knobs are the estate's too ([[XC-078]]): ``SignatureDoorSettings`` reads
``RASK_SIGNATURE_DOORS``, ``RASK_EVENT_SIGNERS`` and ``RASK_EVENT_DELEGATORS``, which decide how
``/maintenance-arrival`` treats the signature of a write event before it evaluates anything.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import FgaSettings, SignatureDoorSettings
from service_kit.governed.store_credential import S3KeyPair, store_key_pair
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lakehouse.objectfs import lance_storage_options


if TYPE_CHECKING:
    import lance


class MaintenanceSettings(SignatureDoorSettings, FgaSettings, BaseSettings):
    """Config for the table-maintenance service + its S3 access to the lakehouse buckets."""

    # `populate_by_name` also teaches the env source the bare FIELD NAME as a second lookup, so
    # every alias below silently gained an un-namespaced twin (MedallionSettings.ray_address
    # answered to Ray's own $RAY_ADDRESS). `env_prefix` redirects that fallback onto the
    # namespace the aliases already declare; an explicit alias bypasses it, so the
    # deliberately-bare ones (DAPR_HTTP_PORT) still land.
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
    # doing both because they have genuinely different cadences: the sweep opens every dataset and
    # reclaims versions, while the drift report only reads three stores and is cheap enough to run often.
    reconcile_binding_name: str = Field(default="maintenance-reconcile-cron", alias="MAINTENANCE_RECONCILE_BINDING_NAME")
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
    #: How many versions must accumulate before the event lane re-evaluates one dataset: the debounce that keeps a burst
    #: of writes from paying an evaluation each. A compaction's or an index's commit skips it. The sweep covers
    #: whatever this skips, which is what makes a threshold safe rather than a way to lose a refresh.
    event_min_versions: int = Field(default=10, ge=1, alias="MAINTENANCE_EVENT_MIN_VERSIONS")
    #: Expire a table's versions only once at least this many are RECLAIMABLE under its policy — Lakekeeper's
    #: `min-snapshots-to-expire`. Both lanes ask it: an expiry pass that reclaims one version a tick costs a vend, a
    #: manifest commit read and a listing for almost nothing. Eligibility is
    #: :func:`service_kit.lakehouse.version_retention.reclaimable_versions`, the rule the catalog's GC preview shows.
    min_versions_to_expire: int = Field(default=20, ge=1, alias="MAINTENANCE_MIN_VERSIONS_TO_EXPIRE")
    #: How old the sweep's clone list may be before the event lane stops trusting it to expire a table. The list is the
    #: only evidence of which tables another dataset resolves its files through, so an older one leaves expiry to the
    #: sweep, which takes a fresh one first. The catalog's commit judge holds the same bound
    #: (`LANCE_CLONE_LIST_MAX_AGE_SECONDS`, default 3600).
    clone_list_max_age_seconds: int = Field(default=3600, ge=60, alias="MAINTENANCE_CLONE_LIST_MAX_AGE_SECONDS")
    #: Stores the PLATFORM owns and the catalog deliberately cannot resolve ([[LH-164]]). Today that is
    #: the model registry: it lives at `<root>/medallion/models/<model>`, an explicit location, and
    #: the catalog's promote/describe doors open it by EXPLICIT URI, because native namespace resolution
    #: would drop the `/medallion/` prefix (`catalog/core/config.py:516-522`).
    #:
    #: NAMED RATHER THAN INFERRED, so the reconciler can tell "known and unresolvable" from "not looked
    #: at". Empty means nothing is declared and every unreadable location is reported as a coverage gap,
    #: which is the fail-loud direction for a report.
    declared_platform_roots: str = Field(default="", alias="MAINTENANCE_DECLARED_PLATFORM_ROOTS")

    @property
    def declared_platform_root_list(self) -> tuple[str, ...]:
        """The declared roots, split and trimmed. Comma-separated for the same reason every other
        multi-value setting in this file is."""
        return tuple(part.strip() for part in self.declared_platform_roots.split(",") if part.strip())

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
    # The catalog id delimiter — to derive a dataset's parent namespace from its table id (matches the
    # catalog's LANCE_DELIMITER default). The catalog lays tables out as <uuid>_<table_id>; table_id is the
    # canonical lineage Dataset name == OpenFGA object id, and its parent is all-but-the-last segment.
    delimiter: str = Field(default=CATALOG_DELIMITER, alias="MAINTENANCE_DELIMITER")

    # --- S3 access to the Lance lakehouse bucket ----------------------------------------------------
    s3_endpoint: str = Field(default="http://localhost:9000", alias="MAINTENANCE_S3_ENDPOINT")
    # The key pair the sweep signs with when the secret store is OFF; with it on, the pair is the store's
    # (`dapr_secret_s3_id_field` and `dapr_secret_s3_field`, ADR 0166) and these two are not read. NO DEFAULT
    # key id: any default would be the store's root account, and a deployment that configured neither half
    # would run the whole sweep as the tenant root, reaching every tenant's bytes and the records that govern
    # maintenance itself. Empty so the boot check can refuse either half; a credential is a PAIR and half of
    # one is not a lesser risk. SecretStr so the secret is redacted in repr/model_dump (parity with the catalog).
    s3_access_key_id: str = Field(default="", alias="MAINTENANCE_S3_ACCESS_KEY_ID")
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

    # --- OpenFGA (READ-ONLY, for the drift reconciler) -----------------------------------------------
    # The reconciler compares three stores: OpenFGA says WHO, the control-root registries say WHAT
    # EXISTS, object storage holds the BYTES. Without a client here, the categories that read tuples
    # (ghost_projects, ghost_warehouses, ghost_tables, unreferenced_projects, ungoverned_tables) can only
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
    #: Run the reclamation pass but perform NONE of its mutations — report the plan instead.
    #:
    #: The estate's rule is that a reclaimer earns its delete permission by first proving its report
    #: runs clean, and this is what lets an operator SEE the price of spending it. Without it,
    #: `trash_purge_enabled=false` returns before the trash prefix is even read, so the only way to
    #: learn what the purge would do was to let it do it — the wrong order for the one irreversible
    #: operation this service performs.
    #:
    #: It runs the REAL pass: the same `due_records`, the same liveness and shallow-clone pre-passes,
    #: the same `check`. Only the three mutations are skipped (the FGA revoke, the byte delete, the
    #: record clear) plus the refusal annotation, which is a write onto the very records being
    #: previewed. A separate preview routine would be a second mechanism that drifts from the one it
    #: previews, and the drift would surface as deleting the wrong thing.
    #:
    #: It subtracts every MUTATION: with both flags on the tick previews and reclaims nothing. The
    #: opposite wiring would be worse — a deployment that means to reclaim and quietly previews forever
    #: looks exactly like a healthy one — which is why `maintenance.trash.purge_planned` and every
    #: reclamation counter carry a `dry_run` dimension, so that state is visible rather than inferred.
    #:
    #: IT ADDS READ COST, and on this pod that is not free. With reclamation off the pass returns
    #: before the trash prefix is read at all; with the preview on it performs the full listing, a
    #: Lance `__manifest` open per maintained root, and the shallow-clone pre-pass that opens EVERY
    #: dataset in EVERY maintained bucket — against a 512Mi limit this service has already been
    #: OOMKilled against. Turning it on is a real workload, not an inspection.
    #:
    #: `would_purge` is an UPPER BOUND, never exact. Four of the seven refusal causes sit below the
    #: preview cut: the FGA revoke, a location that is not a dataset, and a failing delete cannot be
    #: evaluated without performing the act they guard. The fourth — the shallow-clone guard — IS
    #: applied, because the pre-pass has already run and `is_protected` is an in-memory compare.
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
    #: expired records, not for one that writes to OpenFGA.
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
    # BOUNDED at both ends deliberately. 0 maintains nothing; an unbounded walk is not a freedom but a
    # foot-gun, because `sweep.py::_protected_roots` opens every discovered dataset in every bucket
    # before one is expired, so the walk is the sweep's dominant cost.
    discovery_max_depth: int = Field(default=3, ge=1, le=16, alias="MAINTENANCE_DISCOVERY_MAX_DEPTH")
    #: [[LH-101]]. Wall-clock ceiling for ONE sweep tick, checked BETWEEN datasets — never inside one,
    #: because a budget that interrupted a reclaim would leave it half-done. 0 (the default)
    #: means unlimited. The value is an
    #: operator decision: too small and a large estate never completes a pass, too large and the bound is
    #: decorative, and exhausting it logs what was executed and what remains rather than stopping quietly.
    sweep_budget_seconds: float = Field(default=0.0, ge=0.0, alias="MAINTENANCE_SWEEP_BUDGET_SECONDS")

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
    # When on, the S3 key pair is the store's (NOT plaintext env), read at boot (failing closed if absent)
    # and again while the service runs (`service_kit.governed.store_credential`).
    secrets_from_dapr: bool = Field(default=False, alias="MAINTENANCE_SECRETS_FROM_DAPR")
    #: THE ONE STORE, NAMED ONCE (DUP-17). The estate runs a single Dapr secret-store component and
    #: seven env vars named it, each defaulting to the same literal and none of them set by the chart —
    #: so repointing the store meant finding all seven. `RASK_SECRET_STORE` is the estate-wide name
    #: (already what viewer and ingest read); the per-service alias stays FIRST so a single service can
    #: still be moved on its own.
    dapr_secret_store: str = Field(default="lance-secrets", validation_alias=AliasChoices("MAINTENANCE_DAPR_SECRET_STORE", "RASK_SECRET_STORE"))
    #: Maintenance's own secret in the store: its scoped pair, readable by maintenance alone ([[XC-165]]).
    dapr_secret_key: str = Field(default="maintenance-s3", alias="MAINTENANCE_DAPR_SECRET_KEY")

    # THE CATALOG'S CREDENTIAL DOOR. Unset (the default) = no vending, and every expiry pass is signed by
    # the ambient credential; a deployment on `mode_b` vends nothing by design. Set, an expiry pass is signed by a
    # `maintain`-tier credential scoped to that one table and expiring in 900s ([[LH-473]]). See `services/credentials.py` for what
    # deliberately STAYS on the root key (the whole-estate protection pre-pass, which is a read).
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
    #: Maintenance's own scoped pair (`objectStore.maintenanceAccessKey`), never the store's root (ADR 0168).
    dapr_secret_s3_field: str = Field(default="secret_key", alias="MAINTENANCE_DAPR_SECRET_S3_FIELD")
    #: The field of the same secret holding the key id the secret signs under, read with it.
    dapr_secret_s3_id_field: str = Field(default="access_key", alias="MAINTENANCE_DAPR_SECRET_S3_ID_FIELD")

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

    def s3_key_pair(self) -> S3KeyPair:
        """The key pair the sweep signs with: the store's, or the environment's when the store is off.

        Raises:
            StoreCredentialUnavailableError: The store has not confirmed the pair within its TTL (503).
        """
        if self.secrets_from_dapr:
            return store_key_pair(self)
        return S3KeyPair(access_key_id=self.s3_access_key_id, secret_access_key=self.s3_secret_access_key)

    def storage_options(self) -> dict[str, str]:
        """The Lance ``storage_options`` for opening datasets on the (HTTP) S3 endpoint, under the key pair in force."""
        # Via the shared builder — which also stamps path-style addressing; the hand-rolled copy here had
        # silently dropped it (audit 2026-07-15), leaving the sweep one object-store default away from 403s.
        pair = self.s3_key_pair()
        return lance_storage_options(self.s3_endpoint, pair.access_key_id, pair.secret_access_key.get_secret_value(), self.s3_region)


def shared_lance_session() -> lance.Session:
    """The process-wide bounded Lance session (#102). Every maintenance open threads this, so a
    tick's second dataset (and the orphan scan's 500 version checkouts) HIT the cache instead of
    minting and discarding Lance's default 1 GiB + 6 GiB ceilings per open — ceilings that dwarf
    the pod's own 512Mi limit. Caps are LRU soft bounds; session keys carry (uri, version, etag),
    so a commit bumping a version writes new keys and a new version is never served stale. An old
    version whose data files were reclaimed can be (`service_kit.lakehouse.lance_session`)."""
    from service_kit.lakehouse.lance_session import affordable_cache_bytes, lance_session

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
