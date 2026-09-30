# RESULT — the lakehouse dataflow as the code runs it (map at eb53bfc)

Mapped 2026-09-30 at `eb53bfc`, read-only. Every hop cites a file:line opened at that commit; paths are relative to the
repository root. The code is the source of truth. Design INTENT comes only from what the owner has ruled: CLAUDE.md
"Engineering principles" and "Architecture", the five Phase 1 criteria (`open_backlog_left_new2.md` lines 5-7) and the
register's owner rulings (lines 26-46). Where a doc disagrees with the code, it is listed under "Docs that are wrong or
stale". `open_backlog_left_new2.md` was not edited by this map; its proposed changes are recorded separately.

Criteria: (1) provenance/lineage correct, (2) catalog correct for lance-ns and authz/governance, (3) not coupled to a
workflow engine or Ray, (4) events correct, (5) resilient.

Legend for transport: HTTP = a service door; PUB(topic) = Dapr pub/sub on NATS JetStream; WF = Dapr Workflow;
LANCE = a direct Lance/S3 open; FGA = an OpenFGA check; OL = an OpenLineage event. The chart defaults these topics:
`lineage.events.v1` (chart/values.yaml:1168), `catalog.control.v1` (the template default, chart/templates/configmap.yaml:78; service_kit/control_events.py:30), `medallion.bronze`
(values.yaml:1517), and `dlq.*` per app.

## Headline

- **Three bronze heads, three announcers.** The ingest service commits through the catalog's `/commit` and the catalog
  announces the write. The medallion producer's `/produce` and `/ingest-media` write Lance directly with the static
  `rask-medallion` key and announce their own writes. `/ingest-media` also starts its media chain itself instead of
  letting the arrival event do it.
- **The catalog is not the sole committer of tier writes.** `/commit` is Append only (dataplane.py:887). The in-process
  lane and the Ray lane commit add_columns, full-sync merges, creates, overwrites, merges, deletes, schema-metadata
  updates and index builds straight to Lance. LH-202's
  narrowed writer policy would refuse every one of those commits once LH-218 and LH-129 put the lanes on table-scoped
  vends. No writer-tier door commits a client-written fragment set as anything but an Append (`/compaction_commit`
  commits a compaction Rewrite only under `can_maintain`: data.py:289-290; fga_deps.py:143). At eb53bfc no row owned how
  those commits reach the catalog; the register's LH-330 and its commit-door decision now do (see Register §1).
- **The event lane is forgeable.** NATS authenticates no client, and both cascade heads and the stage runner's
  `/medallion-event` check only the Dapr app token (XC-078, LH-064).
- **The default cascade is Ray plus Dapr Workflow** (`medallion.ray: true`), and the ingest service, a bronze head, is
  in neither import-linter decoupling contract (XC-109, parked at eb53bfc).
- **20 weak points**, of which, at eb53bfc, two have no owning row (8 and the door half of 9) and three are owned only by parked
  findings (7, the Ray half of 16, and the maintenance half of 18).
- **20 rows of doc or comment passages are wrong or stale**, including four CLAUDE.md statements.
- **The FOCUS order fixes no criterion-3 defect, carries no bus authentication and no notifications row, and its
  acceptance proof (XC-090) cannot close before rows outside FOCUS.** This describes the FOCUS order at eb53bfc; the
  owner adopted a new FOCUS order from this audit on 2026-09-30 (see §3).

---

## A. Ingest: there are THREE bronze heads, and they announce the write in three different ways

### A-i. The ingest service (`POST /v1/ingests`, gateway `/api/ingest`)

