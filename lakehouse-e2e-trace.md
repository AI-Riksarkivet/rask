# The lakehouse end to end: boundary, one real cascade, scorecard, target, fixed stack (HEAD f61dd73)

Read-only research, 2026-09-30, at `f61dd73`. Paths are relative to the repository root. The code is the
source of truth. Design targets come only from owner rulings (the register header,
`open_backlog_left_new2.md:36-88`), CLAUDE.md, or a backlog row's How.

**Citation rules used here.**
- A plain `file:line` is a line I opened at f61dd73.
- Every code claim is cited to a line opened at f61dd73. `git diff --stat eb53bfc HEAD` touches only
  `docs/audits/2026-09-30/lakehouse-dataflow.md` and `open_backlog_left_new2.md`, so the audit's code cites still
  match; each one relied on here was re-opened.
- **UNVERIFIED** marks live state and anything that could not be checked from the repo.

---

## 0. Headline findings (new or sharpened by this pass)

1. **On `chart/values.yaml` alone, the default cascade cannot finish its first hop.** `medallion.ray: true`
   (values.yaml:1365), and no transform is declared (CP-031), so every stage chooses Ray
   (engine_choice.py:95-98). The Ray address falls back to `http://<release>-ray-head-svc:8265`
   (medallion.yaml:617). No Ray cluster renders by default, though: `ray.cluster.enabled: false` (values.yaml:2529-2530)
   gates raycluster.yaml:1, and `singleTenant.enabled: false` (values.yaml:66-67) gates rayservice.yaml:1. The submit
   therefore fails: the stage runner logs it and acks with the run's plan open (stage_plans.py:253-257), the plan
   sweep resubmits under the same key at most twice, and then closes the plan failed with a FAIL through the lineage
   outbox (stage_plans.py:272-306). values-local.yaml:44-49
   records this exact failure ("Temporary failure in name resolution"). This is CP-041.
2. **The profile that does run the cascade is `make k3s-up`** (Makefile:768, 792, 795), which layers
   `chart/values-local.yaml` on top. That file turns on `ray.cluster.enabled` (values-local.yaml:126-127), pins
   `medallion.rayAddress` (:57), `quality: true` (:63) and `fgaEnabled: true` (:77). Section 2 traces that profile.
3. **The default transforms are generic.** The operations `embed_features` and `aggregate_gold` are labels
   (values.yaml:1536-1537). With no declared transform, in-process work runs `compute.transform_stage`
   (transform.py:882-887; compute.py:311-414), and Ray work runs `scripts/ray_stage_job.py`. Both carry rows forward,
   stamp `stage` and `source_rowid`, write the `lineage` JSONB, and derive a thumbnail and embedding only from image
   blob content (compute.py:1-17). Nothing embeds text or aggregates. In this reading, the use case the code supports today is **governed,
   provenance-carrying promotion of a table through three tiers**, plus a media lane that derives thumbnails and
   embeddings (silver only).
4. **The quality gate in the default chart is the catalog's publish gate, not a stage-runner verdict.** With
   `quality: false` and `qualityReview: false` (values.yaml:1420,1436), `gate_decision` answers PUBLISH whenever
   there is a target and a catalog (gate_decision.py:90-91), unless a project has declared a gate record with review
   enabled (medallion services/gate.py:88-115; transform.py:1303-1312). The catalog runs its assertions and moves the `published`
   tag (publication.py:251-366). HOLD fires only on a band breach, which requires review to be on
   (transform.py:1445-1458,1636-1656).

---

## 1. IN AND OUT: everything crossing the lakehouse boundary

"Lakehouse" means catalog, lineage, medallion (producer and stage runners), maintenance, notifications, controlplane,
and ingest (ruled a Phase 1 component, `open_backlog_left_new2.md:60`).

### 1a. Inputs

| # | Input | Door / topic | Identity and credential | Cite |
|---|---|---|---|---|
| I1 | Human write trigger (synthetic bronze seed) | `POST /produce`, gateway `/api/produce` | OIDC bearer plus FGA `can_administer` on the project, or the shared Dapr app token. The app token is admitted only for the configured project, and through the gateway it is refused as a public-door call. `Idempotency-Key` is required. | gateway `__init__.py`:228; api/produce.py:22-35,68-71; produce_auth.py:1-23,52-67 |
| I2 | External media bytes | `POST /ingest-media`. No gateway row, so it is in-cluster only. It reads `MEDALLION_MEDIA_SOURCE_BUCKET/prefix`, and in the demo it first seeds two PNGs itself. | `authorize_ingest_media`; bytes read with the static `rask-medallion` key | api/ingest_media.py:23-34; media_produce.py:60,92-95; values.yaml:1296 (mediaSeedSamples) |
| I3 | External bulk bytes (object stores, IIIF, …) | ingest `POST /v1/ingests`, gateway `/api/ingest`. Chunks go through raw nats-py JetStream, and fragments are written with a table-scoped vend. | OIDC plus `can_administer`, or the app token limited to the configured project. The vend refuses on failure by default; ambient keys are used when the vend offers none (CP-007). | ingest api.py:388; gateway `__init__.py`:235; queue.py:33,234; config.py:211; ingest auth.py:19-23; catalog_service.py:684; lineage.py:283 |
| I4 | Client data writes and reads through the catalog (stock Lance clients, other services) | catalog `/v1/table/{id}/*` (insert, merge_insert, create, register, describe, query, …), gateway `/api/catalog` | Dex OIDC bearer, or the service door (app token plus the asserted `x-lance-service-identity`, dedicated token for privileged subjects). FGA `authorize` guards every route. | gateway `__init__.py`:226; catalog router.py:48; fga_deps.py:808-814; dapr_auth.py:457-512 |
| I5 | Client-direct fragment commit | `POST /management/v1/table/{id}/commit` (data.py:103,192), folded as a Lance `Append` only | `can_write_data` | dataplane.py:886-887; data.py:207,220-227 |
| I6 | External OpenLineage producers, ingest, the Ray train job | lineage `POST /api/v1/lineage` (HTTP only, never on the bus) | Bearer or service door. The HTTP door overwrites the author with the token subject. | lineage endpoints/ingest.py:103-106; notifications lineage_events.py:176-189; scripts/ray_train_job.py:24,173 |
| I7 | Service-to-service events | Dapr pub/sub on NATS JetStream: `lineage.events.v1`, `catalog.control.v1`, `medallion.bronze`, `medallion.silver`, `medallion.media`, `medallion.promotion`, `training.jobs`, `maintenance.work.v1` | The subscriber checks only the Dapr app token (`require_dapr_token`). NATS authenticates no client, and no NATS auth block is in values.yaml. | values.yaml:1167-1169,1458,1474,1517,1536-1544,2744-2800; bronze_arrival.py:38-48,96-106; events.py:45-57 |
| I8 | Operator and approver control | `/api/promotions/{id}/decision`, `/api/stage-runners/*`, `/api/trains/{id}/terminate`, `/api/cascade/stalled` | OIDC or the service token, plus a per-door rung: `can_promote` on the destination for /promotions (promotions.py:282-297), the edge's `requiredAction` for /stage-runners and rerun (medallion.yaml:239-243), `can_administer` on the resource's project for /trains and /cascade/stalled (produce_auth.py:189-238) | gateway `__init__.py`:247; promotions.py:282,313; train.py:259; values template medallion.yaml:234-243 (MEDALLION_STAGE_RUNNER_GATES) |
| I9 | Config | env through pydantic-settings aliases (for example `MEDALLION_*`, `RASK_*`, `LANCE_*`), all rendered by the chart | n/a | config.py:305-318; medallion.yaml:89-409 |
| I10 | Secrets | the Dapr secret store backed by OpenBao (`*_SECRETS_FROM_DAPR`), or literal env when no store is configured (XC-081). Ray pods read `infra-credentials` through secretKeyRef. ESO is off. | OpenBao dev mode is a root token (XC-079) | medallion.yaml:386-403,593-606; values.yaml:3210-3218,3229-3235; _ray-cluster-config.tpl:173-182 |
| I11 | Crons | Dapr cron bindings: the lineage reconcile and outbox drain (`@every 300s`), the catalog control relay (`@every 60s`), the medallion cascade lag (`@every 300s`), the maintenance sweep (`@every 120s`), and the notifications reconciler | sidecar app token | values.yaml:476-479,1150-1160,1269-1274,1582; notifications reconcile_cron.py:1 |

### 1b. Outputs

