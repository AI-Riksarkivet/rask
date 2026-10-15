# Polaris reference read: TABLE MAINTENANCE, and where its compute runs

Date: 2026-10-15. Asked for LH-467 (#587): rask decided the lakehouse builds nothing (compaction, index
creation and reindexing are bring-your-own compute committing through `version/create`, which judges each
commit) and maintenance only decides. This read checks that decision against Apache Polaris and Canonical's
Polaris charm. Reference only: rask is Lance-only and adopts nothing from an Iceberg catalog.

Reference trees (shallow clones in the session scratchpad):

- `polaris@250f1d00b7` = `apache/polaris` at `250f1d00b7f890eb97ff34d091e8dc76f06d5b4e`
- `charm@c11e05dd4e` = `canonical/polaris-k8s-operator` at `c11e05dd4e1fc9d5b501df4ebc1294ea587f6715`
- `lakekeeper@0d9d2f3` = the Lakekeeper clone read earlier this session (claims below re-verified against it)

Paths are relative to each repo root. `RT/` = `runtime/service/src/main/java/org/apache/polaris/service/`,
`CORE/` = `polaris-core/src/main/java/org/apache/polaris/core/`.

---------------------------------------------------------------------------------------------------------

## 1. Short answer

- **Polaris runs no compaction, no snapshot expiry, no orphan removal and no manifest rewrite.** No code
  path consumes the four maintenance policy types except the content validator. That work is the user's
  engine (Spark procedures), which Polaris only *configures* (policies) and *authorizes* (vended credentials).
- **The one maintenance-like work Polaris does itself is purge on `DROP ... PURGE`**, and it runs **inside
  the catalog server JVM**, on an in-process executor, after the drop has returned. It is off by default.
  Tasks are persisted, but nothing re-drives a task the process lost (upstream says so in the code and in an
  open bug).
- **Offloading heavy work to a separate service** (the "Delegation Service", pull and push modes) is an open
  proposal with no code.
- **The commit judge reads metadata only**: Iceberg requirements against the current `TableMetadata`, plus
  location checks and per-update-type privileges. It never opens a data file.
- **The Canonical charm deploys one Polaris workload plus a console**: no worker, no cron, no Spark, no
  maintenance option.

## 2. The policy store: maintenance rules Polaris stores and serves, never executes

- Four predefined types, all inheritable: `system.data-compaction`, `system.metadata-compaction`,
  `system.orphan-file-removal`, `system.snapshot-expiry` (`polaris@250f1d00b7:CORE/policy/PredefinedPolicyTypes.java:28-31`).
- Docs: they "define rules" for those operations, apply to Iceberg tables, namespaces and catalogs
  (`polaris@250f1d00b7:site/content/in-dev/unreleased/policy.md:48-55`); inheritance flows catalog to
  namespace to table, a lower level overrides the same type above it, so only one instance of a type
  affects a resource (`policy.md:57-87`). Engines read the effective set at
  `GET /polaris/v1/{prefix}/applicable-policies` (`policy.md:157-160`).
- The only production consumer of the four enum values is the content validator
  (`polaris@250f1d00b7:CORE/policy/validator/PolicyValidators.java:57-67`). Measured: `grep -rn
  "DATA_COMPACTION|SNAPSHOT_EXPIRY|ORPHAN_FILE_REMOVAL"` over non-test Java returns only that file and the
  enum. So the policy is a governed, inherited *configuration document* for an external maintainer.
