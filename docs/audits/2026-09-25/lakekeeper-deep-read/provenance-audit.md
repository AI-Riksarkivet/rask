# Lakekeeper deep-read: provenance, audit and observability, and what rask should do

**Scope.** This covers the audit trail of who did what, request and trace ids, `endpoint_statistics.rs`,
`contract_verification.rs`, how the actor is recorded on writes, and tracing and metrics. Each is
compared with rask's OpenLineage-into-AGE provenance, its `lance.audit` stream and its two `audit_read`s.

**Reference tree.** `/home/gabriel/Desktop/lakekeeper-ref`, commit `a58e401` (2026-06-22), plus the
Lakekeeper chart under `reaudit/lakekeeper-charts/charts/lakekeeper`. All Lakekeeper cites are
`path:line` in that tree unless marked otherwise.

**The session holds two newer Lakekeeper copies, and an earlier audit cited one of them.** The
session scratchpad has:

- `scratchpad/lk/`, a git clone at `2b7971b` (2026-09-17) with local modifications;
- flat copies next to it, fetched around 10:57–12:10 today: `lk/audit_mod.rs`,
  `lk/it_audit_corpus.rs` and `lk/events_context.rs`.

`findings_lance_lakekeeper.md` row "Audit records carry no format version…" cites
`backends/audit/mod.rs:18-130` (the `AUDIT_FORMAT` const) and `audit_corpus.rs`. Row "The route-gate
completeness test passes on a docstring…" cites `context.rs:941-966,1418-1470`. **None of these
exists in the reference tree.** `grep -rn "AUDIT_FORMAT\|audit_corpus"` over it returns 0 hits, and
its `context.rs` is 1,345 lines long. The cites match the flat copies (`lk/audit_mod.rs:18-48`, and
`lk/events_context.rs` at 1,674 lines). Those cites describe newer upstream behaviour, not the
reference tree. Where it matters, I say so below.

**Measurements** were run on the installed pylance 12.0.0 (`/home/gabriel/Desktop/rask/.venv/bin/python`).
The tables were written under `reaudit/lakekeeper/measure/` using absolute paths. No repo file was
touched and nothing was run against the cluster.

---

## T1 — How an authorization decision becomes an audit record

**What Lakekeeper does**
- Every API handler builds an `APIEventContext<P, R, A, Z>` (`service/events/context.rs:659-685`).
  - `P` is the entity the caller named, `R` whether it has been resolved, and `A` the action.
  - `Z` is a typestate: `AuthzUnchecked` becomes `AuthzChecked` only through `emit_authz`
    (`context.rs:145-170, 1125-1172`).
  - An entity has a closed set of field names (`warehouse-id`, `namespace`, `table-id`, …;
    `context.rs:40-67`) and closed entity types (`context.rs:58-67`).
- `emit_authz` builds one event per API operation (`service/events/types/authorization.rs:114-162`). It carries:
  - `request_metadata`, which holds the actor;
  - `entities`, `actions` and `extra_context`;
  - an `authorizations[]` array with one self-contained entry per inner check (`authorization.rs:13-42`).
    An entry's `for_principal` names whose permission was evaluated, which can differ from the actor.
    The default entries are synthesised (`context.rs:1275-1324`).
- A failed check adds a closed `failure_reason`. The six values are ActionForbidden, ResourceNotFound,
  CannotSeeResource, InternalAuthorizationError, InternalCatalogError and InvalidRequestData
  (`authorization.rs:170-189`).
  - An outage records `allowed = None`, not `false`, so a backend failure is never logged as a deny
    (`context.rs:1326-1345`).
- The HTTP 404 may be deliberately ambiguous, but the audit record states the concrete reason
  (`authorization.rs:166-169`).
- Opting out is explicit and named: `authz_to_error_no_audit`, used for list sub-filtering
  (`context.rs:1174-1192`).
  - The S3 signer's per-request warehouse `Use` check is unaudited because it is "Too noisy otherwise"
    (`server/s3_signer/sign.rs:86-87`).
- The listener writes one `tracing::info!`. It has `event_source="audit"` and the fields `action` or
  `actions`, `entity` or `entities`, `actor`, `privilege_source`, `authorizations`, `decision`, and on
  failure `failure_reason` and `error` (`service/events/backends/audit.rs:125-243`).
- A second macro, `audit_operation!`, records non-authorization operations that touch identity, with
  an `operation` name and an `outcome` (`audit.rs:443-518`; documented in `docs/docs/logging.md:288-302`).