| # | Output | Door / topic | Identity and credential | Cite |
|---|---|---|---|---|
| O1 | Governed tables (`<proj>-bronze$events`, `-silver$features`, `-gold$catalog`, `bronze-media$objects`, `silver-media$features`) as Lance datasets in MinIO | Direct Lance writes by the producer, the in-process stage lane and the Ray job. Only ingest writes go through `/commit` (Append). | Static keys: `rask-medallion` (producer and in-process), `rask-ray-compute` (Ray job), `rask-catalog` (catalog doors). | produce.py:235; compute.py:246-262,389-399; _ray-cluster-config.tpl:173-182; values.yaml:2389,2435,2443; services.yaml:131 |
| O2 | Vended credentials | `POST /management/v1/table/{id}/credentials?tier=read\|write`. `StsVendor` issues AssumeRole plus an inline session policy (TTL 900 s), signed with the catalog's static key. | Write tier needs `can_write_data` or `can_maintain`. A table with no location, a classified column or a sanctioned base the session policy cannot address returns `server_mediated`, and the data doors that then serve it do not mask the classified column (LH-207; scope is the parked LH-288 decision); an unsanctioned base is refused 409. Both `/credentials` and `/commit` sit under `/management/v1`, rask extensions outside the spec. | credentials.py:78-90,111-112,131-133,144-157,176; values.yaml:1031-1033; vending.py:10-23; services.yaml:131 (XC-083) |
| O3 | Data reads | catalog `query`, `count_rows`, `credentials`, `blobs`, `changes` (the `_DATA_READ_ACTIONS` set, catalog fga_deps.py:95), explorer and viewer, search | FGA `can_read_data`. The viewer and search open Lance directly with the deployment key (LOW-027). | fga_deps.py:95; viewer pages.py:237; search result_cache.py:78 |
| O4 | Lineage events and graph | `lineage.events.v1` → lineage `/lineage-events`, which MERGEs into Apache AGE: Job, Run, Dataset, READ, WROTE{version}, DERIVED_FROM, columns. Read at `/api/lineage` and at `GET /events`. | Producers sign an HMAC facet when a key resolves. Verification is verify-if-present, so an unsigned event is admitted. | lineage dapr.py:173-191; lineage api/fga_deps.py:293-333; cypher.py:12-20,26-35; runs.py:204 |
| O5 | Control events | `catalog.control.v1`: the catalog emits `table_published` and the grant actions through its control outbox (publication.py:343-365); the annotator emits the task actions (services/annotator/src/annotator/api/v1/endpoints/tasks.py); the producer's `request_approval` activity publishes `promotion_review_requested` directly (workflow.py:1302-1330). `CatalogControlEvent` is unsigned. | the emitting service; no signature | publication.py:341-366; control_emit.py (no signing code; grep finds only line 6); values.yaml:1150-1160 |
| O6 | Notifications | Per-subject Dapr `InboxActor` inbox and optional channel push. Delivery is gated by `can_be_notified` and rendering by `can_get_metadata`. | The audience is `author.sub`, the `lance.originator` human, and `project#member` watchers | fanout.py:95-121,146-175; visibility.py:55-61; lineage_events.py:225-256; control_events.py:42-53 |
| O7 | Metrics, traces, audit logs | OTLP from `service_kit.setup_otel` → the Collector → GreptimeDB → vmalert → Alertmanager (alerting is off by default, values.yaml:3346-3347; values-local.yaml:233-235 turns it on). The audit trail is the `lance.audit` logger. | GreptimeDB accepts unauthenticated SQL (XC-003) | otel.py:95; audit.py:4-8,23-24; chart/templates/otel-collector.yaml, alerting.yaml (exist) |
| O8 | DLQ and refusal retention | `dlq.<app-id>` topics (`/dlq-event` parks, ERROR-logs and acks; lineage re-presents once), plus `refused.<app-id>` (transform `_retain_refusal`) | sidecar | medallion.yaml:210-211,565-566; transform.py:1704-1742; lineage dapr.py:117-122,182-191 |
| O9 | Ray jobs (egress to compute) | Ray Jobs REST at `MEDALLION_RAY_ADDRESS`. The dashboard takes no token (`ray.auth.enabled: false`). | none (CP-010) | medallion.yaml:145,617; values.yaml:2563-2564 |

---

## 2. ONE REAL END-TO-END CASCADE

### 2a. What the default chart wires, and what it does not

Wired in `chart/values.yaml`:
- **Bronze head.** The producer is on, with `bronzeNamespace: bronze`, `bronzeDataset: bronze$events` and
  `bronzeTopic: medallion.bronze` (values.yaml:1501,1514,1517). `compute: true` renders
  `MEDALLION_COMPUTE_ENABLED` and a composed `MEDALLION_BRONZE_URI` (values.yaml:1288; medallion.yaml:324-325).
- **Stage runners.** `bronze-to-silver` subscribes to `medallion.bronze`, writes `silver$features`, and needs
  `can_create_table`. `silver-to-gold` subscribes to `medallion.silver`, writes `gold$catalog`, needs `can_promote`,
  and has an empty pubTopic. `media-to-silver` subscribes to `medallion.media` and is terminal (values.yaml:1536-1544).
- **Transform routes.** These are derived as source namespace → subTopic: `{bronze: medallion.bronze,
  silver: medallion.silver, bronze-media: medallion.media}` (medallion.yaml:219-226). They feed
  `/publication-arrival` (publication_trigger.py:189-197).
- **Lanes.** The Ray lane is on (values.yaml:1365). Registered Ray tasks are `stage-transform`, `dummy-lane` and
  `train`; registered in-process tasks are `stage-transform` (values.yaml:1403-1414). **No stage runner declares a
  `transform`**, so the engine comes from the chart (engine_choice.py:95-98; CP-031).
- **Governance.** `projectsEnabled: true` (values.yaml:1231) renders `MEDALLION_CONTROL_ROOT`
  (medallion.yaml:126,546). `catalog.warehouses.enabled: true` (values.yaml:1099-1100). `auth.enabled: true` with
  `dedicatedServiceCredentials: true` (values.yaml:939,965). The catalog runs with OIDC and FGA on
  (services.yaml:274-276).
- **Off by default.** `medallion.fgaEnabled: false`, so stage runners do not self-check FGA
  (values.yaml:1242; medallion.yaml:688-690). `quality: false`, `qualityReview: false`.
- **Durability.** The lineage outbox is on (values.yaml:491-492; medallion.yaml:91-93). The control outbox is on
  (values.yaml:1150-1160). Dapr resiliency is on (values.yaml:2924-2925).

**Not wired in the default chart:**
- **No Ray cluster.** See headline 1: the Ray lane dies at submit (CP-041).
- **The in-process lane works only after an explicit `medallion.ray=false`.** It then needs no Ray. No stage runner
  hosts a Workflow runtime on either lane (stage_runner.py:84-89; engine_choice.py:81).
- **The media lane starts its own cascade.** `/ingest-media` publishes `medallion.media` directly instead of
  going through `/bronze-arrival` (media_produce.py:242-258; LH-329).
- **A project-qualified run needs `scripts/seed_estate.py` first.** That script creates project `acme`, its warehouse
  and the `acme-bronze`, `acme-silver` and `acme-gold` namespaces (seed_estate.py:196-209). A project-less run
  assumes the `bronze`, `silver` and `gold` top-level namespaces already exist. With warehouses on, a top-level
  namespace outside a warehouse is refused (fga_deps.py:1146-1176; values.yaml:1224-1226 records that gold could not
  be created that way). Whether bare `bronze`, `silver` and `gold` exist on a fresh default install is **UNVERIFIED**
  (live state).

### 2b. Use case and configuration

**Use case (it fits what the code does):** *governed promotion of a tabular dataset through three tiers with
row-level provenance and a publication gate.* An example is an event or record feed landed as `{id, payload}`, carried
into silver and then gold. Every row gets a `stage` stamp, `source_rowid` back to the bronze `_rowid`, and a
`lineage` JSONB document, and each tier's readiness is marked by the catalog's `published` tag; committed rows are readable at `latest` whether or not they are published (catalog_register.py:177-179). No business
transform exists: silver does not embed and gold does not aggregate (compute.py:311-414). The **illustrative data** is
what `/produce` actually writes: 8 synthetic rows `{id: 0..7, payload: "event-i", stage: "bronze"}`, or `?rows=N`
(compute.py:211-262; api/produce.py:47).

**Configuration the trace assumes** (the `make k3s-up` profile, plus the seed):
1. `helm upgrade … -f chart/values-local.yaml` (Makefile:792,795). This sets `ray.cluster.enabled: true` so
   `rask-ray-head-svc` exists (values-local.yaml:125-127), `medallion.rayAddress` (:57), `quality: true` (:63) and
   `fgaEnabled: true` (:77).
2. `scripts/seed_estate.py`: project `acme`, warehouse `acme-bucket`, namespaces `acme-bronze`, `acme-silver` and
   `acme-gold` (seed_estate.py:196-209), with stage-runner and producer rungs (seed_estate.py:298-309). The catalog
   also grants writer, publisher and validator to the cascade identities on every warehouse it creates
   (`LANCE_FGA_CASCADE_WRITERS`, services.yaml:307-339).
3. A human who holds `can_administer` on `project:acme`.
4. For the in-process variant, `medallion.ray=false` as well. It changes the stage runners' pod env, so they roll; no
   stage runner is scoped to the actor state store on either lane, since none hosts a workflow.

### 2c. The trace (project `acme`, Ray lane, the k3s-up profile)

Token `T` = the caller's `Idempotency-Key`. Tables: `acme-bronze$events` → `acme-silver$features` → `acme-gold$catalog`.

