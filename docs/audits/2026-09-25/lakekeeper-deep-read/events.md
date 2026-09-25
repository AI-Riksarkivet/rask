# Lakekeeper deep-read — EVENTS, translated onto rask's stack

Scope: `crates/lakekeeper/src/service/events/**` (every file), `crates/lakekeeper-events-nats/**`,
`crates/lakekeeper-events-kafka/**`, the binary wiring (`lakekeeper-bin/src/events.rs`), the
emit call sites in `server/tables.rs` / `server/tables/create_table.rs`, `serve.rs` wiring, and the
event docs (`docs/docs/customize.md`, `configuration.md` §NATS/§Kafka/§Logging Cloudevents,
`concepts.md`, `logging.md` audit section). Lakekeeper paths below are relative to
`/home/gabriel/Desktop/lakekeeper-ref/`; rask paths relative to `/home/gabriel/Desktop/rask/`.

Three measurements were taken this pass (scratch only, absolute paths under
`.../reaudit/lakekeeper/measure/`), plus read-only `kubectl get`:

| # | What | Result |
|---|---|---|
| M1 | pylance 12.0.0: `write_dataset(..., transaction_properties={...})` then `read_transaction(v)` | v1 `Overwrite` and v2 `Append` both return the stamped `{'rask.run_id','rask.author'}`; `LanceDataset.delete` signature has **no** properties parameter and its v3 `Delete` transaction reads back `{}` |
| M2 | pylance 12.0.0 + lance-namespace 0.11.1, `connect("dir")`: `batch_commit_tables` with `declare_table` ops, and with a `deregister_table` op | both raise `UnsupportedOperationError: Not supported: batch_commit_tables` (the method exists, the dir backend does not implement it) |
| M3 | live estate, `kubectl get` only | `ghcr.io/dapr/daprd:1.18.1` ×18, `nats:2.14.2-alpine` ×3; all **14** `pubsub.jetstream` Components carry no auth metadata key (only `natsURL,name,…consumer knobs`); `MAINTENANCE_CONTROL_EMIT_ENABLED` absent from both maintenance deployments; `ANNOTATOR_CONTROL_EMIT_ENABLED=true` on the annotator |

---

## 0. How Lakekeeper's event system is built (the whole mechanism, once)

Two layers (`docs/docs/customize.md:35-40`):

1. **`EventDispatcher` → `EventListener`s, in-process, typed.** `EventDispatcher` is an
   `Arc<RwLock<Vec<Arc<dyn EventListener>>>>` (`events/dispatch.rs:39-64`). Every `dispatch_event!`
   snapshots the list and `join_all`s each listener; **a listener error is logged at WARN and
   swallowed** (`dispatch.rs:15-30`). The trait's contract: methods are past-tense and fire "after
   successful operations"; "If the listener fails, it will be logged, but the request will continue"
   (`dispatch.rs:268-296`). Listeners registered by `serve`: the CloudEvents publisher, plus
   warehouse/namespace/role cache invalidators (`serve.rs:409-440`) and the audit listener.
2. **`CloudEventsPublisher` is itself an `EventListener`** that turns a subset of domain events into
   CloudEvents and pushes them into a bounded `tokio::mpsc` channel (capacity **1000**,
   `serve.rs:229`) with `send_timeout` **50 ms** (`publisher.rs:544-585`). A single background task
   (`CloudEventsPublisherBackgroundTask`, `publisher.rs:615-697`) builds each CloudEvent and fans it
   out to every configured `CloudEventBackend` sink concurrently (`publisher.rs:676-692`). Sinks:
   NATS (`lakekeeper-events-nats/src/lib.rs:73-90 (publish at :79-85)`), Kafka (`lakekeeper-events-kafka/src/lib.rs:105-155`),
   and a tracing logger (`publisher.rs:97-110,705-720`), assembled by the binary
   (`lakekeeper-bin/src/events.rs:20-39`).

Emit helpers on `APIEventContext` are typestated: an event can only be emitted from an
`AuthzChecked` context (`context.rs:145-170,1123-1172,1253-1273`), and nearly every helper does
`tokio::spawn(dispatcher.<method>(event))` — fire-and-forget off the request task (e.g.
`types/table.rs:105-116`, `types/namespace.rs:188-197`, `types/warehouse.rs:110-120`). The one
awaited inline is `transaction_committed` (`server/tables.rs:1261-1273`), and its failures are still
swallowed by the dispatcher.

---

## Topic 1 — CloudEvents shape