- In the reference tree the vocabulary is closed only by Rust types. There is no format version, and
  the only tests are two key-collector unit tests (`audit.rs:520-581`).
  - The newer upstream copy adds more: `AUDIT_FORMAT="1.0"` with a const assert, closed
    `ActorType`/`Decision`/`AuditOperation`/`AuditOutcome` enums (`scratchpad/lk/audit_mod.rs:18-139`),
    and a request-driven corpus test with a record-count floor (`scratchpad/lk/it_audit_corpus.rs:1-79`).

**What rask does today**
- `audit(action: str, outcome: str, *, subject, resource, **fields)` writes one `_log.info("audit", extra={"audit.*": …})`
  on the `lance.audit` logger (`packages/service-kit/src/service_kit/governed/audit.py:82-106`).
  - The outcome is one of four string constants (`audit.py:26-30`). The action is any string.
- The catalog audits each **FGA relation check**, not each API operation. The action is the FGA
  relation name: `audit(relation, ALLOW if allowed else DENY, subject=user, resource=obj)` at
  `services/catalog/src/catalog/api/fga_deps.py:418`.
  - `_require_any` writes one ALLOW for the door that let the caller in, or one DENY per door probed
    (`fga_deps.py:424-448`).
- Authn success and failure are audited separately (`governed/deps.py:150-169`; catalog
  `api/security.py:112-184`), with `reason=` as free text.
- A record does **not** carry:
  - the API operation;
  - the kind of actor (person, service-door principal, or system);
  - whether the privileged-subject path decided it;
  - a closed failure reason;
  - an explicit trace or request id.
- A denial is logged twice: once as the audit record, and again as `log.info("access_denied")`
  (`fga_deps.py:420`). Lakekeeper suppresses the duplicate error line when audit is on
  (`context.rs:1201-1203`).

**What rask should do on its own stack**
1. **Close the vocabulary at the seam, keyed on the operation.** `audit()` takes an
   `AuditAction(StrEnum)` of API operations (`create_table`, `query_table`, `vend_credentials`,
   `grant`, …). The FGA relation moves to its own `relation` field. The outcome becomes an enum, and a
   closed `FailureReason` mirrors Lakekeeper's six values.
2. **Write one record per request**, with an `authorizations[]` list of `{relation, object, allowed}`
   entries. The router-level authorize (`services/catalog/src/catalog/api/v1/router.py:48`) is the
   natural owner. Python cannot enforce the typestate at compile time, so the check has to be
   behavioural: a corpus test in `tests/integration` over `real_ns_client` and the real-OpenFGA
   fixture.
3. **Stamp `audit.format`**, with a golden fixture. This is justified by rask's own consumers
   (GreptimeDB queries, the home audit viewer at `frontend/microfrontends/home/src/lib/server/audit-core.ts`),
   not by the reference tree, which lacks it.
4. **Drop the duplicate `access_denied` line** where the audit record already says it.

**Backlog rows.** The findings row "Audit records carry no format version…" (new). XC-058. LH-063/D5
for the actor key.

---

## T2 — What marks a record as an audit record

**What Lakekeeper does.** A structured field, `event_source="audit"`, is set by the macro on every
audit line (`audit.rs:133,140,147,154,495,510`). The docs tell consumers to filter on it
(`docs/docs/logging.md:42-48,611-631`). The message text plays no part.

**What rask does today**
- The Collector routes on the log **body**: `filter/audit_only` keeps `body == "audit"`, and
  `filter/drop_audit` is its negation (`chart/templates/otel-collector.yaml:439-458`).
- The logs pipeline and `logs/audit` then export to `lance_audit` (`otel-collector.yaml:513-522,566-576`).
- One `audit()` whose message is not the literal `"audit"` would silently leave the trail. That is XC-058.

**What rask should do**
- Route on a structured field that cannot be interpolated. The logger name `lance.audit` becomes the
  OTLP **instrumentation scope name**, which LH-075 measured as the table's `PRIMARY KEY` in
  GreptimeDB. So the condition becomes `instrumentation_scope.name == "lance.audit"` (OTTL log
  context), or an explicit `rask.event_source="audit"` attribute set inside `audit()`.
  - I did not verify this OTTL path against the deployed Collector version.
- Add a gate that `audit()` is the only writer to `lance.audit`, so no caller can reach the logger with
  a different message.