| # | Actor → actor | Transport | Identity / credential | Written (table, tier, txn) | Event emitted | Next trigger | Cite |
|---|---|---|---|---|---|---|---|
| 1 | Browser (zone BFF) → gateway → medallion-producer `/produce?project=acme` | HTTP; the gateway strips spoofable headers and calls Dapr invoke | Dex bearer; FGA `can_administer` on `project:acme`, audited to `lance.audit`. The human sub becomes the `originator`. | nothing | none | same request | gateway `__init__.py`:70-81,228,347; produce_auth.py:52-67; api/produce.py:30-35 |
| 2 | Producer resolves the warehouse root | LANCE read of `_warehouses/` under `MEDALLION_CONTROL_ROOT` | static `rask-medallion` key | nothing | none | same request | produce.py:146-153 |
| 3 | Producer → catalog `POST /v1/table/acme-bronze$events/register` | HTTP. The producer states the location; it does not ask for one. | Service door: `dapr-api-token` (dedicated token for `service-medallion-producer`) plus `x-lance-service-identity` | catalog record only | catalog register marker (a byte-free op, ignored by the head) | same request | produce.py:187-215; catalog_register.py:95-120,444-481; ingest_trigger.py:62,115-116 |
| 4 | Producer → S3 | Direct LANCE write, `seed_bronze`: `create` (v2.2, stable row ids) on the first write, otherwise a full-sync `merge_insert` on `id` | static `rask-medallion` key, no vend | `acme-bronze$events`, bronze. Create, or a MergeInsert that updates, inserts and deletes by source. | none yet | same request | produce.py:217-239; compute.py:211-262; config.py:486-495 |
| 5 | Producer → bus | `emit_lineage`: sign (HMAC facet if a key resolves), stage to `_lineage_outbox`, PUB `lineage.events.v1`, drop on ack | producer key from the Dapr secret store | outbox object (transient) | **OL RunEvent COMPLETE**. job `operation=lance_ray_ingest`; `author {name: ray, sub: service-medallion-producer}`; `lance {token: T, project: acme, originator: <human>, operation}`; outputs `[acme-bronze$events @ version, schema, outputStatistics]`; deterministic uuid5 `runId` | the arrival event | produce.py:243-278; lineage_publish.py:47-100; events.py:233-273,307-312,378; values.yaml:1500-1517 |
| 6a | Bus → lineage `/lineage-events` → AGE | PUB subscription (DLQ on) | app token; signature checked if present; FGA on outputs as the stamped author | AGE: Job, Run, `WROTE{version}` → Dataset `acme-bronze$events` | — | — | lineage dapr.py:173-191; lineage api/fga_deps.py:293-333; cypher.py:12-20 |
| 6b | Bus → notifications `/lineage-events` | PUB | app token | inbox pointer | Audience: author `service-medallion-producer` (a service inbox) **and ORIGINATOR = the human**, plus `acme` watchers. Each is gated by `can_be_notified`. | — | subscriptions.py:59-66; lineage_events.py:225-256; fanout.py:95-121,146-151,189-190 |
| 6c | Bus → producer `/bronze-arrival` | PUB (queue group medallion-producer, DLQ) | **app token only; no signature check** | nothing | — | the matched write fires step 7 | bronze_arrival.py:38-62; ingest_trigger.py:113-125,265-282 |
| 7 | Producer head → catalog `describe acme-bronze$events` → PUB `medallion.bronze` | HTTP describe (a failure degrades to no `from_uri`), then PUB | service door | nothing | **Stage trigger** `{token: T, cascade_id: T, dataset: bronze$events (lane, tenant-free), namespace: bronze, project: acme, from_uri: <catalog location>, originator}` | bronze-to-silver consumes it | ingest_trigger.py:181-194,220-262,281-321 |
| 8 | Bronze-to-silver `/medallion-event` → `handle_stage` pass 1 | PUB (queue group bronze-to-silver, DLQ) | app token. Local profile: FGA self-check `can_create_table` on `namespace:acme-silver` as `service-bronze-to-silver`. | nothing | — | continues | events.py:45-65; transform.py:431-470,1776-1780; values-local.yaml:77 |
| 9 | Stage runner resolves roots | The chart's FROM/TO URI, or the project warehouse root, overridden by the catalog's describe of upstream and then by the trigger's `from_uri` if it is inside `read_root` | static key for control reads | nothing | — | — | transform.py:659-795 (`_resolve_roots`, `_confine_from_uri`; roots and read_root at :684-690) |
| 10 | Stage runner → catalog `describe`, else `create?mode=exist_ok` for `acme-silver$features` (`ensure_stage_output`), then `POST …/credentials?tier=write` (`authorize_stage_write`) | HTTP | Service door. FGA: `can_create_table` at create; `can_write_data` or `can_maintain` at the vend. **The vended STS credential is discarded.** | catalog creates an empty table only if describe refuses (catalog_register.py:331-356); seed_estate declares `acme-silver$features` and `acme-gold$catalog` (seed_estate.py:208-209), so whether the first run creates is UNVERIFIED | on a create, the catalog emits a `create_table` DDL lineage marker (catalog core/lineage_emit.py:677) | — | transform.py:1085-1126; catalog_register.py:221-263,284-357; credentials.py:78-90 |
| 11 | Stage runner → bus | `_emit_start_run` (sign, outbox, PUB) | stage runner key | — | **OL START**, same deterministic runId as the later COMPLETE | — | transform.py:1158-1168,298-358 |
| 12 | **Engine hand-off, Ray lane** (chosen because `ray_enabled` and nothing is declared) | Measures `pre_rows`, then `stage_plans.dispatch`: writes the run's `PlanDocument` under `<control root>/_plans/` (created if absent, ETag CAS), keyed by the action id `derive_idempotency_key(stage, token, from, to, code_version)`, announces it as `run_planned` on `catalog.control.v1`, and **acks** | n/a | the plan record | control event `run_planned` (object `stage_run:<id>`, the plan's URI, claim-check) | the plan owns the run | engine_choice.py:95-98; transform.py:853-885; stage_plans.py:194-257 |
| 13 | Stage runner → Ray Jobs API | `submit_stage_order` through the executor port under the action id (httpx POST `/api/jobs/`); a failed submit is logged and acked, and the sweep owns the retry | **no token to Ray** (CP-010) | — | — | the job reports, or the sweep reads it | stage_plans.py:253-257 |
| 14 | Ray job `ray_stage_job.py` → S3 | Direct LANCE writes. Seeded bronze has no `source_rowid`, so this hop takes the cascade-head branch: a driver-side pylance read with `_rowid`, stamp, then a full-sync `merge_insert` (:799-802) or a `create` (:804-811), with no lance_ray and no staging set (:778-811). The distributed lance_ray → staging → `_land_staged` path (:812-872) is for tiers that already carry `source_rowid`. Media: driver-side round trip. | `S3_KEY`/`S3_SECRET` = static `rask-ray-compute` from pod env | `acme-silver$features`, silver. Create, or MergeInsert; the `lineage` column is written in the same commit. | **none**: the job never reads `LINEAGE_URL` | job SUCCEEDED | ray_stage_job.py:1-31,73-89,679-715,721-868; _ray-cluster-config.tpl:173-182,220-226 |
| 15 | Job → the stage runner's outcome door, or the plan sweep → `run_outcomes.resolve` | HTTP `POST /runs/{action_id}/outcome`, or the `medallion-plan-sweep-cron` binding (`@every 30s`) reading the job through the executor port (a RUNNING job is left alone at any age). On success, a PUB re-publishing the trigger to the stage runner's own `sub_topic` with `ray_job_done: true`, `ray_submission_id`, `ray_duration_seconds` and the marker's committed version | the Ray head's projected `rask-medallion` token, admitted as `trainerIdentity` alone; the sidecar for the PUB | the plan's outcome (CAS, first terminal wins) | FAIL through the lineage outbox on a failed run | pass 2 of `handle_stage` on success | run_outcomes.py:91-158; stage_plans.py:130-191,272-319; stage_outcomes.py:32-36 |
| 16 | `handle_stage` pass 2 | `measure_stage` reads the written dataset; the local profile also runs `assert_quality` (row_count > 0, key non-null, required columns) | static key | reads only; pass 2 also re-runs read_upstream, `ensure_stage_output` and `authorize_stage_write` (a second write vend); only the START emit is suppressed (transform.py:331-332,1087-1126). Measuring rebuilds the lineage index, a `CreateIndex` commit of its own that no event names; the event carries the data version captured before it (compute.py:180-186) | — | — | transform.py:971-983,1187-1200; compute.py:180-186 |
| 17 | Stage runner → bus | `_emit_complete` (sign, outbox, PUB) | stage runner key | — | **OL COMPLETE**: author `{name: data_eng, sub: service-bronze-to-silver}`; `lance {token: T, cascade_id: T, project: acme, originator}`; inputs `[acme-bronze$events]`; outputs `[acme-silver$features @ data version]` with `outputStatistics`, `columnLineage` and `dataQualityAssertions` | AGE: `READ` (no input version: inputs are built bare, events.py:379, and READ gets a version only when one is sent, lineage repository.py:355-365), `WROTE{version}` and a dataset-level, unversioned `DERIVED_FROM` silver → bronze (cypher.py:414), plus column edges. Notifies ORIGINATOR and watchers as in 6b. | transform.py:1809-1839,1279-1281; events.py:90-128; cypher.py:12-20 |
| 18 | **Gate.** `_review_reasons` (band; review is off, so no reasons) → `_probe_gate` (skipped with no band reasons) → `gate_decision` = **PUBLISH** (a target and a catalog) | code | — | — | — | — | transform.py:1428-1458; gate_decision.py:83-95 |
| 19 | Stage runner → catalog `POST /management/v1/table/acme-silver$features/publish {version, key_column, required_columns, cascade_id: T, originator}` | HTTP | Service door. FGA `can_update_tag` (publisher rung) at the router; `can_promote` too if `accept_assertions` is sent. | The catalog runs its assertions and **moves the `published` tag** (a ref, not a data commit). A refusal is 200 with `published: false`. | **Control event `table_published`** through the control outbox → PUB `catalog.control.v1`, `extra {from_version, to_version, location, project, cascade_id, originator, accepted}`. **Unsigned.** | step 20 | catalog_register.py:146-206; fga_deps.py:256-261; publication.py:251-366 |
| 20 | Bus → producer `/publication-arrival` | PUB | **app token only; no signature check** | — | Stage trigger `{token: event_id, dataset: silver$features, namespace: silver, from_version, to_version, from_uri: location, project: acme, cascade_id: T, originator}` published to `transform_routes["silver"] = medallion.silver` | silver-to-gold | bronze_arrival.py:94-112; publication_trigger.py:110-166,169-212; medallion.yaml:219-226 |
| 21 | Silver-to-gold: repeat steps 8-17 | same | `service-silver-to-gold`; local-profile FGA self-check `can_promote` on `namespace:acme-gold`. `from_version` reaches the job as `RASK_VERSION_FLOOR` (work_order.py:169-170; ray_stage_job.py:460-461). On the first silver publication it is None, so the run is full: silver carries `source_rowid`, so it takes the distributed lance_ray → staging → `_land_staged` merge (ray_stage_job.py:812-859). A driver-side delta merge (:754-777) runs only when a floor exists and gold has stable row ids. | `acme-gold$catalog`, gold | START and COMPLETE as above. The token is now the control `event_id`; `cascade_id` is still T. | — | values.yaml:1537; workflow.py:512-515; ray_stage_job.py:460-461,505-570,754-859 |
| 22 | Gold gate → publish → `table_published` for gold | as in 19 | as in 19 | `published` tag on gold | `table_published` (gold). `/publication-arrival` finds no route for `gold` and acks. | **end** | gate_decision.py:90-91 (gold still has a target and a catalog); publication_trigger.py:189-192 |

