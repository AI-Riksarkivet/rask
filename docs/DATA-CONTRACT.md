# The data contract — what it is here, how it's enforced, and what is honestly not yet

**Status: DOCUMENTED 2026-07-11 (§9 P1 — previously existed only in chat).** Answers four
questions in order: what a data contract is, how THIS platform's works, whether it's prod-ready
(honest split), and how it relates to Dapr/NATS and to Lakekeeper. Every claim cites shipped code.

## 1 · What a "data contract" is

The agreement between a data **producer** and its **consumers** about what the data looks like and
how it may change: the schema, the versioning/evolution rules, the quality guarantees, and how a
consumer can rely on all three without talking to the producer's team. In classic stacks this is a
separate ARTIFACT — an Avro schema in a Kafka schema registry, a dbt model contract, a
`datacontract.yaml` — that some gate validates against.

**This platform deliberately has no schema registry.** The contract is *structural*: it falls out
of three properties the storage and bus already have, plus two enforcement points that check it.

## 2 · Our contract, in one table

| Layer | The contract | Where it lives |
|---|---|---|
| **Storage** | **The Lance manifest IS the schema; the version IS the handshake.** Every write commits an immutable, numbered version whose manifest embeds the full schema — data is self-describing, so there is nothing separate to register or drift from. A consumer that pins `(dataset, version)` gets bit-identical data + schema forever (time travel). | Lance format itself; CAS commit (put-if-not-exists), proven per store by `tests/e2e/test_object_store_cas_e2e.py` (`make e2e-cas`), verdict in `docs/DURABILITY.md` |
| **Bus** | **Events carry POINTERS, never data** (claim-check): a catalog write announcement names the table, its Lance version, its branch and its author; a control event names an object and an action. The OpenLineage facets' `_schemaURL`s ARE the event-shape contract — official spec URLs, pinned in code. | `catalog/core/lineage_emit.py`; facet URLs in `packages/service-kit/src/service_kit/openlineage.py` |
| **Identity** | One string threads all planes: `table:<id>` is the catalog id, the OpenFGA object, and the name lineage's feed and edge list record — so the contract's subject is never ambiguous. | catalog + `model.fga` + `lineage/models.py` (`Dataset.lineage_name`) |

