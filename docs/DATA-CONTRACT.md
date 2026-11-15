# The data contract — what it is here, how it's enforced, and what is honestly not yet

**Status: DOCUMENTED 2026-07-11 (§9 P1 — previously existed only in chat).** Answers four
questions in order: what a data contract is, how THIS platform's works, whether it's prod-ready
(honest split), and how it relates to NATS and to Lakekeeper. Every claim cites shipped code.

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
| **Storage** | **The Lance manifest IS the schema; the version IS the handshake.** Every write commits an immutable, numbered version whose manifest embeds the full schema — data is self-describing, so there is nothing separate to register or drift from. A consumer that pins `(dataset, version)` gets bit-identical data + schema forever (time travel). | Lance format itself; CAS commit (put-if-not-exists), proven per store by `tests/e2e-py/test_object_store_cas_e2e.py` (`make e2e-cas`), verdict in `docs/DURABILITY.md` |
| **Bus** | **Events carry POINTERS, never data** (claim-check): a catalog write announcement names the table, its Lance version, its branch and its author; a control event names an object and an action. The OpenLineage facets' `_schemaURL`s ARE the event-shape contract — official spec URLs, pinned in code. | `catalog/core/lineage_emit.py`; facet URLs in `packages/lakehouse/src/lakehouse/openlineage.py` |
| **Identity** | One uuid threads all planes: the catalog mints it for a table and keeps it across a rename (ADR 0173), and it is the OpenFGA object (`table:<uuid>`), the table's location (`<root>/<uuid>`) and the key lineage's edge list records; the name is how a caller addresses the table — so the contract's subject is never ambiguous, even across a rename. | catalog (`table_claims`) + `model.fga` + `lineage/services/provenance.py` |