**Backlog row.** XC-058.

---

## T3 — Where audit records are stored, how long they are kept, and who can erase them

**What Lakekeeper does**
- Audit is on by default (`config.rs:519-520,813-820`; `docs/docs/logging.md:28-38`) and goes to
  stdout JSON with span lists (`crates/lakekeeper-bin/src/main.rs:186-195`).
- Storage, retention and PII handling are left to the operator ("Route logs with `event_source=audit`
  to a secure, long-term storage system", `logging.md:633-647`).
- The chart ships no log sink. It only adds scrape annotations for metrics
  (`charts/lakekeeper/templates/catalog/catalog-deployment.yaml:38-40,97-98`).
- There is no tamper protection at all.

**What rask does today**
- rask is ahead on routing and retention. `lance_audit` is its own table with a TTL of about 1 year,
  against 14 days for the shared logs (XC-003 row, measured 2026-09-18).
- It is behind on integrity. `:4000/v1/sql` accepts unauthenticated `DELETE` in-cluster (XC-003,
  re-verified 2026-09-20), so anything in the cluster can erase the governance trail.
- The feature flag defaults on (`packages/service-kit/src/service_kit/config.py:62`). The chart renders
  it only when auth is on (`chart/values.yaml:955-959`).

**What rask should do**
- Close XC-003's credential clause via option (b) in that row:
  - ESO writes the GreptimeDB `passwd` file into a Secret, which is mounted as a file;
  - `GREPTIMEDB_STANDALONE__USER_PROVIDER=static_user_provider:file:<mount>/passwd`;
  - read and write users are separate.
  This fits the owner's "ESO, never env" rule. A NetworkPolicy is an outer-layer workaround and not
  acceptable here.
- Optional hardening once NATS authenticates publishers (findings row 16): an append-only JetStream
  `AUDIT` stream with `deny_delete`/`deny_purge`, the way the estate's DLQ stream is already configured
  (LH-148 measured `Allows Msg Delete: false`, `Allows Purge: false`). GreptimeDB would then be a
  projection of that stream. This is not required to close any row.

**Backlog rows.** XC-003, LH-075, XC-051.

---

## T4 — Read audit: rask has two streams, and the "Read by" panel shows the wrong one

**What Lakekeeper does.** A read is an ordinary authorization event in the one audit stream: the
`read_data`/`get_metadata` actions pass through `emit_authz` (`context.rs:1132-1171`). There is no
separate reads store and no "readers" API.

**What rask does today.** There are two unrelated streams, and both are called `audit_read`.
- **Data reads.** The catalog's `service_kit.governed.audit.audit_read`
  (`packages/service-kit/src/service_kit/governed/audit.py:49-79`) is called from the data doors
  (`services/catalog/src/catalog/api/v1/endpoints/data.py:608,664,722,758,780`). Records go to
  `lance.audit` and then GreptimeDB `lance_audit`, and carry version and columns.
- **Lineage-metadata views.** Lineage's own `audit_read` dependency
  (`services/lineage/src/lineage/api/fga_deps.py:187-200`) sits on lineage's `/datasets`, `/columns`,
  `/governance` and `/reconcile` routes.
  - It writes `public.lineage_reads` in the lineage Postgres (`services/lineage/src/lineage/services/repository.py:1417-1420`;
    `services/lineage/src/lineage/services/postgres.py:131-141`).
  - It defaults off (`services/lineage/src/lineage/core/config.py:205`); the chart turns it on
    (`chart/values.yaml:449-453`).
- **The product surface reads the second stream and labels it as the first.**
  - `GET /datasets/{name}/readers` (`services/lineage/src/lineage/api/v1/endpoints/datasets.py:86-99`)
    returns `repository.readers()`, documented as "Who READ `name`" (`repository.py:1422-1435`). The
    reply model says "One principal who READ `dataset` — the access-audit twin of ProducerInfo (who
    WROTE it)" (`services/lineage/src/lineage/schemas.py:125-135`).
  - The lakehouse panel prints "Read by" (`frontend/microfrontends/lakehouse/src/lib/ReadersPanel.svelte:2-5,63`).
  - So a principal who queried the table's **data** through the catalog never appears there, and one
    who only opened its **lineage page** does.
  - This comes from reading the code. I did not drive it over HTTP.
  - LH-075 noticed there are two streams but framed the gap as a missing index.

