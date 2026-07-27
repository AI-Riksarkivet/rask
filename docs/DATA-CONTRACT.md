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
of three properties the storage and bus already have, plus three enforcement points that check it.

## 2 · Our contract, in one table

| Layer | The contract | Where it lives |
|---|---|---|
| **Storage** | **The Lance manifest IS the schema; the version IS the handshake.** Every write commits an immutable, numbered version whose manifest embeds the full schema — data is self-describing, so there is nothing separate to register or drift from. A consumer that pins `(dataset, version)` gets bit-identical data + schema forever (time travel). | Lance format itself; CAS commit (put-if-not-exists) validated live against RustFS — `tests/e2e/test_object_store_cas_e2e.py`, verdict in `docs/DURABILITY.md` |
| **Bus** | **Events carry POINTERS, never data** (claim-check): a stage trigger is `{token, dataset, namespace}`, a train trigger is `{token, model, features:[{dataset, version}], config}`. The OpenLineage facets' `_schemaURL`s ARE the event-shape contract — official spec URLs, pinned in code and drift-tripwired in tests. | `services/medallion/services/{transform,train}.py`; facet pins in `services/medallion/schemas/events.py` + `scripts/ray_train_job.py` (equality-pinned by `tests/unit/test_train_job.py`) |
| **Identity** | One string threads all planes: `table:<id>` is the catalog id, the OpenFGA object, and the lineage graph node — so the contract's subject is never ambiguous. | catalog + `model.fga` + AGE ingest |

The **handshake in practice**: a mover reads its upstream at the version the trigger's lineage
event recorded, writes downstream producing a NEW version, and emits that version in the
`DatasetVersionDatasetFacet`. Training sharpens it further — `POST /train` resolves omitted
versions to LATEST **at the head** and the job reads ONLY pinned versions (`docs/RAY-TRAIN.md`
D1; unit-proven: pinned-v1 means ≠ LATEST means in `tests/unit/test_train_job.py`).

## 3 · The three enforcement points (who checks the contract, when)

| When | Gate | What it enforces | Code |
|---|---|---|---|
| **Promotion-time** | Quality gate (`MEDALLION_QUALITY_ENABLED`) | the DATA is good enough to promote: `row_count_positive` + `not_null(key_column)` + `blob_resolves` per blob column (2026-07-12: one real byte read from the first+last rows' payloads — catches a dangling external pointer/bucket wipe AT promotion instead of at first downstream read); a failure still emits the run + its `dataQualityAssertions` facet (auditable) but BLOCKS the next stage trigger — a bad batch cannot cascade | `services/medallion/services/quality.py` |
| **Access-time** | OpenFGA (fail-closed) | WHO may read/write/promote: concentric owner⊇writer⊇reader rungs + the separate `validator` rung for promotion; movers/trainer check as their OWN service identities before spending compute (deny → DROP) | `services/common/fga.py`, `handle_train_trigger`, mover gates |
| **Drift-time** | B4 reconcile (cron) | the GRAPH matches STORAGE: back-fills Lance writes whose lineage event was lost; flags `missing_on_storage`; and probes blob-POINTER health (2026-07-12: `dangling_blob_columns` — an external payload deleted after promotion changes no Lance version, so only the 1-byte probe sees it; same shared probe as the quality gate) | `services/lineage/api/v1/endpoints/reconcile.py` |

Plus edge validation where the bus meets code: consumers treat the bus as a wider trust surface
than token-guarded heads and DROP malformed triggers rather than repair them (version-less
features, path-unsafe names, oversized/non-dict config — `services/medallion/services/train.py`).

## 4 · Is it prod-ready? The honest split

**Enforced today and proven (unit and/or live):**
- Immutable versions + CAS commits — the storage half of the contract rests on
  put-if-not-exists, and that was VALIDATED against RustFS (3-tier contended-write harness, all
  green — `docs/DURABILITY.md`). This is the strongest link.
- Pinned-version consumption for training (D1), quality-gated promotion, FGA gates, reconcile
  back-fill, strict consumer-edge validation — all shipped with tests.
- **Additive** schema evolution is safe *by construction*: `add_columns` makes a new version;
  old versions stay readable; per-version schemas ride the lineage `WROTE` edge.

**NOT yet prod-grade (tracked, deliberate):**
- **Freshness — CLOSED 2026-07-12 (same day it was named).** Arrival cadence is now an ASSERTED
  clause: set `services.lineage.freshnessBudgetHours` (>0) and the reconcile sweep + per-dataset
  GET flag `stale: true` for any dataset whose newest version commit (storage truth — the version
  manifests, so a write that bypassed lineage still counts as fresh) is older than the budget,
  WARN-logged `lineage_reconcile_stale` per tick. 0 (default) = the axis is off, zero extra reads.
- **Breaking changes — gate AND patrol CLOSED 2026-07-12.** The reconcile sweep now re-checks the
  same declarations estate-wide (`missing_declared_columns` on the status, WARN
  `lineage_reconcile_contract_violation`): a write that BYPASSED the mover skipped the gate — the
  patrol doesn't. One declaration source (the movers' `requiredColumns` in values, chart-derived
  into `LINEAGE_DECLARED_COLUMNS`), two enforcement points, so they can never disagree.