**The in-process lane** (`medallion.ray=false`) replaces steps 12-16 with a synchronous
`executor_for(IN_PROCESS_ENGINE)` running `compute.transform_stage` inside the stage runner, under the static
`rask-medallion` key. If the rows line up it uses `add_columns`; otherwise it runs a full-sync `merge_insert(id)`
(update, insert, and delete by source) or a `create` on the first write, then rebuilds the lineage index
(transform.py:849-902,1005-1009; compute.py:311-414). There is no plan, no Ray, and no second pass. Neither lane runs a Dapr Workflow: the Ray lane's run is a plan
closed by its job's report or the sweep (stage_runner.py:84-89).

**HOLD branch** (off by default). It needs review enabled, either by `qualityReview: true` or by a project-declared gate record (gate.py:88-115; transform.py:1303-1312,1656-1657), and a band breach (±25%, or a first promotion;
values.yaml:1436,1457). Then `_probe_gate` asks the catalog with `gate_only` (tag untouched), `gate_decision` returns
HOLD (a failed assertion returns BLOCK), and `_report_hold` emits a FAIL run with `promotion_status`
HELD or BLOCKED and token `T:quality-hold`. With review on it publishes the hold on `medallion.promotion`
(transform.py:1377-1410,1588-1671). The producer's `promotion_review` Workflow emits the control event
`promotion_review_requested` naming the approver when `qualityReviewApprover` is set (workflow.py:1298-1316); the default is empty (values.yaml:1439), and then the review answers BLOCKED. Notifications delivers that named action to the
approver (control_events.py:42-53). The approver answers at `POST /api/promotions/{id}/decision`
(promotions.py:282). An approved resume publishes as `service-medallion-producer` with `accept_assertions`, which
needs `can_promote` (publication.py:271-287; seed_estate.py:293-305). A catalog refusal (`published: false`) is
reported as REFUSED (transform.py:1499-1504; gate_decision.py:111-115).

**What stops or diverts the cascade (none of these is silent):**
- **An unseeded project.** `/produce?project=acme` answers 409 with routing disabled or no warehouse
  (services/produce.py:146-151; api/produce.py:81-86).
- **A describe outage at the head.** It degrades to a composed path (ingest_trigger.py:255-257).
- **A catalog refusal at publish.** Nothing downstream fires, and a FAIL run with `promotion_status: REFUSED` is emitted (transform.py:1499-1504,1626-1637).
- **A lost `table_published` event.** The control outbox and relay recover it (publication.py:341-343); Ray job
  failures are covered by `report_stage_outcome`.

**Who is told, per hop:**
- Every COMPLETE and FAIL run puts its `author.sub`, a service subject, first in the audience (fanout.py:96); delivery still passes `can_be_notified` (fanout.py:189).
- It also reaches the `lance.originator` human carried from step 1 (ORIGINATOR), and `acme` members who watch the
  project (WATCH, re-checked against `project#member`).
- Only terminal states notify (lineage_events.py:249-256; fanout.py:95-121).
- `table_published` is not a NAMED action, so a publication notifies nobody (control_events.py:42-53).
- **Gap:** if `/produce` was called with the app token rather than a human bearer, there is no originator, and a
  failure deeper in the cascade reaches only service inboxes.

---

## 3. SCORECARD, CURRENT STATE

### 3a. Cloud-native

| Item | Score | Evidence | Rows |
|---|---|---|---|
| Helm/K8s deploy | GOOD | One chart for local and prod; KubeRay, NATS, Dapr, CNPG, OpenFGA and MinIO are toggles (values.yaml:2336,2515,2745,2804,2988,3252) | — |
| Operators in the app release | PARTIAL | The operators ride in the app release, and the no-split decision rests on a store the estate left | XC-085, XC-049 |
| Stateless services | GOOD | State lives in S3/Lance, AGE and OpenFGA Postgres; stage runners and the producer are declared stateless (values.yaml:1249-1253). The catalog's control ring buffer is per replica (values.yaml:1126-1129). | LH-305 (parked) |
| Horizontal scaling / replicas | PARTIAL | Every lakehouse deployment defaults to 1 replica (values.yaml:1253,1275; services at 428,444). Queue groups make replicas safe for subscribers (values.yaml:1249-1252). HPA is off and targets only catalog and lineage (values.yaml:2297-2311). Scaling the catalog needs affinity (values.yaml:1127-1129). One image serves eleven deployments. | LH-197 |
| Health probes | GOOD | startup, readiness and liveness on `/livez` and `/readyz` (_helpers.tpl:1081-1101; medallion.yaml:407,710) | — |
| Graceful shutdown | GOOD | `terminationGracePeriodSeconds` and `preStop` (medallion.yaml:34,47,449,462). Draining refuses doors with 503 and asks subscriptions for RETRY (draining.py:88; bronze_arrival.py:49-61). | — |
| Config via env / secret store | PARTIAL | pydantic env config. Secrets come through the Dapr secret store on OpenBao, which is dev-mode root (values.yaml:3210-3218). ESO is off by default (values.yaml:3229-3235), on in the k3s-up profile (values-local.yaml:185-186). Rotation reaches no running pod. | XC-004, XC-079, XC-081, XC-082, LH-160 |
| Observability | PARTIAL | OTel → GreptimeDB → Perses, on (values.yaml:3302-3303); vmalert and Alertmanager are off by default (values.yaml:3346-3347) and on in the k3s-up profile (values-local.yaml:233-235); `setup_otel` at otel.py:95. No CI proves export works; the latency alert cannot fire for lakehouse apps; audit logs are routed by body text. | XC-064, XC-067, XC-058, CP-018 |
| Resilience: retries and DLQ | PARTIAL | Dapr Resiliency is on (values.yaml:2924-2925; dapr-resiliency.yaml:49-51). DLQ per app (medallion.yaml:565-566). The medallion and notifications DLQs only park; lineage re-presents a park once. | LH-148, XC-095 |
| Resilience: outbox | PARTIAL | The lineage outbox and control outbox are on (values.yaml:491-492,1150-1160). The medallion stage path is stage → publish → drop (lineage_publish.py:86-100). The media trigger is a bare publish (media_produce.py:242-258). A crash between commit and stage loses the author. | LH-329, LH-225 |
| Resilience: Ray | GAP | The default chart submits to a Service that does not exist. The Ray cluster is head-only. A head restart loses job records. | CP-041, CP-043, CP-036 |
| Portability: object store | PARTIAL | S3 API only (MinIO; s3Endpoint is http, _helpers.tpl:729-731). MinIO images are not pullable anonymously. CAS was last proven on RustFS. | XC-075, LH-254 |
| Portability: arm64 | GAP | Never brought up on arm64 | XC-070 |

### 3b. Zero trust

