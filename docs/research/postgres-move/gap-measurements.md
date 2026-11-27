# Gap measurements M1-M5 (2026-11-26)

Measured at `main` 4f6af2408, read-only on the repository. Versions: pylance 14.0.3, moto 5.2.2, pyarrow 26.0.1, CPython
3.15.2, aarch64 (Ubuntu 24.04, 20 cores, load about 2.5). Scripts live in
the measuring session's scratch directory, which was not kept (called `$G` below),
each run as `uv run --no-sync python $G/<script> ...` from the repository root. No design is recommended here.

**How the namespace was built.** Through the catalog's own `catalog.core.namespace.build_namespace(settings)` with
`tests/catalog_settings.catalog_settings(LANCE_S3_ENDPOINT=..., LANCE_REST_ROOT=...)`: a `DirectoryNamespace` on the
shared Lance session with `inline_optimization_enabled="false"` and `dir_listing_enabled="false"`
(`services/catalog/src/catalog/core/config.py:236-250`, `namespace.py:84`). A table was added as `declare_table` adds one
(`services/catalog/src/catalog/services/tables/creation.py:261-265`): a `.lance-reserved` marker PUT at `<root>/<uuid>`,
then `register_table(id=["ns", name], location=<uuid>)`. Table `t0` is a real dataset (3 rows); the rest are declared only.

**How requests were counted.** `$G/proxy.py`, a copy of `tests/integration/store_requests.py`'s forwarding proxy
that also records response bytes, status and the keys of batch deletes. Moto ran as `ThreadedMotoServer` in a child
process. **Measurement trap found:** the proxy as written in `store_requests.py` (and my first copy) sends headers and
body in separate writes without `TCP_NODELAY`, which adds about 40 ms (Nagle plus delayed ACK) to many requests. An A/B
(`$G/m1_latency_ab.py`, 100 tables, median of 20) after setting `disable_nagle_algorithm` and `TCP_NODELAY` in the copy:
describe 6.7 ms direct vs 7.0 ms proxied, list 6.7 vs 7.2, register 36.1 vs 34.6. Request counts were the same either
way. Times marked (N) below came from the old proxy and are inflated; their counts are valid.

## M1. What the name registry costs as it grows

Scripts: `$G/m1_registry.py <s3|local> <N,...> <out.json>` (build, then measure); `$G/m1_s3_from_local.py` (the N=10,000
moto row: the latest `__manifest` version file, its one live data file and `t0` copied into a moto bucket from a local
build, then measured). Results: `m1_s3.json` (moto 100 and 1,000), `m1_s3_10k.json` (moto 10,000), `m1_local.json`
(local 100, 1,000, 10,000), `m1_local30k.json` (local 30,000). Median of 20 calls. "Fresh" means a new `build_namespace`
before each call (on the shared session, as rask rebuilds an expired connection). "Warm" means one connection reused.

### Shape of `__manifest` (no inline optimization, no version cleanup during the run)

| N tables | live fragments | live data file (approx.) | data files kept | data bytes kept | version files | all `__manifest` bytes |
|---|---|---|---|---|---|---|
| 100 | 1 | 6.5 KB | 101 | 445 KB | 102 | 0.53 MB |
| 1,000 | 1 | 38.7 KB | 1,041 | 26.5 MB | 1,042 | 27.4 MB |
| 10,000 | 1 | 328 KB | 10,081 | 1.70 GB | 10,082 | 1.70 GB |
| 30,000 | 1 | about 0.96 MB (inferred) | 30,001 | 14.43 GB | 30,002 | 14.45 GB |

Each `register_table` and `deregister_table` rewrites the whole table list as a new data file and adds one version,
so the live table stays at 1 fragment and the stored bytes grow with the square of N until versions are reclaimed.
(The catalog's reclaim tick removes old `__manifest` versions, per `NamespaceConnections`' docstring; it did not run here.)
Live file sizes come from what a full read pulled (`describe_namespace` at 100 and 1,000; the uploaded file at 10,000).
The 30,000 figure is inferred from the growth of total bytes.

### Read costs on moto (patched proxy for 100 and 1,000)

Each read made the same requests fresh or warm: one HEAD of the latest version file plus GETs of the data file. No
connection kept the list in memory between calls.