- **Breaking changes — the GATE half CLOSED 2026-07-12.** Declare consumer dependencies per mover
  (`requiredColumns: "id,embedding"` in the chart → `MEDALLION_REQUIRED_COLUMNS`) and the quality
  gate adds a `column_declared` assertion per name: a promotion whose written schema dropped or
  renamed a declared column is BLOCKED (write still commits; audited FAIL run) — the runtime
  breakage becomes a pre-promotion contract violation. Additive evolution is never blocked; no
  declaration (default) = byte-identical gate. Original framing kept below for the record:
- **Breaking changes were the known gap.** A producer renaming/dropping a column a downstream
  reads is caught only at RUNTIME (the mover's transform fails → RETRY → stall) — not at
  promotion time. The fix is the §9 per-project **schema declaration** item (declare expected
  columns; the quality gate asserts they landed; reconcile flags undeclared writes) — that turns
  a runtime stall into a pre-promotion contract violation. Un-built by decision, tracked in
  `docs/DECISIONS.md` #schema-declaration--claim-check-hardening.
- **The claim-check rule is convention + spot-enforcement, not a universal guard.** The train
  path caps config at 8 KiB (head AND consumer); but there is no payload-size guard at EVERY
  publish site yet (§9 P1, open), and no cap on facet metadata bloat for thousand-column tables
  (§9 P2, open). NATS's ~1 MB message limit is the physical backstop.
- **Quality gate is demo-tier**: two assertions (row count, key non-null). Real deployments add
  domain assertions (and the §9 P2 "blob pointer resolves" check).

## 5 · What Dapr and NATS actually enforce (and what they don't)

Dapr and NATS do **not** enforce the data contract — they enforce the **delivery contract** that
makes the data contract *checkable*:

- **NATS JetStream** guarantees at-least-once delivery on durable streams (`LINEAGE` /
  `MEDALLION` / `TRAINING`), `maxDeliver=5` with backoff, 168h Limits retention. Its ~1 MB
  message bound is *why* the claim-check rule exists: the bus physically cannot carry data, so
  events must be pointers — a contract enforced by constraint.
- **Dapr** supplies the pub/sub abstraction, the SUCCESS/RETRY/DROP ack semantics, the
  app-api-token on subscription routes (a forged trigger can't spend compute), and the 30s ack
  window that shapes every handler (work fast or submit-and-ack).
- The **data** contract itself is enforced by Lance (manifest schema + CAS commit) and by the
  services at the three gates above. The bus's job is narrower: the trigger arrives at least
  once, and because every handler is idempotent (deterministic tokens/run-ids/submission-ids),
  at-least-once is as good as exactly-once.

## 6 · Is this the same as Lakekeeper?

No — related species, different format and bigger scope on our side:

| | **Lakekeeper** | **this platform** |
|---|---|---|
| What it is | Apache **Iceberg** REST catalog (Rust) | **Lance** REST catalog + lineage + governance + medallion orchestration |
| The "contract" | the Iceberg spec: snapshots, schema evolution via column IDs, hidden partitioning — metadata JSON managed by the catalog | the Lance manifest + immutable versions (§2) — self-describing storage, catalog is a thin adapter |
| AuthZ | OpenFGA (same choice — we studied theirs) | OpenFGA, threaded through the SAME `table:<id>` identity as lineage |
| Lineage | none | OpenLineage → AGE graph, column-level, reconcile back-fill |
| Data movement | none (catalog only) | the event-driven medallion cascade + Ray compute + training are IN scope |
| Data contracts as a product | no — like us, the table format IS the contract | no registry either; adds the quality/FGA/reconcile gates on top |

We deliberately mined Lakekeeper for patterns (`docs/SYSTEM-SKETCH.md` has the full diff): vended
credentials with `expires_at_millis`, idempotency keys, scoped event emission — adopted; their
route-enum conformance markers — skipped. Neither system is a "data contract product" like a
schema registry; the difference is that Iceberg's contract semantics (column-ID-based evolution)
are ecosystem-standardized, while ours lean on Lance's manifest + our own gates — which is why
the breaking-change detector in §4 is OUR item to build, not something the format gives us.

## 7 · The event fabric contract

The bus half of the contract, made explicit (2026-07-23). Four rules; every one cites the code that
enforces it, and the topic constants below are pinned by `tests/unit/test_invariants.py` so a rename
or an inline topic literal fails CI, not a live stack.

### 7.1 Envelope: CloudEvents, supplied by Dapr

Every publish goes through the one bounded wrapper `services/common/dapr_publish.py::publish_event`
with `data_content_type="application/json"`; the Dapr **sidecar** wraps the payload in a CloudEvents
envelope (id, source, type, traceparent) — application code never builds an envelope. Consumers
receive the envelope as a dict and read `body["data"]`: `handle_cloud_event` in
`services/lineage/services/consumer.py`, `on_control_event` in `services/catalog/api/dapr.py`, and
`handle_stage` / `handle_train_trigger` in `services/medallion/services/{transform,train}.py` all
treat `body` as an untrusted `Any` and guard with `isinstance` before touching `data`.

### 7.2 The topic is the compatibility unit

| Topic | Producer → consumer | Schema (the pydantic model) | Where the name lives |
|---|---|---|---|
| `lineage.events.v1` | catalog (`catalog/core/lineage_emit.py`), movers/trainer (via the `services/common/outbox.py` stage→publish→drop path), compaction (`compaction/core/lineage_emit.py`) → lineage subscriber | `lineage.models.RunEvent` (OpenLineage) | defaults on `LINEAGE_DAPR_TOPIC` / `LANCE_DAPR_TOPIC` / `COMPACTION_LINEAGE_TOPIC` / `MEDALLION_LINEAGE_TOPIC` in each service's `core/config.py` |
| `catalog.control.v1` | catalog (`catalog/core/control_emit.py`) → every catalog replica (broadcast, no `queueGroupName`) | `common.control_events.CatalogControlEvent` | `CONTROL_TOPIC` in `services/common/control_events.py` — the ONE shared constant both sides import |
| `medallion.raw` / `medallion.bronze` / `medallion.silver` / `medallion.media` | producer head + each mover → the next mover | pointer trigger `{token, dataset, namespace[, project]}` (claim-check §5) | `MEDALLION_RAW_TOPIC` / `MEDALLION_MEDIA_TOPIC` defaults in `medallion/core/config.py`; per-mover `subTopic`/`pubTopic` in `chart/values.yaml` `medallion.movers` |
| `training.jobs` | `POST /train` head (`medallion/services/train.py`) → the trainer consumer | pointer trigger `{token, model, features:[{dataset, version}], config}` | `MEDALLION_TRAIN_TOPIC` default in `medallion/core/config.py` (`medallion.train.topic` in values) |
| `dlq.*` | the Dapr sidecar on retry exhaustion → each app's parking route | the original delivery, parked | `dlq.lineage.events` (chart `services.yaml`), `dlq.<subTopic>` + `dlq.lance-ray` (chart `medallion.yaml`); the `DLQ` stream binds `dlq.>` in `nats-stream-job.yaml` |

The two **cross-plane** topics carry an explicit `.v1`: the version in the NAME is the compatibility
unit — a consumer subscribed to `lineage.events.v1` is entitled to `RunEvent`-shaped payloads
forever. The medallion/training **trigger** topics are intra-cascade wiring: both ends deploy from
the same chart values atomically, so retargeting one is a config change, not a schema break — they
carry no `.vN`. `dlq.*` names derive mechanically from the subscription they park for.

### 7.3 Schema = the pydantic model, validated on consume; invalid → DROP, never crash

There is no schema artifact besides the model class the consumer validates with. The rule at every
subscription: a payload that fails validation is **dropped** (redelivery cannot fix deterministic
garbage), and a handler never raises on malformed input — a crash would poison the subscription.

- lineage: `RunEvent.model_validate(data)` → on `ValidationError` log + `record_outcome(DROPPED)` +
  return `{"status": "DROP"}` (`services/lineage/services/consumer.py`).
- catalog control: `CatalogControlEvent.model_validate(data)` → on failure log + ack `SUCCESS`
  (events are refresh hints; the audit trail is the durable record — `services/catalog/api/dapr.py`).
- medallion triggers: strict field guards (`_safe_name` / `_safe_dataset` / int-version / the 8 KiB
  config cap) → `_DROP`; deny-by-FGA is also `DROP` (redelivery won't grant the rung), only
  transient outages `RETRY` (`services/medallion/services/{transform,train}.py`).

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
(`common.control_events`, `lineage.models.RunEvent`), so `ty` type-checks both sides of every topic
in the same run, and `tests/unit/test_invariants.py` pins the topic constants and rejects inline
topic literals at publish sites. A registry would add an operand and a failure mode to re-prove a
property CI already proves. **Revisit trigger:** the moment a consumer is deployed from OUTSIDE
this repo's CI — an independently-released service, or tenant-authored consumers on the bus — the
structural guarantee dissolves and a registry (or published JSON-schema artifacts per topic
version) becomes the contract carrier.

## Related docs
[`ARCHITECTURE.md`](ARCHITECTURE.md) · [`DURABILITY.md`](DURABILITY.md) (CAS validation) ·
[`RAY-TRAIN.md`](RAY-TRAIN.md) (D1 pins, D4 registry) · [`RESILIENCE.md`](RESILIENCE.md)
(delivery semantics) · [`SYSTEM-SKETCH.md`](SYSTEM-SKETCH.md) (Lakekeeper diff) ·
[`docs/DECISIONS.md` #schema-declaration--claim-check-hardening](DECISIONS.md#schema-declaration--claim-check-hardening) (schema declaration + claim-check hardening, the tracked gaps)