**Lakekeeper.**
- Spec 1.0 via `EventBuilderV10` (`publisher.rs:632-638`): `id` = `Uuid::now_v7()` minted per publish
  call (`publisher.rs:145,174,203,…`), `source` = `uri:iceberg-catalog-service:<hostname>`
  (`publisher.rs:22-28,636`), `type` = the Iceberg REST operationId in camelCase (`createTable`,
  `updateTable`, `dropTable`, `registerTable`, `renameTable`, `createView`, `updateView`, `dropView`,
  `renameView`, `createGenericTable`, `dropGenericTable`, `renameGenericTable`, `undropTabulars`;
  `publisher.rs:146,175,204,234,262,292,322,349,377,408,438,469,505`), `datacontenttype`
  `application/json`.
- `data` = **the caller's request body**, not the committed result: `maybe_body_to_json(request)`
  (`server/tables.rs:2155-2164`) for create/update/rename, `Value::Null` for drop/register/undrop/
  renameView (`publisher.rs:135,176,205,236,378,507`). No committed metadata location, no snapshot id.
- Ten CloudEvent **extensions** (`publisher.rs:652-667`): `tabular-type`, `tabular-id`,
  `warehouse-id`, `name`, `namespace`, `prefix`, `num-events`, `sequence-number`, `trace-id`, `actor`.
  They are not consistent across types: `updateTable` renders `namespace` with `to_url_string()` and
  `prefix` as `""` (`publisher.rs:152-153`), `dropTable` uses `to_string()` and the warehouse id
  (`:181-182`); `renameTable`, `undropTabulars`, `renameView` also send `prefix: ""` (`:270,513,385`).
- A build failure `continue`s silently past the event (`publisher.rs:668-674`).
- Wire encoding differs by sink: NATS publishes **structured-mode JSON** of the whole event as the
  message body on one subject (`nats/src/lib.rs:79-85`); Kafka uses **binary mode** — attributes as
  `ce_*` headers, `datacontenttype` → `content-type` (`kafka/.../binding/mod.rs:213-231`,
  `kafka_producer_record.rs:48-93`), record key = the `tabular-id` extension (`kafka/src/lib.rs:121-131`).

**rask today.**
- rask does not build envelopes; **daprd does**. Every publish goes through
  `service_kit.dapr_publish.publish_event` with `data_content_type="application/json"`
  (`packages/service-kit/src/service_kit/dapr_publish.py:310-335`, called from
  `packages/service-kit/src/service_kit/lakehouse/outbox.py:374-381`), and no call site passes
  `publish_metadata` (grep for `cloudevent.`/`rawPayload` over `packages/` and `services/` source: no
  hits). The installed SDK does accept it (`.venv/.../dapr/aio/clients/grpc/client.py:368-376`,
  dapr 1.18.3).
- Every subscriber reads only `body["data"]` (`services/lineage/src/lineage/api/dapr.py:133`,
  `services/catalog/src/catalog/api/dapr.py:61`,
  `services/notifications/src/notifications/api/subscriptions.py:84`). Identity, dedupe keys and
  object ids live **inside the payload**: `CatalogControlEvent.event_id/actor/object_id`
  (`packages/service-kit/src/service_kit/control_events.py:161-181`), OpenLineage `run.runId` or
  `event_identity(...)` plus the `author`/`lance`/signature facets
  (`services/catalog/src/catalog/core/lineage_emit.py:209-236,369-392,796-845`).
- Payloads are claim-check pointers with a hard 900 KiB cap (`dapr_publish.py:270-299,318-328`).

**What rask should do on its own stack.**
- Keep daprd as the CloudEvents producer (that is the stack's seam; hand-building envelopes with
  `rawPayload` would drop the sidecar's trace propagation). Do **not** copy Lakekeeper's ten
  extensions: extensions sit outside rask's signed payload (`packages/lineage-kit/src/lineage_kit/signing.py:8-11`
  — "the envelope is the transport's; the signature is the PRODUCER's"), so any identity or object id
  moved into an extension would be unauthenticated again.
- Do **not** copy the request-body `data`. rask's pointer-to-committed-state payload is the better
  design and must stay exactly that — which makes the open "insert event names the latest version,
  not its own" defect (findings_lance_lakekeeper row "An insert's lineage event names whatever version
  is latest…", `services/catalog/src/catalog/services/dataplane.py:1310-1331`) the one place rask
  currently violates its own rule. Lakekeeper at least builds its commit event from its own
  transaction's `CommitContext` (`server/tables.rs:1263-1270`).