- The upstream direction says the same: the Delegation Service text describes an external compaction job
  that authenticates, lists tables, pulls `applicable-policies?policyType=system.data-compaction` and runs
  compaction "with the parameters from policy.content"
  (https://github.com/apache/polaris/issues/3786, comment by flyrain, 2026-05-20).
- Polaris's own site points users to Spark's `remove_orphan_files` and how vended credentials apply to it
  (`polaris@250f1d00b7:site/content/in-dev/unreleased/_index.md:151`).

## 3. The task framework: purge on drop, in the server process

- **Trigger.** `DROP_WITH_PURGE_ENABLED` defaults `false`; a purge on an Iceberg table without it is a 403
  (`polaris@250f1d00b7:CORE/config/FeatureConfiguration.java:442-451`, `RT/catalog/iceberg/LocalIcebergCatalog.java:3027-3040`).
  The drop and the creation of an `ENTITY_CLEANUP_SCHEDULER` task entity are one metastore transaction
  (`CORE/persistence/transactional/TransactionalMetaStoreManagerImpl.java:1443-1463`); the catalog then
  hands the task id to the executor after the drop (`LocalIcebergCatalog.java:626-631`).
- **Where it runs.** `TaskExecutorImpl` is an `@ApplicationScoped` bean in the server, registering three
  handlers at `@Startup`: `TableCleanupTaskHandler`, `ManifestFileCleanupTaskHandler`,
  `BatchFileCleanupTaskHandler` (`RT/task/TaskExecutorImpl.java:71-72,129-143`). Its executor is a
  SmallRye `ManagedExecutor` produced in the same app (`RT/config/ServiceProducers.java:381-389`). There is
  no separate deployment for it in Polaris's Helm chart; the chart's only CronJob runs the admin tool's
  `nosql maintenance-run` (`helm/polaris/values.yaml:1288-1298`), which is metastore garbage collection
  ("deletion of stale database entries", `site/content/in-dev/unreleased/admin-tool.md:234-237`), not
  table maintenance.
- **What it does.** The table task reads the last `TableMetadata`, fans out one `MANIFEST_FILE_CLEANUP`
  task per distinct manifest (deleting its data files) and `BATCH_FILE_CLEANUP` tasks for previous
  metadata, manifest lists and statistics files, persists them, then deletes the metadata file
  (`RT/task/TableCleanupTaskHandler.java:98-165,195-297`).
- **Bounds.** `polaris.tasks.max-concurrent-tasks` and `max-queued-tasks` default `-1` (unbounded);
  `file-deletion-timeout` defaults `PT1H` (`RT/task/TaskHandlerConfiguration.java:28-41`).
- **Retry and recovery.** Three in-process attempts with linear 1s backoff; a deletion timeout is terminal
  (`TaskExecutorImpl.java:74,181-230`). A persisted task the JVM lost is not picked up again: the class
  comment says "no runtime component currently drives that recovery"
  (`RT/task/FileDeletionTimeoutException.java:26-32`), `loadTasks` has no production caller (measured by
  grep), and the bug is open: "a 'drop table with purge' returns a successful result to the user, the
  actual purge may never ever happen" (https://github.com/apache/polaris/issues/774).
- **Identity.** The task runs under a clone of the injected `PolarisPrincipal`, re-set in a fresh request
  context (`TaskExecutorImpl.java:186-192,349-353`).
- **Credentials.** The task's FileIO is built from storage access config requested with
  `PolarisStorageActions.ALL` over the table's storage location (`RT/task/TaskFileIOSupplier.java:52-71`):
  the catalog's own storage identity, scoped to the table prefix.

## 4. Offloading: the Delegation Service proposal

- Open issue, label `proposal`: pull mode (an external maintainer calls Polaris over REST for tables,
  policies, credentials, commits) and push mode (a co-deployed service Polaris calls for heavy work such as
  `DROP ... PURGE` and server-side scan planning) (https://github.com/apache/polaris/issues/3786).
- The last maintainer comment (snazy, 2026-05-26) says push mode is "still conceptual/proposed" and its
  "task lifecycle, reliability, security, request-budget, and operational contracts are still future
  work". No delegation module exists in `polaris@250f1d00b7` (grep of `settings.gradle.kts` and sources).
- The older Table Maintenance System proposal (https://github.com/apache/polaris/issues/538, open since
  2024-12) frames a TMS as independent of the catalog, with Polaris defining "what it should handle and what
  it should leave to TMS".

## 5. Commit judging

- `updateTable`/`commitTransaction` validate each Iceberg `UpdateRequirement` against the current
  `TableMetadata` (`RT/catalog/iceberg/CatalogHandlerUtils.java:377,413`) and apply the updates; locations
  are checked against the catalog's allowed storage and the table directory
  (`LocalIcebergCatalog.java:1135-1148,1326-1337`). No data file is opened.
- One compaction-aware rule: with `ICEBERG_ROLLBACK_COMPACTION_ON_CONFLICTS` (default `false`), a writer
  whose requirement fails because REPLACE (compaction) snapshots landed since its base has those snapshots
  rolled back so the writer wins, when the snapshots carry
  `polaris.internal.conflict-resolution.by-operation-type.replace=rollback`
  (`CORE/config/FeatureConfiguration.java:637-645`, `CatalogHandlerUtils.java:413-498`).
- Previous metadata files beyond `write.metadata.previous-versions-max` are deleted after commit when the
  table sets `write.metadata.delete-after-commit.enabled` (`RT/catalog/iceberg/IcebergCatalogHandler.java:1615-1618`).

## 6. Maintenance credentials and authz

- No maintenance-specific privilege. With `ENABLE_FINE_GRAINED_UPDATE_TABLE_PRIVILEGES` (default `true`)
  each update type maps to its own operation, e.g. `RemoveSnapshots` to `REMOVE_TABLE_SNAPSHOTS`, `AddSnapshot`
  to `ADD_TABLE_SNAPSHOT` (`IcebergCatalogHandler.java:1775-1820`; `FeatureConfiguration.java:755-761`).
  `TABLE_REMOVE_SNAPSHOTS` is implied by `TABLE_WRITE_DATA`, `TABLE_FULL_METADATA` and the catalog-wide
  manage privileges (`CORE/auth/PolarisAuthorizerImpl.java:369-378`). So an expiry job needs no data write
  if granted only `TABLE_REMOVE_SNAPSHOTS`; a compaction job needs `ADD_TABLE_SNAPSHOT`.
- Vended credentials: read delegation needs `TABLE_READ_DATA`, write delegation `TABLE_WRITE_DATA`
  (`CORE/auth/RbacOperationSemantics.java:319-320`); the credential is scoped to the table's locations and
  the actions the caller is authorized for (`site/content/in-dev/unreleased/vended-credentials.md:27,44-45`).
  A maintenance engine is an ordinary principal and gets ordinary table-scoped credentials.

## 7. The Canonical charm

- Containers `polaris` and `console`; resource `polaris-image` is "both server and admin tool"
  (`charm@c11e05dd4e:metadata.yaml:58-70`). One Pebble service with `on-failure: restart`
  (`src/core/workload/polaris.py:35-48`).
- Relations: `metastore` (PostgreSQL, required), `s3-credentials` (required), optional `oauth`, `ingress`,
  TLS, logging, Grafana, Prometheus (`metadata.yaml:80-131`). Config: `storage-access-model`
  (`none`|`vended`), `sts-endpoint`, `system-user` (`config.yaml:4-31`). One action, `status-detail`
  (`actions.yaml`).
- Rendered Polaris config sets persistence to `relational-jdbc` and the S3 keys; it sets no task,
  purge or maintenance property (`src/config/polaris.py:31-43,88-117`). Grep of `src/` and `README.md` for
  purge/maintenance/compaction/cron/spark finds only Juju status strings. The charm adds no maintenance
  scheduling; `DROP_WITH_PURGE_ENABLED` stays at its `false` default.

## 8. Comparison: Polaris, Lakekeeper, rask (LH-467)

| Concern | Polaris (`250f1d00b7`) | Lakekeeper (`0d9d2f3`) | rask, LH-467 |
| --- | --- | --- | --- |
| Data compaction | User engine; catalog stores an inherited `system.data-compaction` policy (§2) | User engine; never rewrites data server-side | BYO compute, commits through `version/create`; maintenance lists it as due |
| Snapshot/version expiry | User engine (Spark `expire_snapshots`); policy only (§2) | Enterprise `expire_snapshots` queue (`crates/lakekeeper/src/service/tasks/mod.rs:68`), dedicated worker pods recommended (`docs/docs/table-maintenance.md:53-56`), scheduled right after a commit (`:60`) once `min-snapshots-to-expire`=20 are due (`:33`) | Maintenance expires, triggered by commits, threshold 20, 30-min backstop sweep |
| Orphan removal | User engine; policy only; docs cover vended creds for Spark's procedure (§2) | Enterprise queue, adaptive cadence with a 1-day floor and a 24h age floor (`table-maintenance.md:66,87-106`) | Maintenance orphan scan |
| Index / metadata rewrite | `system.metadata-compaction` policy, executed by nobody in Polaris; old metadata files deleted after commit if the table opts in (§5) | Old metadata files deleted after commit by default (`table-maintenance.md:9`) | Index creation and reindex are BYO compute; due records list it |
| Purge on drop | In-process executor in the server JVM, off by default, no recovery of lost tasks (§3) | `tabular_purge` queue (`crates/lakekeeper/src/service/tasks/tabular_purge_queue.rs:69`) | Trash purge by maintenance |
| Commit judging | Iceberg requirements on metadata, locations, per-update privileges; optional compaction rollback (§5) | Requirements on metadata only (`crates/lakekeeper/src/server/commit_tables.rs:84-91`) | `version/create` judges each commit (incl. foreign file versions, provenance) |
| Maintenance creds / authz | Ordinary principal; fine-grained update privileges such as `TABLE_REMOVE_SNAPSHOTS`; table-scoped vended creds (§6). Purge task uses catalog's own `ALL` scope (§3) | Worker uses the catalog's storage identity | Engine uses vended credentials; FGA authorizes the commit |
| Scheduling trigger | None for maintenance (external, pull); purge fires on the drop request | Post-commit hook `EventListener::transaction_committed` (`crates/lakekeeper/src/service/events/dispatch.rs:452`) plus adaptive self-scheduling | Commit announcements trigger expiry; 30-min backstop sweep |

## 9. What rask could learn

1. **A per-update-type privilege for history removal.** Polaris authorizes `RemoveSnapshots` separately from
   writing data (§6), so an expiry job can hold no write grant. The rask analogue: an FGA relation that
   permits a version-expiry or cleanup commit without `can_write`. Zero trust: narrows the expiry identity;
   cloud native: neutral; Lance-idiomatic: Lance's `cleanup_old_versions` is a distinct operation, so the
   door can name it. Fits LH-467.
2. **Writer-wins over compaction at the judge.** Polaris can roll back a conflicting compaction commit in
   favour of a data writer (§5). Lance already resolves compaction against concurrent appends through its
   own conflict resolution; the inspiration is only to make the precedence explicit in `version/create`'s
   verdict, and to check it there rather than at the engine. Measure on pylance before designing anything.
3. **Inherited, typed maintenance settings as governed configuration.** Polaris's override-by-level policy
   (catalog to namespace to table) is a clean shape for rask's thresholds (min-versions 20, sweep cadence),
   read by BYO compute through an authenticated door. Zero trust and cloud native both hold; it is
   catalog configuration, not execution.
4. **Do not run purge in the catalog's pods, and make lost work recoverable.** Polaris's in-JVM purge with
   no re-drive is the failure LH-467 avoids: rask's purge lives in maintenance with a backstop sweep. Keep
   it that way; the open Polaris issue #774 is the evidence of the cost.
5. **Upstream direction agrees with LH-467.** Polaris's own pull-mode text puts compaction, expiry and
   orphan removal in an external maintainer that calls the catalog over its public doors (§4). Nothing in
   Polaris contradicts a LH-467 decision; the one difference is that Polaris decides *nothing* (no due
   records, no expiry trigger), where rask's maintenance decides and expires.

## 10. Could not confirm

- The contents of the Google Docs linked from #538 and #538's comments (TMS and policy-management
  proposals): not first-party fetchable without auth; not read.
- Whether the Delegation Service has a draft PR beyond the referenced #3990: issue text names it; the PR
  was not opened.
- Polaris's handling of maintenance for generic (non-Iceberg) tables: no maintenance code path found for
  them in `RT/catalog/generic/`, but not read in full.
- Charmhub's documentation pages: `https://charmhub.io/polaris-k8s` returned only the summary; its
  Integrations, Actions and Configurations tabs were not rendered by the fetch. Claims in §7 rest on the
  charm's source.
- Whether Lakekeeper's expire-snapshots post-commit scheduling is wired through `transaction_committed`
  specifically or a sibling hook: the hook exists at `dispatch.rs:452`, and the docs state post-commit
  scheduling (`table-maintenance.md:60`), but the enterprise wiring is not in the open-source tree.