The **handshake in practice**: the catalog announces every committed write on lineage, naming the table and the
version it produced; a user's pipeline reads the upstream at a version, writes downstream through the catalog
producing a NEW version, and names its inputs at their versions in its own OpenLineage run. A pipeline reads the
announcements from lineage's durable `GET /events` with its own token; the forward `since=` cursor and a documented
commit-event shape are LH-472 (#598).

## 3 · The two enforcement points (who checks the contract, when)

| When | Gate | What it enforces | Code |
|---|---|---|---|
| **Publish-time** | The project's quality gate, declared on the catalog's gates door and run by the publish door | the DATA is good enough to publish: `row_count_positive` + `not_null(key_column)` + `blob_resolves` per blob column (one real byte read from the first and last rows' payloads, so a dangling external pointer is caught at publish rather than at first read) + `column_declared` per column the gate declares a consumer depends on | `catalog/services/publication.py`, `packages/service-kit/src/service_kit/lakehouse/quality.py` |
| **Access-time** | OpenFGA (fail-closed) | WHO may read/write/promote: concentric owner⊇writer⊇reader rungs + the separate `validator` rung for promotion, checked at every catalog door | `packages/service-kit/src/service_kit/governed/fga.py`, `catalog/api/fga_deps.py` |

Plus the provenance guard: a writer cannot drop, rename or rewrite the provenance columns (`stage`, `lineage`,
`source_rowid`) or the `lineage.*` / `rask.*` schema-metadata keys (`catalog/core/provenance_guard.py`).

## 4 · Is it prod-ready? The honest split

**Enforced today and proven (unit and/or live):**
- Immutable versions + CAS commits — the storage half of the contract rests on put-if-not-exists, which the
  3-tier contended-write harness proved against RustFS (`docs/DURABILITY.md`); the same harness is the check for
  MinIO, the store now in use, and no MinIO run is recorded there.
- The publish-time quality gate, FGA gates and the provenance guard — all shipped with tests.
- **Additive** schema evolution is safe *by construction*: `add_columns` makes a new version;
  old versions stay readable; each version's manifest carries its own schema.

**NOT yet prod-grade (tracked, deliberate):**
- **Freshness** has no enforcement point: nothing in the estate flags a table whose newest version is older than
  a budget.
- **Drift between a commit and its announcement.** A crash between the Lance commit and the catalog's outbox stage
  loses that write's announcement, and nothing detects it yet (LH-497, #641). An external payload deleted after
  publication changes no Lance version, so only the publish gate's `blob_resolves` probe sees it, at publish.
- **Breaking changes.** A gate's declared columns turn a dropped or renamed column into a publish-time
  `column_declared` failure; nothing patrols the same declarations between publishes. Additive evolution is never
  blocked. The wider schema-declaration item is tracked in
  `docs/adr/0012-schema-declaration-claim-check-hardening.md`.
- **Inputs are declared by the writer.** Today a silver or gold write is authorized like any write, and its inputs
  are named only in the writer's own OpenLineage run. ADR 0156 makes the run and inputs part of the write itself,
  carried in the spec `Context` and stamped by the catalog (LH-225, #180), and a table's tier the `rask.tier`
  namespace property judged at every write door (LH-470, #595).
- **The claim-check rule is convention + spot-enforcement, not a universal guard.** There is no payload-size guard
  at EVERY publish site yet, and no cap on facet metadata bloat for thousand-column tables. NATS's ~1 MB message limit
  is the physical backstop.
- **The quality gate is a floor**: row count, key non-null, blob resolution, declared columns. Real deployments add
  domain assertions in their own pipelines.

## 5 · What Dapr and NATS actually enforce (and what they don't)

Dapr and NATS do **not** enforce the data contract — they enforce the **delivery contract** that
makes the data contract *checkable*:

- **NATS JetStream** guarantees at-least-once delivery on durable streams (`LINEAGE`, `CATALOG_CONTROL`, `DLQ`,
  and maintenance's work queues), `maxDeliver=5` with backoff, 168h Limits retention. Its ~1 MB
  message bound is *why* the claim-check rule exists: the bus physically cannot carry data, so
  events must be pointers — a contract enforced by constraint.
- **Dapr** supplies the pub/sub abstraction, the SUCCESS/RETRY/DROP ack semantics, the
  app-api-token on subscription routes (a forged event cannot reach a subscriber), and the 30s ack
  window that shapes every handler.
- The **data** contract itself is enforced by Lance (manifest schema + CAS commit) and by the
  catalog at the gates above. The bus's job is narrower: an event arrives at least once, and because every
  handler is idempotent (deterministic run ids, feed dedup), at-least-once is as good as exactly-once. The bus is
  internal: a user's pipeline reads the durable feed, not the topic.

## 6 · Is this the same as Lakekeeper?

No — related species, different format and bigger scope on our side:

| | **Lakekeeper** | **this platform** |
|---|---|---|
| What it is | Apache **Iceberg** REST catalog (Rust) | **Lance** REST catalog + lineage + governance, with the medallion as tier governance in the catalog |
| The "contract" | the Iceberg spec: snapshots, schema evolution via column IDs, hidden partitioning — metadata JSON managed by the catalog | the Lance manifest + immutable versions (§2) — self-describing storage, catalog is a thin adapter |
| AuthZ | OpenFGA (same choice — we studied theirs) | OpenFGA, threaded through the SAME `table:<id>` identity as lineage |
| Lineage | none | OpenLineage intake, a durable commit feed and a table-level edge list as catalog-governed Lance tables, every event forwarded to a bring-your-own graph backend |
| Change events | CloudEvents emitted on every change | OpenLineage events on lineage's durable `GET /events` feed |
| Change approval | a `ContractVerification` trait inside the catalog that can refuse a table change that would break a data contract or quality SLO | the same idea on the Lance write path: tier governance and the publish-time gate in the catalog (ADR 0155, ADR 0156) |
| Data movement | none (catalog only) | none either: moving data between tiers is the user's pipeline (ADR 0155) |

We deliberately mined Lakekeeper for patterns: vended
credentials with `expires_at_millis`, idempotency keys, scoped event emission — adopted; their
route-enum conformance markers — skipped. Neither system is a "data contract product" like a
schema registry; the difference is that Iceberg's contract semantics (column-ID-based evolution)
are ecosystem-standardized, while ours lean on Lance's manifest + our own gates — which is why
the breaking-change detector in §4 is OUR item to build, not something the format gives us.

## 7 · The event fabric contract

The bus half of the contract, made explicit (2026-07-23). Four rules; every one cites the code that
enforces it. No test pins the topic constants' values or refuses an inline topic literal at a publish
site: producers and consumers that import the same constant cannot disagree, and a literal typed at a
publish site is caught by review alone.

### 7.1 Envelope: CloudEvents, supplied by Dapr

Every publish goes through the one bounded wrapper `packages/service-kit/src/service_kit/dapr_publish.py::publish_event`
with `data_content_type="application/json"`; the Dapr **sidecar** wraps the payload in a CloudEvents
envelope (id, source, type, traceparent) — application code never builds an envelope. Consumers
receive the envelope as a dict and read `body["data"]`: `handle_cloud_event` in
`services/lineage/services/consumer.py` and `on_control_event` in `services/catalog/api/dapr.py` treat `body` as an
untrusted `Any` and guard with `isinstance` before touching `data`.

### 7.2 The topic is the compatibility unit

| Topic | Producer → consumer | Schema (the pydantic model) | Where the name lives |
|---|---|---|---|
| `lineage.events.v1` | catalog (`catalog/core/lineage_emit.py`), maintenance (`maintenance/core/lineage_emit.py`) → lineage **and notifications** subscribers, one per-app component each | `lineage.models.RunEvent` (OpenLineage) | defaults on `LINEAGE_DAPR_TOPIC` / `LANCE_DAPR_TOPIC` / `MAINTENANCE_LINEAGE_TOPIC` in each service's `core/config.py`; `RASK_NOTIFICATIONS_LINEAGE_TOPIC` in `notifications/api/settings.py`, rendered from `pubsub.topic` by the chart configmap |
| `catalog.control.v1` | the catalog, maintenance and the annotator through `packages/service-kit/src/service_kit/control_emit.py`, each signed as `control_signer_role` names (the annotator's actions are exempt) → every catalog replica (broadcast, no `queueGroupName`) and notifications' `/control-events` | `service_kit.control_events.CatalogControlEvent`, whose model ignores the top-level `rask_signature` member | `CONTROL_TOPIC` in `packages/service-kit/src/service_kit/control_events.py`, imported by every producer and by the catalog subscriber; notifications re-types it as the default of `RASK_NOTIFICATIONS_CONTROL_TOPIC` |
| `dlq.*` | the Dapr sidecar on retry exhaustion → each app's parking route | the original delivery, parked | `dlq.lineage.events` (chart `services.yaml`), `dlq.notifications` (chart `configmap.yaml`); the `DLQ` stream binds `dlq.>` in `nats-stream-job.yaml` |

The two **cross-plane** topics carry an explicit `.v1`: the version in the NAME is the compatibility
unit — a consumer subscribed to `lineage.events.v1` is entitled to `RunEvent`-shaped payloads
forever. `dlq.*` names derive mechanically from the subscription they park for. All of these are internal to the
estate.

### 7.3 Schema = the pydantic model, validated on consume; invalid → DROP, never crash

There is no schema artifact besides the model class the consumer validates with. The rule at every
subscription: a payload that fails validation is **dropped** (redelivery cannot fix deterministic
garbage), and a handler never raises on malformed input — a crash would poison the subscription.

- lineage: `RunEvent.model_validate(data)` → on `ValidationError` log + `record_outcome(DROPPED)` +
  return `{"status": "DROP"}` (`services/lineage/services/consumer.py`).
- catalog control: `CatalogControlEvent.model_validate(data)` → on failure log + ack `SUCCESS`
  (events are refresh hints; the audit trail is the durable record — `services/catalog/api/dapr.py`).

### 7.4 Evolution: additive-only within a topic; breaking = a new `.vN` topic

Within `*.v1` a producer may **add optional fields**: the consuming models tolerate them —
`lineage/models.py` sets `ConfigDict(extra="allow", ...)` explicitly, `CatalogControlEvent` rides
pydantic's default (unknown fields ignored) — and defaulted fields tolerate absence, so old and new
payloads coexist mid-rollout.
It may never rename/remove a field, change a type, or make an optional field required. A breaking
change ships as a **new topic** (`lineage.events.v2`) with the consumer subscribing to both `.v1`
and `.v2` until every producer has moved — the same parallel-consumer pattern the JetStream streams
already support (subjects `lineage.events.*` style bindings cost nothing).

### 7.5 Why there is no schema registry (and when to revisit)

A registry earns its keep when producers and consumers ship independently. Here they cannot drift:
**one repo, one CI** — producers and consumers import the same model classes
(`service_kit.control_events`, `lineage.models.RunEvent`), so `ty` type-checks both sides of every topic
in the same run. A registry would add an operand and a failure mode to re-prove a property CI already
proves. **Revisit trigger:** the moment a consumer is deployed from OUTSIDE
this repo's CI — an independently-released service, or tenant-authored consumers on the bus — the
structural guarantee dissolves and a registry (or published JSON-schema artifacts per topic
version) becomes the contract carrier.

### 7.6 Becoming a notification producer

The notification plane turns events into things addressed to a PERSON. It is a consumer of the fabric
above, not a parallel one — so a service that wants its work to reach someone adds a topic and a door,
never a special case inside `services/notifications`.

**The producer contract is deliberately NOT restated here.** It lives in exactly one place,
`.claude/skills/rask-notifications`, which is maintained against the code and which the project rule
requires be corrected *in the same commit* as any change it describes. This section used to carry a
second copy, and it drifted exactly as a second copy does: it named **three** targeting sources long
after the plane had six, and told producers they owed "exactly four things" while the real rule for a
named action had become a three-file change with a stored-enum compatibility surface. A contract with
two homes has none — and the stale half is worse than no half, because a published page asserts
settledness whatever it says. Read the skill.

What belongs here is the FABRIC-level rule, which this document does own: **a producer never writes
into an inbox.** It publishes; the plane derives the audience, re-checks visibility per recipient, and
stores a claim-check POINTER — never a payload copy. That is what keeps this document's own rule
intact one layer up: an event is a refresh hint, and the governed read is still the only path to the
data.

## Related docs
[`ARCHITECTURE.md`](ARCHITECTURE.md) · [`DURABILITY.md`](DURABILITY.md) (CAS validation) ·
[`LINEAGE.md`](LINEAGE.md) · [`RESILIENCE.md`](RESILIENCE.md)
(delivery semantics) ·
[`docs/adr/0012-schema-declaration-claim-check-hardening.md`](adr/0012-schema-declaration-claim-check-hardening.md) (schema declaration + claim-check hardening, the tracked gaps)