| op | N=100 ms (fresh / warm) | N=1,000 ms | N=10,000 ms (N) | requests | bytes read 100 / 1,000 / 10,000 |
|---|---|---|---|---|---|
| describe_table (t0) | 7.6 / 5.8 | 8.6 / 7.8 | 50.1 / 49.9 | HEAD 1, GET 2 | 13.1 KB / 77.4 KB / 83.9 KB |
| describe_table (declared) | 7.9 / 6.8 | 8.9 / 7.7 | 49.7 / 50.0 | HEAD 1, GET 2 | same |
| table_exists | 7.0 / 8.1 | 8.5 / 7.8 | 49.6 / 49.1 | HEAD 1, GET 2 | same |
| list_tables first page | 7.9 / 7.8 | 9.0 / 8.4 | 12.0 / 11.6 | HEAD 1, GET 2 | 13.1 KB / 77.4 KB / 320.6 KB |
| list_tables full walk | 7.5 / 7.4 | 9.4 / 8.9 | 12.2 / 10.9 | HEAD 1, GET 2 | same as first page |
| describe_namespace | 5.6 / 5.4 | 6.4 / 5.2 | 6.7 / 5.7 | HEAD 1, GET 1 | 6.5 KB / 38.7 KB / 80.9 KB |
| build_namespace (connect) | 14.6 | 54.7 | 44.1 (N) | LIST 1 (`prefix=__manifest/`) | 24 KB / 235 KB / 0.5 KB* |

`list_tables` with no `limit` returned **every table in one page** at every N (100, 1,000, 10,000 and 30,000 tables;
`full_walk_pages` = 1). At 10,000 a describe read 84 KB of the 328 KB file, while a listing read all of it.
*The connect LIST returns `__manifest/`'s keys, so its size follows the number of version and data files (one page, up
to 1,000 keys). The 10,000 row held only 2 objects, so its 0.5 KB is not representative.

### Write costs on moto (one more table, warm connection, marker PUT excluded)

| op | N=100 | N=1,000 | N=10,000 (N) |
|---|---|---|---|
| register_table, ms | 41.3 | 89.6 | 220.0 |
| register_table, requests | 13 (LIST 1, HEAD 5, GET 5, PUT 2) | 13 (same) | 18 (LIST 1, HEAD 5, GET 9.75, PUT 2) |
| register_table, bytes read / written | 56 KB / - | 391 KB / - | 620 KB / 328 KB |
| deregister_table, ms | 40.3 | 95.2 | 298.5 |
| deregister_table, requests | 11 (HEAD 4, GET 4, PUT 2, LIST 1) | 11 | 16 (HEAD 4, GET 9, PUT 2, LIST 1) |

The two PUTs are the new data file and the version file. The written bytes are the whole list again.

### Local directory (same ops, ms, median of 20)

| op | 100 | 1,000 | 10,000 | 30,000 |
|---|---|---|---|---|
| describe_table (t0), fresh / warm | 1.24 / 1.11 | 1.17 / 1.34 | 2.26 / 2.42 | 2.75 / 2.53 |
| table_exists, fresh / warm | 0.85 / 1.20 | 0.76 / 1.05 | 2.53 / 2.49 | 2.58 / 2.11 |
| list_tables full walk, fresh / warm | 1.31 / 1.27 | 1.34 / 1.59 | 5.26 / 5.23 | 10.29 / 9.02 |
| describe_namespace, fresh / warm | 1.10 / 0.84 | 0.99 / 0.77 | 2.50 / 2.27 | 2.57 / 2.22 |
| build_namespace | 0.49 | 0.31 | 1.90 | 5.37 |
| register_table (+1) | 3.14 | 7.45 | 22.16 | 46.51 |
| deregister_table (-1) | 2.90 | 7.30 | 20.31 | 46.19 |

### Build rate

Local: 1,000 tables took 5 s, 10,000 took 141 s, 30,000 took 775 s. The 29,000th to 30,000th took 42 s (about 42 ms
each). Moto with the patched proxy: 100 took 3.9 s, 1,000 took 67.4 s (11,702 requests). **Extrapolated, not
measured:** a register costs about 0.22 s near 10,000 on moto, so building 10,000 there takes about 15-25 minutes. That
is why the moto 10,000 row was measured on a copied `__manifest`, which also means that row has no old versions in its
bucket.

