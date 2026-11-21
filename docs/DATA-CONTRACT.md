# The data contract: what it is here, and where it is enforced

A data contract is the agreement between a table's **producer** and its **consumers** about what the data looks like and
how it may change. This page answers, in order: what the contract is made of here, how a table's owner states one, where
the catalog holds a commit to it, what is deliberately not enforced, and what the bus adds. Every claim cites shipped
code. The decision is [ADR 0212](adr/0212-publication-goes-and-a-tables-data-contract-is-checked-on-every-commit.md).

## 1 · What the contract is made of

| Layer | The contract | Where it lives |
|---|---|---|
| **Storage** | The Lance manifest is the schema, and the version is the handshake. Every write commits an immutable, numbered version whose manifest holds the full schema, so a table describes itself at every version. | `lance_docs/lance/format/table/index.md`, `schema.md` |
| **The table's own contract** | An ODCS v3 document its owner sets, naming the columns consumers rely on. The catalog refuses a commit that breaks one of them. | `catalog/services/contracts.py`, `_contracts/` on the control root |
| **Bus** | Events carry pointers, never data: a write's announcement names the table, its version, its branch and its author. | § 5 and § 7 |
| **Identity** | One uuid threads every plane: the catalog mints it for a table and keeps it across a rename (ADR 0173). It is the OpenFGA object (`table:<uuid>`), the key of the table's contract, and the `tableUuid` on its events. | `catalog/services/table_claims.py` |

The handshake in practice: the catalog announces every committed write on lineage, naming the table and the version
it produced. A pipeline reads its upstream at a version, writes downstream through the catalog, and names its inputs at
their versions. It follows the announcements on lineage's durable `GET /events` with its own token
([`COMMIT-EVENTS.md`](COMMIT-EVENTS.md)).

There is no schema registry: the manifest is the schema, and the contract is one record beside the table.

## 2 · Stating a contract

A contract is an [ODCS v3](https://bitol-io.github.io/open-data-contract-standard/) document (Open Data Contract
Standard, Bitol). The table's owner sets it at the catalog, and a producer keeps it in its own repository and applies it
from there:

```bash
curl -X POST "$CATALOG/management/v1/table/acme\$sales\$orders/contract/set" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d @orders.contract.json
```

```json
{
  "apiVersion": "v3.0.2",
  "kind": "DataContract",
  "id": "orders",
  "version": "1.0.0",
  "status": "active",
  "schema": [
    {
      "name": "orders",
      "properties": [
        { "name": "id", "physicalType": "int64", "required": true, "primaryKey": true, "primaryKeyPosition": 1 },
        { "name": "placed_at", "physicalType": "timestamp[us, tz=UTC]", "required": true },
        { "name": "customer", "physicalType": "struct", "properties": [{ "name": "email", "physicalType": "string" }] }
      ]
    }
  ]
}
```