| Item | Score | Evidence | Rows |
|---|---|---|---|
| Workload identity | GAP | The service door is a static token plus an asserted name header (dapr_auth.py:457-512; catalog_register.py:115-118). ServiceAccounts are off by default (values.yaml:750-751; medallion.yaml:35-37), so pods run as `default`. | LH-220, XC-076 (D1) |
| Authn at the edge | GOOD | Dex OIDC. The gateway strips `dapr-api-token`, `x-lance-service-identity` and related headers (gateway `__init__.py`:70-81). `auth.enabled: true` (values.yaml:965). | — |
| Authn at every internal hop | GAP | NATS takes no client auth (values.yaml:2744-2800). Subscribers check only the app token (bronze_arrival.py:48,106; events.py:57). The Ray dashboard is tokenless (values.yaml:2563-2564). OpenFGA and GreptimeDB accept any pod. | XC-078, CP-010, XC-077, XC-003 |
| Authz (FGA) at every door | PARTIAL | The catalog guards every route (router.py:48; fga_deps.py:808-814). The producer's human door checks `can_administer` (produce_auth.py:52-67). Stage runners self-check only when `medallion.fgaEnabled` (off by default; values.yaml:1242). The bus heads have no FGA. There is no Dapr accessControl. Estate doors use `can_observe_events`. No test hits a real OpenFGA. | XC-009, LH-076, LH-235, LH-237 |
| Least-privilege storage credentials | GAP | Static keys everywhere a tier is written: `rask-medallion` (config.py:486-495), `rask-ray-compute` (_ray-cluster-config.tpl:173-182), and the vendor's parent `rask-catalog`. The stage vend is discarded (catalog_register.py:221-263). A write vend covers the whole table prefix. Maintenance falls back to static keys. | LH-218, LH-129, LH-202, LH-219, XC-083, XC-084, CP-007, LH-229, LOW-027 |
| Signed events | PARTIAL | The lineage lane signs when a key resolves (lineage_publish.py:47-100) but verifies only if a signature is present (lineage api/fga_deps.py:321-323). Only identities with a dedicated `service-token-<identity>` sign, and that HMAC key is the same secret the service door compares (dapr_auth.py:436-438,503-509), so a verifier holding it could also call the catalog as the producer. The control lane is unsigned. The cascade heads and notifications verify nothing. The Ray stage job emits no lineage. | LH-064, XC-078 |
| Bus authentication | GAP | As above. D14 puts NATS auth outside the no-prod parking (`open_backlog_left_new2.md:42`). | XC-078 |
| Secrets handling | PARTIAL | Dedicated per-service tokens (values.yaml:939) and the Dapr secret store. But OpenBao runs as root in dev, the shared bundle exposes peers' keys, the `default` SA can read every Secret, secrets arrive through env, and nothing is generated in the cluster. | XC-079, XC-080, XC-076, LH-160, XC-004, XC-001 |
| TLS in transit | PARTIAL (parked) | Dapr Sentry mTLS between sidecars is on (values.yaml:2940-2946). App→MinIO, NATS, OpenFGA and Dex are plain http (_helpers.tpl:729-731). TLS is under the no-prod parking (`open_backlog_left_new2.md:43`). | XC-007, XC-008 (parked) |
| Network policy | GAP (default) | `networkPolicy.enabled: false` (values.yaml:784); prod turns it on (values-prod.yaml:108-109) | XC-009 |
| Audit trail | PARTIAL | The catalog's, the producer's and ingest's FGA decisions go to `lance.audit` (audit.py:4-8,23-24; produce_auth.py:52-67; ingest auth.py:99-110); the stage-runner self-check (transform.py:431-470), the notification gates and refused signatures (lineage fga_deps.py:335, `log.info` only) do not. But GreptimeDB is erasable by any pod, records carry no format version, routing is by body text, and reclaim is not audited. | XC-003, LH-231, XC-058, LH-232 |

### 3c. Low coupling / bring-your-own

| Item | Score | Evidence | Rows |
|---|---|---|---|
| Import contracts | PARTIAL | `the-lakehouse-is-not-built-on-ray` covers catalog, lineage, medallion, maintenance and notifications, and `the-lakehouse-is-not-built-on-a-workflow-engine` covers catalog, lineage, medallion, maintenance and service_kit, with six medallion ignore lines (.importlinter:51-84). Ingest and controlplane are outside both. | XC-109, XC-093 |
| Workflow-engine seam | PARTIAL | A Ray stage run and a training run are each a plan closed by an outcome door or the plan sweep, with no workflow (stage_plans.py, train_plans.py). The one medallion workflow, `promotion_review`, is started, read and signalled through the `SagaClient` port (`start`, `state`, `signal`; saga.py), whose Dapr adapter is the only medallion module besides `medallion.workflow` that the import contract lets name the engine. Ingest uses `dapr.ext.workflow` directly (ingest `__init__.py`:161,260). | XC-109, CP-025 |
| Compute-engine seam | PARTIAL | The `Executor` port (executor.py:137) and `engine_choice` pick in-process or Ray by record, and refuse an unhosted engine (engine_choice.py:84-118). But no lane is declared (the chart boolean decides), and the train submit and resubmit bypass the port. There are two Jobs clients. | CP-031, CP-044, CP-022 |
| Lakehouse ↔ annotator, search, viewer | PARTIAL | The viewer and search open Lance with the deployment key. Annotator saves go through lancekit directly. Maintenance knows annotator objects. | LOW-027, LOW-003, LH-325 (parked) |
| Event-driven vs call-driven | PARTIAL | Tier-to-tier movement is event-driven (steps 5→7, 19→20). Control calls to the catalog are synchronous HTTP (steps 3, 7, 10, 19). The ingest, Ray-train and external lineage lanes are HTTP-only and bypass the bus. The media head triggers directly. | CTL-021, LH-329, CP-037 |
| Schema contract (opaque payload) | GOOD | A tier row is `{id, payload, stage, lineage, source_rowid}` with an opaque payload (medallion/schemas/tier.py:4-8). The stage stamp is one implementation (stage_stamp.py, imported by both drivers: compute.py:39; ray_stage_job.py:70). | LH-208 (provenance columns writable) |
| Shared-library coupling | PARTIAL | service-kit's base dependencies are light (pyproject deps). Lance, lancedb and pillow sit in extras (`lancekit`, `media`), and the lakehouse services do not take `lancekit`. The medallion takes `media` (medallion pyproject:8). One lakehouse image means one rollout for all. | LH-197, LOW-030 |
| Catalog as sole committer and announcer | GAP | `/produce`, `/ingest-media` and both stage lanes commit Lance directly. `/commit` is Append-only (dataplane.py:887). | LH-330 (commit-door OPEN DECISION), LH-164 (D6) |

---

## 4. TARGET STATE (GAP and PARTIAL items only)

Labels: **RULED** = an owner ruling or CLAUDE.md; **ROW** = the row's How; **OPEN DECISION** = listed under "Decisions still open".