| # | from → to | transport | identity / credential | cite |
|---|---|---|---|---|
| A1 | client → gateway → ingest | HTTP. The gateway strips `dapr-api-token`, `dapr-app-id`, `dapr-caller-app-id`, `x-lance-service-identity`, `x-user` and `x-forwarded-*`, and forwards over Dapr invoke | Dex bearer. Through the gateway the app-token door is refused as a public-door call (ingest auth.py:171-189) | services/gateway/src/gateway/__init__.py:77-100,235,347 |
| A2 | ingest door | FGA `can_administer` on `project:{requested}` (human), or the app token limited to the configured project. With no service token and OIDC and FGA both off, the door is dev-open (auth.py:166-167) | OIDC sub / app token | services/ingest/src/ingest/auth.py:1-30; api.py:388 |
| A3 | door → `ingest_run` | WF (Dapr Workflow, started in ingest's own lifespan) | n/a | services/ingest/src/ingest/__init__.py:161-165,260-265 |
| A4 | workflow → catalog create (empty table, v2.2, stable row ids) | HTTP `/v1/table/{id}/create` with an empty Arrow body | `dapr-api-token` + `x-lance-service-identity: service-ingest` (the dedicated token, or the shared one) | services/ingest/src/ingest/catalog_service.py:308-332,595-623; service_identity.py:52-71 |
| A5 | chunk units → workers | NATS JetStream, called directly with nats-py (no Dapr); `nats.connect` passes no credentials | none (the chart configures no NATS auth, values.yaml:2744-2800; XC-078) | services/ingest/src/ingest/queue.py:33-35,234,377 |
| A6 | worker → catalog vend → S3 | HTTP `/management/v1/table/{id}/credentials?tier=write`, then LANCE `write_fragments` with the vended STS triple, cached in a VendedCredentialCache | table-scoped STS session. A failed vend refuses by default (`insecure_allow_ambient_storage=False`). The ambient chain is used only when the vend offers none (`server_mediated`), or there is no vending seam or no namespace (CP-007) | catalog_service.py:227-290; config.py:211; runtime.py:836-900; lander.py:292,323-325 |
| A7 | finalize → catalog `/commit` | HTTP. The catalog folds the fragments as a Lance `Append` using its own static key | service identity. FGA `can_write_data` | runtime.py:697-741; catalog_service.py:420-440; services/catalog/src/catalog/api/v1/endpoints/data.py:192-230; services/catalog/src/catalog/services/dataplane.py:887 |
| A8 | catalog → bronze-write OL event | PUB(`lineage.events.v1`) through the catalog's lineage outbox (`LANCE_LINEAGE_OUTBOX_URI`, on by default, values.yaml:491-492). The event is a `lance-catalog` job named `insert.<table>`, COMPLETE, signed by the catalog's identity when a key resolves | catalog service key | data.py:220-229 (emit_measured_write INSERT); services/catalog/src/catalog/main.py:205-229; core/lineage_emit.py:374,774-809; chart/templates/services.yaml:186-202 |
| A9 | ingest's own START/terminal run events | HTTP `POST /api/v1/lineage` via lineage-kit (whose transports are auto, http, console and noop), never on the topic | dedicated `service-ingest` token when the store has it | services/ingest/src/ingest/lineage.py:143-195; packages/lineage-kit/src/lineage_kit/config.py:68; services/lineage/src/lineage/api/v1/endpoints/ingest.py:103 |
| A10 | bronze is not published | `_publish` returns `published=None`. The cascade head fires on A8, not on a publication | n/a | runtime.py:801-835 |

### A-ii. The medallion producer `POST /produce` (gateway `/api/produce`)

| # | from → to | transport | identity | cite |
|---|---|---|---|---|
| P1 | door | FGA `can_administer` on the project (OIDC), or the shared app token (project-blind; the "Shared service token" ruling) | OIDC / app token | services/medallion/src/medallion/api/produce_auth.py:57-70,284-289; api/produce.py:22-35 |
| P2 | producer → catalog `register` (the producer tells the catalog where the table is; it does not ask) | HTTP | `catalog_service_identity` + app/dedicated token | services/medallion/src/medallion/services/produce.py:187-215 |
| P3 | producer → S3 | LANCE `seed_bronze`: a direct write at the chart's `bronze_uri`, or `{root}/medallion/{ns}` composed for a project. No catalog commit door and no vend | `settings.storage_options()` = the static `rask-medallion` key (config.py:486-495; medallion.yaml:385-388; values.yaml:2443) | produce.py:141-153,235 |
| P4 | producer → bronze-write OL event | signed with the producer's dedicated key if one resolves (unsigned otherwise), staged to `_lineage_outbox`, PUB(`lineage.events.v1`) | HMAC from the Dapr secret store | produce.py:240-278; services/medallion/src/medallion/core/lineage_publish.py:47-100; packages/service-kit/src/service_kit/lakehouse/outbox.py:342-390 |

### A-iii. The medallion producer `POST /ingest-media` (no gateway row)

| # | from → to | transport | identity | cite |
|---|---|---|---|---|
| M1 | producer → catalog `ensure_stage_output`: `describe`, else `create?mode=exist_ok`. It returns the location the bytes then land at; it never calls the register door | HTTP | service identity | services/medallion/src/medallion/services/media_produce.py:167-193; catalog_register.py:284-357 |
| M2 | producer → S3 source + bronze | LANCE plus pyarrow S3 with `allow_bucket_creation=True`. It optionally seeds demo PNGs into the source bucket, then `ingest_to_bronze` | static `rask-medallion` key | media_produce.py:63-105 |
| M3 | producer → OL event | signed, outbox, PUB(lineage topic) | as P4 | media_produce.py:238-250 |
| M4 | producer → **media-chain trigger, published DIRECTLY** | PUB(`settings.media_topic`), a bare publish outside the outbox. The code calls this deliberate: the outbox re-ingests lineage and never re-fires triggers (:238-243) | n/a | media_produce.py:251-283 |

No gateway route exists for `/ingest-media` (the route table at gateway `__init__.py:223-272` has no such row), so the door
is reachable only in-cluster.

### A-lineage. How the lineage service ingests (for every head)

| # | hop | transport | cite |
|---|---|---|---|
| L1 | topic → lineage `/lineage-events` | PUB subscription guarded by `require_dapr_token`. `enforce_signature_if_present` admits an UNSIGNED event (`if found is None: return`) and refuses one whose signature does not verify; `_StampedAuthor` then bounds the stamp by FGA on the outputs | services/lineage/src/lineage/api/dapr.py:33,173-191; services/lineage/src/lineage/api/fga_deps.py:293-339 |
| L2 | HTTP `POST /api/v1/lineage` (ingest, the Ray train job, external producers) | HTTP service door | endpoints/ingest.py:103 |
| L3 | MERGE into AGE (Run on run_id, Dataset on name) | Postgres/AGE | services/lineage/src/lineage/services/cypher.py:26-35,75-76 |
| L4 | outbox relay: the reconcile cron drains `_lineage_outbox` and re-ingests and re-publishes survivors | Dapr cron binding `@every 300s` | chart/values.yaml:478-492; endpoints/dlq.py:1-13,107-116; services/staged.py:38-53 |

---

## B. The cascade

| # | from → to | transport | identity / credential | cite |
|---|---|---|---|---|
| B1 | lineage topic → producer `/bronze-arrival` | PUB subscription with DLQ. `require_dapr_token` only; **no signature check** (no `verify` or `signature` in bronze_arrival.py, ingest_trigger.py or publication_trigger.py) | sidecar app token | services/medallion/src/medallion/api/bronze_arrival.py:38-62; services/medallion/src/medallion/services/ingest_trigger.py:265-321 |
| B2 | head → catalog `describe` (the upstream location, I2) | HTTP. An outage (`RegisterError`) is swallowed and the trigger still fires without `from_uri` | service identity | ingest_trigger.py:220-262 |
| B3 | head → PUB(`medallion.bronze`) trigger `{token, cascade_id, dataset, namespace, project?, from_uri?, originator?}`. The head matches the configured bronze dataset or any lane-declared table | PUB to `settings.bronze_topic` | n/a | ingest_trigger.py:120-150,290-316 |
| B4 | stage runner `/medallion-event` → `handle_stage` | PUB subscription (`sub_topic`) with DLQ and `require_dapr_token` only, so a trigger carrying the app token is accepted without passing either head (bounded by `_confine_from_uri` and B5) | app token | services/medallion/src/medallion/api/events.py:45-65; services/medallion/src/medallion/services/transform.py:1745 |
| B5 | stage runner authz as its own identity | FGA `fga_required_action` (for example `can_create_table`) on the `to` namespace, when FGA is on | `service-<lane>` | transform.py:431-491 |
| B6 | resolve roots | The chart's `MEDALLION_FROM_URI`/`TO_URI`, or `{root}/medallion/{ns}` composed for a project; THEN the stage runner's own catalog `describe` of the upstream overrides the read side; THEN the trigger's `from_uri` wins if it lies inside `read_root`. The composed read path survives only with no catalog URL, or when describe answers any 4xx, which includes a 403 denial. Control reads use the static key | static key (control reads) | transform.py:684-728,777-795; catalog_register.py:397-418 |
| B7 | stage runner → catalog `ensure_stage_output` (`describe`, else `create?mode=exist_ok`; it returns the destination location, which replaces `to_uri`) and then `authorize_stage_write` (a write-tier vend) | HTTP. **The vended credential is discarded.** The chart's vending default is `sts` (values.yaml:1031), so a real STS credential is minted and thrown away | service identity; FGA `can_write_data` or `can_maintain` at the vend door | transform.py:1097-1126; services/medallion/src/medallion/services/catalog_register.py:221-263,284-357; services/catalog/src/catalog/api/v1/endpoints/credentials.py:78-112 |
| B8 | START run OL event | signed, outbox, PUB | as P4 | transform.py:1158-1168,298-358 |
| B9 | engine choice: the declared TransformSpec's task registration, else `MEDALLION_RAY_ENABLED` (code default `False`, chart default `true`) | code | n/a | services/medallion/src/medallion/services/engine_choice.py:1-60; transform.py:927; core/config.py:316; chart/values.yaml:1365; chart/templates/medallion.yaml:137-144,609-616 |
| B10a | **in-process**: `executor_for(IN_PROCESS_ENGINE, storage_options=settings.storage_options)`. It reads upstream and writes the destination directly: `add_columns` when the rows line up, otherwise a full-sync `merge_insert` (`when_matched_update_all`, `when_not_matched_insert_all`, `when_not_matched_by_source_delete`), or `create` on the first write. It never overwrites | LANCE, static `rask-medallion` key, no catalog commit door | transform.py:882,980-983,1087; services/medallion/src/medallion/services/compute.py:363-398 |
| B10b | **Ray**: dispatch `stage_run` (WF, deterministic instance id). `submit_stage` goes to the Ray Jobs API (httpx) and polls through the executor port on a durable timer with `continue_as_new` | WF plus HTTP to `MEDALLION_RAY_ADDRESS` (code default `http://ray-lance-head:8265`, chart `<fullname>-ray-head-svc:8265`) | none to Ray (the dashboard is unauthenticated, CP-010) | transform.py:945-968; services/medallion/src/medallion/workflow.py:252-356,478-520,575-594; services/medallion/src/medallion/services/stage_submit.py:155; core/config.py:317; chart/templates/medallion.yaml:145 |
| B10c | Ray job writes the tier | LANCE `write_dataset`/`merge_insert`/`delete`, committed directly by Lance and not through the catalog | `S3_KEY`/`S3_SECRET` from the Ray pod's environment: the static `rask-ray-compute` user (_ray-cluster-config.tpl:173-181; values.yaml:2389; infra-credentials.yaml:56-57) | scripts/ray_stage_job.py:73-89,288-300,570,715,839 |
| B10d | WF `publish_stage_ready` → re-publish the trigger with `ray_job_done=True` on the runner's own `sub_topic` → pass 2 measures | PUB | n/a | workflow.py:599-651; transform.py:970-979 |
| B11 | quality measurement (`assert_quality`) on the destination | LANCE, static key | n/a | transform.py:1193-1200 |
| B12 | COMPLETE run OL event | signed, outbox, PUB | as P4 | transform.py:1279-1281 |
| B13 | gate: `gate_decision` → PUBLISH → catalog `/publish`. The catalog runs the assertions and moves the `published` tag | HTTP | service identity | transform.py:1428-1549; services/catalog/src/catalog/api/v1/endpoints/publication.py:251,346 |
| B14 | HOLD → PUB(`promotion_topic`) → producer `/promotion-held` → `promotion_review` WF → a `can_promote` holder answers at `/api/promotions/*` | PUB plus WF plus HTTP | OIDC | services/medallion/src/medallion/services/promotion_hold.py:92; api/promotions.py:282,345-353; workflow.py:1145; producer.py:120-141,187,204 |
| B15 | catalog → control event `table_published` | PUB(`catalog.control.v1`) through the control outbox (on by default, values.yaml:1130,1150-1151); **the actor is unsigned** (`service_kit/control_emit.py` carries no signing code) | catalog | main.py:239-250; publication.py:341-346; chart/templates/services.yaml:211,233-244 |
| B16 | producer `/publication-arrival` → next tier's topic (looked up in `transform_routes`) → next stage runner (B4) | PUB, `require_dapr_token` only, **no signature check** | app token | bronze_arrival.py:94-112; services/medallion/src/medallion/services/publication_trigger.py:169-211 |

Media lane: M4's direct trigger → the media stage runner (B4 onwards). It does not go through B1.

---

## C. Governance

| # | hop | transport / credential | cite |
|---|---|---|---|
| C1 | browser → zone BFF (Dex OIDC session) → gateway `/api/*` | HTTP, Dex bearer. The BFF sends the service header pair only when there is no session AND the call is a read (FE-002) | packages/service-kit/src/service_kit/governed/dapr_auth.py:345-348; frontend/packages/api/src/bff.ts:195-198 |
| C2 | gateway strips the spoofable headers, re-stamps X-Forwarded-*, and forwards through Dapr invoke | HTTP | gateway `__init__.py`:77-121,340-349 |
| C3 | catalog `authenticate`: OIDC verify (Dex JWKS), or the service door = a token plus an asserted `x-lance-service-identity` → a synthetic IDToken (iss `rask://service-door`, 60 s). Privileged subjects must present a dedicated token (`auth.dedicatedServiceCredentials: true`); the rest may use the shared one | code | services/catalog/src/catalog/api/security.py:50-160; dapr_auth.py:457-512 (dedicated check :502-509); values.yaml:939 |
| C4 | catalog router guard: `authorize` on every route (FGA check on `token.sub`, the raw Dex sub; LH-063) | FGA | services/catalog/src/catalog/api/v1/router.py:48; services/catalog/src/catalog/api/fga_deps.py:808-814 |
| C5 | vend: `can_write_data` or `can_maintain` for write. Classified columns and unreachable bases route to `server_mediated`. `StsVendor.vend` = AssumeRole plus an inline session policy (TTL 900 s) | HTTP → MinIO STS, **AssumeRole signed with the catalog's static key**: `LANCE_S3_ACCESS_KEY_ID`, rendered as `rask-catalog` | credentials.py:78-112,139-157; services/catalog/src/catalog/core/vending.py:423,589-690; catalog main.py:157-158; chart/templates/services.yaml:130-142; values.yaml:2435 (values.yaml:1031-1033 sets only mode, ttl and roleArn) |
| C6 | client → S3 directly (ingest workers, stock Lance clients) | LANCE with the vended triple. A main-branch write vend grants Get/Put/Delete/AbortMultipartUpload on `<bucket>/<prefix>/*` (LH-202). A branch write vend grants main READ only, and write on `<prefix>/tree/<branch>/*` | vending.py:129-134,499-526 |
| C7 | a client-direct commit goes back through `/commit` (Append only). The INSERT emit passes no `pin_version` and re-reads latest | HTTP | data.py:192-230; dataplane.py:875-887 |

---

## D. Events

| # | lane | producer → consumer(s) | cite |
|---|---|---|---|
| D1 | `lineage.events.v1` | catalog (INSERT/DDL markers), medallion producer, stage runners, maintenance (sweep lineage) → lineage `/lineage-events`, medallion `/bronze-arrival`, maintenance `/maintenance-arrival`, notifications `/lineage-events` | lineage dapr.py:176-191; bronze_arrival.py:38-43; services/maintenance/src/maintenance/api/arrival.py:82-85; services/notifications/src/notifications/api/subscriptions.py:62-65; chart/templates/dapr-component.yaml:24 |
| D2 | the lineage HTTP door | ingest, the Ray train job (tokens from pod env), external producers → lineage only. **Never on the bus**, so the medallion head, maintenance and the notifications subscription never see these events | ingest lineage.py:143-195; scripts/ray_train_job.py:46,168-182; values.yaml:1575-1577 |
| D3 | `catalog.control.v1` | catalog (staged through the control outbox and relayed), maintenance reclamations → producer `/publication-arrival`, notifications `/control-events`, the catalog's own per-replica ring buffer (an ephemeral consumer, LH-305 parked) | catalog main.py:229-250; subscriptions.py:109-112; bronze_arrival.py:99; maintenance purge.py:750; values.yaml:1120-1150,2061 |
| D4 | DLQ | lineage `/lineage-dlq` re-presents each park once, at park time (LH-148). Medallion and notifications `/dlq-event` park, ERROR-log and ack. The lineage outbox replay door exists only for STAGED events | lineage dapr.py:117-161,186-191; producer.py:84-87; services/notifications/src/notifications/api/dlq.py:35-60; lineage endpoints/dlq.py:97 |
| D5 | notifications reconciler | cron binding → `GET /events` on lineage (the per-dataset-governed feed) under an asserted service identity (CTL-021) | services/notifications/src/notifications/api/reconcile_cron.py:85-150; api/service_identity.py:14,60-70 |
| D6 | outboxes | `_lineage_outbox` (catalog, medallion, maintenance, ingest's recovery hook) is drained by the lineage reconcile cron (`_drain_outbox`, `republish_staged`). The control outbox is drained by the catalog's control relay | outbox.py:1-22,277-390; chart/templates/services.yaml:190-202; medallion.yaml:92,500; maintenance.yaml:219; fleet.yaml:212; lineage reconcile_cron.py:499; staged.py:38; catalog api/control_relay.py |

---

## E. Maintenance

| # | hop | transport / credential | cite |
|---|---|---|---|
| E1 | cron tick (`@every 120s`) plans the whole estate and enqueues units on the work topic (`maintenance.work.v1`, on by default); fresh writes also arrive via `/maintenance-arrival` (lineage topic) | PUB | services/maintenance/src/maintenance/api/routes.py:79-113; arrival.py:61-100; values.yaml:1570-1582,1630; dapr-component.yaml:220 |
| E2 | `/maintenance-work` executes a unit (only when `execute_work`; dedicated workers on) | PUB subscription | services/maintenance/src/maintenance/api/work.py:144-151 |
| E3 | per-table vend (write tier, `can_maintain`). **It falls back to the static `rask-maintenance` key** when there is no catalog URL, no derivable table id, or a vend that answers nothing (`record_credential_tier("ambient")`) | HTTP → STS, else static | services/maintenance/src/maintenance/services/credentials.py:86-150 |
| E4 | compaction plan and commit through the catalog doors (`compaction_plan`, `compaction_commit`; an unavailable plan allows the in-pod fallback); cleanup_old_versions and index work run in the pod | HTTP plus LANCE | catalog data.py:235,289; services/maintenance/src/maintenance/services/catalog_compaction.py:1-22; optimize.py:639 |
| E5 | reconcile/drift report → gated trash purge: FGA tuple revoke first, then `delete_location`. **The purge signs with `settings.storage_options()`**, the static maintenance key | LANCE/S3 plus FGA | routes.py:169-335; services/maintenance/src/maintenance/services/purge.py:442-486,712,765-811; maintenance core/config.py:569-578; values.yaml:2417 |
| E6 | counters and alerts: `record_credential_tier{ambient}`, `compaction.datasets.mixed_file_versions`, and the rules `MaintenanceRefusalsRising`, `LanceMixedDataFileVersions`, `MaintenanceTableParked`, `CompactionSigningWithAmbientCredential` | OTLP → GreptimeDB → vmalert | credentials.py:137-150; sweep.py:889; chart/alerting/rules.yml:693,710,743,771 |

---

## F. Where compute, the workflow engine and the explorer/annotator touch the lakehouse

| # | edge | cite |
|---|---|---|
| F1 | Two static contracts. `the-lakehouse-is-not-built-on-ray` forbids `ray` and `ray_kit` in catalog, lineage, medallion, maintenance and notifications. `the-lakehouse-is-not-built-on-a-workflow-engine` forbids `dapr.ext.workflow` and `durabletask` in catalog, lineage, medallion, maintenance and service_kit, with six ignore lines naming five medallion modules (workflow, api.promotions twice, producer, services.dapr_saga, stage_runner). **`ingest` and `controlplane` are in neither contract** (XC-109, parked at eb53bfc) | .importlinter:51-84 |
| F2 | Medallion producer: it starts a WF runtime when `quality_review_enabled or ray_enabled` (chart: `ray: true`, `qualityReview: false`), hosting `promotion_review` and `train_run` | producer.py:109-141; values.yaml:1365,1436 |
| F3 | Stage runner: it starts a WF runtime and a raw `DaprWorkflowClient` only when `ray_enabled`. `stage_run` is the only way a Ray stage learns its outcome | stage_runner.py:83-114; workflow.py:252 |
| F4 | Medallion defaults to Ray: `ray_address` default `ray-lance-head:8265` (CP-042), `ray_enabled` code default False and chart default true, `ray_entrypoint` default script (CP-031) | core/config.py:316-318; values.yaml:1365; medallion.yaml:137-145 |
| F5 | `/train` in the lakehouse producer resolves versions and PUBLISHES a train trigger to a dedicated topic. The trainer consumer `handle_train_trigger`, in the same producer app, FGA-gates as the trainer identity and then calls `ray_submit.submit_train_job` directly, bypassing the executor port; `train_run` is scheduled through `DaprSagaClient` | services/medallion/src/medallion/services/train.py:1-12,27,337,387-399 |
| F6 | Ingest: `dapr.ext.workflow` (ingest_run, cancel/pause/resume; module scope in workflow.py) plus a raw nats-py JetStream client | ingest `__init__.py`:161-165,260,325-370; ingest workflow.py:56,70; queue.py:33-35 |
| F7 | Operator routes drive a Dapr workflow client: the stage runner's `stage_ops` and the producer's `api/train.py` use `app.state.workflow_client`, and `api/promotions.py` builds `wf.DaprWorkflowClient()`. The producer's `stage_runner_ops` holds no workflow client: it FGA-gates and forwards over httpx to the stage runner's `stage_ops` (LH-226) | services/medallion/src/medallion/api/stage_ops.py:63-131; api/train.py:185,233,259; api/promotions.py:141; api/stage_runner_ops.py:85-149 |
| F8 | The viewer and search open Lance directly with the deployment key (`state.settings.storage_options()`), bypassing the catalog | services/viewer/src/viewer/api/v1/endpoints/pages.py:237; topics.py:59; graph.py:309; services/search/src/search/services/result_cache.py:78; packages/service-kit/src/service_kit/media/state.py:108 (LOW-027) |
| F9 | The annotator publishes labels THROUGH the catalog create door, but saves annotation versions through `service_kit.lancekit.registry` directly | services/annotator/src/annotator/projects/lakehouse.py:202; annotations/save.py:29; annotations/commit.py:19 (LOW-003) |
| F10 | Maintenance reconcile and repair know annotator objects (`OrphanedAnnotationTask`) | services/maintenance/src/maintenance/services/reconcile.py:279,352; repair.py:189,218 (LH-325, parked) |
| F11 | compute (`/api/ray`, `/api/serve`) is not called by any lakehouse service. The medallion reaches the Ray dashboard itself with a second Jobs API client (CP-022) | services/medallion/src/medallion/services/ray_jobs_api.py; stage_submit.py |

---

## Weak points (prioritized)

"Owner row" names the register row at eb53bfc; "(proposed)" marks a row this audit proposes. The owner admitted LH-329,
LH-330 and XC-109 as counted rows on 2026-09-30.

| # | Hop | What is wrong | Why (criterion) | Change | Owner row |
|---|---|---|---|---|---|
| 1 | B1, B4, B16, A5, D1, D3 | Any pod can publish to NATS, and the cascade heads accept UNSIGNED lineage and control events. A forged `lance-catalog` `insert.<table>` or `table_published` starts a cascade, and the stage runner's `/medallion-event` accepts a forged `medallion.bronze` trigger with only the app token, skipping both heads (bounded by `_confine_from_uri` and the stage runner's own FGA check) | 4, 1, 2 | NATS JWT per app-id; heads call the lineage-kit verifier and require a signature; sign CatalogControlEvent | XC-078, LH-064 |
| 2 | C3, A4, P2, B7 | The service door is a token plus an asserted name header: the shared app token for non-privileged subjects, a dedicated token from the secret store for privileged ones (services.yaml:376-399). The catalog reads peers' dedicated tokens (dapr_auth.py:504), which double as lineage signing keys, as its own does (catalog core/lineage_emit.py:780-789) | 1, 2 | D1: projected SA tokens, delete `x-lance-service-identity` | LH-220 (+XC-076) |
| 3 | B7, B10a, P3, M2, B11 | The medallion signs every in-process and head byte with the static `rask-medallion` key, discarding the STS vend it just obtained (catalog_register.py:221-263). `/produce` and `/ingest-media` never ask for a vend at all | 2 | Hold a per-table vend cache. A `server_mediated` answer (a table with no location, a classified column, a base the session policy cannot address, or no credential minted; credentials.py:111-112,139-157,176) means that table's writes go through whatever the commit-door decision provides (LH-330); a refused vend fails the stage with its reason | LH-218 |
| 4 | B10c | The Ray lane writes governed tiers with `S3_KEY` from the pod environment and commits directly, not through the catalog | 2, 3 | Jobs vend or open through the namespace with a projected SA token | LH-129, CP-029 |
| 5 | C6, C7 | A main-branch writer vend grants Put/Delete over the whole table prefix (it can move tags, forge branches or restore around doors), and `/commit` trusts client fragment metadata (it verifies file existence, file versions and base ids, not row counts) and caller-owned run markers | 2 | Narrow to `data/*` and commit through doors. **This breaks B10a/B10c's client-direct commits until the commit-door decision (LH-330) gives them a commit path; see Register §1** | LH-202, LH-211, LH-280; LH-330 (proposed) |
| 6 | B9, B10b, B14, F2, F3, F7 | The default cascade is Ray plus Dapr Workflow. Outcomes are known only through `stage_run`/`train_run`, HOLD needs `promotion_review`, and the operator routes drive a Dapr workflow client | 3 | An outcome door plus a plan document; SagaClient gains state, terminate and signal; declare lanes | CP-029, LH-226, CP-044, CP-031; proof XC-093 |
| 7 | F1, F6, A3, A5 | Ingest (a bronze head) imports `dapr.ext.workflow` and raw NATS, and sits outside both decoupling contracts, so XC-093 can pass while ingest stays engine-built | 3 | Add ingest (and controlplane) to .importlinter, then port ingest_run to the saga port | XC-109 (**parked at eb53bfc; promotion proposed**) |
| 8 | M4 | `/ingest-media` fires its media-chain trigger itself, a bare publish outside the outbox, instead of letting the arrival event drive the cascade. CLAUDE.md's literal sentence ("Neither publishes `medallion.bronze` directly") is not broken, because the media head publishes the media topic; its intent ("driven by the ARRIVAL event rather than by the ingest call") is. A trigger lost after a landed emit is not recovered by the outbox relay, which never re-fires triggers; only the caller's retry with its idempotency token recovers it (media_produce.py:242-243,279-283) | 4, 1 | Drop the direct publish. `/bronze-arrival` cannot see the media write today: the configured branch expects `bronze_namespace` while the media event names `bronze-media` (core/config.py:607-608; ingest_trigger.py:117-125), the declared-lane branch needs a project the media emit does not stamp (:171-172), the trigger's namespace is always `bronze_namespace` (:294), and it publishes only to `settings.bronze_topic` (:312). The requirements are LH-329's: match the media write but not the catalog's `create_table` event for the empty table (ingest_trigger.py:62), name the matched namespace, and publish to its stage runner's topic (`transform_routes`, publication_trigger.py:189-197) | LH-329 (proposed) |
| 9 | P3/P4 vs A7/A8, B10a, B10c | Three heads, three announcers. The catalog announces ingest writes (catalog authority, after `/commit`), while the producer self-announces `/produce` and `/ingest-media` writes after a direct Lance write the catalog never commits. Neither stage lane commits through a catalog door either, and `/commit` is Append only | 1, 2 | Route producer bronze writes through catalog doors, so the catalog is the only announcer. Neither head's write is an Append (a full-sync `merge_insert` on a re-seed, compute.py:242-252; an overwrite, services/medallion/src/medallion/services/ingest.py:199-208), and `/produce` asking for its location is D6. The commit-door decision (LH-330) chooses how the non-Append commits reach the catalog: (a) the existing server-side data doors, (b) a new client-direct non-Append commit door, or (c) the maintainer tier for stage identities. What to measure before choosing is in the register's LH-330 | partly LH-218, LH-164; LH-330 (proposed) |
| 10 | B6, P3 | `/produce` writes bronze at the chart's URI, or at `{root}/medallion/{ns}` for a project, and registers that location with the catalog instead of asking for one. The stage runner asks the catalog on both sides, but falls back to the composed `{root}/medallion/{ns}` when there is no catalog URL or describe answers any 4xx, and a 403 denial counts as 4xx | 2 | Under D6 (open): create through the catalog and write at the returned location; a stage whose upstream the catalog will not describe fails with the reason | LH-164 (D6 open), LH-194 |
| 11 | A8, B8, B12 | Write events can name a version or ref the write did not commit (the INSERT emit passes no `pin_version`), branch writes are recorded as main on some doors, and a crash between commit and stage loses the author | 1 | Pin the response version; commit markers in transaction properties | LH-214, LH-225 |
| 12 | D2, D5 | The bus is incomplete (ingest, train and external producers emit HTTP-only), and the one lane that closes the gap (the notifications reconciler) was dead on the live estate at the last read-back and reads a tenant-blind feed (CTL-021); the fixed image is pinned but not read back, and HEAD carries the fix (service_identity.py:60-70) | 4 | CTL-021's narrow feed rung plus the SA token | CTL-021 |
| 13 | D4 | A refused lineage park is never re-presented. The medallion and notifications DLQs are park-and-log only | 4, 5 | An on-demand re-drive door | LH-148 (LOW) |
| 14 | E3, E5 | Maintenance rewrites fall back to static or ambient keys, and the purge deletes with the estate-wide static maintenance key | 2 | Remove the fallback arms; a workload vend for the purge | LH-219, XC-084 |
| 15 | C5 | The vendor's parent credential is the catalog's static key (`rask-catalog`), and the catalog's own IO uses it (main.py:217,249) | 2 | A workload identity for the vendor | XC-083 |
| 16 | B10a/B10c | The in-process full-sync merge rewrites every matched row (`when_matched_update_all`), the Ray media lane retracts by run id, and the Ray lane implements merge and delta separately from the in-process engine | 1, 5 | Conditional merges; one write semantics per lane | LH-212, LH-213, LH-216; Ray half LH-326 (**parked**) |
| 17 | E1 | The sweep re-plans the whole estate every 120 s even with the event lane on | 5 | Hourly wall-clock backstop | LH-195 |
| 18 | F8, F9, F10 | The explorer trio and annotator saves read and write lakehouse storage with deployment keys, bypassing the catalog, and maintenance knows annotator objects | 2 (and CLAUDE.md's no-modality-in-shared-seams rule) | Open through the catalog | LOW-027, LOW-003; LH-325 (**parked**) |
| 19 | F5 | Training lives in the Phase 1 lakehouse producer: `/train` publishes a train trigger, and its consumer in the same producer submits the Ray job directly, bypassing the executor port. Compute is "second" and the models zone is LOW | 3 | Move the train head out with CP-029's outcome door, or record that it stays | CP-044, CP-029 (they keep it in medallion); decision proposed |
| 20 | A6 | Ingest signs with the ambient chain when the vend offers none (`server_mediated`), or there is no vending seam or namespace. A failed vend already refuses by default | 2 | Refuse, as LH-219 proposes | CP-007 |

---

## Register vs architecture

### 1. Rows that conflict with the design or with each other

This is the register at eb53bfc. The current register places CP-022's client outside ray-kit, counts XC-109 (admitted
2026-09-30), and gives XC-093 an ingest leg.

- **CP-022** (LOW) builds "one httpx Jobs API client in ray-kit" that "compute and the medallion share". `ray_kit` is a
  forbidden import for `medallion` (.importlinter:51-63, criterion 3). As written, the row adds the coupling the
  contract bans. The client belongs in the medallion's Ray adapter, or in a Ray-free package both may import.
- **LH-202 vs LH-218 / LH-129 / CP-029.** LH-202 narrows a writer vend to Put on `data/*`, with no `_versions/` or
  `_transactions/` and no Delete, and its How routes commits through /commit (Append) and the server-side doors. The
  stage lanes commit add_columns, full-sync merge_insert and create (compute.py:363-398), and write_dataset
  (creates at :288, :804 and :864, an overwrite with an empty table when the source is empty at :313, and a staging
  dataset at :839), merge_insert and delete (ray_stage_job.py:288-313,570,667,715,800-868), directly. Once LH-218 and LH-129 move
  those lanes onto table-scoped vends, LH-202's policy refuses every such client-direct commit. No writer-tier door
  commits a client-written fragment set as anything but an Append (`/commit`, dataplane.py:887; `/compaction_commit`
  commits a compaction Rewrite only under `can_maintain`, data.py:289-290). At eb53bfc no row moved the lanes onto the server-side doors or added a client-direct non-Append
  door, and none ordered these rows; on 2026-09-30 the owner admitted LH-330, and the adopted FOCUS orders LH-218 and
  LH-129 after its decision. The decision's options are (a) the existing server-side data doors, (b) a new client-direct
  non-Append commit door, and (c) the maintainer tier for stage identities; what to measure first is in LH-330.
- **CP-044 (2)** routes the train submit through the executor port but keeps training in the lakehouse producer. That
  is consistent with criterion 3's letter, but in tension with "compute comes second / models zone LOW" (CLAUDE.md).
  Not a hard conflict; flagged for the owner.
- **XC-093's closes-when** requires a no-engine kind-lane variant under `-m phase1` and an engine-outage drill on the
  deployed release, not only static checks. Neither covers ingest: the variant turns the medallion's Ray off, the drill
  lists catalog writes, lineage ingest, the reconcile tick and the maintenance sweep, and the static contracts exclude
  ingest (XC-109 is parked). The criterion-3 proof can close while a bronze head is still built on Dapr Workflow and
  raw NATS.
- **LH-010** (Phase 2) deletes `packages/storage` build_source/build_sink for one runner. It is consistent with the
  sealed-runner rule (it removes the shared seam's only caller). No conflict.

### 2. Design goals no counted row serves

This is the register at eb53bfc. On 2026-09-30 the owner admitted LH-329, LH-330 and XC-109 as counted rows, and the
register's XC-043 now owns the passages below, except those it hands to LH-195, LH-218 and LH-329.

- An arrival-driven cascade for the media head (WP 8): **none**.
- The catalog as the single committer and announcer of tier writes, from `/produce`, `/ingest-media` and both stage
  lanes (WP 9): only partly served (LH-218 credential, LH-164 location), and how the heads' and the stage lanes'
  non-Append writes reach the catalog is owned by nobody.
- Criterion 3 for ingest and controlplane: only XC-109, **parked**.
- Criterion 4 for the Ray lane's tier-write semantics: LH-326, **parked**.
- CLAUDE.md "Architecture" says the producer's three doors are "the whole INGEST surface", while `services/ingest`
  serves `POST /v1/ingests` (api.py:388; gateway `__init__.py`:235). No row reconciles CLAUDE.md or decides whether
  ingest is a Phase 1 component; CLAUDE.md's Phase 1 list does not name it.
- The **controlplane** service is named a Phase 1 component, and one counted row touches `services/controlplane`:
  LH-076, which moves its project listing onto `can_list_all_projects` (controlplane security.py:42,52). The CTL rows
  are gateway and notifications. Whether anything else there is wrong was not examined.
- The stale docs (below): XC-043 (LOW) covers system-overview.md and microservices.md, and its closes-when grep also
  catches the `/ingest-iiif` lines at data-flow.md:6 and data-model.md:6, and, among others, the `orchestrator` and
  `volumes-api` lines at data-flow.md:82-104 and data-model.md:122,132. It does not cover data-flow.md's HTR content (:21-90, beyond the orchestrator lines :82 and :87),
  medallion-data-flow.md §3, medallion-cascade.md §12, the four CLAUDE.md statements, or the chart and code comments.

### 3. The FOCUS order (register lines 10-24) against the criteria

This judges the FOCUS order at eb53bfc. On 2026-09-30 the owner adopted a new order from this audit's findings: it pairs
XC-078 with LH-064 (already in FOCUS), adds CP-029 with LH-226, LH-218 with LH-129 and CTL-021, and closes XC-090 last.

- It covers criterion 2 (LH-280, LH-281, LH-220), criterion 1 (LH-280, LH-064) and criterion 4 partly (LH-064 is the
  lineage lane only). XC-090 is the composition proof.
- **No FOCUS item fixes a criterion-3 defect.** XC-090 composes XC-093, the criterion-3 proof, but CP-029 (HIGH),
  LH-226, CP-044 and CP-031 are all outside FOCUS.
- **The bus-authentication half of criterion 4 is absent**: XC-078 is HIGH, and its How puts it "after XC-049
  (headroom) and LH-064".
- **Notifications is absent**, although the FOCUS scope sentence names it: CTL-021 is HIGH and was dead at its last
  live read-back.
- LH-218 and LH-129 (HIGH, the criterion-2 byte path) are absent, while LH-220 is in (LH-129 builds on D1, which LH-220 implements; LH-218's row names neither).
- XC-090 is item 3, but XC-091 depends on LH-214, LH-064, LH-225, CP-037 and the parked LH-282, and XC-094 on LH-064,
  XC-078, LH-148, CP-037 and CTL-021. The proof can be built early as a RED harness, which the principles endorse, but
  cannot close before rows that are not in FOCUS.
- XC-049 is correctly conditional (only as a release-space enabler), consistent with the Kueue ruling (D11).

---

## Docs that are wrong or stale (doc:line vs code)

| Doc:line | Claim | Code |
|---|---|---|
| docs/architecture/system-overview.md:40-61 | "a distributed image-to-ALTO-XML pipeline for the Swedish National Archives", batches DB, core-api/orchestrator | CLAUDE.md Architecture forbids exactly this description. The services tree has no core_api, orchestrator or batches (gateway `__init__.py`:223-272) |
| system-overview.md:424-426; microservices.md:201 | "No event bus", "No auth" | bronze_arrival.py:38-43 (Dapr pub/sub); catalog security.py:50-160; ingest auth.py:1-30 |
| microservices.md:205-242 | "Decision: do not adopt Dapr" | stage_runner.py:91-97; ingest `__init__.py`:161; workflow.py:252 |
| system-overview.md:6; data-flow.md:6; microservices.md:17; data-model.md:6 | Ingestion is `POST /ingest-iiif` | No such route: producer.py:176-191; the comment at gateway `__init__.py`:229-235 says the row is gone. (docs/architecture/live-proof-2026-07-28.md:22,172 carries it too, as a dated record) |
| data-flow.md:21-111 | The whole page is the HTR Ray pipeline plus batches; only the header note (:6-9) mentions the medallion | No medallion content; see sections A and B |
| medallion-data-flow.md:83-91 | Write path: lander "THE one Lance writer", catalog "create + register" | Deployed ingest commits through catalog `/commit` and bypasses the lander (runtime.py:723-744). `/produce` and `/ingest-media` write Lance directly (produce.py:235; media_produce.py:96-104). The lander's own docstring (lander.py:1-24) is also stale |
| medallion-data-flow.md:98-104; CLAUDE.md Architecture ("driven by the ARRIVAL event rather than by the ingest call") | Every head goes bronze write → event → `/bronze-arrival` | The media head publishes its trigger directly (media_produce.py:251-283) |
| medallion-cascade.md:142-153 | "medallion exposes no workflow management endpoints" | stage_ops.py:118; stage_runner_ops.py:85-149; rerun.py:214; api/train.py:259; promotions.py:282,313 |
| medallion-cascade.md:30-35,102,112-113 | Cites ingest_trigger.py:112, publication_trigger.py:103, workflow.py:131-224, :421-426 | `_cascade_token` is ingest_trigger.py:181; publication_trigger.py:103 is a docstring line (`build_stage_trigger` is :110); `stage_run` is workflow.py:252; `_is_terminal` is workflow.py:848 |
| medallion-data-flow.md:264-268 | ray-cluster "builds packages/ratch" | .docker/ray-cluster.dockerfile:45-73 builds `ray-cluster-env`; ratch is dissolved |
| ingest-and-tier-movement.md:59-66 | Manual push = a human `writer` on `namespace:<proj>-bronze` | Both human push doors check `can_administer` on the project: produce_auth.py:57-70; ingest auth.py:19-22 |
| CLAUDE.md Architecture, "services/medallion" | The three producer doors "are the whole INGEST surface" | services/ingest api.py:388 `POST /ingests`, gateway `/api/ingest` (`__init__.py`:235) |
| CLAUDE.md Architecture, "The orchestrator is gone" | "ONE bronze-write OpenLineage event through packages/lineage-kit" | For ingest the announcer is the catalog's INSERT event (data.py:220-229; ingest lineage.py:154-158); ingest's own events go HTTP only |
| CLAUDE.md Repository layout, `services/` | Lists gateway, compute, notifications, medallion | services/ also holds catalog, lineage, maintenance, ingest, controlplane, viewer, search, annotator, flows (13 directories) |
| chart/values.yaml:1570 | The maintenance event lane is "OFF (this default)" | `workTopic` defaults on (values.yaml:1630), and the 120 s sweep still runs beside it |
| chart/values.yaml:1577 | "the catalog's lineage lane has no outbox" | chart/templates/services.yaml:190-202 renders it by default (values.yaml:491-492); catalog main.py:216 |
| services/ingest/src/ingest/lineage.py:21-24 | "the cascade triggers on the catalog's publication event rather than on a lineage event" | Same file :154-158 and runtime.py:801-835: bronze is never published, and `/bronze-arrival` fires on the catalog's INSERT lineage event |
| services/medallion/src/medallion/services/catalog_register.py:234-240 | The Ray stage job writes with the "RustFS ROOT credential", and `mode_b` vends nothing | The chart gives the Ray pod the `rask-ray-compute` key (_ray-cluster-config.tpl:173-181; values.yaml:2389), the store is MinIO, and the vending default is `sts` (values.yaml:1031) |
| services/medallion/src/medallion/producer.py:1-14 | "In production the head is a real Ray Data job"; and (:9-13) only a write to `bronze_namespace`/`bronze_dataset` fires the head, "an ordinary catalog table write does NOT" | The head is the producer or ingest; no Ray job writes bronze. A write to a lane-declared table fires the head (ingest_trigger.py:147-148), and ingest's bronze is announced by the catalog's own INSERT event (data.py:220-229) |
| services/medallion/src/medallion/services/media_produce.py:167-170; api/ingest_media.py:39 | `ensure_stage_output` is "the catalog's own register door", whose REGISTER_TABLE marker fires no cascade; the door "REGISTERS the bronze media table" | `ensure_stage_output` calls describe, else create with `mode=exist_ok`, and never the register door (catalog_register.py:284-357) |

---

## Verification

Two independent reviewers re-checked 121 claims of this map against eb53bfc, each opening the cited file and line.
One took hop tables A and B and nine weak points (50 claims), the other tables C to F, eleven weak points, "Register
vs architecture" and the docs table (71 claims). 102 were confirmed as written, 14 were wrong or partly wrong, 3
needed a wording fix, and 2 could not be checked from code because they are live state. Every correction is applied
above: the stage runner's root resolution and its describe fallback (B6, WP 10), the destination door (B7, M1), the
in-process write modes (B10a), the contract membership (F1), the train path (F5, WP 19), the operator routes (F7),
ingest's ambient fallback (A6, WP 20), the vendor's signing key (C5), and five statements about the register (XC-093,
XC-043, the FOCUS criterion-3 claim, XC-078's order, XC-091's preconditions). The two passages this map earlier left
unread, the vending session policy (vending.py:129-134,499-526) and lineage's signature gate (fga_deps.py:293-339),
were read and confirmed, and two further stale passages were found (values.yaml:1570, catalog_register.py:234-240).

## Not verified

- The live state: pods, images and NATS auth. This map is code and chart only.
- WP 12's "dead live" and "tenant-blind": both come from CTL-021's last read-back; the fixed image is pinned but was
  not read back, and the feed's tenant scope was not re-measured.
- Whether `services/controlplane` has defects. Only its module docstring and `security.py` were read.