The **handshake in practice**: the catalog announces every committed write on lineage, naming the table and the
version it produced; a user's pipeline reads the upstream at a version, writes downstream through the catalog
producing a NEW version, and names its inputs at their versions in its own OpenLineage run. A pipeline reads the
announcements from lineage's durable `GET /events` with its own token; the forward `since=` cursor and a documented
commit-event shape are LH-472 (#598).

## 3 · The two enforcement points (who checks the contract, when)

| When | Gate | What it enforces | Code |
|---|---|---|---|
| **Publish-time** | The project's quality gate, declared on the catalog's gates door and run by the publish door | the DATA is good enough to publish: `row_count_positive` + `not_null(key_column)` + `blob_resolves` per blob column (one real byte read from the first and last rows' payloads, so a dangling external pointer is caught at publish rather than at first read) + `column_declared` per column the gate declares a consumer depends on | `catalog/services/publication.py`, `packages/lakehouse/src/lakehouse/quality.py` |
| **Access-time** | OpenFGA (fail-closed) | WHO may read/write/promote: concentric owner⊇writer⊇reader rungs + the separate `validator` rung for promotion, checked at every catalog door | `packages/lakehouse/src/lakehouse/governed/fga.py`, `catalog/api/fga_deps.py` |

Plus the provenance guard: a writer cannot drop, rename or rewrite the provenance columns (`stage`, `lineage`,
`source_rowid`) or the `lineage.*` / `rask.*` schema-metadata keys (`catalog/core/provenance_guard.py`).

## 4 · Is it prod-ready? The honest split

**Enforced today and proven (unit and/or live):**
- Immutable versions + CAS commits — the storage half of the contract rests on put-if-not-exists, which the
  3-tier contended-write harness proved against RustFS (`docs/DURABILITY.md`), and 16 racing conditional writers
  left one winner in each of 20 rounds on RustFS 1.0.1 (measured 2026-10-18 by `scripts/rustfs_sts_measure.py`; the audit is in git history at 64ef118a9).
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
- **Inputs are declared by the writer, and required only under the run rule.** Under a namespace that declares
  `rask.requires_run = "true"`, the run and inputs are part of the write itself, carried in the spec `Context` and
  stamped by the catalog (ADR 0156 C2, LH-225, #180), and the write judge refuses a write adding data without them at
  every write door (ADR 0211). Outside the rule a writer names its inputs only if it chooses, so a table there may
  have no recorded upstream.
- **The claim-check rule is a guard on size, not on shape.** Every publish goes through `lakehouse.bus.publish_event`,
  which refuses an envelope past 900 KiB and logs one past 64 KiB; nothing below that caps facet metadata bloat for
  thousand-column tables. NATS's ~1 MB message limit is the physical backstop.
- **A message body is read as an envelope only when it is a JSON object nested no deeper than 300 levels**
  (`lakehouse.bus_delivery.envelope_of`, the one reader every lane and every publisher's read-back goes through). The
  envelope is the first level, and each object or array inside another is one more. A body past that is handled as one
  that carries no event: lineage takes it as malformed and records nothing, the catalog's lane passes it over, and the
  maintenance worker ends it. Lineage's own model accepts an event at most 258 levels deep in its envelope, so nothing
  its HTTP door accepts is past the bound.
- **The quality gate is a floor**: row count, key non-null, blob resolution, declared columns. Real deployments add
  domain assertions in their own pipelines.

## 5 · What NATS and the bus seam actually enforce (and what they don't)

NATS and the lakehouse's bus seam do **not** enforce the data contract — they enforce the **delivery contract** that
makes the data contract *checkable*:

- **NATS JetStream** guarantees at-least-once delivery on durable streams. `LINEAGE`, `CATALOG_CONTROL` and `DLQ` keep
  Limits retention for `nats.streamMaxAgeSeconds` (604,800 s, 168 h). Lineage pulls the durable consumer
  `lineage-ingest`, which the stream Job declares with a 300 s ack wait (`services.lineage.delivery.ackWaitSeconds`);
  a delivery lineage cannot record is delivered again up to 5 times, 120 s apart, then parked in the provenance
  namespace's `refusals` table (`lakehouse.bus_delivery`, `lineage.api.ingest_lane`). Nothing of the lakehouse's is
  published on `DLQ`: it holds what a subscriber outside the release parks on `dlq.<its own name>`. Maintenance's streams are work queues it reads with
  nats-py (ADR 0198): `MAINTENANCE_TASKS` holds one task per table for at most 24 h, delivered up to
  `maintenance.queue.maxDeliver` + 1 (11) times on a `maintenance.queue.ackWaitSeconds` (60 s) ack wait kept alive by
  heartbeats; `MAINTENANCE_SCHEDULES` holds the message schedules; `STORE_EVENTS` holds the store's change events up to
  `maintenance.queue.storeEventsMaxBytes` (128 MiB), dropping its oldest (`chart/templates/nats-stream-job.yaml`). Its ~1 MB
  message bound is *why* the claim-check rule exists: the bus physically cannot carry data, so
  events must be pointers — a contract enforced by constraint.
- **The bus seam** (`lakehouse.bus`) supplies the consuming side: each service publishes and takes its deliveries on a
  connection of its own, as its own bus user, and the bus lets each user pull only its own consumer. Every delivery is
  settled by one rule (`lakehouse.bus_delivery.judged`): a handling answers taken (acknowledged), again (delivered
  again after its lane's delay) or park (kept by the lane, then ended), and a handling is cut 10 s before its
  consumer's ack wait. Who wrote an event is proven by its `rask_signature`, which each lane verifies, not by the bus.
- The **data** contract itself is enforced by Lance (manifest schema + CAS commit) and by the
  catalog at the gates above. The bus's job is narrower: an event arrives at least once, and because every
  handler is idempotent (deterministic run ids, feed dedup), at-least-once is as good as exactly-once. The bus is
  internal: a user's pipeline reads the durable feed, not the topic.

## 6 · Is this the same as Lakekeeper?

No — related species, different format and bigger scope on our side:

| | **Lakekeeper** | **this platform** |
|---|---|---|
| What it is | Apache **Iceberg** REST catalog (Rust) | **Lance** REST catalog + lineage + governance, with opt-in write governance (the run rule, labels) in the catalog |
| The "contract" | the Iceberg spec: snapshots, schema evolution via column IDs, hidden partitioning — metadata JSON managed by the catalog | the Lance manifest + immutable versions (§2) — self-describing storage, catalog is a thin adapter |
| AuthZ | OpenFGA (same choice — we studied theirs) | OpenFGA, threaded through the SAME table uuid as lineage (`table:<uuid>`) |
| Lineage | none | OpenLineage intake, a durable commit feed and a table-level edge list as catalog-governed Lance tables, every event forwarded to a bring-your-own graph backend |
| Change events | CloudEvents emitted on every change | OpenLineage events on lineage's durable `GET /events` feed |
| Change approval | a `ContractVerification` trait inside the catalog that can refuse a table change that would break a data contract or quality SLO | the same idea on the Lance write path: the run rule and the publish-time gate in the catalog (ADR 0155, ADR 0156, ADR 0211) |
| Data movement | none (catalog only) | none either: moving data between tables is the user's pipeline (ADR 0155) |

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

### 7.1 Envelope: CloudEvents, built by the lakehouse

Every publish goes through the one bounded seam `packages/lakehouse/src/lakehouse/bus.py::publish_event`, which
takes the event in its envelope and sends it on the process's own connection in structured mode
(`application/cloudevents+json`), under the envelope's `id` as the message's `Nats-Msg-Id`. The envelope is derived from the event alone
(`packages/lakehouse/src/lakehouse/cloud_events.py`; its attributes are `docs/COMMIT-EVENTS.md` § The envelope on
the bus), so a relay's copy of an event carries the `id` its first publish carried, the message id a stream
deduplicates on. Its `traceparent` and `tracestate` are the publish's own, made in the trace of the request that caused
the event, which an outbox stages with the envelope (ADR 0209). Consumers
read the envelope from the delivery (`Delivery.envelope()` in `lakehouse.bus_delivery`, None for a body that is no
JSON object) and read its `data`: `handle_cloud_event` in `services/lineage/services/consumer.py` and
`ControlLane.handle` in `services/catalog/api/control_lane.py` treat it as untrusted and guard with `isinstance` before
touching `data`.

### 7.2 The topic is the compatibility unit

| Topic | Producer → consumer | Schema (the pydantic model) | Where the name lives |
|---|---|---|---|
| `lineage.events.v1` | catalog (`catalog/core/lineage_emit.py`), maintenance (`maintenance/core/lineage_emit.py`) → lineage (consumer `lineage-ingest`) and, under `maintenance.arrivalEnabled`, the maintenance worker's write-event lane (consumer `maintenance-arrival`), each a durable pull consumer the stream Job declares | `lineage.models.RunEvent` (OpenLineage) | defaults on `LINEAGE_EVENTS_TOPIC` / `LANCE_LINEAGE_TOPIC` / `MAINTENANCE_LINEAGE_TOPIC` in each service's `core/config.py` |
| `catalog.control.v1` | the catalog and maintenance through `packages/lakehouse/src/lakehouse/control_emit.py`, each signed as `control_signer_role` names → every catalog replica, each following the topic by reading its stream by sequence (`lakehouse.bus.Bus.follow`), each verifying the signature | `lakehouse.control_events.CatalogControlEvent`, whose model ignores the top-level `rask_signature` member | `CONTROL_TOPIC` in `packages/lakehouse/src/lakehouse/control_events.py`, imported by every producer and by the catalog subscriber |
| `dlq.*` | a subscriber outside the release, on `dlq.<its own name>` | what that subscriber parks; nothing of the lakehouse's | the `DLQ` stream binds `dlq.>` in `nats-stream-job.yaml` |

The two **cross-plane** topics carry an explicit `.v1`: the version in the NAME is the compatibility
unit — a consumer subscribed to `lineage.events.v1` is entitled to `RunEvent`-shaped payloads
forever. All of these are internal to the estate.

### 7.3 Schema = the pydantic model, validated on consume; invalid → taken, never crash

There is no schema artifact besides the model class the consumer validates with. The rule at every
consumer: a payload that fails validation is **acknowledged and counted** (redelivery cannot fix deterministic
garbage), and a handler never raises on malformed input — a raise would be read as *again* and delivered back.

- lineage: `parse_event(data)` → on `ValidationError`, `TypeError` or `ValueError` log `lineage_event_invalid` +
  `record_outcome(UNREPAIRABLE)` + return `Verdict.TAKEN` (`services/lineage/services/consumer.py`).
- catalog control: `CatalogControlEvent.model_validate(data)` → on failure log `control_event_invalid` and return, so
  the event is taken (events are refresh hints; the audit trail is the durable record —
  `services/catalog/api/control_lane.py`).

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
(`lakehouse.control_events`, `lineage.models.RunEvent`), so `ty` type-checks both sides of every topic
in the same run. A registry would add an operand and a failure mode to re-prove a property CI already
proves. **Revisit trigger:** the moment a consumer is deployed from OUTSIDE
this repo's CI — an independently-released service, or tenant-authored consumers on the bus — the
structural guarantee dissolves and a registry (or published JSON-schema artifacts per topic
version) becomes the contract carrier.

### 7.6 Becoming a notification producer

A notifier turns events into things addressed to a PERSON. It is a client of the fabric above, not a parallel one, and
the lakehouse runs none ([ADR 0193](adr/0193-the-inbox-is-retired-and-the-console-joins-the-product.md)) — so a service that wants its work to reach someone stamps
the fields a notifier targets by, never a special case inside a notifier.

**The targeting contract is deliberately NOT restated here.** It lives in exactly one place,
[`COMMIT-EVENTS.md`](COMMIT-EVENTS.md#telling-people-the-notifier-contract), the lakehouse's published contract for any
notifier ([ADR 0190](adr/0190-the-notifications-inbox-is-a-client-of-the-lakehouses-events.md)), and the producer's
how-to against it in `.claude/skills/rask-notifications`, maintained against the code, which the project rule
requires be corrected *in the same commit* as any change it describes. This section used to carry a
second copy, and it drifted exactly as a second copy does: it named **three** targeting sources long
after the plane had six, and told producers they owed "exactly four things" while the real rule for a
named action had become a three-file change with a stored-enum compatibility surface. A contract with
two homes has none — and the stale half is worse than no half, because a published page asserts
settledness whatever it says. Read the contract, then the skill.

What belongs here is the FABRIC-level rule, which this document does own: **a producer never writes
into a notifier.** It publishes; a notifier derives the audience, re-checks visibility per recipient, and
keeps a claim-check POINTER — never a payload copy. That is what keeps this document's own rule
intact one layer up: an event is a refresh hint, and the governed read is still the only path to the
data.

## Related docs
[`ARCHITECTURE.md`](ARCHITECTURE.md) · [`DURABILITY.md`](DURABILITY.md) (CAS validation) ·
[`LINEAGE.md`](LINEAGE.md) · [`RESILIENCE.md`](RESILIENCE.md)
(delivery semantics) ·
[`docs/adr/0012-schema-declaration-claim-check-hardening.md`](adr/0012-schema-declaration-claim-check-hardening.md) (schema declaration + claim-check hardening, the tracked gaps)