| Item | Target | Label |
|---|---|---|
| Workload identity | Kubernetes SA tokens; identity comes from the verified token, and no header asserts it | RULED D1 (`open_backlog_left_new2.md:38`); ROW LH-220 (projected token re-read per request; SA issuer in the OIDCVerifier; delete `x-lance-service-identity`), XC-076 (one SA per service; `secretReader` off) |
| Internal authn: bus | NATS decentralized JWT per Dapr app-id; the heads require a signature; `CatalogControlEvent` signed | RULED D14 (NATS auth outside the parking, :42); ROW XC-078 |
| Internal authn: Ray, OpenFGA, GreptimeDB | Ray token auth for the chart head in every profile; OpenFGA OIDC with SA tokens and the playground off; GreptimeDB static-user-provider from OpenBao | ROW CP-010; RULED D14 (OpenFGA authn outside the parking) + ROW XC-077; ROW XC-003 |
| Signed lineage | kid/canon/versioned schema, previous-key window, chart-derived delegator allowlist, then require-a-signature | ROW LH-064 |
| Least-privilege storage | Stage lanes keep a per-table vend cache; `server_mediated` follows the commit-door decision; a refused vend fails the stage | ROW LH-218 |
| | Ray jobs get a projected SA token and a write-tier table-scoped credential bound to their registered run, and open through the namespace. pylance 12's storage-options provider refreshes with `DescribeTable(vend_credentials=true)` (measured), and describe vends the read tier (tables.py:462), so the run-bound write vend has to come from describe (with LH-229); lance_ray `write_lance` commits client-direct and has no merge_insert, so under a catalog-door decision jobs write with `LanceFragmentWriter` and the catalog commits | ROW LH-129 (builds on D1) |
| | Writer vends narrowed to `data/*` (plus `_deletions/*` for a merge, update or delete, which write deletion files before any commit; measured on pylance 12.0.0), after the commit-door decision | ROW LH-202 |
| | The vendor's parent uses AssumeRoleWithWebIdentity | ROW XC-083 (D1) |
| | A workload credential door replaces the six static users | ROW XC-084 |
| | Maintenance fallbacks removed | ROW LH-219 |
| | Ingest refuses ambient credentials | ROW CP-007 |
| Catalog as sole committer | Choose (a) the server-side data doors, (b) a client-direct non-Append door, or (c) the maintainer tier for stage identities, after LH-330's measurements | OPEN DECISION "commit-door decision" (`open_backlog_left_new2.md:85`); ROW LH-330 |
| | Where the default lane lives, and whether the head asks for its location | OPEN DECISION D6 (:68); ROW LH-164 (under D6(a)) |
| Media head arrival-driven | Drop the direct publish; `/bronze-arrival` matches the media write and routes it by `transform_routes` | ROW LH-329 (admitted, :58); CLAUDE.md Architecture ("driven by the ARRIVAL event") |
| | Ingest and controlplane added to both contracts; `ingest_run` behind the saga port | RULED "Ingest is a Phase 1 component" (:60) + ROW XC-109 |
| | A runtime proof with no engine and no Ray | ROW XC-093 |
| Compute-engine seam | Lanes declared through the transform door from a chart hook | ROW CP-031 |
| | Resubmit and train through the port | ROW CP-044 |
| | One Ray address define, `medallion.rayAddress` deleted | ROW CP-041 |
| | One Jobs client outside ray-kit | ROW CP-022 |
| | Where training lives | OPEN DECISION "Where training lives" (:86) |
| Ray resilience | Worker groups, and a head that holds no drivers | ROW CP-043 |
| | GCS fault tolerance | OPEN DECISION "Ray GCS fault tolerance" (:75) + ROW CP-036 |
| Replicas and images | One image per lakehouse member | RULED D14(1) (:42); ROW LH-197 |
| Operators in the app release | Move the eight operators to an infra release | ROW XC-085; OPEN DECISION D11 option (c) (:73) |
| Secrets | Generated in-cluster, ESO, a narrow OpenBao sidecar token, a split bundle, TTL caches | ROW XC-004, XC-079, XC-080, XC-082, LH-160; OPEN DECISION D8 (:70) |
| Observability | A CI lane proves OTLP export; one OTel path for agent-launched apps; audit routed on a structured key; audit format and closed vocabulary | ROW XC-064, XC-067, XC-058, LH-231 |
| DLQ | An admin re-drive door for parked lineage; the medallion and notifications DLQs only park | ROW LH-148; LH-331 (parked finding) |
| Network policy and access control | Per-app Dapr `accessControl` defaultAction deny, then NetworkPolicy | ROW XC-009 |
| TLS | Deferred until a production estate exists | RULED "No-prod parking (2026-09-21)" (:43); certificate source: OPEN DECISION (parking list, :82) |
| arm64 | Bring-up on an arm64 host | ROW XC-070 |
| Object store portability | Pullable MinIO images; CAS re-proven on MinIO | ROW XC-075 (+ OPEN DECISION D10, :72), LH-254 |
| Explorer/search/annotator coupling | Open through the catalog (ListTables, DescribeTable vend) | ROW LOW-027 (open through the catalog); ROW LOW-003 (canvas saves go through the task draft) (both LOW, CLAUDE.md Phase 1 scope); LH-325 is parked, no target |
| Notification lane completeness | The reconciler reads a narrow feed rung under an SA token | ROW CTL-021 (FOCUS item 6) |
| Event-emission gaps on compute lanes | START, terminal-once and staging for compute-plane lineage | ROW CP-037 |
| Authz outcome proof | A real-OpenFGA drive of the catalog door | ROW LH-235 |

### Unowned recommendations (my recommendation, not a ruling or a row)

1. **The default chart's unreachable Ray head is owned.** CP-041's How already covers it: one define that fails
   the render if medallion Ray is on and no head renders, gated over the default, local and prod renders
   (open_backlog_left_new2.md:422-423). Listed here only so headline 1 is not read as unowned.
2. **Originator on app-token-triggered runs.** A cascade started through the shared app token carries no originator
   (produce_auth returns None on the service path; api/produce.py:33,80), so failures reach only service inboxes
   and project watchers. This is designed: tests/unit/test_cascade_originator.py pins "no audience is a legitimate
   answer" for a run with no human behind it. Any change is an owner question, not a row.
3. **The Ray stage job emits no lineage of its own.** It is not armed, per _ray-cluster-config.tpl:220-226. The
   stage runner's START and COMPLETE bracket it, but a job that fails after committing gets a FAIL from `report_stage_outcome`
   (workflow.py:653-727) with a bare output and no version (events.py:348-361), so the committed version is recorded
   only by the reconcile back-fill. LH-129 touches the job's emits; CP-037 covers the dummy and htr runners and the
   train job only (open_backlog_left_new2.md:1421-1423). CP-029's clause (e) owns this case.
4. **`table_published` notifies nobody.** A tier becoming readable is arguably the event a watcher wants. The rule
   "not a NAMED action" (control_events.py:42-53) is deliberate. Any change is an owner question, not a row.

---

## 5. The fixed stack: how each piece is used today, and how to use it better

**The owner's statement (2026-09-30, relayed by the coordinator).** The stack is fixed: OpenBao, MinIO,
OpenTelemetry, Kueue, OpenLineage, KubeRay and lance-namespace, together with what CLAUDE.md names (Dapr, NATS
JetStream, OpenFGA, Dex). Every target in §4 stays inside this stack except XC-049 (§3a, Operators), whose How removes Kueue from rask's release; §5.11 sets out that conflict. Below,
"Better use" is sourced from a ruling or a row where one exists, and is otherwise marked **recommendation**.

### 5.1 OpenBao (secrets)

- **Current use**
  - Deployed in-chart with `devMode: true` and `devToken: root` (values.yaml:3210-3218).
  - Every app in `lance.secretScopes` (dapr-component.yaml:374-378) reads it through the Dapr `secretstores.hashicorp.vault` component `lance-secrets`. In dev mode
    the component carries the literal root token; otherwise it reads an out-of-band Secret
    `<release>-openbao-token` (dapr-component.yaml:333-356).
  - TLS verification is skipped for an http address (dapr-component.yaml:368).
  - Apps fetch their S3 secret and dedicated service tokens from it (`*_SECRETS_FROM_DAPR`, medallion.yaml:395,403,600,606).
  - Signing keys resolve through it (lineage_publish.py:47-73).
  - ESO is off by default (values.yaml:3229-3235) and on in the k3s-up profile (values-local.yaml:185-186).
- **Gaps** (scorecard: secrets handling, config)
  - Dev uses root, and prod uses one shared unprovisioned token (XC-079).
  - Secrets are cached for the life of the process, so a rotation reaches no pod (XC-082).
  - One shared bundle exposes peers' keys and the ROOT S3 pair (XC-080).
  - The chart mints and carries the credentials, and nothing installs ESO (XC-004).
  - Infra and zone secrets arrive through env (LH-160, XC-001).
  - The Ray head cannot read the store and mounts `infra-credentials` instead (_ray-cluster-config.tpl:173-196).
- **Better use**
  - Kubernetes auth with a narrow provisioning role, and a periodic, policy-bound sidecar token refreshed by ESO
    (ROW XC-079).
  - Per-consumer paths (ROW XC-080).
  - Create-if-absent generation in-cluster and ESO-written Secrets (ROW XC-004).
  - A jittered 300-600 s TTL cache (ROW XC-082).
  - Unseal and prod custody wait under the no-prod parking (RULED :43; OPEN choices :82).

### 5.2 MinIO (object store and STS)

- **Current use**
  - A single MinIO server holds every Lance table and the control roots (values.yaml:2336-2345,2469;
    s3Endpoint http, _helpers.tpl:729-731).
  - Six static per-service users are created by `mc admin user add`: maintenance, ray-compute, medallion,
    lineage, catalog and viewer (minio-scoped-users.yaml:288,373,444,494,552,588), with broad statements such as
    `arn:aws:s3:::*` / `*/*` (:236-245).
  - The catalog vends through STS `AssumeRole` plus an inline session policy (TTL 900 s), signed with its own static
    user (values.yaml:1031-1033; vending.py:10-23).
  - MinIO's OpenID provider (AssumeRoleWithWebIdentity) exists in the template but is off by default
    (minio.yaml:142-164; values.yaml:2510-2512).
- **Gaps** (scorecard: least-privilege storage, portability)
  - Tier writes run on static users (LH-218, LH-129).
  - The vendor's parent key is static (XC-083).
  - The users never expire, and backups sign as ROOT (XC-084).
  - A writer vend covers the whole prefix (LH-202).
  - Stock clients get no vend (LH-229).
  - The images are not pullable anonymously (XC-075).
  - CAS was last proven on RustFS (LH-254).
- **Better use**
  - Register the k3s SA issuer as a MinIO OpenID provider, so that vending and the catalog's own IO use
    AssumeRoleWithWebIdentity (ROW XC-083, under RULED D1).
  - A workload-credential door replaces the six users (ROW XC-084).
  - Narrow write-tier policies (ROW LH-202, after the commit-door OPEN DECISION :85).
  - Where the binary comes from is OPEN DECISION D10 (:72).

### 5.3 OpenTelemetry

- **Current use**
  - `service_kit.setup_otel` does OTLP/HTTP traces and RED metrics (otel.py:95), called by the app factory and the
    gateway (service_kit/app.py; gateway `__init__.py`).
  - The chart Collector sends to GreptimeDB and Perses, and vmalert hands to Alertmanager
    when `observability.alerting.enabled` (default false, values.yaml:3346-3347; true in values-local.yaml:233-235).
    The Collector itself is on (`observability.enabled: true`, values.yaml:3302-3303).
  - The audit trail is the `lance.audit` logger shipped as logs (audit.py:4-8,23-24).
  - A span covers the whole produce operation (produce.py:140).
  - Trace context crosses into Ray jobs through TRACEPARENT (ray_stage_job.py:29-31).
