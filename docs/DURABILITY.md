# Durability & deployment tiers

This makes explicit what the chart only *implied*: which parts of the system are **ephemeral compute**,
which are the **durable data** that must be backed up, and which should be **external / managed in
production**. The product chart runs rask's own services; the object store, the secret store, Dex, NATS, Dapr and the
observability store are prerequisites (ADR 0153), installed in development from `deploy/fixtures`.

## The three tiers

### 1. Ephemeral compute — stateless, roll freely
The FastAPI services and the event plumbing hold no durable state; they can be killed, scaled, or
re-imaged at any time with no data loss.

- `catalog`, `lineage`, `maintenance`, `notifications`, `flows`, the zones (`web-<zone>`), the media trio
- the Dapr control plane and Dex (prerequisites)

These read their config from env / OpenBao and their data from the durable tier below. Rolling them is a
no-op for data.

### 2. Durable data — must persist + be backed up
| Store | Holds | Persistence | Lifecycle |
|-------|-------|-------------|-----------|
| **Object store (S3)** | the **lakehouse** (Lance datasets), lineage's provenance tables (the commit feed and edge list, in the catalog's `provenance` namespace), the lineage outbox, trash and the registries | the MinIO fixture's PVCs in development; the platform's S3 in production | the permanent data asset — lives for months/years |
| **Apache AGE / Postgres** | OpenFGA's authz store + the Dapr state store (notifications' inbox read state) | PVC (`volumeClaimTemplates`) | grows with history; permanent |
| **NATS JetStream** | the catalog→lineage event buffer | PVC (`fileStore`) | transient (replay window); survives restart so no event is lost across a roll |
| **OpenBao** | secrets | PVC (prod path, `openbao.devMode=false`) | permanent; in prod usually **external** (see tier 3) |

#### Object-store commit safety (conditional writes / CAS) — VALIDATED ✅

Lance's correctness under concurrent writers rests entirely on the object store honoring **put-if-not-exists**
(`If-None-Match: *`): every commit publishes `_versions/N.manifest` conditionally, and the format requires the
store to guarantee **exactly one writer wins** that race (`lance_docs/file_format.md` — conflict resolution). A
store that silently *ignores* the header accepts both PUTs and silently loses a commit — a real failure mode
(GCS S3-interop) that a single-writer smoke test never catches, so a store has to be proven, not assumed.

**Measured 2026-07-05 against RustFS, the store at the time: it enforces CAS.** No measurement against MinIO, the
store since [ADR 0063](adr/0063-the-object-store-is-minio-and-the-reason-is-who-may-call.md), is recorded here;
`make e2e-cas` against the MinIO fixture is the check. The `tests/e2e/test_object_store_cas_e2e.py` harness (`make
e2e-cas`) validates it in three tiers against the deployed store, asserting **data invariants** (not S3 error
names): (1) a second `If-None-Match: *` PUT of a live key is rejected **412** and does not overwrite; (2) 8
threads racing the same key at a barrier yield **exactly one winner per round** (the silent-ignore detector);
(3) 8 processes each appending 100 rows to one Lance dataset **all land — 800/800 rows** with the full id set
intact, exercising Lance's real manifest-CAS commit+retry path under contention. All three pass. This is the
gate for any backend swap: re-run `make e2e-cas` against a candidate store before trusting it with the
lakehouse. If a store FAILS, the remedy lance_docs describe is an external manifest store: the namespace's
`managed_versioning`, through which the catalog would do the put-if-not-exists itself. The catalog does not serve it
yet (its `CreateTableVersion` answers 406); ADR 0157 decides it and LH-471 #597 builds it.

### 3. External / managed in production
Development runs every store in-cluster; production names the platform's — disable the in-cluster copy and point
the apps at the external endpoint:

| Service | Why external in prod | How (wired) |
|---------|----------------------|-----|
| **S3 lakehouse** | the permanent system of record; you want a managed, replicated, lifecycle-policied object store (AWS S3 / a MinIO cluster) | `minio.enabled=false` + `minio.externalEndpoint=…` → catalog/lineage/maintenance + vending connect there |
| **Postgres (AGE)** | a managed DB (RDS/Cloud SQL/CNPG) for backups, HA, PITR | `age.enabled=false` + `age.externalHost=…` (+ matching user/password/dbs) → the OpenFGA datastore Secret and the Dapr state store point there (also override the openfga subchart's `datastore.uri`) |
| **OpenBao / secrets — app tier** | a hardened external Vault / cloud KMS | `openbao.enabled=false` + `openbao.externalAddr=…` → the apps consume from the external Vault via their Dapr sidecar (`secretsViaDapr` keys off `externalAddr`, so there is **no** plaintext-env fallback) and the in-cluster OpenBao is not rendered |
| **infra-tier Secret** | the `infra-credentials` + `observability-s3` Secrets (AGE and object-store root creds; GreptimeDB's S3 **secret** key) should come from Vault, not chart values | `externalSecrets.enabled=true` → the external-secrets.io operator syncs `<release>-infra-credentials` **and** `<release>-observability-s3` from Vault (both static Secrets are skipped); the openfga DSN is assembled from the fetched password. The non-sensitive S3 access-key *id* is still templated (it ships in app env regardless) |
| **Observability** (OTel Collector → the platform's store) | you don't want observability to die with the cluster it observes — run the store on a separate platform/cluster | rask ships only the **OTel Collector** (`observability.enabled`), its telemetry seam: apps export OTLP to it, it tails infra logs (`filelog`, retires Vector) and scrapes the Dapr sidecars (`prometheus`+k8s SD), and exports all three signals to **`observability.export.endpoint`**, the platform's backend (ADR 0153; development: the `observability` fixture's GreptimeDB). The store, dashboards and alerting are the platform's; the alert rules are `deploy/alerting/rules.yml`. `filelog` checkpoints its read offsets (`file_storage` extension) so a Collector container restart resumes without a log gap — Vector `data_dir` parity |

> All five overrides are **wired and verified** (`prod-render-check` legs 10–11 + the OTel decoupling assert:
> no in-cluster DNS leaks into app env when externalized);
> external-Vault keeps the apps on Dapr secrets with no plaintext fallback **and TLS
> verification on for an https Vault** — `skipVerify` applies only to the plain-http in-cluster dev OpenBao;
> the external-secrets operator owns the infra + observability Secrets — the object store's secret key + AGE
> password come from Vault, only the non-sensitive S3 access-key id stays templated). The
> `values-prod.yaml` EXTERNALIZE block shows the full set.
>
> No credential is a chart value ([[XC-004]]): the render refuses one by name, and a real deployment
> with the dev OpenBao is refused. Every credential, the app token that authenticates every
> sidecar-delivered route included, comes from the store through ESO, a prerequisite.

## Data lifecycle — what's permanent vs transient
The S3 store is permanent **infrastructure**; the *data within it* has a lifecycle:

- **Every governed tier** is a Lance table with immutable versions; old versions age out under the table's
  maintenance policy. The **maintenance** service runs version expiry (`cleanup_old_versions`) after each write and
  on a 30-minute backstop, trash purge, the orphan scan and cross-store reconcile, honouring protection. It rewrites no
  data: compaction and index builds are a bring-your-own job's, committed through `version/create` (ADR 0162).
- **`gold` + the catalog's registered tables** are what downstream consumers are anchored to.
- **Lineage's commit feed** keeps `services.lineage.eventsRetentionDays` (default 7) of arrivals, pruned on the
  relay tick; the long-lived lineage graph is the forwarded backend's (`services.lineage.forward.url`).

So: a long-lived lakehouse with provenance, and a version-expiry lifecycle that keeps history bounded.

## Backup strategy
- **Lakehouse (S3)** — the highest-value asset. In prod: an external object store with versioning +
  cross-region replication + lifecycle rules. In-cluster: `backups.volumeSnapshot` (off by default) snapshots the MinIO
  fixture's data PVCs (`VolumeSnapshot` on a CSI StorageClass). Lance datasets are immutable-versioned on disk, so an object-store snapshot is consistent.
- **AGE / Postgres** — `pg_dump` / WAL archiving (managed DB does PITR); or PVC `VolumeSnapshot`. The
  in-cluster pg-backup CronJob (`backup-pg.yaml`) dumps `openfga` + `daprstate` gzipped to
  `_backups/pg/<UTC>/` on the object store and prunes all but the newest `backups.pgDump.keep` dumps (default 7;
  0 disables).
- **OpenBao** — the file-backend PVC snapshot, or (prod) the external Vault's own backup; secrets are also
  re-derivable from the source of truth the operator populates from.
- **NATS** — transient; no backup needed (lineage's insert-only merges mean a replayed event lands no second
  row).

**Known open gaps in the in-cluster path** (accept the loss window or externalize — see
[docs/adr/0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md "P4/P7"](adr/0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md) and [RUNBOOK-restore.md](runbooks/RUNBOOK-restore.md)):
- **Backup fate-sharing**: the pg_dump lands on the same object store, so a total store loss loses both the Lance data
  *and* the DB dumps. Ship the dumps off-cluster (a second object store / off-site), or externalize
  Postgres to CloudNativePG and use its PITR.
- **OpenBao PVC**: no automated backup path is shipped — snapshot it yourself and back up the unseal
  material out-of-band.
- **RPO/RTO are undocumented**, and nothing verifies the lakehouse `VolumeSnapshot` actually succeeds (the
  empty `snapshotClassName` is a per-cluster value) — confirm both on your cluster before trusting the
  snapshot leg.

## Prod profile
`deploy/values-prod.yaml` is a starting overlay — it flips the safe-by-default-off prod switches (sealed
OpenBao, governance on, Dapr dashboard off, the cluster route on the platform's Gateway) and turns the in-cluster OpenFGA and Postgres off in favour of
the platform's (`openfga.enabled: false`, `age.enabled: false`). Apply with `helm upgrade … -f chart/values.yaml -f deploy/values-prod.yaml`.
