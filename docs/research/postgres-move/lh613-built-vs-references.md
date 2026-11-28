# LH-613 (#862, 48bd35aa0): rask's built Postgres layer compared with Lakekeeper and Polaris

I read the reference code from source at these commits on 2026-11-28:

- Lakekeeper `lakekeeper/lakekeeper` main @ `fee1729be8b30f0c84b52c3a99bc3c4fbf256a46` (paths below are under `crates/`)
- Lakekeeper chart `lakekeeper/lakekeeper-charts` main @ `732badae09566f2d1815a49ab8d7342d0dd1515b` (paths under `charts/lakekeeper/`)
- Polaris `apache/polaris` main @ `396dd238e3e097f44270ac540fcf114fdbb13b07`.

I re-read every Lakekeeper claim used here at today's main. None comes from the older 2ee5516c study.
Abbreviations: `LK-pg` = `crates/lakekeeper-storage-postgres/src`, `PJ` = `persistence/relational-jdbc/src/main/java/org/apache/polaris/persistence/relational/jdbc`.

## Summary

| # | Point | rask (built) | Lakekeeper | Polaris | Verdict |
|---|---|---|---|---|---|
| 1 | Transaction scope | One `reading()`/`writing()` per path, session passed down. Reads `SET TRANSACTION READ ONLY`. A ContextVar refuses a second transaction; the suite's pool of one catches a second connection | Transaction passed down (`StateOrTransaction`). Reads `BEGIN READ ONLY`. Separate read and write pools. No guard: a function may take the pool instead of the transaction | A connection per statement, or `runWithinTransaction` for several. No read-only mode. No guard | SAME on scope and read-only. DIFFERENT ON PURPOSE on the guard (ADR 0230 §10) |
| 2 | Isolation and create race | READ COMMITTED. `INSERT … ON CONFLICT DO NOTHING RETURNING`, then read the standing row | Default isolation. `INSERT … ON CONFLICT (id) DO UPDATE … RETURNING` with a `UNION ALL` fallback. Row locks (`FOR NO KEY UPDATE`) in a fixed order | Driver default. Unique key 23505 → already exists or retry. CAS on `entity_version` | SAME |
| 3 | Error mapping | No SQLSTATE, class 08, 57P01-03, invalidated connection, pool timeout → 503 code 17. That includes a connection lost **during COMMIT**. Everything else propagates (500) | A failed COMMIT that may have applied → `CommitStateUnknownException` (500). 40001/40P01/25P0x → 409. Unique violation → 409. Pool timeout → 500 (counted as a metric) | A COMMIT-phase failure → `AmbiguousCommitOutcomeException`, never retried, `CommitStateUnknown` → 500. Retries 40001 and refused or reset connections. Other errors → 500 | **GAP** (unknown commit outcome answered as a retryable 503) |
| 4 | Migrations | Alembic. Every pending revision in ONE transaction behind `pg_advisory_xact_lock`. No downs. Ahead → refuse, apply nothing | sqlx. Core and extensions in ONE transaction behind `pg_advisory_xact_lock(crc(db))`. Down migrations skipped. Ahead → refuse. Applied checksums verified. `SET LOCAL statement_timeout` of 60 min | No migrator. `schema.sql` is idempotent DDL with a `version` row. Upgrades are hand-applied DDL | SAME as Lakekeeper. Checksums and timeouts go to "For later" |
| 5 | Start-up schema check | Init container `wait` (polls for "behind", fails fast on "ahead"). Lifespan `require_schema` refuses behind or ahead | Init container `wait-for-db -dm`. `serve` runs `wait_for_db(true,0,0)` unless `--force-start`. Ahead fails fast | `validateSchemaCompatibility` at the first request of a realm (lazy). Exact version equality | SAME as Lakekeeper. rask has no `--force-start`, DIFFERENT ON PURPOSE (no backward compatibility) |
| 6 | How the release migrates | Plain Job named by revision, Argo CD `Sync` hook with `BeforeHookCreation`. Pods wait in an init container | Job named `…-db-migration-<Release.Revision>`, Argo `Sync` hook wave 0. A Helm post-install/upgrade hook unless `helmWait: true`, which turns it into a plain Job | No migration Job in the chart. The admin tool bootstraps by hand | SAME as Lakekeeper's `helmWait: true` mode, which is the one that works with `--wait` |
| 7 | Credentials and roles | Password file from an ExternalSecret, re-read at every new connection (rotates without a restart). Three roles: migrator owns, catalog `SELECT, INSERT`, maintenance reads the version. Grants live in the migration | `LAKEKEEPER__PG_PASSWORD` env from a Secret, read once at start (`CONFIG`). One role migrates and serves | `QUARKUS_DATASOURCE_PASSWORD` env from a Secret, or a Quarkus `credentials-provider` (AWS IAM RDS). One datasource role. A DBA creates the schema | DIFFERENT ON PURPOSE (zero trust, ADR 0230 §8-9). rask is stricter on both |
| 8 | Connection pool | 4, no overflow, 5 s acquire, 1800 s recycle, `pre_ping`, 5 s connect timeout | read 10 / write 5, 5 s acquire, `max_lifetime` unset, `test_before_acquire` false, pool gauges and acquire-timeout counter | Agroal defaults. The docs suggest `max-size 20`, `acquisition-timeout 30S` | DIFFERENT ON PURPOSE (sizing). Metrics go to "For later" (#864) |
| 9 | Principal/user record | `rask.principals(principal PK, kind CHECK, deleted_at DEFAULT now(), deleted_by)`, written once. OpenFGA revoke → row (own transaction) → event (intent) | `users(id PK, …, deleted_at)`, soft delete that scrubs PII. In ONE DB transaction: soft delete + role assignments + grants, then OpenFGA `delete_all_relations` (pre-commit), commit, events after. Re-login clears `deleted_at` | Principal = `entities` row, hard-deleted first. Grant records removed "best-effort" afterwards | DIFFERENT ON PURPOSE (ADR 0230 §6: revoke before the record, tuples outside the transaction). Re-login semantics go to "For later" |
| 10 | Database tests | A database per test, cloned `TEMPLATE` from one migrated per worker. Runs as the catalog role. Pool of one. Race staged by `LOCK TABLE` and waiting on `wait_event_type='Lock'` | A database per test from a migrated template (hash-named, built under an advisory lock). Runs as admin. Pool of 5. Races staged by the same `pg_stat_activity … wait_event_type = 'Lock'` | Testcontainers Postgres with an init script, shared per test resource | SAME as Lakekeeper. rask is stricter (role grants, pool of one) |

## Per point

### 1. Transaction scope

- **rask.** `services/catalog/src/catalog/core/database.py:111-152`.
  - `reading()` runs `SET TRANSACTION READ ONLY` (line 40) first. `writing()` runs nothing first.
  - `_transaction` refuses when `_OPEN` is set (lines 139-141, `SecondTransactionError`, answered 500).
  - The doors open a block and pass the session down (`access_admin.py:775-776`, `786-787`). The block closes before the announcement and the answer.
  - `tests/postgres_fixtures.py:68-89` sets `RASK_DATABASE_POOL_SIZE=1` with a 2 s acquire timeout, so a path that takes a second connection fails in tests.
- **Lakekeeper.**
  - `PostgresTransaction::begin_write` and `begin_read`, the latter `begin_with("BEGIN READ ONLY")` (`LK-pg/lib.rs:101-125`).
  - The transaction is passed down as `&mut sqlx::Transaction` (`lib.rs:95`, `150-152`).
  - `StateOrTransaction` (`crates/lakekeeper/src/service/catalog_store.rs:236-250`) explicitly lets a function take the pool (`State`) instead of the transaction, and read and write are two pools (`lib.rs:155-160`).
  - I found no runtime guard against a second connection (grep for task_local/thread_local/"second transaction": only an unrelated `task_drain.rs:84`).
  - A test proves read transactions refuse writes with 25006 (`lib.rs:470-485`).
- **Polaris.** `PJ/DatasourceOperations.java:262-298` (`executeUpdate`) borrows a connection per statement. `:366-399` (`runWithinTransaction`) is for callbacks. There is no read-only transaction and no guard.
- **Verdict.** SAME on scope and read-only reads. The guard is DIFFERENT ON PURPOSE: ADR 0230 §10 says "no second connection is taken inside one", which neither reference enforces.

### 2. Isolation and how a create race is closed

- **rask.**
  - No isolation level is set (default READ COMMITTED; ADR 0230 §10).
  - `services/catalog/src/catalog/services/principals.py:407-422` inserts with `on_conflict_do_nothing(index_elements=["principal"]).returning(...)`. With no row back, it reads the standing row in the same transaction. Under READ COMMITTED the SELECT is a new statement with a new snapshot, so it sees the conflicting row the ON CONFLICT waited for once that row committed.
  - Test: `services/catalog/tests/test_a_departed_principal_holds_no_grant.py:166-191` holds `LOCK TABLE … SHARE`, waits until both sessions wait on a lock (`:152-163`), then releases. Both answers are 200 with the same record.
- **Lakekeeper.**
  - No `SET TRANSACTION ISOLATION` anywhere (grep for isolation/SERIALIZABLE/REPEATABLE READ finds only a comment at `catalog_store.rs:288-289`: "at the default isolation level each of its statements sees only committed data").
  - The user upsert is `INSERT … ON CONFLICT (id) DO UPDATE … WHERE … RETURNING` with a `UNION ALL` fallback that returns the unchanged row (`LK-pg/user.rs:440-465`).
  - Delete takes `FOR NO KEY UPDATE` row locks in a fixed order (`user.rs:312-339`).
- **Polaris.**
  - No isolation set (grep for `setTransactionIsolation`/`TRANSACTION_SERIALIZABLE`: not found).
  - A uniqueness violation 23505 (`DatasourceOperations.java:71-73`, `:591`) becomes `EntityAlreadyExistsException`, or `RetryOnConcurrencyException` when the conflicting row is invisible (`JdbcBasePersistenceImpl.java:243-277`).
  - Updates CAS on `entity_version` and `grant_records_version` (`:213-237`).
- **Verdict.** SAME. A unique key at the default level, the idiom both use.

### 3. Error mapping

- **rask.** `database.py:83-108` (`_unanswered`) and `:143-151`.
  - These become `ServiceUnavailableError` (503, code 17): pool `TimeoutError`; `connection_invalidated`; `OperationalError`/`InterfaceError` whose psycopg SQLSTATE is None, class 08, or 57P01-03.
  - Anything else propagates (500).
  - The `except` wraps the whole block, including the COMMIT at the exit of `session.begin()`. The comment at line 150 says so: "A connection lost while committing leaves the commit's outcome unknown (ADR 0230 §6): say only the outage."
- **Lakekeeper.**
  - `LK-pg/dbutils.rs:11-32` (`commit_state_unknown`): a failed COMMIT is proven rolled back only by an ERROR outside 40003 and classes 08/53/57/58/XX, or by FATAL 25P03. Anything else, a missing server reply included, is unknown.
  - `lib.rs:127-140` maps that to `ErrorModel::internal(…, COMMIT_STATE_UNKNOWN_ERROR_TYPE)`. The trait contract is at `crates/lakekeeper/src/service/catalog_store.rs:431-450`: "Any other error means it was rolled back".
  - Tested per SQLSTATE in `lib.rs:507-545`, with `pg_terminate_backend` at `:547-565`.
  - `dbutils.rs:46-72`: unique violation → 409 `EntityAlreadyExists`. 2D000/25000/25001/25P01-03/40000-40004/40P01 → 409 `TransactionFailed`. Else 500 `DatabaseError`.
  - Pool timeout → 500 `DatabaseError`, plus an acquire-timeout metric (`lib.rs:102-106`, `pool_metrics.rs:56-58`).
  - Lakekeeper's own 503 rule for storage errors says "Iceberg clients may retry a 503 … so a storage error must only be returned before the catalog commit" (`crates/iceberg-ext/src/catalog/rest/error.rs:330-342`).
- **Polaris.**
  - `DatasourceOperations.java:655-669` (`classifyByPhase`): only a failure inside `commit()` that looks like a lost connection or timeout (class 08, 57014, HYT00, `:75-81`) becomes `AmbiguousCommitOutcomeException`.
  - `isRetryable` never retries one (`:489-505`). Only 40001 and "connection refused/reset" are retried.
  - `JdbcBasePersistenceImpl.java:279-290` turns it into `PersistenceCommitStateUnknownException`.
  - The REST mapper sends `CommitStateUnknownException` → 500 (`runtime/service/src/main/java/org/apache/polaris/service/exception/IcebergExceptionMapper.java:199`) and an unmapped `RuntimeException` → 500 (`:214`).
- **Verdict.** **GAP** (below). Both references separate "the database may have committed" from "the database did not answer", and never answer the first with a retryable status. rask answers both 503 code 17, which the spec and rask's own envelope (`packages/lakehouse/src/lakehouse/problems/ns_errors.py:184`) describe as "which a retry may" heal. ADR 0230 §6 itself says "A commit whose outcome is unknown is an error of its own".
  - For the principal door this is harmless today. A retry re-revokes nothing and `ON CONFLICT DO NOTHING` answers the standing record. The intent judge `PrincipalRecorded` (`api/announcing.py:364-367`) sends the event if the row landed.
  - The seam is shared by every later kind, though, and the first non-idempotent write on it (a create, a counter, an idempotency key) will invite a retry after an applied commit.
  - Serialization and deadlock failures (40001, 40P01) answer 500 in rask. That cannot happen with today's single-row insert. See "For later".

### 4. Migrations

- **rask.**
  - `services/catalog/src/catalog/core/schema.py:211-223` (`_apply`): one `connection.begin()`, then `pg_advisory_xact_lock(_MIGRATION_LOCK)`, `CREATE SCHEMA IF NOT EXISTS`, the ahead check, then `command.upgrade(head)`.
  - `migrations/env.py:16` sets `transaction_per_migration=False`.
  - `versions/r0001_principals.py:48-49`: `downgrade` raises.
  - Ahead: `_ahead` (`schema.py:126-133`) refuses before anything is applied.
  - Retrying a refused login: `_connect_patiently` (`:199-208`).
- **Lakekeeper.** `LK-pg/migrations/mod.rs`:
  - The whole run is one outer transaction (`:318-322`, `:391-395`), with `SET LOCAL statement_timeout = '60min'` and `idle_in_transaction_session_timeout` (`:397-405`).
  - `pg_advisory_xact_lock` is keyed on the database name, chosen over the session lock because `pg_advisory_unlock` is rejected on a poisoned transaction (`:407-420`, `:44-61`).
  - Down migrations are filtered out (`:466-476`).
  - `validate_applied_migrations` refuses an applied version the binary lacks (`:833-848`).
  - The checksum of every applied migration is compared and `VersionMismatch` is refused unless explicitly sha-patched (`:544-571`). The dirty flag is checked (`:589-603`).
  - Post-commit work runs in short transactions of its own (`:514-517`).
- **Polaris.**
  - No migration tool. `executeScript` runs the shipped `schema.sql` in one `runWithinTransaction` (`DatasourceOperations.java:132-162`).
  - The schema file is idempotent (`CREATE TABLE IF NOT EXISTS`, the `version` row via `ON CONFLICT DO NOTHING`, `persistence/relational-jdbc/src/main/resources/postgres/schema.sql:19-25`).
  - Upgrades are DDL the operator applies by hand, found by git-diffing `schema.sql` between tags (`site/content/in-dev/unreleased/metastores/relational-jdbc.md:213-249`).
  - No lock and no down migrations.
- **Verdict.** SAME as Lakekeeper in structure: one transaction, a transaction-scoped advisory lock, no downs, ahead refused. Polaris is weaker here. Lakekeeper's checksum and timeout guards are not in rask; see "For later".

### 5. Start-up schema check

- **rask.**
  - `schema.py:166-189` (`wait`): retries "behind" and "unanswered" until `database_schema_wait_seconds` (600 s, `chart/values.yaml` `postgres.catalog.schemaWaitSeconds`). Raises at once on `SchemaAheadError` or on a refused statement.
  - Run as an init container (`chart/templates/services.yaml:31-45`).
  - `catalog/main.py:287-299` (`_open_the_database`) calls `require_schema` before anything else in the lifespan. It refuses behind or ahead.
- **Lakekeeper.**
  - `crates/lakekeeper-bin/src/wait_for_db.rs:37-101` retries `Missing`. It fails fast on `Ahead` ("Retrying never resolves this") and on `SchemaMismatch`.
  - It reads from the writer pool so that a lagging replica cannot hide `Ahead` (`:40-45`).
  - The chart's `check-db` init container runs `wait-for-db -dm` (`lakekeeper-charts templates/_pods.tpl:22-45`, `templates/catalog/catalog-deployment.yaml:82-86`).
  - `serve` runs `wait_for_db(true, 0, 0, true)` with no retries unless `--force-start` (`crates/lakekeeper-bin/src/main.rs:472-492`).
  - The ahead detection is `migrations/mod.rs:720-745`.
- **Polaris.**
  - `DatasourceOperations.validateSchemaCompatibility` (`:598-640`) requires exact equality with `CURRENT_SCHEMA_VERSION = 6` (`:60-67`).
  - It is called once, lazily, on the first realm access (`PJ/JdbcMetaStoreManagerFactory.java:107-112`, `:237-238`). A behind schema fails a request rather than the start.
- **Verdict.** SAME as Lakekeeper: init wait plus a start check, with ahead failing fast. rask has no `--force-start`. That is DIFFERENT ON PURPOSE: the estate is pre-production and has no backward compatibility (CLAUDE.md), so no escape hatch is wanted.

### 6. How the release runs the migrations

- **rask.**
  - `chart/templates/catalog-migrate.yaml:1-44` is a Job `…-catalog-migrate-<bootstrapRev>`, annotated `argocd.argoproj.io/hook: Sync` with `hook-delete-policy: BeforeHookCreation` (`_helpers.tpl:44-47`).
  - `backoffLimit: 20`, `activeDeadlineSeconds: 1800` (`_helpers.tpl:1107-1109`), TTL 3600.
  - It is not a Helm hook (ADR 0230 §11 amendment: a post-install/upgrade hook deadlocks `--wait --wait-for-jobs` while the pods wait for the schema).
- **Lakekeeper chart.**
  - `templates/db-migration.yaml:1-24` is a Job `…-db-migration-{{ .Release.Revision }}`, `argocd.argoproj.io/hook: Sync`, `sync-wave: "0"`.
  - When `helmWait` is false it is also `helm.sh/hook: post-install,post-upgrade`, weight -100, `before-hook-creation`. When true it is a plain Job.
  - `values.yaml:19-21` states the same trade-off rask measured: "If this is false, helm install --wait will not work."
  - The Job has its own `check-db -d` init container (`db-migration.yaml:69-73`).
- **Polaris chart.** No migration or bootstrap Job. `helm/polaris/templates/` holds deployment, services, HPA, maintenance CronJob and so on, and `deployment.yaml:74-77` renders only `extraInitContainers`. Bootstrap is the admin tool (`site/.../metastores/relational-jdbc.md:165-180`).
- **Verdict.** SAME as Lakekeeper with `helmWait: true`, which is the mode compatible with `--wait`. rask always takes it, which is right for a chart deployed with `--wait` and by Argo CD.

### 7. Credentials and roles

- **rask.**
  - `database.py:162-180` (`connector`) reads `username` and `password` from `RASK_DATABASE_CREDENTIALS_DIR` in a thread on every new connection.
  - `chart/templates/_helpers.tpl:132-140` mounts the Secret `…-database-<role>` as a volume (with `items`, not `subPath`, so kubelet updates it in place).
  - `chart/templates/external-secrets.yaml:87-105` fills that Secret from the store (`refreshInterval` default "1h", `values.yaml:1907`).
  - Three roles (`values.yaml` `postgres.catalog.roles`). The fixture's `provision.sql:13-27` makes every role `NOSUPERUSER NOCREATEDB NOCREATEROLE …`, makes the migrator own the database, `REVOKE ALL … FROM PUBLIC`, and grants `CONNECT` to catalog and maintenance.
  - The migration grants catalog `USAGE` on schema `rask`, `SELECT` on `alembic_version`, and `SELECT, INSERT` on `principals`. Maintenance gets `USAGE` and the version only (`r0001_principals.py:37-41`).
  - The tests run every door as the catalog role (`tests/postgres_fixtures.py:79`), so a missing grant fails offline.
- **Lakekeeper.**
  - `LAKEKEEPER__PG_PASSWORD` comes as an env `secretKeyRef` (`lakekeeper-charts templates/_helpers.tpl:197-221`). It is read into the static `CONFIG` (`LK-pg/config.rs:16-35`) and baked into `PgConnectOptions` once (`lib.rs:334-393`), so a rotation needs a restart.
  - One role for the migration Job and the server: both use `iceberg-catalog.env` (`db-migration.yaml:77-80`, `catalog-deployment.yaml:90-96`).
  - No GRANT in any migration (grep of `migrations/*.sql` for `grant`: not found). The only role advice is an error message that says to grant USAGE and CREATE on the schema (`migrations/mod.rs:309-314`).
- **Polaris.**
  - `quarkus.datasource.username/password` come as env from a Secret (`helm/polaris/templates/_helpers.tpl:258-260`).
  - Alternatively a Quarkus `credentials-provider`, documented for AWS IAM RDS tokens (`site/.../metastores/relational-jdbc.md:63-79`).
  - One datasource user. A DBA creates the schema, and the user needs USAGE, plus CREATE for bootstrap (`:169-175`).
- **Verdict.** DIFFERENT ON PURPOSE. ADR 0230 §8-9 and the zero-trust fact call for a credential per service as a file, never through env, three scoped roles, and grants reviewed with each table. rask is stricter than both references. The password is still long-lived today (#863).

### 8. Connection pool

- **rask.**
  - `database.py:183-193`: `pool_size` 4, `max_overflow=0`, `pool_timeout` 5 s, `pool_recycle` 1800 s, `pool_pre_ping=True`.
  - `connect_timeout` 5 s and `application_name` are set at `:166-180`.
  - The values come from the chart's `postgres.catalog.pool`, each `required` (`services.yaml:96-98`).
- **Lakekeeper.**
  - Defaults are read 10 and write 5 connections, `pg_acquire_timeout` 5, `pg_test_before_acquire` false, `pg_connection_max_lifetime` None (`LK-pg/config.rs:39-58`), applied in `lib.rs:313-325` and `crates/lakekeeper-bin/src/serve.rs:151-162`.
  - Pool gauges are sampled into Prometheus (`lib.rs:246-262`, `pool_metrics.rs`). A pool health `SELECT 1` feeds `/health` (`lib.rs:162-209`).
- **Polaris.** No pool setting in the repo (grep for `jdbc.max-size` and similar finds only docs). The Helm docs suggest `min-size 5`, `max-size 20`, `acquisition-timeout 30S`, `idle-removal-interval 5M` via `advancedConfig` (`site/content/in-dev/unreleased/helm-chart/persistence.md:94-104`).
- **Verdict.** DIFFERENT ON PURPOSE.
  - Size: one pool sized per replica, with the server's connection limit named in `values.yaml`.
  - Recycle: inside the credential's life, ADR 0230 §8.
  - Pre-ping: rask's choice. Lakekeeper turns it off for latency (`lib.rs:596`: "Lakekeeper's default"). That is a cost trade-off, not a defect.
  - rask has no pool metrics or pool health. That belongs to #864.

### 9. The principal/user record

- **rask.**
  - `r0001_principals.py:25-33`: `principal TEXT PK`, `kind TEXT NOT NULL CHECK IN ('person','service')`, `deleted_at timestamptz NOT NULL DEFAULT now()`, `deleted_by TEXT NOT NULL`. The model is `core/rows.py`. The catalog has no UPDATE or DELETE grant.
  - Door order (`access_admin.py:755-779`):
    1. Refusals (git declares it, stranding).
    2. An announcement intent filed.
    3. Revoke every tuple naming the principal in OpenFGA.
    4. Then a `writing()` transaction for the row.
    5. Then the event.
  - A re-run revokes what was granted since and answers the first record.
- **Lakekeeper.**
  - `users(id text PK, name, email, user_type, last_updated_with, deleted_at timestamptz)` (`LK-pg/migrations/20241009122911_users_and_roles.sql:7-18`).
  - `delete_user` soft-deletes: `deleted_at = now()`, name scrubbed to 'Deleted User', email null, only while `deleted_at IS NULL`. It deletes the role assignments and the sync log (`LK-pg/user.rs:300-381`).
  - The API handler (`crates/lakekeeper/src/api/management/v1/user.rs:536-600`):
    1. Opens a write transaction.
    2. Soft-deletes.
    3. Deletes grants in the same transaction (`:568-571`).
    4. Calls OpenFGA `delete_user` (= `delete_all_relations`, `crates/authz-openfga/src/authorizer.rs:784-790`) **before the commit**, propagating its error ("a best-effort cleanup could leave grants a re-provisioned user inherits", `:573-578`).
    5. Commits, then emits the events, inside `run_to_completion` so that a cancelled request cannot separate them (`catalog_store.rs:272-286`).
  - A re-login upserts with `deleted_at = null` (`user.rs:452-458`), reviving the user.
- **Polaris.** A principal is an `entities` row (`schema.sql:28-50`) plus `principal_authentication_data` (`:98`). `dropEntity` deletes the entity row first, then grant records as a "Best-effort cleanup" (`polaris-core/src/main/java/org/apache/polaris/core/persistence/AtomicOperationMetaStoreManager.java:184-215`). No tombstone.
- **Verdict.** DIFFERENT ON PURPOSE. ADR 0230 §6 says "a failure leaves a grant missing … never a grant too many; a tuple that removes access … is written before the commit". rask revokes first, outside any database transaction (§10: never hold one across OpenFGA), and an intent covers the event (§7).
  - Lakekeeper holds a write connection open across the OpenFGA call. rask does not.
  - Polaris's order (row first, grants best-effort) is the one ADR 0230 rules out.
  - rask's record is a write-once tombstone and never revives. Lakekeeper revives on re-login. See "For later".

### 10. Tests of the database layer

- **rask.**
  - `tests/postgres_server.py:157-194`: `build_template` creates the three roles and `rask_template`, migrated with the real `migrate`. Then `CREATE DATABASE … TEMPLATE` per test, dropped on pass and kept on failure (`postgres_fixtures.py:38-60`).
  - `catalog_database`: catalog role, pool of one, 2 s acquire.
  - The race is staged by a held lock and `wait_event_type='Lock'` (point 2).
  - Migration races: `test_the_release_migrates_the_catalogs_schema_once.py:54` (two at once), `:75` (ahead refused, nothing applied), `:109`.
  - The start check: `test_a_catalog_refuses_to_start_against_a_schema_it_was_not_built_for.py:54`.
- **Lakekeeper.**
  - `LK-pg/test_utils/test_db.rs:1-244`: a per-test database copied from a migrated template. The template's name derives from a hash of the migrations and Rust sources (`:200-216`), and it is built under an advisory lock (`:21-22`, `:95-126`) on a pool of 1 (`:140`).
  - The test pool has 5 connections (`:56`). Tests connect as the admin `DATABASE_URL` user.
  - Races are staged by `pg_stat_activity … wait_event_type = 'Lock'` (`LK-pg/role_assignment.rs:3062`, `warehouse.rs:2164`, `tasks/task_schedule.rs:1166`).
  - Lock-set assertions are used where a blocked create never returns (`tabular/mod.rs:2233-2249`).
  - `lib.rs:590-637` checks that a cancelled `BEGIN` leaves no open transaction.
- **Polaris.** Testcontainers Postgres with an init script per Quarkus test resource (`runtime/test-common/src/main/java/org/apache/polaris/test/commons/PostgresRelationalJdbcLifeCycleManagement.java:29-75`). No per-test database or template found.
- **Verdict.** SAME as Lakekeeper (template clone, lock-wait staging), and stricter: role grants are exercised and the pool of one catches a second connection. rask has no test that a cancelled transaction start leaves no open transaction. See "For later".

## GAPs to fix in rask

1. **An unknown commit outcome is answered as a retryable outage.**
   - **Where:** `services/catalog/src/catalog/core/database.py:142-151`. The one `try` covers both the block body and the COMMIT at the exit of `session.begin()`, and `_unanswered` maps a connection lost at either point to `ServiceUnavailableError` (503, code 17, "a retry may" heal).
   - **Why it matters:**
     - ADR 0230 §6 says an unknown commit outcome is "an error of its own".
     - Lakekeeper (`LK-pg/dbutils.rs:11-32`, `lib.rs:127-140`) and Polaris (`DatasourceOperations.java:655-669`, `:489-495`) both classify a COMMIT-phase failure separately and never answer it as retryable. Lakekeeper's own rule is that a 503 "must only be returned before the catalog commit" (`iceberg-ext/.../error.rs:335-337`).
     - The principal door is idempotent, so no failure today. Every later kind ADR 0230 moves onto `writing()` (registries, claims, idempotency keys, outbox rows) inherits a 503 that invites a client to replay a write that may already have landed.
   - **Fix:**
     - Run the commit as its own step in `_transaction`: yield inside `session.begin()` without committing, then `await session.commit()` in its own `try`, or track a `committing` flag before the block exits.
     - A failure there that `_unanswered` (or Lakekeeper's broader rule: anything but a plain ERROR outside 40003/08/53/57/58/XX, or FATAL 25P03) does not prove rolled back raises a distinct `CommitOutcomeUnknownError`, mapped to 500 with its own problem type, never 503 and never `Retry-After`.
     - Failures before the commit keep today's 503.
     - One test in the seam: commit with the backend terminated (`pg_terminate_backend` of the session's pid, as `LK-pg/lib.rs:547-565` does) answers the unknown error, not 503.
     - Take this into #864 with the 503 work, since that row defines the retry hint.
   - **Fixed** in the change that added this file (2026-11-28), not in #864. `writing()` flushes, then commits as a step
     of its own, and every answer of the database leaves the seam as the spec's error (`_answered`). Before the commit, no
     answer is the outage (503, code 17) and a refusal an `InternalError` (500, code 18). A commit is a refusal only when
     its answer proves the rollback (an ERROR outside 40003 and classes 08, 53, 57, 58 and XX, as Lakekeeper reads it; a
     FATAL 25P03, a session ended idle in its transaction, answers 503), and `CommitOutcomeUnknownError` otherwise (500,
     code 18, `lakehouse.problems.ns_errors`). `reading()` keeps its commit inside the outage mapping: a read-only
     transaction applies nothing. The request is marked when a write's commit begins, so a request deadline passing after
     the mark answers the same 500, and a request the deadline answered 503 refuses every commit after it (ADR 0203,
     amendment of 2026-11-28, owner). Pinned by
     `services/catalog/tests/test_a_departed_principal_holds_no_grant.py` (a session ended at its insert answers 503; at
     its commit, 500 `CommitOutcomeUnknownError`; a commit a deferred check refuses, 500 `InternalError` and no record; an
     offboarding held at its commit past the deadline, 500, then applied; one held before it, 503, never applied; a
     session ended idle in its transaction, 503) and
     `packages/lakehouse/tests/test_a_request_is_answered_whatever_its_door_does.py`.

There is no other GAP. Every other difference is on purpose, or no worse than a reference.

## For later rows

- **#864 (database unavailable → 503, retry hint, readiness):**
  - Readiness: Lakekeeper refreshes a pool health of `SELECT 1` per pool (`LK-pg/lib.rs:162-209`) and samples pool gauges with an acquire-timeout counter (`lib.rs:102-106`, `:246-262`, `pool_metrics.rs:56-64`). rask has no pool metric. Its first saturation signal would be 503s.
  - Retry-After belongs only on pre-commit failures (GAP 1).
  - Decide 40001/40P01/55P03: Lakekeeper answers 409 "TransactionFailed" (`dbutils.rs:57-66`), Polaris retries 40001 in-process (`DatasourceOperations.java:497-500`, `:507-560`). In rask they are 500 today. That is harmless with one single-row insert, but it matters once a transaction takes row locks (deadlock 40P01).
  - Bound a stuck transaction server-side. rask sets no `statement_timeout`, `lock_timeout` or `idle_in_transaction_session_timeout` (grep of `database.py`, `schema.py`, `provision.sql`: none). The request deadline answers 503 but lets the door run on (`packages/lakehouse/src/lakehouse/service_app/deadline.py:1-15`), so four doors blocked on a lock hold the whole pool of 4. Lakekeeper sets these only on the migration (`migrations/mod.rs:397-405`). A per-role `ALTER ROLE … SET` or a connect-time `options` is the place.
- **#863 (expiring credentials):**
  - rask's per-connection file read already lets a rotated password reach new connections without a restart, which neither reference does (Lakekeeper's static `CONFIG`; Polaris env). The remaining work is a short-lived credential, and `pool_recycle` must stay below its life.
  - Polaris's route is a Quarkus `credentials-provider` called per connection (`relational-jdbc.md:63-79`, AWS IAM RDS). The equivalent here is the secret store's database secrets engine issuing a dynamic role.
  - A login the server refuses reads as "unanswered" (503) in rask (`database.py:93-96`, psycopg gives no SQLSTATE). An expired credential would therefore look like an outage. Name it in readiness.
  - The ExternalSecret `refreshInterval` of 1h plus kubelet sync bounds how stale the file is.
- **Migrations, from the next revision on:**
  - Lakekeeper refuses an applied migration whose checksum changed (`migrations/mod.rs:558-571`). Alembic records only the revision id, so a revision edited in place after the live estate applied it is silently not re-run. That is plausible in a pre-production estate with no backward compatibility. Either forbid editing an applied revision (a lint over `versions/` against `origin/main`), or store a checksum per revision in a table beside `alembic_version` and refuse a mismatch.
  - Old pods keep serving while the `Sync` Job migrates (Lakekeeper says the same, `migrations/mod.rs:497-498`). Every revision must therefore be readable and writable by the previous image: expand, then contract, and never a rename or a drop in the release that stops using it. ADR 0230 §11 does not say this yet.
  - A DDL statement waiting for `ACCESS EXCLUSIVE` behind a catalog transaction blocks every new query on that table. Set `SET LOCAL lock_timeout` in `_apply` (`schema.py:217-223`) before the first DDL, so a migration fails and the Job retries rather than stalling the catalog. Lakekeeper's 60-minute `statement_timeout` is a backstop only. rask's `activeDeadlineSeconds: 1800` already bounds the run.
- **Re-login after offboarding:** Lakekeeper revives a deleted user on the next login (`user.rs:452-458`). rask's record is write-once and never cleared, so a principal granted again after offboarding still reads "deleted" at `GET /principals/{p}`. The owner should state whether that record means "was offboarded once" (today) or "is offboarded now".
- **Cancellation:** Lakekeeper tests that a request dropped during `BEGIN` leaves the pooled connection idle (`lib.rs:590-637`), and runs commit plus events to completion on their own task (`catalog_store.rs:272-286`). rask's deadline lets doors finish, and its intents cover the event, but no test shows that a cancelled `writing()` (server shutdown) returns a clean connection to the pool.
- **`_OPEN` and tasks:** a task created inside a `reading()`/`writing()` block copies the context with `_OPEN=True` and can never open a transaction of its own. No door does this today (the principal door's block holds only `record_deletion`). Later kinds should keep task creation out of the block, or the guard should key on the task as well as the context.

## Could not confirm

- **SQLAlchemy and psycopg behaviour on asyncio cancellation inside `_transaction`** (whether the connection is invalidated or returned mid-transaction). I did not read SQLAlchemy's source or run it. I looked only at rask's `database.py` and `deadline.py`.
- **That rask's 503 carries code 17 and no `Retry-After`.** This is taken from the brief and from `ns_errors.py:184` and `envelope.py:62-104`. I did not trace the handler that renders `ServiceUnavailableError`.
- **The platform Postgres version.** `REVOKE ALL ON DATABASE … FROM PUBLIC` (`provision.sql:26`) leaves PUBLIC's CREATE on schema `public` on servers before 15. The fixture is 16.13 (`deploy/fixtures/postgres/chart/values.yaml:7`). I did not find a stated minimum in `docs/OPERATORS.md`.
- **Polaris HTTP status for a non-ambiguous database outage.** I found no mapping to 503 for SQL errors. `IcebergExceptionMapper.java:214` defaults to 500, and I did not trace every wrapping.
- **Polaris pool defaults.** They are Quarkus or Agroal's, not in the repo. I did not read Quarkus.
- **The prior study `docs/research/postgres-move/polaris-gravitino-unity.md`.** I did not re-read it. Gravitino and Unity were out of scope for this check.