- **Gaps** (scorecard: observability, audit)
  - Seven chart commands run `opentelemetry-instrument` (grep count). Those apps never get the bucket View, so
    `HttpServerLatencyHigh` cannot fire for them (XC-067).
  - No CI lane proves export works (XC-064).
  - Audit records are selected by body text (XC-058) and are unversioned (LH-231).
  - GreptimeDB is unauthenticated (XC-003).
  - Ray logs are not shipped (CP-018), and Serve tracing is unobserved (CP-019).
  - Ingest exports no Lance IO metrics (LH-096).
  - Request-id competes with trace context (XC-048).
- **Better use**
  - One OTel path through `setup_otel` (ROW XC-067).
  - A Dagger export lane (ROW XC-064).
  - Structured audit routing (ROW XC-058).
  - Trace context supersedes request-id (RULED D14(2) :42; ROW XC-048).

### 5.4 OpenLineage

- **Current use**
  - `packages/lineage-kit` is the emission kernel.
  - The medallion, catalog and maintenance publish OL RunEvents and DatasetEvents on `lineage.events.v1` through
    the object-store outbox (lineage_publish.py:86-100; values.yaml:491-492).
  - Events carry the `author`, `lance` (token, cascade_id, project, originator), `outputStatistics`,
    `columnLineage` and `dataQualityAssertions` facets (events.py:90-128,233-273,307-312).
  - The lineage service MERGEs events into AGE (cypher.py:12-20).
  - Ingest, the Ray train job and external producers POST to `/api/v1/lineage` over HTTP only
    (endpoints/ingest.py:103).
  - Signing is an HMAC facet, verified only when present (lineage api/fga_deps.py:293-333).
- **Gaps** (scorecard: signed events, event-driven)
  - Unsigned events are admitted (LH-064).
  - HTTP-only run events never reach the bus consumers. Ingest's bronze data write still reaches the cascade head,
    because the catalog announces `/commit` on the bus (data.py:220-229). For notifications the reconciler is the only
    bridge, and it is dead or tenant-blind (CTL-021); maintenance's cron sweep is its backstop (values.yaml:1574-1578).
  - The Ray stage job emits nothing itself (_ray-cluster-config.tpl:220-226).
  - Compute lanes are fire-and-forget (CP-037).
  - Emitted versions can be wrong (LH-214).
  - A crash between commit and stage loses the author (LH-225).
  - Stage runs name no input version, so READ is unversioned and DERIVED_FROM is dataset-level (LH-333, parked).
  - The custom facets (`author`, `lance`, `signature`, `model`) carry no project prefix and point at BaseFacet (openlineage.py:30-35,70-78); LH-064's How renames them.
- **Better use**
  - Require signatures with versioned facets and a rotation window (ROW LH-064).
  - The reconciler reads a narrow feed rung (ROW CTL-021).
  - START, terminal-once and staging on compute lanes (ROW CP-037).
  - Pin the committed version (ROW LH-214).
  - Commit markers go in Lance transaction properties (ROW LH-225).
  - **Recommendation:** publish HTTP-ingested runs onto the bus through the lineage service's outbox after they
    are admitted, so bus consumers see one stream. No row owns this beyond CTL-021's reconciler.

### 5.5 KubeRay

- **Current use**
  - The chart renders a RayCluster only under `ray.cluster.enabled` (raycluster.yaml:1; default false,
    values.yaml:2529-2530), or a RayService under `singleTenant.enabled` (rayservice.yaml:1; default false,
    values.yaml:66-67).
  - `make k3s-up` turns the cluster on (values-local.yaml:126-127).
  - The medallion submits stage and train jobs over the Ray Jobs REST API to `<release>-ray-head-svc:8265`
    (medallion.yaml:145,617; workflow.py:478-520).
  - The job signs S3 with the static `rask-ray-compute` key from pod env (_ray-cluster-config.tpl:173-182).
  - Token auth is available but off (`ray.auth.enabled: false`, values.yaml:2563-2564; wiring at
    _ray-cluster-config.tpl:19-31).
- **Gaps** (scorecard: Ray resilience, internal authn, least privilege)
  - The default render submits to a Service that does not exist (CP-041).
  - The dashboard is tokenless (CP-010).
  - The cluster is head-only (CP-043).
  - GCS job records are lost on a head restart (CP-036).
  - The head image bypasses `rask.image` (CP-050).
  - Logs are not shipped (CP-018).
  - There is a static S3 key and lineage tokens in env (LH-129).
  - There are two Jobs clients (CP-022).
- **Better use**
  - One Ray-address define that fails the render when medallion Ray is on and no head exists (ROW CP-041).
  - Token auth delivered as a file (ROW CP-010). It is one cluster-wide shared token, so the Ray submission is the one hop that carries no per-caller platform identity.
  - A worker group with `num-cpus: 0` on the head (ROW CP-043).
  - A projected SA token plus a table-scoped vend, opening through the namespace via lance_ray's `namespace_impl`
    (ROW LH-129).
  - GCS fault tolerance is an OPEN DECISION (CP-036): the chart already implements Ray's embedded RocksDB store on a ReadWriteOnce PVC (`_ray-cluster-config.tpl:296-298` sets RAY_gcs_storage=rocksdb and RAY_gcs_storage_path; `raycluster.yaml:40-60` renders the PVC), behind `ray.cluster.gcsFaultTolerance.enabled: false`. Ray 2.58 documents it as alpha and single-writer. Enabling it today opens a second `env:` on the head and drops its OTel, lineage and S3 env. The open choice is: enable it (fixing that first), accept job loss, or make a Redis exception.
  - The Ray head is owned by the chart (RULED "Ray head (2026-09-25)", :45).

### 5.6 lance-namespace

- **Current use**
  - The catalog serves the Lance Namespace REST spec (`/v1/table/{id}/…`, `/v1/namespace/…`) with rask's
    governance on top: every route passes `authorize` (router.py:40-55; fga_deps.py:808-814).
  - Only Lance tables are accepted, by ruling (CLAUDE.md "LANCE ONLY").
  - The medallion talks to it over raw httpx: describe, create?mode=exist_ok, register, publish and credentials
    (catalog_register.py:206,259,328,348,481).
  - lance_ray and pylance 12 can open through `namespace_impl` / `namespace_client`, and nothing in the cascade
    does so (LH-129 How).
- **Gaps** (scorecard: catalog as sole committer, least privilege)
  - Conformance defects on edge paths (LH-258).
  - 36 of 54 operations are never round-tripped with the stock client (XC-092).
  - Stock clients get no credential (LH-229).
  - Unfiled upstream defects (LH-048; the upstream go-ahead is an OPEN DECISION).
  - The medallion half-uses the shared client (LH-276).
  - Non-Append tier writes bypass the namespace entirely (LH-330).
- **Better use**
  - Vend on describe (read tier) and ship a service-kit `LanceNamespace` subclass (ROW LH-229).
  - Drive every operation through typed requests (ROW XC-092).
  - Route tier commits through namespace doors per the commit-door OPEN DECISION (:85; ROW LH-330).
  - Jobs open through the namespace (ROW LH-129).

### 5.7 Dapr

- **Current use**
  - pub/sub on NATS JetStream, eight `pubsub.jetstream` Component definitions (dapr-component.yaml:16,42,91,146,243,311,404,477); the one at :243 renders once per subscriber (`range`, :235).
  - Workflow: medallion `promotion_review` (workflow.py), hosted by the producer under `qualityReview`, plus ingest
    `ingest_run`. A Ray stage run and a training run are plans, not workflows.
  - Actors: notifications `InboxActor` (inbox_actor.py:210).
  - A Postgres actor state store (dapr-statestore.yaml:47,72).
  - Cron bindings, eight Components (catalog-control-relay-cron.yaml:28 … services.yaml:492).
  - The Vault secret store (5.1).
  - Resiliency and DLQ (values.yaml:2924-2925).
  - Sentry mTLS (values.yaml:2940-2946).
  - Component `scopes` restrict which app-ids may load a component (dapr-component.yaml:21-26).
- **Gaps**
  - No `accessControl` on any callee (XC-009).
  - The app token is the only proof at subscription routes (bronze_arrival.py:48; events.py:57).
  - Workflows are unversioned (CP-025).
  - Ingest uses raw nats-py beside Dapr (queue.py:33; XC-109).
- **Better use**
  - Per-app Configuration `accessControl` defaultAction deny (ROW XC-009).
  - Versioned workflows (ROW CP-025).
  - `dapr-api-token` stays only as proof of sidecar arrival (ROW LH-220 How).

### 5.8 NATS JetStream

- **Current use**
  - A 3-replica cluster with JetStream and file-store PVCs (values.yaml:2744-2800).
  - No component carries a NATS credential (no jwt, seedKey or token); the subscriber components also set queueGroupName, deliverPolicy, durableName and topic scopes (dapr-component.yaml:246-267).
  - Ingest connects directly with nats-py and no credentials (queue.py:234).
- **Gap**
  - Any pod can publish or subscribe (XC-078). Dapr component scopes do not stop a pod that talks to NATS
    directly.
- **Better use**
  - Decentralized JWT auth per Dapr app-id, with `jwt`/`seedKey` from OpenBao on each component, and narrowed
    port 4222 (RULED D14 :42; ROW XC-078).
  - Ingest's raw client moves behind the saga port (ROW XC-109).

