# System sketch — where we are, the holes, and how we differ from Lakekeeper

> Living status doc (bird's-eye). Detailed design: [`ARCHITECTURE.md`](ARCHITECTURE.md)
> (catalog) and [`LINEAGE.md`](LINEAGE.md) (provenance). Roadmap: [`docs/DECISIONS.md`](DECISIONS.md).
> This file = the sketch of everything + the gap register + the Lakekeeper diff.
>
> ⚠️ **Point-in-time markers.** Some ✅/⛔ status cells below are historical. Since this was written,
> **credential vending** (4 modes incl. RustFS-native web_identity), **the OpenBao/Dapr secret store**,
> **read-side lineage authz**, and **the event-driven medallion cascade** are all built + deployed —
> defer to [`ARCHITECTURE.md`](ARCHITECTURE.md), [`DEPLOY.md`](DEPLOY.md), and [`COVERAGE.md`](COVERAGE.md)
> for authoritative current state.
>
> 🖱️ **Interactive version:** [`system-diagram.html`](system-diagram.html) — click-through
> diagram of the four flows with per-mode payloads (Mode B server-mediated vs STS vending).
> Text companion: [`system-diagram.md`](system-diagram.md). The ASCII below is the static fallback.

## 1. The whole system at a glance

**Three planes, three governance axes, one identity** (the catalog `table:<id>`).

```
                         OIDC (Dex / IdP)        OpenBao (secrets — planned)
                               │                        │
   clients ──Bearer JWT──▶  ┌──┴─────────────────────────────────┐
   (LanceDB SDK,            │  CATALOG  (control plane, FastAPI)  │
    lance-ray, apps)        │  authn → authz → locate → record   │
                            │  never moves data                  │
                            └───┬───────────────┬────────────────┘
                                │               │ describe_table → location (+creds via
              OpenFGA ◀─can_*?──┘               │ CredentialVendor: ModeB today)
              (Postgres)                        ▼
                                       object store  (MinIO / S3-compatible)   ── DATA PLANE ──
                                          ▲  compute (lance-ray / pylance) moves bytes
                                          │  (Mode B: via catalog endpoints today)
   compute/ETL jobs ──OpenLineage──▶  ┌──┴──────────────────────────┐
   (emit run events)                  │  LINEAGE  (provenance plane) │  ── separate service ──
                                       │  ingest → Apache AGE graph   │
                                       └──────────────┬──────────────┘
                                            AGE / Postgres (its OWN db)
```

**Three axes — same object, governed three ways:**

| Question | System | Identity |
|---|---|---|
| **who may** read/write/commit | OpenFGA `can_*` on `table:<id>` | `table:bronze$images` |
| **what / when** changed | Lance versions + tags (immutable, time-travel) | same dataset |
| **how / from where / by whom** | OpenLineage → Apache AGE graph | dataset node `name` = `table:<id>` |

**Medallion flow (target, end-to-end):**
`ingest → bronze`, then `promote bronze→silver→gold` as lance-ray jobs. Each hop:
authz on the catalog → creds via the vendor (Mode B today) → bytes moved by the engine →
emit an OpenLineage event. Layers are **separate Lance tables** (namespaces); promotion is a
**compute client**, never a catalog endpoint.

> ### ⚠️ Audit-verified corrections (`w8u4rc2tg`, 2026-06-24)
> A grounded re-audit of the real code (full citations in §6 / [`docs/DECISIONS.md`](DECISIONS.md)) refined three things:
> - **Secret responsibility (least-privilege).** Only the **catalog** and **lineage svc** consume
>   OpenBao. Compute jobs (**lance-ray**) never read it — they get short-TTL scoped creds *from the
>   catalog* and authenticate with **workload identity** (KubeRay SA / OIDC token). The sketch showing
>   only the catalog on OpenBao is intentional, not a missing wire.
> - **Storage = S3-compatible (HCP dropped).** Target is **MinIO** (default test backend) + AWS S3,
>   Ceph RGW, RustFS, GCS-via-interop. **STS vending** (`AssumeRole` + inline session policy) is the
>   recommended scoped-cred path — works on MinIO / Ceph / AWS. **Mode B** (server-mediated) is the
>   simple default; **static keys** for S3 backends without STS (e.g. GCS interop).
> - **Provenance — ✅ now done (P0 #1/#2/#3).** Lineage reads + ingest are authz-gated, ingest binds the
>   verified author (no forgery), and the catalog emits create-lineage so "who created the table" is an
>   audit fact (`GET /datasets/{id}/creator`). The lineage service is deployed. Default OFF; enable in prod.

## 2. Component status

| Component | What | Status |
|---|---|---|
| Catalog REST API | FastAPI over pylance `DirectoryNamespace` (Lance Namespace spec) | ✅ built |
| OIDC authn | PyJWT/JWKS, fail-closed | ✅ built |
| OpenFGA authz | op→`can_*`, concentric+cascade, roles-as-`#assignee`, `grant_on_create` | ✅ built |
| Resilience | transient-aware retries; network → 503 (never 500) | ✅ built |
| `CredentialVendor` | pluggable modes: mode_b / web_identity / sts / static (`services/catalog/core/vending.py`) | ✅ built + wired (`POST /v1/table/{id}/credentials`) |
| Maintenance read-only | 503+Retry-After middleware (`services/catalog/api/maintenance.py`) | ✅ built (default off) |
| Lineage service | OpenLineage ingest → AGE graph (`services/lineage/`) | ✅ built, deployed (`chart/`), in-service authz gate (default off) |
| OpenBao SecretStore | secrets out of env | ✅ built + deployed (two-tier; app services fail-closed on it) |
| medallion movers + lance-ray producer | the medallion movement (promotion) + compaction | ✅ built & deployed — event-driven Dapr movers; distributed lance-ray (Ray Data) is the rask future ([`FLOW.md`](FLOW.md)) |
| Governance P1 | `project` type + 3-axis (teams×projects×layers) | 🔶 planned |
| OTel / NATS / Dapr | observability / events / durable workflows | ✅ built & deployed (OTLP→GreptimeDB; Dapr pub/sub over NATS JetStream; Dapr **Workflow** still deferred) |

## 3. Holes / what's missing (gap register)

> ⚠️ **Historical (pre-build) register — most of these are now RESOLVED.** Since this was written, lineage
> read+ingest authz (#1/#2), OpenBao secrets (#5), the medallion movement + compaction (#6), lineage
> deployment (#7), 3-axis governance model (#11), and OTel/NATS/Dapr (#12) are all built & deployed. See
> [`FLOW.md`](FLOW.md) + [`DEPLOY.md`](DEPLOY.md) for current status; the Lakekeeper study below remains valid reference.

| # | Gap | Plane/axis | Risk if left | Priority |
|---|---|---|---|---|
| 1 | **Lineage read endpoints unauthenticated** | provenance | leaks the data estate (which tables exist, how they connect, who ran what) | **P0** |
| 2 | **Lineage ingest endpoint unauthenticated** | provenance | anyone can inject false provenance / flood the graph (no producer trust) | **P0** |
| 3 | ~~CredentialVendor not wired~~ ✅ CLOSED — vending shipped at its own `POST /v1/table/{id}/credentials?tier=read\|write` (superseded the describe-param idea) | data | — | done |
| 4 | ~~STS vending not wired~~ ✅ CLOSED — four modes in `vending.py` (`sts`, `mode_b` default, `web_identity` = RustFS-native scoped STS, `static`) | data | — | done |
| 5 | ~~Secrets in env~~ ✅ CLOSED — OpenBao deployed as the two-tier Dapr secret store (app tier via sidecar; infra tier via external-secrets pattern) | security | — | done |
| 6 | ~~medallion jobs absent~~ ✅ CLOSED — event-driven producer + movers + compaction sweeper built & deployed ([`FLOW.md`](FLOW.md)); distributed Ray Data variant = rask merge | data/medallion | — | done |
| 7 | Lineage not deployed (no image / compose service) | provenance | runs only via `uvicorn`; not in the stack | P1 |
| 8 | No routes-vs-spec conformance test | control | spec drift goes unnoticed (we have a known conformance gap) | P1 |
| 9 | OpenFGA model hand-edited (`.fga`/`.json`) | authz | no versioned migration / reconcile-from-catalog → risky to evolve to 3-axis | P1 |
| 10 | No write idempotency | control | retried promotion/ingest jobs can double-apply | P2 |
| 11 | Governance P1 (`project` + 3-axis) not built | authz | teams×projects×layers model not yet expressible | P1 |
| 12 | No OTel / NATS async ingest / Dapr | obs/infra | limited prod observability; synchronous lineage ingest | 🔶 |

## 4. Incoming lineage feature — review (the 3 pulled commits)

- `fa44da5` — Apache AGE store: `docker-compose.lineage.yml` (PG16+AGE, pinned — PG18 crashes
  `create_graph`) + `lineage-init.sql` (enable AGE, `create_graph('lineage')`).
- `5e5898c` — the service: `models/schemas/age/repository/main` + producer `seed.py` + tests;
  updated `ARCHITECTURE.md` §7/§9 lineage status → built.
- `e176645` — docs: the read-side authz design + open-endpoints caveat in `LINEAGE.md`.

**Strengths (verified by reading the code):** Cypher-injection-safe (`age.py:56-95`: graph name
`_IDENT`-validated + `sql.Literal`; Cypher `LiteralString`-typed; user input only as `agtype`
bind params, never interpolated); layered (no Cypher in endpoints); transactional ingest;
**DB-per-service** (own AGE Postgres, never co-mingled with OpenFGA's); `author` = OIDC sub;
unit + e2e tested; ruff/ty clean.

**Holes:** gaps #1 and #2 above — read *and* ingest are open. The docs flag reads; the **ingest
trust gap is additional** and must be closed when real jobs emit (service token / OIDC on the
producer).

## 5. Diff vs Lakekeeper — what we keep, what we do differently

**Our core principles:** thin control-plane catalog (authorize + locate + record, never moves
data) · control/data/provenance plane separation · **vending-first, pluggable** credentials ·
relational OpenFGA authz (one identity across three axes) · fail-closed + least-privilege +
secrets in OpenBao · small & Pythonic (FastAPI + pylance) over a large multi-warehouse service ·
DB-per-service · spec-faithful (Lance Namespace REST).

| Capability | Lakekeeper (real, cloned `lakekeeper-ref`) | Us | Verdict |
|---|---|---|---|
| Catalog impl | Rust, multi-warehouse, many crates | Python FastAPI over pylance, ~focused | **smaller, Lance-native** |
| Table format | Iceberg tables (+ generic tables) | Lance datasets (vector / blob / medallion) | **our domain** |
| Control vs data plane | catalog never moves data | same + **Mode B** server-mediated fallback | we add Mode B as the simple default |
| Credential vending | STS (S3) · SAS (Azure) · bearer (GCS) + remote signing | pluggable `CredentialVendor` (Sts/Static/ModeB), vending-first | **design matches**; STS is the recommended path (MinIO/Ceph/AWS) |
| AuthZ model | OpenFGA v4: hierarchy/ownership tuple split, golden drift tests, **reconcile-from-catalog**, **versioned model migration** | OpenFGA `can_*` + cascade + roles + `grant_on_create` | core matches; **ADOPT** reconcile + versioned migration + golden drift tests |
| Lineage | emits **CloudEvents** → external consumer (Kafka/NATS); no built-in graph | **built-in lightweight-Marquez** (OpenLineage → AGE openCypher) | **we're more integrated** |
| Secrets | pluggable `SecretStore` (Postgres-encrypted + Vault KV2) | planned **OpenBao** (KV v2, Vault-compatible) | **ADOPT** (OpenBao drops into the KV2 pattern) |
| Maintenance read-only mode | `api/maintenance.rs` | ✅ `services/catalog/api/maintenance.py` | **matched** |
| Routes-vs-spec test | `test_endpoint_completeness` (enum vs OpenAPI) | ⛔ none | **ADOPT** (closes our conformance gap) |
| Idempotency | `idempotency_record` table | ⛔ none | **ADOPT** for jobs |
| Events backend | `CloudEventBackend` (NATS/Kafka) + EventDispatcher | ⛔ (lineage ingest is direct POST) | adopt as the OpenLineage emit hook later |
| Audit | `PrivilegeSource` + instance-admin bypass | partial | consider |

---

_The three sections below are the **cited Lakekeeper study output** (study `wfb25lg74`, run against the real clone at `/home/blackwell/Desktop/lakekeeper-ref`) — they expand §5 with `file:line` evidence on both sides, a sequenced top-5, and the keepers + deliberate non-adoptions._

## Lakekeeper → Lance-NS adoption backlog (prioritized, de-duped)

> Citations verified against the cloned repos. "Our state" cites `/home/blackwell/Desktop/lance-ns/...`; Lakekeeper cites `/home/blackwell/Desktop/lakekeeper-ref/...`. Data-plane reality: **target = S3-compatible (MinIO default; AWS/Ceph/RustFS). STS vending (`AssumeRole`) is the recommended scoped-cred path and works on MinIO/Ceph/AWS; Mode B is the server-mediated default; static keys for STS-less S3 backends.**

| # | Pattern | Maps to | Our state | Recommendation | Priority | Effort |
|---|---------|---------|-----------|----------------|----------|--------|
| 1 | ~~Wire vending into `describe_table?vend_credentials`~~ ✅ **SHIPPED as its own endpoint** | CREDENTIAL_VENDING / SECRETS | `POST /v1/table/{id}/credentials?tier=read\|write` with four modes (`sts`, default `mode_b`, `web_identity` = RustFS-native scoped STS, `static`), base credential from OpenBao (`credentials.py`, `vending.py`). | The describe-param shape was dropped for a dedicated endpoint — cleaner authz tiering. | done | — |
| 2 | **Always-present `expires_at_millis` + separate `credentials` vs `config`** in vended/load-table response | CREDENTIAL_VENDING | `VendedCredentials` mixes everything into `storage_options`; `expires_at_millis` optional (vending.py:36-45). | Mirror Lakekeeper `TableConfig` (s3.rs:499,568,599): add a `config` dict beside `storage_options`; require `expires_at_millis` whenever an expiring token is vended (STS). Keep null/absent for Mode B and static. | **P0** | M |
| 3 | **Trace-ID + actor propagation on every request** (UUID request_id + OIDC sub) | OBSERVABILITY/EVENTS | Absent — no request_id, no actor threaded to a context. OIDC token verified but not propagated. | Add a tiny middleware/dependency generating `request_id` (UUID) and capturing actor (OIDC sub); store on request scope. This is the cheap precondition for events, audit, and lineage correlation. | **P0** | S |
| 4 | **Emit only table/namespace mutation events** (NOT warehouse/role/multi-format) | OBSERVABILITY/EVENTS / GOVERNANCE_P1 | Absent. | Deliberately scope events to create/drop/rename of namespace/table. Add project_* events only when GOVERNANCE_P1 lands. (Avoids Lakekeeper's role/warehouse event sprawl — publisher.rs:223-251.) | **P0** | S |
| 5 | **lance-ray promotion + compaction jobs** with idempotency keys | LANCE_RAY_JOBS | Absent — no background-job framework; promotions vulnerable to duplicate work on retry. | Build the promotion (bronze→silver→gold) + compaction jobs as catalog *clients*. Add `Idempotency-Key` handling for the write ops (Lakekeeper idempotency.rs check-on-read + insert-at-commit); use in-memory/Redis for dev, no Postgres advisory lock yet. | **P1** | M |
| 6 | **Pluggable OpenBao SecretStore** (KV v2) with background token refresh | SECRETS | S3 master creds + OIDC config are env-only (config.py:34-35,42-44). No vault, no refresh. | Add a `SecretStore` protocol (shape it like `CredentialVendor`): `OpenBaoKV2Backend` (hvac, Vault-API/KV-v2 compatible) + `EnvBackend` fallback. Instantiate in `main.py` lifespan; static-vendor keys and OIDC client secret read via SecretStore. Include a daemon refresh task (Lakekeeper login_task lib.rs:174-196). | **P1** | M |
| 7 | **NATS JetStream event backbone** (Dapr pub/sub) | EVENTS / LINEAGE | ✅ Built & deployed — Dapr pub/sub over NATS JetStream carries the medallion triggers + the OpenLineage events; the movers emit OpenLineage that the lineage service ingests. See [`FLOW.md`](FLOW.md). | The catalog publishes structural lineage; the medallion movers emit OpenLineage transform events; they meet at shared `table:<id>` identity. | done | — |
| 8 | **Routes-vs-spec conformance test** (parse spec.yaml, diff against implemented routes) | CONFORMANCE | Absent — the CI e2e suites exercise ops live but nothing diffs implemented routes against `spec.yaml`, so spec drift can deploy silently (the old manual `smoke_test.py` coverage-matrix was superseded by the e2e suites and removed). | Add `test_endpoint_completeness()` parsing spec.yaml `(method, path)` pairs vs FastAPI routes; fail on drift (Lakekeeper endpoints.rs:413). Cheap CI gate. | **P1** | S |
| 9 | **Versioned authz-model migration** (`ACTIVE_MODEL_VERSION` + idempotent `migrate()`) | AUTHZ / GOVERNANCE_P1 | `model.json` loaded with no versioning, no migration hooks (services/common/auth/). | Add `ACTIVE_MODEL_VERSION` + versioned schema files + idempotent `migrate()` recording applied version (Lakekeeper migration.rs:11-18,142-163). Mandatory **before** the 3-axis (teams×projects×layers) model introduces new types. | **P1** | M |
| 10 | **Split hierarchy vs ownership tuple helpers + golden tuple tests** | AUTHZ / GOVERNANCE_P1 | Single inline grant in `grant_on_create` (fga.py:408+); no split, no golden tests. | Extract `tuples.py` with `hierarchy_tuples_for_*()` / `ownership_tuples_for_*()`; add golden unit tests pinning exact triples per entity (Lakekeeper tuples.rs:248-547). Precondition for reconcile (#11). | **P1** | M |
| 11 | **Reconcile-from-catalog (additive + drift deletion)** for safe model evolution | AUTHZ / GOVERNANCE_P1 | Absent — no rebuild path, no drift detection/deletion, no dry-run. | Implement `reconcile.py`: `rebuild_*` (additive) + `reconcile_*` (drift deletion, dry-run flag). Deletion only targets managed structural relations; preserves ownership/grants (Lakekeeper reconcile.rs:1-108,199-247). Use `asyncio.Lock` not Postgres advisory lock at our scale. | **P1** | L |
| 12 | **URL-encode user IDs** when serializing to OpenFGA | AUTHZ | No encoding in fga.py (confirmed: identity passed directly as `user:<sub>`). | Apply `urllib.parse.quote`/`unquote` on user IDs (Lakekeeper entities.rs:156-189). Mandatory before prod OIDC if subjects contain `@`/`+`/`:`. Audit your IdP's subject claim first. | **P1** | S |
| 13 | **STS endpoint separation from S3 endpoint** | CREDENTIAL_VENDING | `s3_sts_endpoint` defined (config.py:83) but `StsVendor` stores it as the single `self._endpoint` and emits one endpoint (vending.py:177,203-204). | No action for MinIO/Ceph (same endpoint). If a deployment ever splits the STS and S3 endpoints, emit separate keys. | P2 | S |
| 14 | **Credential expiry / revalidation window + `/refresh-credentials` endpoint** | CREDENTIAL_VENDING | `expires_at_millis` set for STS but no refresh endpoint, no revalidation window (vending.py:45). | Only relevant if STS vending is enabled. Then add a refresh endpoint + `revalidation_window_ms` (half remaining TTL, cap 1h — Lakekeeper storage/mod.rs:148-165). **Mode B doesn't vend, so N/A there.** | P2 | M |
| 15 | **Azure SAS / GCS bearer-token vendors** | CREDENTIAL_VENDING | Absent — vending is S3-only. | Add `AzureSasVendor` / `GcsDownscopeVendor` only if those backends are ever adopted (Lakekeeper az_profile.rs:235-275, gcs/mod.rs:348-448). Not on the current S3/MinIO roadmap. | P2 | L |
| 16 | **S3 KMS-on-write advertising** | CREDENTIAL_VENDING | Absent — no KMS in Settings or vended config. | Only if AWS S3 with KMS is adopted. MinIO doesn't use AWS KMS. Add `s3_kms_key_arn` to the `config` half then (Lakekeeper s3.rs:525-530). | P2 | S |
| 17 | **`role#assignee` tuple generation on role create** | GOVERNANCE_P1 | Model defines `role` with `assignee` (model.fga.yaml) but grant-on-create does not seed role tuples programmatically. | If the 3-axis model adds project-scoped roles, add `ownership_tuples_for_role()` and call it in `grant_on_create`. | P2 | S |
| 18 | **`fga model test` in CI / pre-commit** | AUTHZ | model.fga.yaml has comprehensive check/list_objects tests but no automated runner. | Add a CI step running `fga model test services/common/auth/model.fga.yaml`; keep model.fga / model.fga.yaml / model.json in sync. Status: GOOD, just gate it. | P2 | S |
| 19 | **Lightweight RequestMetadata for audit** (request_id + actor + privilege_source) | OBSERVABILITY/EVENTS | Absent. | Start minimal (covered by #3); add `privilege_source` only when GOVERNANCE_P1 introduces admin-only ops. Do NOT build Lakekeeper's heavyweight struct (request_metadata.rs:92-170). | P2 | M |
| — | **Fail-closed authz on OpenFGA outage (503, never silent-allow)** | AUTHZ | **DONE & CORRECT** — `_FAIL_CLOSED` + `_TRANSIENT_NETWORK` → `ServiceUnavailableError`→503, all paths via `_retrying()` (fga.py:76-82,300-302,330-332,360-362,400-405). | Status GOOD. Maintain: every new read/write authz path must go through `_retrying()` + fail-closed. No work. | P2 | S |
| — | **Maintenance read-only mode (503 + Retry-After)** | MAINTENANCE | **DONE & WIRED** — maintenance.py:24-53 mirrors Lakekeeper maintenance.rs:40-70; wired at main.py:75; config.py:87; tests present. | Status COMPLETE. No work. (Pairs well with #9/#11 migration windows.) | P2 | S |
| SKIP | **`Location` newtype hardening / max-length / scheme validation** | CREDENTIAL_VENDING | `split_s3_location` = urlsplit wrapper (vending.py:61-71). | **SKIP** — premature for a small Python service with internal clients (LanceDB SDK/lance-ray/Iceberg). Lakekeeper's newtype (io/location.rs) defends untrusted multi-tenant input we don't have. Revisit only if a public API appears. | SKIP | L |
| SKIP | **Postgres session advisory locks for maintenance mutex** | MAINTENANCE / LANCE_RAY_JOBS | No DB, no multi-worker job fleet. | **SKIP** — `asyncio.Lock` suffices for single-pod jobs. Postgres advisory locks (advisory_lock.rs:1-80) earn their keep only with multiple job workers / a real queue. | SKIP | S |
| SKIP | **Type graph `user_of`/`usersets` for list_objects/list_users** | AUTHZ | No list_objects endpoints beyond check/batch_check (fga_deps.py). | **SKIP for now** — defer until a Lance list API actually needs FGA-gated enumeration (models.rs:1-46). | SKIP | M |
| SKIP | **Instance-admin / PrivilegeSource bypass tiers** | AUTHZ / GOVERNANCE_P1 | Absent. | **SKIP** until admin-only ops exist (no in-process bypass, no warehouse mgmt today). Revisit in GOVERNANCE_P1 (request_metadata.rs:68-87,174-191). | SKIP | M |
| SKIP | **Kafka events backend** | OBSERVABILITY/EVENTS | Absent. | **SKIP** — NATS JetStream is sufficient at our scale; the dispatcher stays pluggable so Kafka can be added later if ever needed. | SKIP | S |
| SKIP | **`unimplemented()` enum markers / feature-gated endpoint enum** | CONFORMANCE | Backend raises `UnsupportedOperationError`→501 (native.py:17-32). | **SKIP** — our backend-driven 501 is simpler; the enum pattern (endpoints.rs:125-135) only pays off with a hand-maintained route enum we don't have. | SKIP | S |

## Top 5 highest-leverage adoptions (sequenced for our near-term path)

### 1. Wire the credential vendor into `describe_table?vend_credentials` (STS-first)
> ✅ **Superseded & shipped (2026-07):** vending landed as its OWN endpoint — `POST
> /v1/table/{id}/credentials?tier=read|write` (`services/catalog/api/v1/endpoints/credentials.py`)
> — not as a describe param, with FOUR modes (`sts`, default `mode_b`, `web_identity` for
> RustFS-native scoped STS, `static`) sourcing the base credential from OpenBao. The paragraphs
> below are the original (historical) recommendation.
- **What:** Build **`StsVendor`** as the recommended path — `AssumeRole` + an inline session policy against the S3 endpoint → short-TTL, per-table, read/write-scoped creds. Keep **`mode_b`** (server-mediated) as the safe OOTB default and **`static`** for S3 backends without STS.
- **Why now:** It's the fork every other vending decision hangs on, and it's now buildable: MinIO, Ceph RGW, and AWS all implement the STS `AssumeRole` API, so the scoped-credential path is real (it was dead weight only when the target was a non-STS backend). `StsVendor` already has the boto3 plumbing (vending.py); the work is wiring it into `describe_table` + the OpenFGA tier gate.
- **First step:** Wire `describe_table?vend_credentials=true` → pick the vendor by `LANCE_VENDING_MODE`; for `sts`, point boto3's STS client at `LANCE_S3_STS_ENDPOINT` and call `AssumeRole(LANCE_S3_ASSUME_ROLE_ARN, Policy=<session policy>)`. Source the base/role credential from OpenBao (threads into #3).
- **Threads with:** OpenBao supplies the base/role credential. The expiring-credential machinery (refresh window, `expires_at_millis`) lives on the STS branch (#13/#14).

### 2. lance-ray promotion + compaction jobs with idempotency keys
- **What:** Build the bronze→silver→gold promotion job and compaction job as **clients of the catalog**, and add `Idempotency-Key` handling on their write ops (check-on-read fast path + insert-at-commit), mirroring Lakekeeper's idempotency module.
- **Why now:** These jobs are the reason the catalog exists, and retries on a long promotion will otherwise duplicate work / double-commit. This is also the natural emitter of OpenLineage events later — but the catalog must *not* emit them.
- **First step:** Stand up the promotion job skeleton + an `Idempotency-Key` extractor; back it with an in-memory/Redis store for dev (no Postgres advisory lock — `asyncio.Lock` is enough at single-pod scale; explicitly SKIP Lakekeeper's pg advisory locks).
- **Threads with:** The job reads `storage_options` from whichever vendor #1 selected (Mode B → server-mediated data endpoints; static → OpenBao keys). It will later emit OpenLineage; the catalog only emits structural CloudEvents (#5), meeting at shared `table:<id>` identity.

### 3. Pluggable OpenBao SecretStore with background token refresh
- **What:** A `SecretStore` protocol shaped exactly like `CredentialVendor`, with `OpenBaoKV2Backend` (hvac — Vault-API + KV v2 compatible) and an `EnvBackend` fallback, instantiated in the `main.py` lifespan, plus a daemon refresh task.
- **Why now:** Today S3 master creds and OIDC config are env-only (config.py:34-35,42-44). Decoupling secrets from deployment lets us rotate static per-bucket keys (for the static vendor) and the OIDC client secret without redeploying — and OpenBao is already the chosen secret manager.
- **First step:** Define the protocol + `EnvBackend` (no behavior change), then add `OpenBaoKV2Backend` with a well-known secret ID for static-vendor keys; copy Lakekeeper's `login_task`/`refresh_login` shape (lib.rs:174-196) as an `asyncio.Task` daemon (log-and-retry, non-fatal on refresh failure).
- **Threads with:** This is where the static-vendor keys from #1 actually live; STS (S3-family/dev) reads its master creds the same way. No raw env in prod.

### 4. Maintenance read-only mode (DONE) + cheap routes-vs-spec conformance test
- **What:** Maintenance mode is already complete and wired (maintenance.py:24-53, main.py:75) — just leverage it. Add the missing piece: a `test_endpoint_completeness()` that parses spec.yaml `(method, path)` pairs and diffs them against the registered FastAPI routes, failing on drift.
- **Why now:** It's a near-free CI gate (Lakekeeper runs the equivalent on every build — endpoints.rs:413). It catches silent spec/implementation drift before deploy, which the live e2e suites don't guarantee (they exercise ops, they don't diff the route inventory against `spec.yaml`). And the maintenance gate gives us the zero-downtime window we'll need for the model migrations in #5 (below).
- **First step:** Write the pytest that `yaml.safe_load`s spec.yaml and asserts every operation maps to a route (and vice-versa); add it to CI.
- **Threads with:** The maintenance gate is the operational primitive for safely running versioned-model migrations and reconcile (next), and OpenBao secret rotation (flip read-only, rotate, flip off if a backend ever needs a restart-free swap).

### 5. Versioned-model migration + reconcile-from-catalog for the P1 3-axis model
- **What:** Before introducing teams×projects×layers types, add (a) `ACTIVE_MODEL_VERSION` + idempotent `migrate()`, (b) split `hierarchy_tuples_for_*()` / `ownership_tuples_for_*()` helpers with golden tuple tests, and (c) `reconcile.py` (additive rebuild + opt-in drift deletion with dry-run).
- **Why now:** The 3-axis governance model is the next big schema change, and changing an authz model in place without versioning or a rebuild path is how you silently strand or over-grant tuples. Lakekeeper treats this as load-bearing (migration.rs:11-18,142-163; reconcile.rs:199-247; golden tests tuples.rs:248-547). Also URL-encode user IDs (#12) before prod OIDC if subjects contain special chars — confirmed absent in fga.py today.
- **First step:** Extract the inline `grant_on_create` logic into `tuples.py` helpers + golden tests (no behavior change), then add `ACTIVE_MODEL_VERSION` and an idempotent `migrate()`; build reconcile last, gated by the maintenance window from #4. Use `asyncio.Lock`, not Postgres advisory locks.
- **Threads with:** Run migrations/reconcile inside the read-only maintenance window (#4). CloudEvents (#6/#7) for project_* mutations get added *when* this model lands — not before — keeping the event contract minimal.

*(CloudEvents/EventDispatcher as the OpenLineage hook is the immediate follow-on: trace-ID + actor propagation (P0, item #3 in the backlog) is the cheap precondition, then a lightweight async-callback dispatcher + one NATS backend emitting table/namespace mutations after commit — explicitly NOT Lakekeeper's full EventListener trait or role/warehouse events.)*

## Lessons adopted from Lakekeeper

We studied the cloned Lakekeeper source (`/home/blackwell/Desktop/lakekeeper-ref`, a production Rust Iceberg REST catalog) subsystem-by-subsystem and selectively adopted its patterns. Lakekeeper is a multi-warehouse, multi-table-format, high-scale service; we are a single-warehouse FastAPI catalog over native pylance `DirectoryNamespace`. The guiding principle below is **adopt the shape, not the scale** — and several mature Lakekeeper patterns are deliberately *not* adopted because they solve problems we do not have.

### Data-plane reality (read this first)

The object store is **S3-compatible**: MinIO is the default test backend; AWS S3, Ceph RGW, RustFS,
and GCS-via-interop are all targets. Therefore:

- **STS vending is the recommended path.** `StsVendor` calls `AssumeRole` + an inline session policy
  against the S3 endpoint → short-TTL, per-table, read/write-scoped creds. MinIO, Ceph RGW, and AWS
  all implement the STS API. The expiring-credential machinery — revalidation windows,
  `/refresh-credentials`, `credentials_expiration_ms` gating of `304 Not Modified` — lives on this branch.
- **Mode B (server-mediated) is the safe OOTB default** — `ModeBVendor.vend()` returns `None` and
  clients use the catalog's Arrow-IPC data endpoints. **Static per-bucket keys** are the fallback for
  S3 backends without STS (e.g. GCS interop). `LANCE_VENDING_MODE` selects the vendor.
- The base/role credential (for STS) and static-vendor keys are sourced from **OpenBao**
  (Vault-fork, KV v2), never from raw env in production.

Our `CredentialVendor` protocol (`services/catalog/core/vending.py`) already mirrors Lakekeeper's per-backend `TableConfig` abstraction: `ModeBVendor` / `StaticPrefixVendor` / `StsVendor`, returning `{storage_options, expires_at_millis}`. The remaining vending work is metadata-shape parity (separate `credentials` from static `config`; always set `expires_at_millis` when vending an expiring token), not new backends.

### Keepers (patterns we adopt)

- **Read-only maintenance mode (503 + `Retry-After`).** Already implemented and wired (`services/catalog/api/maintenance.py`, `services/catalog/main.py`), adapted from Lakekeeper's `crates/lakekeeper/src/api/maintenance.rs` (mutating = non-GET/HEAD/OPTIONS; default off). This is our zero-downtime window for model migrations and reconcile.
- **Fail-closed authorization.** On OpenFGA outage we raise `ServiceUnavailableError`→503, never silent-allow (`services/common/fga.py`, all check/grant paths via `_retrying()` + `_FAIL_CLOSED`). Matches Lakekeeper's `OpenFGABackendUnavailable`→503. Status: correct; maintain by routing every new authz path through the same decorator.
- **Vended-credential response shape.** Separate `credentials` (expiring) from `config` (static endpoint/region/KMS metadata) and always emit `expires_at_millis` for expiring tokens (Lakekeeper `s3.rs` `TableConfig`, `credentials_expiration_ms`).
- **Versioned authz-model migration.** `ACTIVE_MODEL_VERSION` + idempotent `migrate()` recording applied versions (Lakekeeper `migration.rs`). Adopted *before* the 3-axis (teams×projects×layers) governance model introduces new types.
- **Split hierarchy/ownership tuple helpers + golden tuple tests** and **reconcile-from-catalog** (additive rebuild + opt-in drift deletion with dry-run), preserving ownership/grants on deletion (Lakekeeper `tuples.rs`, `reconcile.rs`). These make authz-model evolution safe. We use an in-process `asyncio.Lock`, not a Postgres advisory lock.
- **URL-encoded user IDs** when serializing to OpenFGA (Lakekeeper `entities.rs`), required before prod OIDC if the IdP's subject claim contains `@`/`+`/`:`. Absent today.
- **Routes-vs-spec conformance test.** A CI test parsing `spec.yaml` `(method, path)` pairs and diffing against registered FastAPI routes, failing on drift (Lakekeeper `endpoints.rs::test_endpoint_completeness`). Cheap, high-value.
- **Trace-ID + actor propagation** (UUID `request_id` + OIDC subject) on every request — the cheap precondition for events, audit, and lineage correlation (Lakekeeper `RequestMetadata`, kept *lightweight*).
- **CloudEvents dispatch for mutations.** A lightweight async-callback `EventDispatcher` that emits **table/namespace** create/drop/rename events *after commit* to an opt-in NATS JetStream backend (off unless `LANCE_EVENTS_ENABLED`). Pattern from Lakekeeper's `service/events/{dispatch,publisher}.rs`, minus the trait hierarchy.
- **Pluggable OpenBao SecretStore (KV v2)** with a background token-refresh daemon, shaped like our `CredentialVendor`, replacing env-only secrets (Lakekeeper `lakekeeper-secrets-kv2/src/lib.rs` `SecretStore` + `login_task`/`refresh_login`).
- **Idempotency keys on job write ops** (check-on-read + insert-at-commit) so retried lance-ray promotions/compactions don't double-commit (Lakekeeper `idempotency.rs`), backed by in-memory/Redis at our scale.

### Deliberate non-adoptions (and why)

- **`Location` newtype hardening / max-length / scheme validation** (`io/location.rs`). Premature for a Python service whose clients are internal (LanceDB SDK / lance-ray / Iceberg). `urlsplit` (`split_s3_location`) suffices. Revisit only if we expose a public, untrusted API.
- **Postgres session advisory locks** (`advisory_lock.rs`). We have no DB and no multi-worker job fleet; `asyncio.Lock` covers single-pod coordination. Adopt only with a real job queue or multiple workers.
- **Heavyweight `RequestMetadata` + `PrivilegeSource`/instance-admin bypass tiers** (`request_metadata.rs`). We have no in-process privilege escalation and no admin-only ops yet. Keep request context minimal (request_id + actor); revisit privilege tiers in GOVERNANCE_P1.
- **Type-graph `user_of`/`usersets` for `list_objects`/`list_users`** (`models.rs`). No Lance list API needs FGA-gated enumeration today. Defer until one does.
- **Role/warehouse/multi-format event sprawl and the full `EventListener` trait** (`publisher.rs`). We emit only table/namespace mutations; `project_*` events arrive only when GOVERNANCE_P1 adds those object types. A `Callable` list beats a trait hierarchy at our scale.
- **Kafka events backend.** NATS JetStream is sufficient; the dispatcher stays pluggable so Kafka can be added if scale ever demands it.
- **`unimplemented()` route-enum + feature gates** (`endpoints.rs`). Our backend raises `UnsupportedOperationError`→501 directly; a hand-maintained route enum earns nothing here.
- **Mandatory/unconditional STS, KMS-on-write, Azure SAS, GCS bearer-token downscoping.** STS itself *is* adopted (the recommended vendor), but it stays **optional/pluggable** (Mode B is the default); KMS/SAS/GCS-bearer get a vendor plug *only* if that backend/feature is adopted — not on the current S3/MinIO roadmap.

The throughline: Lakekeeper's patterns are correct *for a multi-warehouse Rust service*. We borrow the ones that make a small, secure FastAPI+pylance catalog evolve safely (migration/reconcile, fail-closed authz, maintenance windows, conformance gates, OpenBao secrets, post-commit events) and decline the ones whose cost is justified only by scale or untrusted multi-tenancy we do not have.