- Lakekeeper's inconsistency (`namespace`/`prefix` spelled two ways) is the lesson: pin **one**
  canonical id per event. rask already routes every lineage id through `fga.canonical_object_id`
  (`lineage_emit.py:938-941`); keep a contract test per `ControlAction` so shape drift fails CI.
- Optional, low value: stamp a stable CloudEvent `id`/`type` via `publish_metadata` so the envelope id
  equals the payload's dedupe key. **Not verified this pass** that daprd 1.18.1 honours
  `cloudevent.*` metadata overrides — check Dapr's pubsub-cloudevents docs before relying on it.

Rows: LH-064 (identity stays in the signed payload), new "insert event version" (findings row 58).

---

## Topic 2 — Which operations emit

**Lakekeeper.**
- **CloudEvents (the broker)** cover only the 13 tabular operations above:
  `CloudEventsPublisher` implements only `transaction_committed`, `table_dropped/registered/created/renamed`,
  `view_created/committed/dropped/renamed`, `generic_table_created/dropped/renamed`,
  `tabular_undropped` (`publisher.rs:112-530`).
- **Governance events never reach the broker**: namespace create/drop/properties/protection,
  project/warehouse create/delete/rename/protection/storage/**credential** updates, task-queue config,
  role create/update/delete and role-sync are dispatched to in-process listeners only
  (`dispatch.rs:150-265`; types in `types/{server,project,warehouse,namespace,role}.rs`), used for
  cache invalidation (`serve.rs:414-440`, `service/catalog_store/warehouse_cache.rs:345-375`).
- Read events (`table_loaded`, `view_loaded`, `generic_table_loaded`, `namespace_metadata_loaded`)
  exist as listener hooks (`dispatch.rs:106-108,126-128,138-140,260-265`) — not published.
- **Every** authorization decision, allowed or denied, becomes an audit record through
  `AuthorizationSucceeded/FailedEvent` → `AuditEventListener` (`context.rs:1132-1234`,
  `backends/audit.rs:173-243`). That is a tracing log line (`event_source="audit"`), not a CloudEvent.
- Background tasks (soft-delete expiration, purge) dispatch **no** events (no dispatcher use in
  `service/tasks/*.rs` other than a comment at `task_registry.rs:833`).

**rask today.**
- Two lanes. Lineage (`lineage.events.v1`): catalog data writes and DDL as OpenLineage
  RunEvent/DatasetEvent (`lineage_emit.py:198-236,237-392`), plus medallion/maintenance/ingest
  producers. Control (`catalog.control.v1`): ~50 `ControlAction`s including grants, projects,
  warehouses, policies, transforms, gates, namespace/table lifecycle, protection, undrop, purge, the
  ref plane and annotation tasks (`control_events.py:36-150`). rask already publishes the governance
  mutations Lakekeeper keeps in-process.
- Emit-site count per catalog endpoint module (grep of `emit_write_event|emit_control|…emitter`):
  tables 15, namespaces 7, warehouses 7, policies 5, projects 5, publication 4, tags 4, and so on.
- **Gaps found this pass:**
  - `POST /v1/table/{id}/version/delete` emits **nothing** (`services/catalog/src/catalog/api/v1/endpoints/versions.py:456-475`),
    although it destroys versions (and today removes tagged/current manifests — findings row 52).
    `create_table_version` does emit (`versions.py:420-431`); its docstring says the other routes
    "mint nothing … a delete governed by the deletion control" (`versions.py:394-398`), which
    classifies a destructive op as needing no provenance.
  - `POST /v1/table/batch-commit` emits nothing (`versions.py:146-215`) although the spec defines it
    to carry `DeclareTable, CreateTableVersion, DeleteTableVersions, DeregisterTable` atomically
    (`lance_docs/ns_catalog/spec.yaml`, `BatchCommitTablesRequest` / `CommitTableOperation`).
    **Latent, not live**: M2 shows the dir backend answers Unsupported on 12.0.0, so the door fails
    before writing. It becomes a silent writer the day a backend implements it.
  - Maintenance's `table_purged`/`namespace_purged` (`services/maintenance/src/maintenance/services/purge.py:734-747`)
    is off by default (`services/maintenance/src/maintenance/core/config.py:457`,
    `chart/values.yaml:2035-2038`) and off live (M3). So in the live estate the last event an object
    ever produces is never announced. (Lakekeeper doesn't announce its automated purge either, so
    this is parity, not a regression. rask built the verb and left it off.)

**What rask should do.**
- Keep the governance lane: it is ahead of the reference.
- Make coverage **derived from the spec**: one table mapping each of the 54 `spec.yaml` operationIds
  (plus rask's management doors) to `lineage | control | none(reason)`, gated by a unit test that
  fails when a write door has no row. That turns "the batch door is silent" and "version delete is
  silent" into CI failures instead of audit findings.
- Version delete must announce itself. The event depends on how row 52 is fixed (the fix routes it
  through `cleanup_old_versions`): emit an untargeted control action (e.g. `table_versions_deleted`
  with `extra={from,to}`) through the three-file `ControlAction` contract, the same shape LH-056 used
  for the ref plane.
  **Timing (rulings.md, updated 2026-09-25):** the owner put the version-delete row ("removes
  tagged/current manifests and reuses version numbers") in the next batch of three. Ship the
  announcement in that row's commit, not as a follow-up, so the fixed door is never a silent
  destructive door. D3 is now ruled (a): branch create/delete stay owner-tier, and the ref-plane
  control events LH-056 shipped stay untargeted.
- Batch-commit: either emit per operation on success (one OpenLineage run with N outputs, see
  Topic 7) or refuse at the door until a backend supports it. Don't leave a write path that is only
  silent because the backend happens to be unsupported.
- The LH-099 decline (compaction emits no control event) is consistent with Lakekeeper, which
  announces no maintenance either. Record it per reconciliation P0.4.
- Turn on `maintenance.controlEmit` together with the outbox for that lane (Topic 5), or record why
  purge stays silent.

Rows: LH-099, LH-144 (drop announcement coverage), new: "version-delete emits no event" (bundle with
findings row 52), new: "batch-commit door would write silently" (latent, low), new: "maintenance purge
announcement off by default and unstaged".

---

## Topic 3 — Emitted before or after commit; transactional or not

**Lakekeeper.**
- Always **after** the Postgres commit, never inside it:
  - multi-table commit: `transaction.commit()` at `server/tables.rs:1705`, return at `:1757`, then
    `transaction_committed` dispatched at `:1261-1273`;
  - create: `t.commit()` at `server/tables/create_table.rs:392`, then `emit_table_created_async` at `:402-408`;
  - drop: `t.commit()` at `server/tables.rs:880`, authz cleanup, then `emit_table_dropped_async` at `:892-895`.
- No outbox anywhere: `grep -rniw outbox` over the repo returns 0 hits; no JetStream either (0 hits).
- Idempotency keys are written **in the same DB transaction** as the mutation
  (`service/idempotency.rs:66-78`, e.g. `server/tables.rs:859-880`), and a replay returns **before**
  the mutation and before any emit (`server/tables.rs:1380-1383,741-743`). So a client retry never
  produces a second event, but the event itself is not transactional: a crash after `commit()`
  loses it.

**rask today.**
- Also after the commit, and also outside it: the catalog's lineage emit is awaited inline after the
  Lance write (`lineage_emit.py:898-954`), and the outbox stages **after** the commit
  (`packages/service-kit/src/service_kit/lakehouse/outbox.py:8-13`: "Because the stage happens AFTER the
  commit…"). So a crash between the Lance commit and the stage loses the event. The reconcile then
  back-fills an authorless `reconcile` run (findings row 68; reconciliation P3.9).
- `lineage_emit.py:23-24` still says "The outbox gap … remains: the catalog has no DB for a
  transactional outbox", while `DaprEmitter._send` at `:816-845` stages through the outbox. The prose
  contradicts the code next to it.
- Control events are emitted after the backend/FGA mutation and its audit record
  (`packages/service-kit/src/service_kit/control_emit.py:15-19,186-224`), which matches Lakekeeper's
  "after successful operations" rule.

**What rask should do.**
- rask has no Postgres to put an outbox row in, but it doesn't need one: **a Lance commit is itself a
  transaction record**. M1 shows pylance 12.0.0 persists `transaction_properties` atomically with the
  manifest and returns them from `read_transaction(v)`. So the Lance-idiomatic transactional outbox is
  to stamp the event's identity **into the commit**: `rask.run_id`, `rask.author` (the D5 principal
  key), `rask.operation`, `rask.on_behalf_of`. The reconcile can then rebuild an attributed event from
  `read_transaction(v)` instead of writing `author='reconcile'`. This is the same guarantee
  Lakekeeper gets for idempotency keys by writing them in its DB transaction, applied to the event.
- State the coverage honestly: M1 shows `delete` (and per findings `update`, native-namespace ops)
  cannot carry properties. Those doors stay on the after-commit outbox path, and the prose must name
  them. Lance's transaction file format is specified in `lance_docs/file_format.md:4783-4795` (and
  `protos/transaction.proto` upstream).
- Rewrite `lineage_emit.py:23-24` in the same commit.
- Keep "announce only what happened" (Lakekeeper `dispatch.rs:268-282`). CP-034 breaks it: an
  `unnotified` stage verdict writes a FAIL for a committed write.

Rows: new "crash between commit and outbox stage" (reconciliation P3.9 / findings row 68), LH-141
(repair of stale stamps: same read_transaction surface), CP-034.

---

## Topic 4 — Delivery guarantee

**Lakekeeper: at-most-once, with several silent loss points.**
1. Channel full or slow: `send_timeout(50ms)` fails, WARN logged (`publisher.rs:561-584`), and the
   dispatcher swallows it (`dispatch.rs:20-27`).
2. `tokio::spawn` emit: the request never learns the outcome (`types/*.rs` passim).
3. CloudEvent build failure: `continue` (`publisher.rs:668-674`).
4. Sink failure: WARN logged, not retried (`publisher.rs:676-692`).
5. NATS: core `client.publish` with no JetStream PubAck, no persistence and no retry
   (`nats/src/lib.rs:79-85`).
6. Kafka: one `send` with a 1 s queue timeout, error returned then only logged
   (`kafka/src/lib.rs:119-149`).
7. Shutdown: `Shutdown` is enqueued (`serve.rs:257`) and the loop exits at the first non-`Event`
   message (`publisher.rs:625-630`). Anything enqueued after it is dropped.

**rask today: at-least-once by design, with holes.**
- Producer side: stage to S3, publish via the sidecar, drop the staged copy on ack
  (`outbox.py:271-392`). JetStream persists. Durable per-subscriber consumers carry
  `ackWait/maxDeliver/backOff` (`chart/templates/dapr-component.yaml:99-124`), with a Dapr DLQ.
  Consumers are idempotent: lineage MERGEs on `run_id` (`lineage_emit.py:915-919`); notifications on
  `<event_id>@<ACTION>` (rows_14 CTL-027).
- Holes (all cited):
  - **The relay deletes every staged DatasetEvent as poison.** It parses with
    `RunEvent.model_validate_json` only (`services/lineage/src/lineage/api/reconcile_cron.py:526`,
    drop at `:545-549`), while the catalog stages DDL as DatasetEvents (`lineage_emit.py:367-368`).
    Reconciliation P1.5.
  - A stage failure silently degrades to a plain publish (`outbox.py:361-370`). It is counted, but
    the event is then only as durable as the bus hop.
  - Maintenance and the annotator build their control emitters **with no `outbox_uri`**
    (`services/maintenance/src/maintenance/service.py:174-180`,
    `services/annotator/src/annotator/main.py:81-87`). Only the catalog stages control events
    (`services/catalog/src/catalog/main.py:235-243`, relay `api/control_relay.py`). The lineage lane
    is staged for catalog, maintenance, worker, ingest and medallion (`chart/templates/services.yaml:201`,
    `maintenance.yaml:218`, `maintenance-worker.yaml:182`, `fleet.yaml:212`, `medallion.yaml:91,498`).
  - The compute plane has no outbox (CP-037), and nothing re-ingests the DLQ (LH-148).

**What rask should do.**
- Keep at-least-once plus idempotent consumers. Lakekeeper is the negative reference here.
- Close the holes in order: P1.5 (relay parses with the consumer's own discriminator, and
  DatasetEvent → `ingest_dataset_event`) **before** the LH-064 require-signature flip. Then give every
  control producer the outbox: `make_control_emitter` already takes `outbox_uri`
  (`control_emit.py:159-183`). Each lane keeps its own prefix (`outbox.py:292-294`); a second control
  producer needs a relay that drains **its** prefix, or the catalog's relay must drain a shared
  control prefix. Maintenance matters most: purge is irreversible (`control_emit.py:17-19`).
- Compute plane (phase 2): wire `LineageRun(on_undelivered=…)` to stage into `_lineage_outbox`
  (CP-037). Emit off the critical path (LIN-003) **but staged**. Lakekeeper's bounded channel with a
  50 ms timeout is exactly the lossy version of "off the critical path".

Rows: new "relay drops DatasetEvents" (P1.5), LH-148, CP-037, LIN-003, new "maintenance/annotator
control emits unstaged".

---

## Topic 5 — Ordering, dedup, grouping

**Lakekeeper.**
- A multi-table transaction becomes N `updateTable` events sharing one `trace-id` (the request id),
  numbered with `num-events`/`sequence-number` (`publisher.rs:139-160,659-663`), published concurrently
  (`try_join_all`, `:161-163`).
- Kafka's partition key is `tabular-id`, which gives per-table order (`kafka/src/lib.rs:121-131`).
- The event `id` is fresh per publish, so it cannot dedupe a replay (`publisher.rs:145`).

**rask today.**
- The per-table Lance version is the ordering token (the graph takes max(version); findings "RASK
  AHEAD"). Consumers MERGE on `run_id`/`event_id` (`control_events.py:167-168`,
  `lineage_emit.py:915-919`).
- The catalog mints a fresh `run_id` per emit (`lineage_emit.py:946`), so a retried door that
  re-executes produces a second run. The `Idempotency-Key` seam covers create
  (`services/catalog/src/catalog/api/idempotency.py:1-22`).
- `BatchCommitTables` is unsupported on the dir backend (M2), so no multi-table atomic unit exists today.

**What rask should do.**
- No `sequence-number`/`num-events` extensions and no broker partition keys. The Lance version orders.
- Group an atomic multi-table commit the OpenLineage way: **one** RunEvent with N `outputs`, each
  version-pinned, under one `runId`. That is strictly better than Lakekeeper's N events plus
  counters, and it is what the graph already ingests. Needed only when batch-commit is supported (Topic 2).
- For replays, prefer carrying the stamped `rask.run_id` from the commit (Topic 3) as the event's
  `runId`, so a reconcile-reconstructed event and the original dedupe to one run.

Rows: new "batch-commit" (latent), P3.9.

---

## Topic 6 — Identity carried in events

**Lakekeeper.**
- The `actor` extension is a JSON string (`publisher.rs:30-67,157,666-667`):
  `{"type":"principal","principal":"oidc~123"}`,
  `{"type":"role","principal":…,"assumed-role":<uuid>}` or `{"type":"anonymous"}` (tests
  `publisher.rs:726-765`).
- The principal key is `<idp_id>~<user-id>` (`service/authn.rs:30,73-74,765`), with idp ids `oidc`
  and `kubernetes` (ServiceAccount tokens). The audit log carries an extra `lakekeeper-internal` actor
  and `privilege_source` (`backends/audit.rs:381-406,182-183,220-221`).
- **Nothing is signed.** Consumers trust whoever can publish to the subject.

**rask today.**
- Control events: `actor` is the verified sub (e.g. `user:alice`), stamped by the producer and
  **unsigned** (`control_events.py:161-178`). The docstring's trust argument ("internal catalog-only
  channel") is false while NATS admits any pod (findings row 61).
- Lineage: `author.sub` facet plus an HMAC-SHA256 signature facet naming the signer identity and
  `on_behalf_of` (`signing.py:45-58,160-237`; catalog signs at `lineage_emit.py:796-814`). The door
  verifies only if a signature is present (`services/lineage/src/lineage/api/fga_deps.py:288-333`).

**What rask should do.**
- Adopt **D5** in every event identity field in one move (pre-production, so no dual spelling):
  control `actor` and `extra.subject`, OpenLineage `author.sub`, `lance.originator`, and the signature
  facet's `identity`/`on_behalf_of` all become `<idp-id>~<claim>`. Move every consumer in the same
  change: the notifications targeting keys, the lineage `(:User)` node key, and the FGA `user:` object
  ids they are checked against.
- Under **D1**, a service producer's identity is its ServiceAccount principal
  (`kubernetes~system:serviceaccount:<ns>:<sa>`, Lakekeeper's `K8S_IDP_ID`). The signer identity in the
  facet becomes that key, not a chart-invented `service-*` name.
- Model "service acting for a person" the way Lakekeeper's `Role{principal, assumed_role}` keeps both
  facts: **signer** (the machine principal) plus **on_behalf_of** (the person), both inside the
  signed payload. rask already has this shape (`signing.py:160-194`) but must extend it to control
  events (LH-064 part 3 / findings row 62).
- Keep the per-event producer signature. It is the one thing Lakekeeper lacks, and it is what turns
  "any client on the subject" (which broker auth, Topic 8, narrows) into "this named producer".
  Layering: Dapr component `scopes` (which sidecar may use the component) < NATS user permissions
  (who may publish to the subject) < HMAC signature (which identity produced this event).

Rows: LH-064, D5 (principal key), D1, XC-048 (actor rides the payload as a claim, per D14).

---

## Topic 7 — Correlation / tracing

**Lakekeeper.** The `trace-id` extension carries the **request id**, not W3C trace context, with a
TODO pointing at distributed-tracing issue 63 (`publisher.rs:156,664-665`).

**rask today.** daprd propagates W3C trace context through the CloudEvent envelope
(`lineage_emit.py:745-757` states it). Request id is an edge echo only (rows_05 XC-048).

**Should.** Record D14's XC-048 ruling in `docs/DECISIONS.md` §9: the CloudEvent's `traceparent` set
by daprd is the cross-hop correlation. No request-id extension. rask is already where Lakekeeper's
TODO points.

Rows: XC-048.

---

## Topic 8 — Broker authentication used by the publisher (NATS)

**Lakekeeper.**
- `DynAppConfig { nats_address, nats_topic, nats_creds_file, nats_user, nats_password, nats_token }`
  from env prefixes `LAKEKEEPER__`/`ICEBERG_REST__` (`nats/src/config.rs:9-38`).
- The connect builder applies the creds file (`.creds` = user JWT + NKey seed), then
  user/password, then token (`nats/src/lib.rs:34-62`).
- The chart has no NATS values. Config reaches the pod as environment through `catalog.config`
  ("mounted as environment variables", `lakekeeper-charts/.../values.yaml:136-140`), and the chart's
  config Secret is `envFrom` (`templates/config/secret-config-envs.yaml:11-39`). A creds file needs
  `extraVolumes`. Kafka does the same: `sasl.password` etc. from env or a config file
  (`kafka/src/config.rs:8-54`).
- The broker authenticates the **publisher** only. Consumers are out of Lakekeeper's scope.

**rask today.**
- No NATS authentication at all. `lance.natsUrl` is a bare `nats://<host>:4222`
  (`chart/templates/_helpers.tpl:795-797`), and the `nats:` values block has no auth, accounts or
  users (`chart/values.yaml:2716-2773`).
- M3 confirms that no live `pubsub.jetstream` Component carries an auth key.
- Only one component uses Dapr topic scoping (`protectedTopics/publishingScopes/subscriptionScopes`,
  `dapr-component.yaml:228-269`), and that bounds the sidecar, not a raw NATS client (findings row 61).

**What rask should do.**
- Translate Lakekeeper's best option, the creds-file path, not its env path. Use NATS
  **decentralized JWT/NKey** auth: operator and account JWTs are public; one user per Dapr app-id,
  with publish/subscribe permissions enumerated from each app's `/v1.0/metadata` (including
  `$JS.API.>`).
- **Sidecar apps:** the NATS client is daprd, so the credential goes into each `pubsub.jetstream`
  Component's auth metadata as a `secretKeyRef` resolved through the OpenBao-backed Dapr secret store
  (`auth.secretStore`). The app process never holds it.
- **Non-sidecar Jobs** (stream provisioning/reconcile): an ESO-written file mount (`.creds`), which
  is Lakekeeper's `nats_creds_file` shape.
- Never token or password: those travel in the CONNECT line and, on today's plaintext `nats://`
  (XC-007, parked), would cross the wire in cleartext. With NKey auth the client signs the server's
  nonce and the seed never leaves the pod. This also satisfies the owner's rule ("never secret
  through envs").
- This is reconciliation **P8.14**. Per D14 it is NOT covered by the no-prod parking.
- **Not verified this pass:** the exact jetstream Component metadata key names for JWT/seed auth in
  daprd 1.18.1 (no local Dapr source or docs). Read Dapr's `pubsub.jetstream` spec before writing the
  template.

Rows: new "NATS broker accepts unauthenticated clients" (findings row 61 / P8.14), XC-007 (TLS
remains parked), LH-064 (defence-in-depth pair).

---

## Topic 9 — In-process listeners and cache invalidation

**Lakekeeper.** Cache listeners are per replica (`serve.rs:414-440`). Cross-replica staleness is
bounded only by TTL (`catalog_store/warehouse_cache.rs:30-40,94-98`). An event is a hint, not state
(the listener inserts or invalidates, `:345-375`).

**rask today.** Cross-replica invalidation rides the broadcast control subscription
(`services/catalog/src/catalog/api/dapr.py:55-75`, ephemeral `deliverPolicy=new`,
`dapr-component.yaml:28-49`), with a TTL floor of 300 s
(`services/catalog/src/catalog/core/config.py:594-602`). Consumers treat events as refresh hints
(`control_events.py:13-15`).

**Should.** Already aligned, and stronger than the reference (bus broadcast plus TTL). No change. Keep
the TTL, because an ephemeral consumer misses anything published while its sidecar was down.

Rows: none.

---

## Topic 10 — Audit events (authorization decisions)

**Lakekeeper.**
- Every authz check produces a succeeded or failed event (`context.rs:1132-1234`).
- Each record carries an always-non-empty `authorizations[]`. Each entry holds `for_principal`,
  `action`, `entity` and `allowed`, where `allowed` is `true`, `false`, or **absent when no verdict was
  reached** (`types/authorization.rs:13-42`; `context.rs:1275-1345`; `docs/docs/logging.md:52-122`).
- A failure reason is one of six, split into "denied" and "never decided"
  (`types/authorization.rs:166-189`).
- Also carried: `privilege_source` (`backends/audit.rs:183`).
- Delivered async (`tokio::spawn`, `context.rs:1159-1166,1225-1232`) to a tracing log.

**rask today.**
- The `lance.audit` logger, with outcomes `allow|deny|success|failure`
  (`packages/service-kit/src/service_kit/governed/audit.py:21-31`).
- The catalog audits every decision, and marks an unreachable authz backend as FAILURE, not DENY
  (`services/catalog/src/catalog/api/fga_deps.py:421-450`). That is the same "no verdict ≠ deny" split.

**Should.** Mostly aligned. One refinement worth taking: when a service acts `on_behalf_of` a person,
record both principals on the audit record (Lakekeeper's `actor` vs `for-principal`). This belongs to
the authz domain; not pursued further here.

Rows: none directly (XC-017 inventory, if anything).

---

## Topic 11 — Listener weight and fan-out

**Lakekeeper.** "An implementation should be light-weight, ideally every longer running task is
deferred to a background task via a channel or is spawned as a tokio task" (`dispatch.rs:288-290`).

**rask.** A group grant expands to members in a sequential loop inside the bus handler, bounded only
by `ackWait` (CTL-027, `services/notifications/src/notifications/api/control_events.py:200-215`).

**Should (phase 3).** Bound or move the fan-out off the handler while keeping CTL-027's all-or-retry
property. The durable form on rask's stack is re-publishing per-member work items to a queue-group
topic, not an in-memory channel.

Rows: CTL-027.

---

## Row map (summary)

| Row | Bearing |
|---|---|
| LH-064 | Identity stays in the signed payload. Extend signing to control events. SA-principal signer (D1). Pair with NATS auth |
| LH-144 | Drop announcements: coverage table plus the version-delete gap. The DatasetEvent relay fix is a precondition |
| LH-148 | Delivery holes: the DLQ has no re-ingest (unchanged by this read) |
| LH-099 | Decline is consistent with Lakekeeper (no maintenance events there either) |
| CP-037, LIN-003 | Off the critical path, but staged. Lakekeeper's bounded channel is the lossy anti-example |
| CP-034 | "Announce only what happened" (Lakekeeper `dispatch.rs:268-282`) |
| XC-048 | traceparent is the correlation. Lakekeeper's request-id `trace-id` is a TODO it has not finished |
| XC-007 | NKey auth does not need TLS to protect the seed. Token/password would |
| CTL-027 | Listener weight |
| new (findings row 61 / P8.14) | NATS JWT/NKey per app via Dapr Component `secretKeyRef` and ESO creds-file for Jobs |
| new (P1.5) | Relay drops DatasetEvents |
| new (P3.9 / findings row 68) | Stamp event identity into the Lance commit (M1) |
| new (findings row 58) | Insert event names the latest version |
| new (this read) | version/delete emits nothing (`versions.py:456-475`). Ship it in the owner-ordered version-delete row's commit |
| new (this read) | batch-commit door would write silently if a backend supported it (M2: dir backend unsupported, latent) |
| new (this read) | Maintenance purge announcement off by default and live; maintenance/annotator control emits unstaged |

## What this pass did not check

- Dapr docs: `pubsub.jetstream` auth metadata key names; whether daprd 1.18.1 honours `cloudevent.*`
  publish-metadata overrides; whether the jetstream component sets `Nats-Msg-Id` for broker-side
  dedupe. No local source or docs; the estate cannot be exec'd into.
- Lakekeeper integration tests for events, `authz-openfga`, and `limes` internals were not read.
- The Kafka vendored `binding/mod.rs` and `rdkafka/mod.rs` Apache-license bodies (lines ~11-185) were
  skimmed; their code sections were read in full.
- rask: the notifications service beyond cited lines; lineage `consumer.py`/`ingest.py` beyond the
  audit's cites; `control_relay.py` beyond grep. No rask test was run. The relay-poison defect was
  confirmed by reading `reconcile_cron.py:520-549`, not by execution.
- Measurement scratch (`.../reaudit/lakekeeper/measure/{t.lance,nsroot}`) is left in place.