### 5.9 OpenFGA

- **Current use**
  - The chart-deployed server on the AGE Postgres (values.yaml:2987-2990).
  - The model lives in `service_kit/governed/auth/model.fga`.
  - Checks run at every catalog route (fga_deps.py:808-814), the producer's human door (produce_auth.py:52-67),
    the stage runner self-check when enabled (transform.py:431-470), lineage output checks (lineage api/fga_deps.py:339) and
    notifications delivery and render (visibility.py:55-61).
- **Gaps**
  - The API accepts any pod, with the playground on and CORS `*` (XC-077).
  - The principal key is the raw Dex `sub` (LH-063).
  - Estate doors use one relation (LH-076).
  - No test runs against a real store (LH-235).
  - Stage-runner self-check is off by default (values.yaml:1242).
- **Better use**
  - OIDC authn with SA tokens (RULED D14 :42; ROW XC-077).
  - `<idp-id>~<claim>` principals (RULED D5 :40; ROW LH-063).
  - Narrow machine rungs, e.g. `event_reader` (ROW CTL-021).

### 5.10 Dex

- **Current use**
  - The human OIDC IdP (values.yaml:3187).
  - Services verify through `OIDCVerifier` (oidc.py:162), with discovery in-cluster (_helpers.tpl:1298-1307).
  - MinIO's optional WebIdentity provider points at Dex (minio.yaml:148-149).
- **Gap**
  - Machines have no token of their own. The service door stands in for one (LH-220).
- **Better use**
  - Dex stays the human IdP. Machines use k8s SA tokens registered as a second issuer in the same verifier
    (RULED D1 :38; ROW LH-220).
  - The prod IdP is parked (OPEN :80).

### 5.11 Kueue

**What the chart ships today**

- **Subchart.** Kueue 0.18.1, `condition: kueue.enabled` (Chart.yaml:65-68). `kueue.enabled: true` by default
  (values.yaml:2960).
- **Queue topology.** A post-install/post-upgrade hook Job applies one ResourceFlavor `rask`, one ClusterQueue
  `rask` (cpu 18, memory 120Gi, `nvidia.com/gpu` derived from the GPU signal, else "0") and one LocalQueue `rask`
  in the release namespace (kueue-queues.yaml:1-116; values.yaml:2967-2983).
- **Nothing is admitted through it.**
  - No template or service sets `kueue.x-k8s.io/queue-name`. A grep over chart/templates, services and packages
    finds no queue-name label; the only mention is the values comment at values.yaml:2966.
  - `medallion.kueueQueue: ""` (values.yaml:1387) is read by no template or code (grep: only values.yaml:1387).
    The values comment says it is "Inert while the medallion submits through the Ray Jobs API … the RayJob CR path
    this admitted was deleted 2026-09-15" (values.yaml:1383-1386).
  - DECISIONS.md records the deletion: `RayJobExecutor` "submitted a `RayJob` CR … with Kueue admission … had
    zero production callers" (docs/DECISIONS.md:1908-1918).
  - The handover note measured rask's own queue at "admitted 0 workloads" (docs/audits/2026-09-25/kueue-handover-note.md:18-19).
  - The only code mention is a docstring saying the Ray adapter's config may carry "which Kueue queue"
    (engine_registry.py:62-63).
- **Coexistence.** Another team's `htr-batch` Kueue v0.19.0 runs on the same cluster, and its Workloads depend on
  CRDs owned by the `rask` release (kueue-handover-note.md:6-15).

**Where Kueue could help the lakehouse and the bring-your-own compute seam** (all **recommendations**; per-stage
admission is CP-053, blocked on the owner's choice of shape)

| Candidate | What it would take in this code | Fits the decoupling rule? |
|---|---|---|
| Admission control and quotas for stage jobs from any engine | Kueue admits Kubernetes workload objects (Job, RayJob, RayCluster, pods) carrying a queue-name label. The Ray adapter submits through the Jobs API to a standing cluster (workflow.py:478-520), so no per-stage Kubernetes object exists for Kueue to admit; at most the whole RayCluster could be admitted once. Per-stage admission needs the engine adapter to create a Kubernetes object: a RayJob CR (the deleted path) or a batch Job for a non-Ray engine. | **Yes, if it lives in the executor adapter.** Kueue is engine-neutral at the Kubernetes level. The queue name belongs in the per-engine adapter config the registry already reserves (engine_registry.py:62-63), never in `handle_stage` or the catalog. The lakehouse would still see only the port's `RunState` (executor.py:137). |
| Per-project fair share | In Kueue's personas the batch administrator creates ClusterQueues, cohorts and LocalQueues; under D11 that is the cluster's owner, not rask. It would give each project a LocalQueue with admission fair sharing or priority classes, and the Ray adapter would only put the project's queue name on its job. The trigger already carries `project` (ingest_trigger.py:296-297). | **Yes, if the adapter only sets the queue-name label** and rask ships no Kueue object (D11, XC-049). It needs a RayJob adapter first. |
| Gang scheduling of RayJobs | Kueue admits a RayJob's head and workers all-or-nothing. It needs the RayJob CR path and a worker group (CP-043: head-only today). | **Only for the Ray adapter.** Acceptable as adapter-internal. It must not become a lakehouse capability. |
| Queue instead of reject when the cluster is full | Kueue suspends a workload until quota frees. A suspended CR would show as PENDING through the port. | **Yes.** It maps onto the port's existing `RunState.PENDING` (service_kit/lakehouse/executor.py:33), so no lakehouse change is needed; the wait moves with `stage_run` to whatever engine sits behind the port (CP-029). |
| Resource flavors for GPU vs CPU | Separate ResourceFlavors (cpu, gpu) with node labels. Today there is one flavor covering both (kueue-queues.yaml:82-108). This pairs with CP-043's CPU and GPU worker groups. | **Yes.** It is compute-plane only. The sealed runners' GPU needs stay their own (CLAUDE.md "runners/"). |

**The owner's answer (2026-09-30).** Kueue is a cluster service installed once per cluster outside rask's release,
with KubeRay on the compute plane; D11 stands and the lakehouse never references Kueue. The register records
this as the fixed-stack ruling. The record that prompted the question follows.

**The conflict, stated plainly (an owner decision to confirm; not resolved here)**

- **Register ruling D11**, quoted exactly (`open_backlog_left_new2.md:41`):
  > "**D11** — Kueue and htr-batch belong to someone else, so rask ships no Kueue (option b); execution follows
  > the handover note once the owner confirms the other team knows."
- **Row XC-049**, quoted exactly:
  - Title (`open_backlog_left_new2.md:1054`): "XC-049 · rask's release ships Kueue for a lane it does not use, on
    CRDs that another team's htr-batch Workloads depend on".
  - Its Why (:1057): "Criterion 5: the release ceiling blocks every chart-borne Phase-1 fix, and a careless disable
    deletes another team's Workloads. D11 rules that rask ships no Kueue."
  - Its How's step (4) (:1058) reads: "`kueue.enabled=false` and delete the dependency, the values block, templates,
    init container and its test, medallion.kueueQueue and the hook-applied ClusterQueue/LocalQueue/ResourceFlavor."
  - Its closes-when (:1059) begins: "`grep -rni kueue chart/` returns nothing but the handover record".
- **FOCUS item 8** (:32-33): "XC-049, conditional as today … Why: the release object's headroom; Kueue is decoupled
  and LOW (D11)."
- **CLAUDE.md:58-59**: "the models zone and Kueue are decoupled and LOW. A cross-cutting or infra row (chart, CI,
  Kueue) is taken only as the stated enabler of a named Phase 1 row."
- **The owner's new statement** puts Kueue in the fixed stack, which the analysis must use and must not remove.
- **Conflict.** D11 and XC-049 remove Kueue from rask's release, while the new statement keeps Kueue in the stack.
  They are compatible only if "in the stack" means "a cluster service rask uses but does not install", for example
  the htr-batch team's kueue-system controller (kueue-handover-note.md:6-10). They are not compatible if it means
  rask ships and configures Kueue itself. Either reading also changes whether any compute-seam row (CP-044, CP-029,
  CP-043) should name Kueue admission.
- **Owner decision to confirm**
  1. Does the fixed-stack statement supersede D11(b), or does it mean "use the cluster's Kueue, installed by
     another team"?
  2. Accordingly, does XC-049 keep its step (4) removal, or narrow to the CRD keep-annotation and the
     two-controller fix?
  3. Is Kueue admission for stage jobs a Phase 1 or a compute-second concern? CLAUDE.md:58-59 currently says LOW,
     taken only as an enabler.

  The release-ceiling cost XC-049 measured (about 24% of the 1 MiB object; :1052) and the two-controller cert
  fight are facts either answer must handle, for example through D11 option (c), the infra/app split (OPEN :71;
  ROW XC-085).

---

## Not verified

- **Live state.** Pods, images, whether the `bronze`/`silver`/`gold` namespaces exist on a fresh install, NATS
  auth, and whether any signing key resolves in each pod.
- **The owner's fixed-stack statement.** Given on 2026-09-30 and recorded in the register as the fixed-stack and workflow-engine rulings; not in CLAUDE.md.
- **Live Kueue state** (two controllers, htr-batch Workloads). This comes only from the 2026-09-25 handover note and XC-049's evidence; not re-measured.