| Door | Asks | Does |
|---|---|---|
| `POST /management/v1/table/{id}/contract/set` | `can_set_contract` (the table's owner) | Reads the document strictly, checks the table meets it, records it under the table's uuid, announces `table_contract_set` |
| `POST /management/v1/table/{id}/contract/describe` | `can_get_metadata` | Answers the document as it was set, with who set it and when; `contract` is null for a table holding none |
| `POST /management/v1/table/{id}/contract/delete` | `can_set_contract` | Removes it (idempotent), announces `table_contract_deleted` |

**What each field becomes.**

- `physicalType` is the column's Arrow type as pyarrow spells it (`int64`, `string`, `timestamp[us, tz=UTC]`). A
  property with nested `properties` is a struct, judged child by child. `logicalType`, where stated, must be the
  family that type belongs to (`integer` for `int64`).
- `required: true` is a non-nullable Lance field. Lance refuses a fragment whose schema differs from the table's,
  nullability included, so Lance holds the rows to it and the catalog holds the schema to it.
- `primaryKey: true` is a field of Lance's unenforced primary key, which is fixed once set
  (`lance_docs/lance/format/table/index.md` § Unenforced Primary Key). The catalog already refuses a commit that
  repeats a key value (`catalog/services/primary_keys.py`).

**What a set refuses.**

| Answer | When |
|---|---|
| 406 (code 0) | The document states a `quality` rule, `slaProperties`, or `unique` on a column that is no primary key. The catalog scans no data and runs no timer, so a contract may not promise a check nobody runs. |
| 400 (code 13) | The document is not ODCS v3, or carries a field outside the enforced subset. |
| 409 (code 19) | The table does not meet the contract now, on main or on a branch: a contract states what the table already is. |
| 400 (code 13) | It takes back what the standing contract promises without a higher major version, or its version did not move forward. |

## 3 · Where a commit is held to it

A commit that drops, renames, retypes or loosens a contracted column is refused **409 (code 19)**, naming the
column and the contract's version. A commit is refused for what it breaks: a violation the version it follows
already had (a branch cut from a version older than the contract) is not the next writer's, so its commits land
and the owner mends the table or the contract. It is checked where every commit already passes, so no writer can go around it:

| Writer | Where | Code |
|---|---|---|
| A stock Lance client | `POST /v1/table/{id}/version/create`, which judges the staged manifest before it is finalized | `catalog/services/managed_versions.py::judge` |
| A catalog door (`alter_columns`, `drop_columns`, `restore`, …) | The stamped commit every door makes | `catalog/services/tables/commits.py::commit_stamped` |
| `create?mode=Overwrite` over an existing table | Before the overwrite is written | `catalog/services/tables/creation.py::create_table` |

A column is named by its path, so a rename reads as a removal. Adding a column passes. Nothing else is checked, and
nothing is checked at any other time: the catalog holds no version back, scans no row and runs no freshness timer.

**Making a breaking change.** Set a contract that no longer forbids it, under a higher major version, then commit the
change, then (if the column returns in a new shape) set the contract that names it again.

Access is a separate gate, checked at every door: OpenFGA decides who may read, write or tag
(`packages/lakehouse/src/lakehouse/governed/fga_reads.py`, `catalog/api/authz/`).

## 4 · What is and is not enforced

**Enforced and proven:**

- **Immutable versions and compare-and-swap commits.** The storage half rests on put-if-not-exists, proven against
  RustFS ([`DURABILITY.md`](DURABILITY.md)).
- **Breaking changes to a contracted column**, at every commit
  (`services/catalog/tests/test_a_stock_client_commits_every_operation_through_version_create.py`).
- **Additive schema evolution** is safe by construction: `add_columns` makes a new version, old versions stay
  readable, and each version's manifest carries its own schema.
- **A lost announcement is found.** A crash between a commit and its announcement loses the event, and the catalog's
  commit reconciler announces it from the version log (ADR 0187).

**Not enforced, by design:**

- **Quality and freshness.** No rule on the data's values or its age is checked anywhere in the lakehouse. A pipeline
  that vets its data marks the version it vetted with a tag of its own
  ([`WRITE-GOVERNANCE.md`](WRITE-GOVERNANCE.md) § Marking a version ready).
- **Columns no contract names.** A table with no contract, and every column a contract leaves out, changes freely.
- **Inputs outside the run rule.** Under a namespace declaring `rask.requires_run = "true"` a write names its run and
  inputs (ADR 0211). Elsewhere a writer names them only if it chooses, so a table there may record no upstream.
- **The claim-check rule is a guard on size, not on shape.** Every publish goes through `lakehouse.events.bus.publish_event`,
  which refuses an envelope past 900 KiB and logs one past 64 KiB.
- **A message body is read as an envelope only when it is a JSON object nested no deeper than 300 levels**
  (`lakehouse.events.bus_delivery.envelope_of`). A body past that is handled as one that carries no event.

**Recorded, not built:** a `DataContract` custom resource, reconciled by the rask operator (XC-210), will later apply
the same door from git.

## 5 · What NATS and the bus seam actually enforce (and what they don't)

NATS and the lakehouse's bus seam do **not** enforce the data contract — they enforce the **delivery contract** that
makes the data contract *checkable*:

- **NATS JetStream** guarantees at-least-once delivery on durable streams. `LINEAGE`, `CATALOG_CONTROL` and `DLQ` keep
  Limits retention for `nats.streamMaxAgeSeconds` (604,800 s, 168 h). Lineage pulls the durable consumer
  `lineage-ingest`, which the stream Job declares with a 300 s ack wait (`services.lineage.delivery.ackWaitSeconds`);
  a delivery lineage cannot record is delivered again up to 5 times, 120 s apart, then parked in the provenance
  namespace's `refusals` table (`lakehouse.events.bus_delivery`, `lineage.api.ingest_lane`). Nothing of the lakehouse's is
  published on `DLQ`: it holds what a subscriber outside the release parks on `dlq.<its own name>`. Maintenance's streams are work queues it reads with
  nats-py (ADR 0198): `MAINTENANCE_TASKS` holds one task per table for at most 24 h, delivered up to
  `maintenance.queue.maxDeliver` + 1 (11) times on a `maintenance.queue.ackWaitSeconds` (60 s) ack wait kept alive by
  heartbeats; `MAINTENANCE_SCHEDULES` holds the message schedules; `STORE_EVENTS` holds the store's change events up to
  `maintenance.queue.storeEventsMaxBytes` (128 MiB), dropping its oldest (`chart/templates/nats-stream-job.yaml`). Its ~1 MB
  message bound is *why* the claim-check rule exists: the bus physically cannot carry data, so
  events must be pointers — a contract enforced by constraint.
- **The bus seam** (`lakehouse.events.bus`) supplies the consuming side: each service publishes and takes its deliveries on a
  connection of its own, as its own bus user, and the bus lets each user pull only its own consumer. Every delivery is
  settled by one rule (`lakehouse.events.bus_delivery.judged`): a handling answers taken (acknowledged), again (delivered
  again after its lane's delay) or park (kept by the lane, then ended), and a handling is cut 10 s before its
  consumer's ack wait. Who wrote an event is proven by its `rask_signature`, which each lane verifies, not by the bus.
- The **data** contract itself is enforced by Lance (manifest schema + CAS commit) and by the
  catalog where a commit lands (§ 3). The bus's job is narrower: an event arrives at least once, and because every
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
| Change approval | a `ContractVerification` trait inside the catalog that can refuse a table change that would break a data contract; the open-source binary installs no verifier | the same idea on the Lance write path, shipped: a table's ODCS contract is checked on every commit, beside the run rule (ADR 0155, ADR 0156, ADR 0211) |
| Data movement | none (catalog only) | none either: moving data between tables is the user's pipeline (ADR 0155) |

We deliberately mined Lakekeeper for patterns: vended
credentials with `expires_at_millis`, idempotency keys, scoped event emission — adopted; their
route-enum conformance markers — skipped. Neither system is a "data contract product" like a
schema registry; the difference is that Iceberg's contract semantics (column-ID-based evolution)
are ecosystem-standardized, while ours lean on Lance's manifest and the table's contract, which is why
the breaking-change check in § 3 is the catalog's own, not something the format gives us.

## 7 · The event fabric contract

The bus half of the contract, made explicit (2026-07-23). Four rules; every one cites the code that
enforces it. No test pins the topic constants' values or refuses an inline topic literal at a publish
site: producers and consumers that import the same constant cannot disagree, and a literal typed at a
publish site is caught by review alone.

### 7.1 Envelope: CloudEvents, built by the lakehouse

Every publish goes through the one bounded seam `packages/lakehouse/src/lakehouse/events/bus.py::publish_event`, which
takes the event in its envelope and sends it on the process's own connection in structured mode
(`application/cloudevents+json`), under the envelope's `id` as the message's `Nats-Msg-Id`. The envelope is derived from the event alone
(`packages/lakehouse/src/lakehouse/events/cloud_events.py`; its attributes are `docs/COMMIT-EVENTS.md` § The envelope on
the bus), so a relay's copy of an event carries the `id` its first publish carried, the message id a stream
deduplicates on. Its `traceparent` and `tracestate` are the publish's own, made in the trace of the request that caused
the event, which an outbox stages with the envelope (ADR 0209). Consumers
read the envelope from the delivery (`Delivery.envelope()` in `lakehouse.events.bus_delivery`, None for a body that is no
JSON object) and read its `data`: `handle_cloud_event` in `services/lineage/services/consumer.py` and
`ControlLane.handle` in `services/catalog/api/control_lane.py` treat it as untrusted and guard with `isinstance` before
touching `data`.

### 7.2 The topic is the compatibility unit

| Topic | Producer → consumer | Schema (the pydantic model) | Where the name lives |
|---|---|---|---|
| `lineage.events.v1` | catalog (`catalog/core/lineage_emit.py`), maintenance (`maintenance/core/lineage_emit.py`) → lineage (consumer `lineage-ingest`) and, under `maintenance.arrivalEnabled`, the maintenance worker's write-event lane (consumer `maintenance-arrival`), each a durable pull consumer the stream Job declares | `lineage.models.RunEvent` (OpenLineage) | defaults on `RASK_LINEAGE_TOPIC` in each service's `core/config.py` |
| `catalog.control.v1` | the catalog and maintenance through `packages/lakehouse/src/lakehouse/events/control_emit.py`, each signed as `control_signer_role` names → every catalog replica, each following the topic by reading its stream by sequence (`lakehouse.events.bus.Bus.follow`), each verifying the signature | `lakehouse.events.control_events.CatalogControlEvent`, whose model ignores the top-level `rask_signature` member | `CONTROL_TOPIC` in `packages/lakehouse/src/lakehouse/events/control_events.py`, imported by every producer and by the catalog subscriber |
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
(`lakehouse.events.control_events`, `lineage.models.RunEvent`), so `ty` type-checks both sides of every topic
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