## M2. Concurrent creates under one root (moto)

Scripts: `$G/m2_concurrent.py` (one shared connection, as the catalog shares one per root in a process),
`$G/m2b_separate_connections.py` (each thread its own `DirectoryNamespace` and `lance.Session`, standing in for separate
replicas), `$G/m2c_conflicts_on_wire.py` (one round of each through the proxy, to count refused commit PUTs). Results
are in `m2.json` and `m2b.json`. Each round used a fresh bucket, with a barrier start and DIFFERENT table names. Every
failed call would have been retried up to 10 times. Serial means the same count registered one at a time on another fresh bucket.

| threads | connections | rounds | first-try success | exceptions raised | wall s (median) | serial s | duplicates / missing |
|---|---|---|---|---|---|---|---|
| 2 | shared | 5 | 10/10 | none | 0.044 | 0.052 | 0 / 0 |
| 8 | shared | 5 | 40/40 | none | 0.169 | 0.214 | 0 / 0 |
| 32 | shared | 5 | 160/160 | none | 0.701 | 0.895 | 0 / 0 |
| 2 | separate | 5 | 10/10 | none | 0.080 | 0.051 | 0 / 0 |
| 8 | separate | 5 | 40/40 | none | 0.836 | 0.214 | 0 / 0 |
| 32 | separate | 5 | 160/160 | none | 7.55 | 0.906 | 0 / 0 |

No call raised, so there was no loser exception to report. After every round, `__manifest` (read as a Lance table) named
each table exactly once and `list_tables` agreed. The conflict happens inside pylance, which retries on its own. What
reached the store (`m2c`, one round each):

| threads | connections | wall s | requests | version-file PUTs 200 / 412 | data-file PUTs |
|---|---|---|---|---|---|
| 8 | shared | 0.21 | 89 | 8 / 0 | 8 |
| 8 | separate | 0.80 | 312 | 8 / 16 | 24 |
| 32 | shared | 0.76 | 329 | 32 / 0 | 32 |
| 32 | separate | 7.53 | 4,006 | 32 / 285 | 317 |

On one shared connection, writes did not collide at the store: wall time came out slightly below serial and nothing was
refused. Inferred: the connection orders its writes in-process. With separate connections, each refused conditional PUT
(412) cost a rewrite of the data file. At 32 writers that meant 12x the requests and 8x the serial time.

## M3. What one commit costs in control-root requests today

Script: `$G/m3_commit_cost.py`, result `m3.json`. It runs the real catalog app under uvicorn through the stock-client
suite's own helpers (`_serve`, `_created`, `_keyed` from
`services/catalog/tests/test_a_stock_client_commits_every_operation_through_version_create.py`), on a moto bucket used as
both root and control root. The catalog reaches the store through one counting proxy. The stock client reaches it
through another, set as `LANCE_S3_VENDED_ENDPOINT`, as `served_through_a_proxy` does (that test file, lines 184-215).
Setup: FGA stubbed to allow, OIDC verifier mocked, `LANCE_CONTROL_EMIT_ENABLED=false`, lineage emit off, no outbox URIs,
reconciler period 86,400 s. Run with `RASK_INSECURE_ALLOW_UNAUTHENTICATED=true RASK_INSECURE_ALLOW_STATIC_STORE_KEY=true`,
as the root conftest sets them. Each case ran after two warm-up commits, five times, counting from 1 s after each answer.
Numbers are the median per run, and the five runs agreed except where noted.