**What rask should do**
- Keep **one** read-audit stream, and let the catalog own it: it is the only service that serves data.
- Lineage-metadata reads become ordinary `audit(AuditAction.GET_LINEAGE, …)` records in `lance.audit`,
  the way Lakekeeper audits `get_metadata`.
- `/readers` answers from `lance_audit` (`audit.action='read_data' AND audit.resource=<table>`), read
  with the read-only GreptimeDB user from T3.
- Delete `lineage_reads`, `LINEAGE_READ_AUDIT_ENABLED` and `readAudit`. There must be no dual path.
- RED test first: a catalog `query_table` by bob must make bob appear in `/readers`.

**Backlog rows.** LH-075 (rewrite it around this), plus one new row.

---

## T5 — How the actor is recorded on a write

**What Lakekeeper does**
- Nothing persisted names the writer. Its Postgres migrations have no `created_by`/`actor` column; I
  grepped `crates/lakekeeper-storage-postgres/migrations/*.sql`, and even `idempotency_record` holds no
  principal (`20260318120000_idempotency_record.sql`).
- The actor survives only in two places:
  - the audit log line (T1);
  - the CloudEvent `actor` extension, a JSON string of `{type: principal|role|anonymous, principal: "<idp>~<sub>"}`
    (`service/events/publisher.rs:30-67,157,666`).
- Neither is signed or durable (T8).

**What rask does today — ahead of Lakekeeper**
- Every catalog write's OpenLineage event carries:
  - `author` = the verified sub (`services/catalog/src/catalog/core/lineage_emit.py:319-320`);
  - `lance.originator`, the person a service wrote for (`lineage_emit.py:304-305`);
  - `lance.project` (`lineage_emit.py:300-301`).
- The event is signed with HMAC and declares `onBehalfOf` (`lineage_emit.py:796-814`;
  `packages/lineage-kit/src/lineage_kit/signing.py:45-57,160-190`).
- The lineage door overwrites `author` with the verified principal (`services/lineage/src/lineage/api/fga_deps.py:203-222`).
  The result lands durably in AGE as a `(:User)-[:CREATED|WROTE]->` edge.
- Gaps: LH-064's enforcement is still verify-if-present (`fga_deps.py:290-335`), and a refused
  signature is only `log.info("lineage_signature_refused")` (`fga_deps.py:332`; unsigned events pass at `:319`). It never reaches
  the audit stream.

**Measured on pylance 12.0.0: stamping the author into the Lance commit.** This refines findings row
"Stamp who and which run into each Lance commit".
- `write_dataset(..., transaction_properties={"rask.author": ...})` round-trips through
  `read_transaction(v).transaction_properties` for both `create` and `append`.
- `LanceDataset.delete(...)` takes no properties; `read_transaction(3).transaction_properties == {}`.
- **After `cleanup_old_versions`, `read_transaction(v)` on a reclaimed version raises `OSError`**
  (manifest not found). The cleanup reported `transaction_files_removed: 4`.
- So the stamp is a **bounded-window recovery aid**. It lets reconcile re-attribute a write whose
  outbox stage was lost between the commit and the stage. It is **not** durable provenance: the
  durable record stays the AGE graph.
- The row's erasure caveat ("transaction files are immutable history") therefore holds only until
  cleanup. That makes the LH-178 exposure smaller than stated.

**What rask should do**
- Stamp `rask.author`, `rask.run_id`, `rask.operation`, `rask.on_behalf_of` and `rask.trace_id` (T8)
  on every door that can carry them. Name the doors that cannot: delete, update, native namespace ops.
- Have reconcile read the stamp before cleanup reaches the version.
- Flip LH-064 to require a signature (after the outbox relay fix, findings row 1).
- Audit every signature refusal as `audit(VERIFY_SIGNATURE, DENY, …)`.

**Backlog rows.** Findings row 23 (new), LH-064, LH-178.

---

## T6 — The principal key used across authorization, audit and lineage

**What Lakekeeper does.** A `UserId` is `<idp-id>~<subject>` (`service/authn.rs:30,69`). A Kubernetes
service account is `kubernetes~…` (`authn.rs:74`). The same string appears in:
- the audit actor (`audit.rs:337-368`, and `docs/docs/logging.md:73-87`, where the internal actor is
  `lakekeeper-internal`);
- the CloudEvent actor (`publisher.rs:727-765`, where the tests pin `oidc~123`);
- the authorizer.

