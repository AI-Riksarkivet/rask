# Durability & deployment tiers

This makes explicit what the chart only *implied*: which parts of the system are **ephemeral compute**,
which are the **durable data** that must be backed up, and which should be **external / managed in
production**. The defaults target a local `kind` cluster; a prod deployment flips toggles to externalize the
stateful layer.

## The three tiers

### 1. Ephemeral compute — stateless, roll freely
The FastAPI services and the event plumbing hold no durable state; they can be killed, scaled, or
re-imaged at any time with no data loss.

- `catalog`, `lineage`, `medallion` (producer + 3 movers), `compaction`, `gateway`, `web`
- `dapr` control plane, `dex`

These read their config from env / OpenBao and their data from the durable tier below. Rolling them is a
no-op for data.

### 2. Durable data — must persist + be backed up
| Store | Holds | Persistence | Lifecycle |
|-------|-------|-------------|-----------|
| **RustFS / S3** | the **lakehouse** (Lance datasets) + GreptimeDB's observability bucket | **PVC** (`rustfs.persistence.enabled`, default on) | the permanent data asset — lives for months/years |
| **Apache AGE / Postgres** | the lineage graph (provenance history) + OpenFGA's authz store | PVC (`volumeClaimTemplates`) | grows with history; permanent |
| **NATS JetStream** | the catalog→lineage event buffer | PVC (`fileStore`) | transient (replay window); survives restart so no event is lost across a roll |
| **OpenBao** | secrets | PVC (prod path, `openbao.devMode=false`) | permanent; in prod usually **external** (see tier 3) |

> Before this work RustFS was `emptyDir` — the one place the *durable* asset sat on *ephemeral* storage, so
> every RustFS roll wiped the lakehouse. It is now a PVC by default.

#### Object-store commit safety (conditional writes / CAS) — VALIDATED ✅

Lance's correctness under concurrent writers rests entirely on the object store honoring **put-if-not-exists**
(`If-None-Match: *`): every commit publishes `_versions/N.manifest` conditionally, and the format requires the
store to guarantee **exactly one writer wins** that race (`lance_docs/file_format.md` — conflict resolution). A
store that silently *ignores* the header accepts both PUTs and silently loses a commit — a real failure mode
(GCS S3-interop) that a single-writer smoke test never catches. Since RustFS is a pre-1.0 store, this had to be
proven, not assumed.

**Verdict (2026-07-05): RustFS enforces CAS.** The `tests/e2e/test_object_store_cas_e2e.py` harness (`make
e2e-cas`) validates it in three tiers against the deployed store, asserting **data invariants** (not S3 error
names): (1) a second `If-None-Match: *` PUT of a live key is rejected **412** and does not overwrite; (2) 8
threads racing the same key at a barrier yield **exactly one winner per round** (the silent-ignore detector);
(3) 8 processes each appending 100 rows to one Lance dataset **all land — 800/800 rows** with the full id set
intact, exercising Lance's real manifest-CAS commit+retry path under contention. All three pass. This is the
gate for any backend swap: re-run `make e2e-cas` against a candidate store before trusting it with the
lakehouse. If a store FAILS, the documented remedy is an external manifest store (lance-namespace
`table_version_management`).

### 3. External / managed in production
You **can** run everything in-cluster (the default), but in production the stateful + cross-cutting
services are better run as managed/external services — disable the in-cluster copy and point the apps at the
external endpoint:

| Service | Why external in prod | How (wired) |
|---------|----------------------|-----|
| **S3 lakehouse** | the permanent ingest+export surface; you want a managed, replicated, lifecycle-policied object store (AWS S3 / a RustFS or MinIO cluster) | `rustfs.enabled=false` + `rustfs.externalEndpoint=…` → catalog/lineage/compaction + vending connect there (also override `greptimedb-standalone.objectStorage.s3.endpoint`) |
| **Postgres (AGE)** | a managed DB (RDS/Cloud SQL/CNPG) for backups, HA, PITR | `age.enabled=false` + `age.externalHost=…` (+ matching user/password/dbs) → lineage DSN + the OpenFGA datastore Secret point there (also override the openfga subchart's `datastore.uri`) |
| **OpenBao / secrets — app tier** | a hardened external Vault / cloud KMS | `openbao.enabled=false` + `openbao.externalAddr=…` → the apps consume from the external Vault via their Dapr sidecar (`secretsViaDapr` keys off `externalAddr`, so there is **no** plaintext-env fallback) and the in-cluster OpenBao is not rendered |
| **infra-tier Secret** | the `infra-credentials` + `observability-s3` Secrets (AGE/RustFS root creds; GreptimeDB's S3 **secret** key) should come from Vault, not chart values | `externalSecrets.enabled=true` → the external-secrets.io operator syncs `<release>-infra-credentials` **and** `<release>-observability-s3` from Vault (both static Secrets are skipped); the openfga DSN is assembled from the fetched password. The non-sensitive S3 access-key *id* is still templated (it ships in app env regardless) |
| **Observability** (OTel Collector / GreptimeDB / Perses) | you don't want observability to die with the cluster it observes — run it on a separate platform/cluster | **OTel-first:** a single in-chart **OTel Collector** (`observability.otelCollector`) is the telemetry hub — apps export OTLP to it, it tails infra logs (`filelog`, retires Vector) and scrapes the Dapr sidecars (`prometheus`+k8s SD), exporting all three signals → GreptimeDB. Prod: set `observability.otelCollector.externalEndpoint=…` → **no in-cluster Collector renders**; apps aim their OTLP at your operator-managed external Collector (the "just point to the existing one" path). The app SDK wiring keys off `lance.otelEnabled` = `observability.enabled` **OR** `externalOtlpEndpoint`, so telemetry flows even with the in-cluster store off. `filelog` checkpoints its read offsets (`file_storage` extension) so a Collector container restart resumes without a log gap — Vector `data_dir` parity |

> All five overrides are **wired and verified** (`prod-render-check` legs 10–11 + the OTel decoupling assert:
> no in-cluster DNS leaks into app env when externalized — including the **RustFS→GreptimeDB companion**
> (`greptimedb-standalone.objectStorage.s3.endpoint` must be overridden alongside `rustfs.externalEndpoint`,
> a static subchart value that can't follow the helper — the check fails if you set one without the other);
> external-Vault keeps the apps on Dapr secrets with no plaintext fallback **and TLS
> verification on for an https Vault** — `skipVerify` applies only to the plain-http in-cluster dev OpenBao;
> the external-secrets operator owns the infra + observability Secrets — the RustFS secret key + AGE
> password come from Vault, only the non-sensitive S3 access-key id stays templated). The
> `values-prod.yaml` EXTERNALIZE block shows the full set.
>
> The prod path also **fails the render** instead of shipping known-bad values: `openbao.devMode=false`
> with the dev-default `age.password`/`rustfs.secretKey` (and no externalSecrets) aborts, and the
> `values-prod.yaml` `dapr.appToken` placeholder aborts until a real secret is passed — the app token
> authenticates every sidecar-delivered route, so a public constant there would make them forgeable.

## Data lifecycle — what's permanent vs transient
The S3 store is permanent **infrastructure**; the *data within it* has a lifecycle:

- **Ingest point (`raw`) + export point (`gold`/catalog tables)** are **permanent surfaces** — the lakehouse
  is the durable system of record.
- **`raw` / `bronze` / `silver`** are **transient**: produced, consumed downstream, then aged out. The
  **compaction service** (a Dapr cron) runs `cleanup_old_versions()` + `compact_files()` to GC old versions
  and reclaim space, so intermediate data doesn't accumulate forever.
- **`gold` + the catalog's registered tables** are **permanent** — the curated output that lives for
  months/years and what downstream consumers + the lineage moat (`/reconcile`) are anchored to.

So: long-lived lakehouse + provenance, with a compaction/GC lifecycle aging out the intermediate layers.

## Backup strategy
- **Lakehouse (S3/RustFS)** — the highest-value asset. In prod: an external object store with versioning +
  cross-region replication + lifecycle rules. In-cluster: snapshot the RustFS PVC (`VolumeSnapshot` on a CSI
  StorageClass). Lance datasets are immutable-versioned on disk, so an object-store snapshot is consistent.
- **AGE / Postgres** — `pg_dump` / WAL archiving (managed DB does PITR); or PVC `VolumeSnapshot`. The
  in-cluster pg-backup CronJob (`backup-pg.yaml`) dumps `lineage` + `openfga` gzipped to
  `_backups/pg/<UTC>/` on RustFS and prunes all but the newest `backups.pgDump.keep` dumps (default 7;
  0 disables).
- **OpenBao** — the file-backend PVC snapshot, or (prod) the external Vault's own backup; secrets are also
  re-derivable from the source of truth the operator populates from.
- **NATS** — transient; no backup needed (idempotent MERGE on `run_id` means a lost-then-replayed event is a
  no-op).

**Known open gaps in the in-cluster path** (accept the loss window or externalize — see
[DECISIONS.md "P4/P7"](DECISIONS.md) and [RUNBOOK-restore.md](RUNBOOK-restore.md)):
- **Backup fate-sharing**: the pg_dump lands on RustFS, so a total RustFS loss loses both the Lance data
  *and* the DB dumps. Ship the dumps off-cluster (a second object store / off-site), or externalize
  Postgres to CloudNativePG and use its PITR.
- **OpenBao PVC**: no automated backup path is shipped — snapshot it yourself and back up the unseal
  material out-of-band.
- **RPO/RTO are undocumented**, and nothing verifies the RustFS `VolumeSnapshot` actually succeeds (the
  empty `snapshotClassName` is a per-cluster value) — confirm both on your cluster before trusting the
  snapshot leg.

## Prod profile
`chart/values-prod.yaml` is a starting overlay — it flips the safe-by-default-off prod switches (sealed
OpenBao, governance on, Dapr dashboard off, ingress) and sizes the already-on durable RustFS volume, and is
where you'd disable the in-cluster stateful services + point at external endpoints once those endpoint
overrides are wired. Apply with `helm upgrade … -f chart/values.yaml -f chart/values-prod.yaml`.