| key prefix | insert door (`POST /v1/table/{id}/insert`, append, 1 row) | stock append via `version/create` (all catalog calls it makes) |
|---|---|---|
| table's own Lance objects | 26 (HEAD 6, GET 5, LIST 4, PUT 3, MPU init/part/complete 2/2/2, batch delete 2) | 40 (HEAD 13, GET 9, LIST 6, MPU 3/3/3, batch delete 3) |
| `__manifest/` | 18 (HEAD 6, GET 12) = 6 namespace lookups | 39 (HEAD 13, GET 26) = 13 namespace lookups |
| `_locations/` | 7 GET | 12 GET |
| `_trash/` | 3 HEAD | 2 HEAD |
| `_commit_turns/` | 2 GET | 2 GET |
| `_contracts/` | 1 HEAD | 1 HEAD |
| `_commit_pending/` | 1 PUT | 1 PUT |
| `_file_identities/` | 1 PUT | 1 PUT |
| `_commit_authors/` | 1 PUT | 1 PUT |
| `_bases/` | 0 | 1 GET |
| bucket root | 0 (1 HEAD in 1 of 5 runs: the store health probe) | 0 (same) |
| **catalog total** | **60** (59-61) | **100** (100-101) |
| stock client's own requests | 0 | 6 (PUT 3, HEAD 1, GET 1, LIST 1) |
| wall time | 0.25-0.41 s | 0.45-0.49 s |

The batch deletes remove the table's own temporary `_versions/d<n>.manifest` objects and, for the stock commit, its
staged `_versions/<n>.manifest-<uuid>`. Their keys were captured, so they are counted under the table. The
`_commit_pending` entry PUT during a commit was not deleted within the 1 s window. No `_idempotency/`, `_history_fences/`,
`_table_inputs/`, `_announcement_intents/` or outbox request appeared, **with emit and outboxes off**. Control-root
requests excluding `__manifest`: 16 for the insert, 21 for the stock commit. Including `__manifest`: 34 and 60.

**Existing budgets:** none for a commit. `tests/integration/test_a_door_judges_whether_a_table_is_only_declared_by_its_storage.py:315-319`
asserts one LIST of `_versions/`. `tests/integration/test_maintenance_costs_follow_what_changed.py:126-155` counts
maintenance passes, not commits. `services/catalog/src/catalog/core/config.py:239-241` records 12 requests per create and
3 per describe at 100 and 1,000 objects on pylance 13.0.0, which matches M1's 3 per describe.

## M4. A real Postgres for the test suite without docker

**(a) On the host:** no server. `command -v postgres pg_ctl initdb` finds nothing. Only `postgresql-client-16`
(16.15) is installed: `/usr/lib/postgresql/16/bin` holds `psql`, `pg_dump`, `pg_isready` and so on, but no `postgres`,
`initdb` or `pg_ctl`. `/usr/bin/psql` exists.

**(b) Exported through Dagger, the `make openfga` way.** `make openfga` runs
`$(DAGGER) core container from --address=$(OPENFGA_IMAGE) file --path=/openfga export --path=.localbin/openfga`
(`Makefile:64-72`, image pinned in `.dagger/governed.go:14`; `DAGGER` carries the `dagger-engine-rask` runner prefix,
`Makefile:58-59`). The same pattern against the dev fixture's pinned image
`postgres:16.13@sha256:5d143123fdf80462d1778cd4f24b9f7ca13c87174bca19141fb194c5a1ebca59` (Debian trixie, multi-arch) ran
on dagger v0.21.8 with `_EXPERIMENTAL_DAGGER_RUNNER_HOST=docker-container://dagger-engine-rask`:

```
dagger core container from --address=<image> directory --path=/usr/lib/postgresql/16 export --path=$G/pg/usr/lib/postgresql/16
dagger core container from --address=<image> directory --path=/usr/share/postgresql/16 export --path=$G/pg/usr/share/postgresql/16
dagger core container from --address=<image> file --path=/usr/share/postgresql/postgresql.conf.sample export --path=$G/pg/usr/share/postgresql/postgresql.conf.sample
dagger core container from --address=<image> file --path=/usr/lib/aarch64-linux-gnu/libicu{i18n,uc,data}.so.76 export --path=$G/pg/extra/...
```

`$G/pg` holds exactly that export. Its parts: `usr/lib/postgresql/16` (bin and lib, 48 MB),
`usr/share/postgresql/16` (3.7 MB), the `postgresql.conf.sample` the Debian share dir symlinks to one level up, and
`extra/` (the three ICU 76 libraries, 37 MB; the host has ICU 74, and `ldd` showed `libicui18n.so.76` and
`libicuuc.so.76` as not found). The binaries are glibc, dynamically linked aarch64. Every other library resolved on the
host. **The install relocates:** `pg_config --sharedir` answered the exported path, because Debian's bin and share
directories keep their relative layout.