**What rask does today.** It uses the bare `token.sub` for all three: the FGA subject
(`packages/service-kit/src/service_kit/governed/deps.py:181,208`), the audit subject (`fga_deps.py:418`,
`deps.py:169`) and the lineage author (`lineage/api/fga_deps.py:218`).

**What rask should do.** D5 is ruled: `<idp-id>~<claim>`, plus D1 service-account identities
(`k8s~system:serviceaccount:<ns>:<sa>`). Land it as **one** key, derived once in `governed/deps.py`
and used by FGA, `audit()` and `enforce_author`. Otherwise the audit trail and the graph cannot be
joined on who did something. Existing tuples, AGE edges and audit rows are test data and are re-keyed
without a shim.

**Backlog rows.** LH-063, D5, D1.

---

## T7 — How privileged access is shown in the trail

**What Lakekeeper does**
- `PrivilegeSource` is one of `internal`, `instance_admin` or `authorizer`, and is on every
  authorization audit record (`request_metadata.rs:65-88,272-282`; `audit.rs:183,221`).
- In-process work runs as `InternalActor::LakekeeperInternal` (`request_metadata.rs:247-263`), and is
  audited as `actor_type: lakekeeper-internal` (`audit.rs:381-406`).
- An instance-admin bypass therefore shows in the trail, not only in the config.

**What rask does today**
- Nothing in the trail tells apart:
  - an OIDC person;
  - a service-door principal;
  - a privileged subject (catalog `api/security.py:87-170`);
  - a system actor, such as maintenance's root-signed rewrite (`services/maintenance/src/maintenance/services/sweep.py:1039-1075`)
    or reconcile's `author='reconcile'`.
- `audit("authn", SUCCESS, subject=principal.sub)` has the same shape whichever door let the caller in
  (`security.py:151,184`).

**What rask should do.** Add `actor_type` (person / service / system) and `privilege_source`
(`fga` / `privileged_subject` / `system`) to every audit record and to `CatalogControlEvent.actor`,
which today is an unsigned `str` (`packages/service-kit/src/service_kit/control_events.py:178`). After
D1, `actor_type=service` means the identity comes from a verified service-account token.

**Backlog rows.** The findings row "service door identity…" (new). LH-063. The audit-format row. LH-064 (control lane).

---

## T8 — Correlation: request id and trace id

**What Lakekeeper does**
- An inbound `x-request-id` is accepted **only if it parses as a UUID**; otherwise a fresh UUIDv7 is
  minted (`request_metadata.rs:576-590`).
- `set_x_request_id(MakeRequestUuid7)` and `.propagate_x_request_id()` wrap the router
  (`api/router.rs:187-215`; `request_tracing.rs:146-155`).
- Every request span carries `request_id` (`request_tracing.rs:44-143`), and the JSON logs print the
  span list (`lakekeeper-bin/src/main.rs:190`). Audit dispatch is `.instrument(span)`-ed
  (`context.rs:1159-1166,1225-1232`), so an audit line carries its request id.
- Change events carry `trace-id` = the request id as a CloudEvent extension (`publisher.rs:156,664-665`),
  with the comment "Implement distributed tracing: issue #63".
- **Lakekeeper has no OpenTelemetry at all.** `grep -rn "opentelemetry|otlp|traceparent"` over
  `crates/` returns 0 hits.

**What rask does today**
- W3C trace context flows across HTTPX, requests, gRPC and Dapr (`packages/service-kit/src/service_kit/otel.py:230-262`),
  and log records carry `otelTraceID` (`packages/service-kit/src/service_kit/context.py:8-13`). rask
  is ahead of Lakekeeper here.
- Defects:
  - `RequestIDMiddleware` accepts **any** inbound `X-Request-ID` verbatim, with no format or length
    bound (`packages/service-kit/src/service_kit/middleware.py:72`). It echoes it, prints it on every
    stdout line (`context.py:57`; `app.py:97`) and copies it onto the span (`otel.py:35-36`).
  - `request_id` never reaches the OTLP records: `CorrelationFilter` is on the stdout handler only
    (`app.py:86`).
  - No trace or request id is carried in:
    - the OpenLineage events (`lineage_emit.py:237-380`);
    - the staged outbox object (`grep -in "trace|request_id"` on `service_kit/lakehouse/outbox.py` gives 0 hits);
    - `CatalogControlEvent` (`control_events.py:161-181`).
    So a relayed event starts a fresh trace, and a WROTE edge in AGE cannot be joined to its audit
    record.