**A server started from it, as uid 1001, non-root:**
`LD_LIBRARY_PATH=$G/pg/extra $B/initdb -D data -U rask --auth=trust -E UTF8 --no-sync` took 0.23 s.
`pg_ctl -D data -o "-k '' -p 55432 -c listen_addresses=127.0.0.1" -w start` took 0.10 s. `psql` created a table, inserted
1,000 rows and counted them, and the server answered `PostgreSQL 16.13 (Debian 16.13-1.pgdg13+1) on aarch64-unknown-linux-gnu`.
Stop took 0.10 s and the data directory came to 39 MB (then deleted). Two traps:
- The Unix socket cannot live under the scratch dir: the path exceeds the 107-byte limit. Use TCP or a short socket dir.
- The first `initdb` failed until the `postgresql.conf.sample` symlink target was exported.

What a `.dagger` function would export, inferred from this run: the two directories plus that one file, and the ICU
libraries, or a host-matched base image. Alpine `postgres:16-alpine` (the image `.dagger/governed.go:16` uses for the
lanes) is musl-linked and would not run on this glibc host. Not tried on x86_64.

**(c) Wheels (PyPI JSON, read 2026-11-26):**

| package | latest | what it gives |
|---|---|---|
| `pgserver` 0.1.4 (2024-06) | cp39-cp312 only | manylinux x86_64, macOS and win wheels; no linux aarch64, no cp315 |
| `postgresql-wheel` 14.1.2 (2021) | x86_64 only | Python under 3.10 |
| `pytest-postgresql` 10.0.0 | pure Python | needs a server binary |
| `testing.postgresql` 1.3.0 (2016) | pure Python | needs a server binary |
| `py-pglite` 0.5.3 | no server wheel | PGlite, via Node |

No candidate ships a server for this host's Python 3.15 on aarch64.

**(d) The dev estate's Postgres** is `deploy/fixtures/postgres`:
- A StatefulSet `rask-postgres` running image `postgres:16.13@sha256:5d14…` (`chart/values.yaml:18`, live pod confirmed
  by `kubectl get pods`), with a 1 Gi claim, user `lance` and database `openfga` (`chart/values.yaml:15-26`). It runs as
  uid 999 with a read-only root filesystem (`chart/templates/postgres.yaml:51-70`).
- Its password is `POSTGRES_PASSWORD_FILE=/run/secrets/postgres/password` (`postgres.yaml:63`), from Secret
  `rask-postgres-auth`. That Secret is built by an ExternalSecret reading `postgres`/`password` through rask's SecretStore
  `rask-vault` (`postgres.yaml:11-23`).
- The value is generated by an ESO `Password` generator (40 characters) named `rask-dev-credential-postgres-password`
  (`chart/templates/external-secrets.yaml:16-25`, `_helpers.tpl:442-449`). The OpenBao seed writes it with
  `bao kv put secret/postgres password=@…` (`chart/templates/openbao-seed.yaml:444`).

**Lanes:** `.dagger/governed.go:100-127` already runs Postgres as a Dagger service for OpenFGA. It uses
`postgres:16-alpine` with `POSTGRES_HOST_AUTH_METHOD=trust`, a cache volume on the data directory, `WithExposedPort(5432)`
and `AsService()`, bound as `fga-postgres`, and waits with a `pg_isready` loop. The test lane copies the OpenFGA binary
in with `WithFile` (`.dagger/test.go:110`), so a test lane could bind a database the same way or copy an exported server.

## M5. Short-lived database credentials on the development secret store

- **Store and version:** OpenBao `openbao/openbao:2.2.0` (`deploy/fixtures/openbao/chart/values.yaml:14`; live pod
  `rask-openbao-7c8b9b9fff-5p4sw` confirmed by `kubectl get pods`). It uses `storage "inmem"` with TLS disabled and is
  not a `-dev` server (`deploy/fixtures/openbao/chart/templates/openbao.yaml:56-71`).
- **Engines enabled today:** one secrets engine, KV version 2 at `externalSecrets.kvMount` (`secret`)
  (`chart/templates/openbao-seed.yaml:74`), and one auth method, Kubernetes at `externalSecrets.kubernetesAuthPath`
  (`openbao-seed.yaml:468-470`). Roles and policies are written from rendered files and pruned when git drops them
  (`openbao-seed.yaml:471-487`). The seed enables no `database` engine.