**What rask should do.** D14 is ruled (XC-048: trace context supersedes request-id).
1. Write the `docs/DECISIONS.md` §9 entry.
2. Delete `RequestIDMiddleware` and `request_id_ctx`, so there is no dual path. The W3C trace id
   becomes the only correlation id, and responses echo it (e.g. `traceresponse`). This also removes
   the unvalidated inbound id. Update the gateway's minting (`services/gateway/src/gateway/__init__.py:494-513`)
   in the same change.
3. `audit()` stamps `audit.trace_id` explicitly, so the stdout copy carries it too.
4. The staged outbox object records the `traceparent`. The relay publishes under a span linked to it.
5. The `lance` run facet carries `trace_id`, and so does `CatalogControlEvent`. That joins a graph
   edge to its audit record, which is Lakekeeper's `trace-id` extension done with real traces.

**Backlog rows.** XC-048, CTL-006 (the access line should key on the trace id), CTL-011.

---

## T9 — The change-event envelope and its durability

**What Lakekeeper does**
- Only table, view, generic-table and undrop events reach the broker. The `CloudEventsPublisher`
  listener implements nothing else (`publisher.rs:112-530`); namespace, warehouse and role events never
  leave the process.
- Extensions: `tabular-type`, `tabular-id`, `warehouse-id`, `name`, `namespace`, `prefix`,
  `num-events`, `sequence-number`, `trace-id`, `actor` (`publisher.rs:652-667`).
- The `data` is the caller's request body.
- Delivery is fire-and-forget:
  - a 1000-slot channel (`serve.rs:229`) with a 50 ms `send_timeout` (`publisher.rs:544-584`);
  - a failing sink is only warned (`publisher.rs:676-692`);
  - a listener error never fails the request (`dispatch.rs:15-30,295-296`);
  - NATS gets a core publish, not JetStream (`crates/lakekeeper-events-nats/src/lib.rs:78-85`).

**What rask does today — ahead on every axis the domain cares about.** The event is OpenLineage with
the committed version, schema, inputs, author, originator and project. It goes through a staged S3
outbox and Dapr JetStream, and it is signed. Known holes are all tracked: the relay treats DatasetEvents
as poison (findings row 1), there is no DLQ re-ingest (LH-148), the compute plane is fire-and-forget
(CP-037), and the control-lane actor is unsigned (LH-064).

**What rask should do.** Do not copy the request-body payload or the `sequence-number`/`num-events`
extensions. The Lance version already orders events (findings "LANCE SOLVES"). Take only the typed
`actor` object (T7) and the trace link (T8).

**Backlog rows.** LH-064, LH-148, CP-037, findings row 1.

---

## T10 — Endpoint statistics (`endpoint_statistics.rs`)

**What Lakekeeper does**
- A middleware sends `(request_metadata, status, path_params, query_params)` down a channel
  (`service/endpoint_statistics.rs:29-58`; channel of 1000 at `serve.rs:224-226`; layer at `api/router.rs:161-164`).
- A tracker counts per `(project, endpoint enum, status, warehouse id or name)`
  (`endpoint_statistics.rs:94-116,256-297`). It skips a request with no project (`:280-283`).
- Every 30 s (`config.rs:1065`) it flushes to `EndpointStatisticsSink`s (`:235-254`).
- The Postgres sink aggregates and upserts into `endpoint_statistics` (`crates/lakekeeper-storage-postgres/src/endpoint_statistics/sink.rs:67-182`).
  After five retries it logs "lost stats" and drops them (`sink.rs:17-44`).
- The data is exposed per project and warehouse through a management API gated by
  `CatalogProjectAction::GetEndpointStatistics` / `CatalogWarehouseAction::GetEndpointStatistics`
  (`api/management/v1/project.rs:355-407`).
- This is usage metering, not audit. It records no actor.

**What rask does today.** OTel `http.server.duration` histograms by service, route and status, with
measured bucket layout (`otel.py:89-92,164-176`), feeding Perses "Fleet — RED". There is no project or
warehouse dimension and no tenant-facing usage API.

**What rask should do.** Do not build a table; the estate has no relational DB for this. If per-tenant
request usage is wanted:
- emit one OTel counter `rask.catalog.requests` with bounded attributes `rask.project`,
  `rask.warehouse`, `operation` (the closed `AuditAction` from T1) and `status`. Emit it at the
  router-level authorize point, which already resolves the object.