- **Database engine available in 2.2.0:** not confirmed on the running store. The store's API was not called, per the
  brief. OpenBao ships a database secrets engine with a built-in PostgreSQL plugin, but that is from memory, not
  measured. A network gap: `chart/templates/network-policy.yaml:192-212` admits to Postgres:5432 only the OpenFGA pods
  and the openfga-migrate Job, so the store's pod could not reach Postgres under `networkPolicy.enabled`.
- **How OpenFGA's password arrives today:** ExternalSecret `rask-infra-credentials` reads `remoteRef key: postgres,
  property: password` into template key `pgPassword` (`chart/templates/external-secrets.yaml:80-107`). It renders
  `openfga-config.yaml` with `datastore.uri: postgres://<user>:{{ .pgPassword }}@<host>:<port>/<db>?sslmode=disable`
  (line 102), refreshes every `externalSecrets.refreshInterval`, and is mounted by OpenFGA and its migrate Job. The
  password is static: generated once, carried forward across upgrades (`external-secrets.yaml:16-21`), never rotated by a lease.
- **How a pod logs in (`packages/lakehouse/src/lakehouse/governed/secrets.py`):** Kubernetes auth with its projected
  ServiceAccount token, `POST /v1/auth/<auth_mount>/login {role, jwt}` (`:564`). The store token is kept in memory with a
  lease of 60 to 3,600 s (`:244`). The pod logs in again at 1/2 of the lease and stops using a login at 9/10 (`:109-112`,
  `:575`). It never renews the token, "made again, never renewed" (`:11-21`). A 403 is taken as the store's answer, and
  `auth/token/lookup-self` (`:139`) tells a dropped token from an unnamed secret (`:23-45`).
- **What reading an expiring credential would need, by what the code does not do:**
  - Reads are KV v2 only: `GET /v1/<kv_mount>/data/<name>` (`:503`), parsed as `data.data` string fields
    (`_ReadAnswer` `:262`, `_fields` `:630-641`, which refuses any other shape). A `database/creds/<role>` answer
    (top-level `lease_id`, `lease_duration`, `renewable`, `data.{username,password}`) would be refused as "not a KV v2 read".
  - No code handles a secret's lease: grep for `lease_id`, `renewable` and `sys/leases` in `packages/` and `services/`
    found nothing. So there is no renewal through `sys/leases/renew`, no revoke on shutdown, and no re-read timed to a
    lease. Holders re-read on their own fixed period: `signing_key.py:147,226,233` and `catalog/core/commit_ids.py:111`.
  - The rendered store policy grants only `<kv>/data/<name>` read, `auth/token/lookup-self` read, and
    `auth/token/renew-self` for external readers only (`chart/templates/_helpers.tpl:1145-1151`). Nothing grants a
    `database/creds/*` path.

## Could not confirm

- **Moto 10,000 timings** (marked N) were taken through the Nagle-affected proxy. A re-run with the patched proxy was
  started, then cut off by the session restart and not repeated. Its request counts and bytes are valid. That row also
  held only the latest `__manifest` version, not 10,000 old ones.
- **Moto build at 10,000 and 30,000:** not built. The rate is extrapolated, as marked above.
- **Live data file size at 30,000:** inferred from total bytes, not read.
- **Why one shared connection never conflicts:** an in-process ordering inside pylance's `DirectoryNamespace` is
  inferred from zero 412s; the source was not read. Separate processes were stood in for by separate connections and
  sessions in one process.
- **M3 with emits and outboxes on:** not measured. No NATS binary is on the host, so `_announcement_intents/`, the
  outboxes and their deletes are absent from the counts. FGA was stubbed, so OpenFGA calls are not counted (they make no
  store requests). The HTTP calls the stock client made to the catalog were not counted, only their store requests.
- **M3 on a registered warehouse:** not measured. The estate warehouse's root equals the control root here, so the
  warehouse-record read (`C/api/dependencies.py:108-155`) is absent.
- **OpenBao 2.2.0's database engine and PostgreSQL plugin:** not verified against the running store or its source.
- **Exported Postgres on x86_64 CI runners:** not tried. ICU and other library needs on other hosts are unknown.