- expose it next to LH-074's storage usage, through the catalog door D14 names, reading PromQL with the
  T3 read-only credential.

This is optional: no Phase-1 criterion needs it, and it is not a quota control.

**Backlog rows.** Adjacent to LH-074. Would be a new row, LOW.

---

## T11 — Contract verification (`contract_verification.rs`)

**What Lakekeeper does**
- A `ContractVerification` trait covers `check_table_updates`, `check_view_updates`, `check_drop` and
  `check_rename` (`service/contract_verification.rs:121-146`).
- `ContractVerifiers` runs a list of them (`:189-313`):
  - the first `Violation` short-circuits and becomes the returned error (the doc example uses a typed
    409 `ContractViolation`);
  - a checker error propagates, so it fails closed;
  - a block is only `tracing::info!`, and is never audited.
- Call sites run before anything is written:
  - commit, before the metadata file is written (`server/tables.rs:1643-1655`);
  - drop (`tables.rs:777-782`);
  - rename (`server/tables/rename_table.rs:104`);
  - views (`server/views/commit.rs:217`, `drop.rs:91`, `rename.rs:104`).
- Default: no verifiers (`serve.rs:134-135`).

**What rask does today.** The refusals are scattered, and each has its own logging.
- The format guard `reject_unsupported_format` (`services/catalog/src/catalog/core/formats.py:25`,
  called at `data.py:461` and `tables.py`).
- The staged pylance-12 `_refuse_foreign_file_versions` (`services/catalog/src/catalog/services/dataplane.py:847-880`),
  which logs `log.warning("catalog_commit_refused_foreign_file_version")` and writes no audit record.
- TransformSpec validation, which only warns (LH-171).
- The medallion quality gate, answered on `/promotions`.

**What rask should do.**
- Make **one `CommitGuard` protocol** in the catalog dataplane, consulted on every commit door. That
  means insert, merge, update, delete, the fragment commit, alter, drop and rename.
- Feed it Lance-native inputs, not Iceberg `TableUpdate`s: the pending `Transaction` / fragments, the
  base manifest's schema, `data_storage_version` and feature flags. Every door already has these before
  `commit()`.
- Outcomes:
  - a violation returns a typed problem+json 4xx **and** writes an `audit(COMMIT_REFUSED, DENY, guard=<name>)` record;
  - a guard error returns 503, failing closed.
- Folding the existing refusals in:
  - the staged file-version refusal becomes the first guard;
  - `reject_unsupported_format` becomes one too;
  - LH-171 is closed by turning its WARN into a guard that refuses.

**Backlog rows.** LH-171, findings row 10 (the staged fix), plus one new row.

---

## T12 — Lance's own file-level audit trail

**What Lakekeeper does.** Not applicable: it is Iceberg-side, and its I/O crate emits no file audit.

**What Lance does.** It emits `lance::file_audit` events (`mode` create/delete/delete_unverified,
`type` manifest/data/index/deletion) and `lance::dataset_events` (loading/writing/committed/…/cleaning)
(`lance_docs/guide.md:2911-2930`, which also documents `LANCE_LOG`).

**Measured on pylance 12.0.0.** `lance.tracing.capture_trace_events(cb)` delivered **18
`file_audit` and 12 `dataset_events`** in-process for create, append, delete, `compact_files` and
`cleanup_old_versions`.
- Deletes name the path. Manifest *creates* carry `path: "dummy"`.
- `committed` events carry `operation`, `read_version` and `committed_version`.
- The docstring says the callback runs on a **dedicated thread**. So the request's OTel context is not
  available there, and correlation has to go by `(uri, committed_version)`.

**What rask does today.** It wires Lance metrics, but sets no `LANCE_LOG` and captures nothing
(findings row 28). So maintenance GC and the orphan purge leave per-file deletes on no trail.

**What rask should do**
- In maintenance and the catalog lifespans, register `capture_trace_events` once.
- Forward to `lance.audit`:
  - `file_audit` records with `mode` in `{delete, delete_unverified}`;
  - `dataset_events` records with `committed`/`cleaning`.
  Use a closed action (`LANCE_FILE_DELETED`, `LANCE_COMMITTED`).
- Do not forward creates; they are one per write and add volume without adding information.
- This beats `LANCE_LOG` to stderr: app-pod stderr is not tailed by the Collector's filelog receiver
  (per findings row 28), and structured fields would be lost there.
- The commit event gives `sweep.audit_material_work` (`sweep.py:1039-1075`) the exact version it
  produced.

**Backlog rows.** Findings row 28 (new), LH-099.

---

## T13 — Metrics and tracing infrastructure, and how reliably audit records are delivered

**What Lakekeeper does**
- Prometheus on `:9000`: axum RED, tokio runtime and cache hit/miss metrics (`metrics.rs:20-66`;
  `service/cache_metrics.rs:13-34`).
- No traces.
- Audit dispatch is a detached `tokio::spawn` (`context.rs:1159-1166`). Nothing guarantees that an
  audit line was written for a completed request.

**What rask does today — ahead.** All three signals go through the Collector to GreptimeDB, with
vmalert and Perses (`otel.py:95-262`). Audit rides the SDK's `BatchLogRecordProcessor`
(`otel.py:190-192`), which drops on queue overflow and exposes no dropped-audit metric. Open rows:
XC-067's wiring for the eight apps launched under `opentelemetry-instrument`; XC-064 (the e2e lanes run
with observability off); CTL-011.

**What rask should do.** Alert on the Collector's own exporter and refused-record metrics for the
`logs/audit` pipeline, so a lost audit export pages someone. That metric name is unverified here.
Nothing else from Lakekeeper is worth copying in this area.

**Backlog rows.** XC-067, XC-064, CTL-011, XC-051.

---

## Row map

| Row | Topics | Direction |
|---|---|---|
| XC-048 | T8 | Write DECISIONS §9; delete request-id; the trace id goes into audit, outbox, lineage facet and control event |
| XC-058 | T1, T2 | Route on scope or attribute, not body; closed `audit()` |
| XC-003 | T3 | ESO-written passwd file; separate read and write users |
| LH-075 | T4, T3 | Rewrite: one read stream; `/readers` from `lance_audit`; delete `lineage_reads` |
| LH-063 / D5 / D1 | T6, T7 | One `<idp>~<claim>` key for FGA, audit and author |
| LH-064 | T5, T7, T9 | Require signatures; audit refusals; sign the control actor |
| LH-171 | T11 | Becomes a CommitGuard |
| LH-178 | T5 | Transaction-stamp exposure is bounded by cleanup (measured) |
| LH-099 | T12 | The commit event from Lance tracing gives the exact version |
| LH-074 | T10 | Optional per-tenant request counter next to storage usage |
| LH-148, CP-037 | T9 | Unchanged; rask is ahead of Lakekeeper |
| CTL-006, CTL-011 | T8, T13 | The access line keys on the trace id |
| XC-064, XC-067, XC-051 | T13 | Unchanged |
| new: "Read by" shows lineage-page views, not data readers | T4 | RED: a catalog query must appear in `/readers` |
| new: CommitGuard seam | T11 | Fold the format guard and the staged file-version refusal into it |
| findings row 23 (stamp commit) | T5 | Refined by measurement: a recovery aid, not durable |
| findings row 27 (audit format) | T1 | Keep, but its Lakekeeper cites are newer-upstream, not the reference |
| findings row 28 (LANCE_LOG) | T12 | Prefer in-process `capture_trace_events` (measured) |

## What I did not check

- The Kafka backend (`crates/lakekeeper-events-kafka/src/lib.rs`). I skimmed the warehouse, view and
  generic-table event types; they follow the same pattern as table.rs. Of `authn.rs` I read only the
  `Actor`/`UserId` and IDP constants. I did not read Lakekeeper's `endpoint_stats.rs`/`stats.rs`
  integration tests.
- The newer upstream copies in `scratchpad/lk/` beyond the first 140 lines of `audit_mod.rs` and the
  header of `it_audit_corpus.rs`. I did not verify their origin.
- Nothing live: no query of `lance_audit` for a `trace_id` column, no check of the Collector's OTTL for
  `instrumentation_scope.name`, no Dapr traceparent in a real CloudEvent, and no driving of
  `/readers` over HTTP.
- Whether RustFS audit logs record the STS `RoleSessionName`. Both Lakekeeper (`"lakekeeper-sts"`,
  `service/storage/s3.rs:817-819`) and rask (`"lance-catalog-vend"`,
  `services/catalog/src/catalog/core/vending.py:618,699`) use a constant, so neither attributes
  object-store I/O to a principal. A per-principal session name would only help if RustFS logs it.
- The relayed user request, a separate test-validity workflow, falls outside this agent's computed task.
